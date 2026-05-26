"""Tests for node/ai.py — the Claude API wrapper's helpers. The actual
call_claude() roundtrip through the Anthropic SDK is exercised in
integration / end-to-end paths only; here we cover the resilience
helpers (fence stripping, JSON parsing) that the handlers depend on."""
import json
from types import SimpleNamespace

import pytest

from node import ai


def test_strip_fences_removes_json_fence():
    """Claude is asked for raw JSON only; it sometimes returns
       ```json … ``` anyway. The wrapper strips that before the
       handler's json.loads sees it."""
    body = json.dumps({"a": 1, "b": [2, 3]})
    wrapped = f"```json\n{body}\n```"
    assert ai._strip_fences(wrapped) == body


def test_strip_fences_removes_plain_triple_backtick_fence():
    body = json.dumps({"x": 1})
    wrapped = f"```\n{body}\n```"
    assert ai._strip_fences(wrapped) == body


def test_strip_fences_passthrough_when_no_fence():
    body = json.dumps({"x": 1})
    assert ai._strip_fences(body) == body


def test_strip_fences_ignores_inline_backticks_inside_json():
    """A code-fenced wrapper is `^```...\\n...\\n```$`; backticks
       inside a JSON string value must not be misread as a fence."""
    body = json.dumps({"note": "uses `git apply --check` to verify"})
    assert ai._strip_fences(body) == body


# --- parse_json_response --------------------------------------------------

def test_parse_json_response_returns_the_object():
    out = ai.parse_json_response(json.dumps({"k": "v"}))
    assert out == {"k": "v"}


def test_parse_json_response_raises_on_malformed_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        ai.parse_json_response("Sorry, can't help with that.")


def test_parse_json_response_raises_on_non_object():
    """The contract is one JSON OBJECT per call. A bare array, string,
       or number is a contract violation."""
    with pytest.raises(ValueError, match="expected an object"):
        ai.parse_json_response("[1, 2, 3]")
    with pytest.raises(ValueError, match="expected an object"):
        ai.parse_json_response('"just a string"')


# --- auth-error translation ----------------------------------------------

def test_call_claude_translates_authentication_error(monkeypatch):
    """A 401 from Claude reaches the runner as a clean domain
       CallClaudeAuthError carrying a short, operator-facing message.
       The SDK's traceback is suppressed (we raise from None) so a
       wrong ANTHROPIC_API_KEY surfaces in `docker logs` as a single
       ERROR line, not a 30-line stack trace."""
    import anthropic

    class _StubMessages:
        def create(self, **kw):
            # Fake a 401 the way the SDK does — using its public class so
            # the translation's isinstance check matches the real path.
            raise anthropic.AuthenticationError(
                "invalid x-api-key",
                response=_FakeResponse(401),
                body={"error": {"message": "invalid x-api-key"}})

    class _FakeResponse:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}
            self.request = None

    class _StubClient:
        def __init__(self, **kw): pass
        @property
        def messages(self): return _StubMessages()

    monkeypatch.setattr(anthropic, "Anthropic", _StubClient)
    cfg = SimpleNamespaceCfg()
    with pytest.raises(ai.CallClaudeAuthError) as ei:
        ai.call_claude(cfg, "system", "user")
    msg = str(ei.value)
    assert "Claude rejected the API key" in msg
    assert "ANTHROPIC_API_KEY" in msg
    # Suppressed cause — the SDK traceback isn't useful to the operator.
    assert ei.value.__cause__ is None


def test_call_claude_translates_permission_denied_error(monkeypatch):
    """A 403 from Claude (key valid but lacks permission for the model
       or endpoint) is also configuration-fatal and gets the same
       clean translation."""
    import anthropic

    class _FakeResponse:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}
            self.request = None

    class _StubMessages:
        def create(self, **kw):
            raise anthropic.PermissionDeniedError(
                "forbidden",
                response=_FakeResponse(403),
                body={"error": {"message": "forbidden"}})

    class _StubClient:
        def __init__(self, **kw): pass
        @property
        def messages(self): return _StubMessages()

    monkeypatch.setattr(anthropic, "Anthropic", _StubClient)
    with pytest.raises(ai.CallClaudeAuthError, match="HTTP 403"):
        ai.call_claude(SimpleNamespaceCfg(), "s", "u")


class SimpleNamespaceCfg:
    """Minimum config shape call_claude reads — backend (sdk default) +
       the SDK-path API key. Tests for the CLI backend instantiate with
       claude_backend='cli' to flip the dispatcher."""
    anthropic_api_key = "sk-test-placeholder"
    claude_backend = "sdk"


class _CliCfg:
    """A config with HONE_CLAUDE_BACKEND=cli set, for the CLI-path tests.
       anthropic_api_key isn't read but kept for shape parity."""
    anthropic_api_key = ""
    claude_backend = "cli"


# --- CLI backend ---------------------------------------------------------

def _envelope(text="ok", model="claude-opus-4-7",
              input_tokens=100, output_tokens=50, is_error=False):
    """A `claude --output-format json` envelope shaped how the CLI
       emits it on a successful turn."""
    return {
        "type":             "result",
        "subtype":          "success",
        "result":           text,
        "model":             model,
        "is_error":          is_error,
        "duration_ms":       1234,
        "duration_api_ms":   567,
        "num_turns":         1,
        "total_cost_usd":    0.0001,
        "usage": {"input_tokens":  input_tokens,
                  "output_tokens": output_tokens,
                  "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens":     0},
    }


def _patch_run(monkeypatch, *, returncode=0, stdout="", stderr="",
                file_not_found=False, timeout=False):
    """Replace subprocess.run with a stub that records its call args
       so the assertions can pin the cmdline shape."""
    import subprocess as sp
    calls = []

    def stub(cmd, input=None, capture_output=False, text=False, timeout=None):
        calls.append({"cmd": cmd, "input": input, "timeout": timeout})
        if file_not_found:
            raise FileNotFoundError("claude")
        if timeout is not None and stub._raise_timeout:
            raise sp.TimeoutExpired("claude", timeout)
        return SimpleNamespace(returncode=returncode,
                                stdout=stdout, stderr=stderr)
    stub._raise_timeout = timeout
    monkeypatch.setattr("node.ai.subprocess.run", stub)
    return calls


def test_cli_backend_runs_claude_with_the_right_cmdline(monkeypatch):
    """The CLI invocation is `claude -p --output-format json
       --system-prompt <sys> --model <model>`, with user_text piped
       through stdin (not -p) so multi-thousand-line prompts avoid
       the ARG_MAX cap."""
    calls = _patch_run(monkeypatch, stdout=json.dumps(_envelope()),
                        returncode=0)
    ai._record_outcome("auth")            # pre-existing failure category
    out = ai.call_claude(_CliCfg(), "SYS-PROMPT", "USER-MESSAGE",
                         model="claude-opus-4-7")
    assert out["text"] == "ok"
    assert out["model"] == "claude-opus-4-7"
    assert out["usage"]["input_tokens"] == 100
    assert out["usage"]["output_tokens"] == 50
    # The success cleared the prior failure category.
    assert ai.get_last_error() is None
    # Cmdline shape.
    cmd = calls[0]["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert "--system-prompt" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS-PROMPT"
    assert "--model" in cmd
    # User text on stdin (not in argv).
    assert calls[0]["input"] == "USER-MESSAGE"
    assert "USER-MESSAGE" not in cmd


def test_cli_backend_strips_fences_in_the_envelope_result(monkeypatch):
    """Even though Claude is asked for raw JSON only, the CLI
       sometimes wraps the assistant text in markdown fences. The
       same _strip_fences helper that protects the SDK path also
       protects the CLI path."""
    body = json.dumps({"a": 1})
    wrapped = f"```json\n{body}\n```"
    _patch_run(monkeypatch,
                stdout=json.dumps(_envelope(text=wrapped)), returncode=0)
    out = ai.call_claude(_CliCfg(), "s", "u")
    assert out["text"] == body            # fence removed


def test_cli_backend_translates_auth_stderr_to_auth_error(monkeypatch):
    """A `claude` CLI non-zero exit whose stderr mentions credentials /
       login surfaces as CallClaudeAuthError so the runner's existing
       config-fatal exit path triggers cleanly."""
    _patch_run(monkeypatch, returncode=1,
                stderr="Error: Please run `claude` to log in.")
    with pytest.raises(ai.CallClaudeAuthError) as ei:
        ai.call_claude(_CliCfg(), "s", "u")
    assert "claude" in str(ei.value).lower()
    assert ai.get_last_error() == "auth"
    assert ei.value.__cause__ is None


def test_cli_backend_classifies_rate_limit_for_health(monkeypatch):
    """A `rate limit` stderr → _LAST_ERROR='rate_limit' (and a generic
       RuntimeError raised, since rate-limit is recoverable via
       backoff but isn't auth-fatal)."""
    _patch_run(monkeypatch, returncode=1,
                stderr="Error: rate limit exceeded; retry later")
    with pytest.raises(RuntimeError):
        ai.call_claude(_CliCfg(), "s", "u")
    assert ai.get_last_error() == "rate_limit"


def test_cli_backend_handles_missing_binary(monkeypatch):
    """If the `claude` binary isn't in PATH (Dockerfile built without
       the CLI layer), surface the operator-facing message — same
       shape as a wrong API key."""
    _patch_run(monkeypatch, file_not_found=True)
    with pytest.raises(ai.CallClaudeAuthError) as ei:
        ai.call_claude(_CliCfg(), "s", "u")
    assert "claude" in str(ei.value)
    assert "PATH" in str(ei.value)
    assert ai.get_last_error() == "auth"


def test_cli_backend_handles_unparseable_envelope(monkeypatch):
    """If the CLI's stdout isn't a JSON object (e.g. it crashed
       mid-emit), bail with a clear RuntimeError rather than an
       opaque ValueError from the JSON decode."""
    _patch_run(monkeypatch, returncode=0, stdout="not json at all")
    with pytest.raises(RuntimeError, match="unparseable JSON"):
        ai.call_claude(_CliCfg(), "s", "u")
    assert ai.get_last_error() == "other"


def test_cli_backend_handles_error_envelope(monkeypatch):
    """A returncode=0 with an is_error=true envelope (which the CLI
       can emit on internal failures) raises rather than returning
       the assistant text."""
    env = _envelope(is_error=True)
    _patch_run(monkeypatch, returncode=0, stdout=json.dumps(env))
    with pytest.raises(RuntimeError, match="error envelope"):
        ai.call_claude(_CliCfg(), "s", "u")
    assert ai.get_last_error() == "other"
