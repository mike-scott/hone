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


def test_parse_json_response_raises_on_no_json_present():
    with pytest.raises(ValueError, match="no parseable JSON object"):
        ai.parse_json_response("Sorry, can't help with that.")


def test_parse_json_response_raises_on_non_object():
    """The contract is one JSON OBJECT per call. A bare array, string,
       or number is a contract violation."""
    with pytest.raises(ValueError, match="expected an object"):
        ai.parse_json_response("[1, 2, 3]")
    with pytest.raises(ValueError, match="expected an object"):
        ai.parse_json_response('"just a string"')


def test_parse_json_response_recovers_from_prose_preamble():
    """Claude habitually narrates before emitting JSON — "Based on my
       discovery, …" preambles + bullets, then a fenced JSON block at
       the end. The parser scans past the prose, finds the first
       parseable `{` object, returns it. This is the production
       failure mode we hit on work-item 1."""
    raw = (
        "Based on my discovery, no kernel tree path is accessible.\n"
        "I'll operate in heuristic mode.\n\n"
        "Now analyzing the patchset content:\n"
        "- No `base-commit:` trailer\n"
        "- All patches target Qualcomm clock drivers\n\n"
        '```json\n'
        '{"outcome": "prepared", "patchset_id": "<r1@x>"}\n'
        '```')
    out = ai.parse_json_response(raw)
    assert out == {"outcome": "prepared", "patchset_id": "<r1@x>"}


def test_parse_json_response_recovers_from_prose_postamble():
    """The mirror case — JSON first, then a trailing explanation.
       raw_decode stops at the end of the first complete object and
       ignores everything after it."""
    raw = ('{"outcome": "prepared", "k": "v"}\n\n'
           "Note: I operated in heuristic mode because no tree was "
           "available.")
    out = ai.parse_json_response(raw)
    assert out == {"outcome": "prepared", "k": "v"}


def test_parse_json_response_skips_a_stray_brace_in_prose(monkeypatch):
    """A `{` inside a prose preamble that doesn't open valid JSON
       (e.g. an apologetic '{ note: ... }' written as prose) must
       not trap the scanner — it keeps trying later `{` positions."""
    raw = ("Note: in this case { I'd normally do X } but actually:\n"
           '{"outcome": "ok"}')
    out = ai.parse_json_response(raw)
    assert out == {"outcome": "ok"}


def test_parse_json_response_handles_fenced_json_after_prose():
    """The exact production shape — multi-paragraph prose, then a
       ```json … ``` block. The first-pass fence strip won't match
       (prose precedes the fence), but the second-pass scan finds
       the JSON inside the fence anyway."""
    raw = ("Some narration.\n\n"
           "```json\n"
           '{"outcome": "prepared"}\n'
           "```")
    out = ai.parse_json_response(raw)
    assert out == {"outcome": "prepared"}


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
    anthropic_model = ""
    claude_backend = "sdk"


class _CliCfg:
    """A config with HONE_CLAUDE_BACKEND=cli set, for the CLI-path tests.
       anthropic_api_key isn't read but kept for shape parity."""
    anthropic_api_key = ""
    anthropic_model = ""
    claude_backend = "cli"


# --- CLI backend ---------------------------------------------------------

def _envelope(text="ok", model="claude-opus-4-7",
              input_tokens=100, output_tokens=50, is_error=False,
              cache_creation_input_tokens=0, cache_read_input_tokens=0):
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
                  "cache_creation_input_tokens": cache_creation_input_tokens,
                  "cache_read_input_tokens":     cache_read_input_tokens},
    }


# --- stream-json event builders --------------------------------------------

def _init_event(model="claude-sonnet-4-6", tools=()):
    return {"type": "system", "subtype": "init", "session_id": "3f9cabcd12",
            "model": model, "tools": list(tools)}


def _assistant_text_event(text="ok", model="claude-sonnet-4-6"):
    return {"type": "assistant",
            "message": {"role": "assistant", "model": model,
                        "content": [{"type": "text", "text": text}]}}


def _assistant_tool_event(name="Read", tool_input=None, tool_id="toolu_1"):
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": tool_id,
                                     "name": name,
                                     "input": tool_input or {}}]}}


def _tool_result_event(tool_id="toolu_1", content="file contents"):
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "tool_result",
                                     "tool_use_id": tool_id,
                                     "content": content}]}}


def _patch_popen(monkeypatch, *, events=(), returncode=0, stderr="",
                 file_not_found=False, delay=0.0):
    """Replace subprocess.Popen with a fake that streams `events` (dicts →
       newline-delimited stream-json) on stdout after an optional `delay`,
       captures stdin, drains a `stderr` string, and exits `returncode`.
       Returns a `state` dict recording the cmd, stdin, and kill()."""
    import time as _t
    state = {"cmd": None, "stdin": "", "killed": False}
    lines = [json.dumps(e) + "\n" for e in events]
    stderr_out = stderr               # not shadowed by __init__'s stderr param

    def gen_stdout():
        if delay:
            _t.sleep(delay)
        yield from lines

    class _Stdin:
        def write(self, s): state["stdin"] += s
        def close(self): pass

    class _FakePopen:
        def __init__(self, cmd, stdin=None, stdout=None, stderr=None,
                     text=False, cwd=None):
            if file_not_found:
                raise FileNotFoundError("claude")
            state["cmd"] = cmd
            state["cwd"] = cwd
            self.stdin = _Stdin()
            self.stdout = gen_stdout()
            self.stderr = iter([stderr_out] if stderr_out else [])
        def wait(self, timeout=None):
            return returncode
        def kill(self):
            state["killed"] = True

    monkeypatch.setattr("node.ai.subprocess.Popen", _FakePopen)
    return state


def test_cli_backend_runs_claude_with_the_right_cmdline(monkeypatch):
    """The CLI invocation is `claude -p --output-format stream-json
       --verbose --system-prompt <sys> --model <model>`, with user_text
       piped through stdin (not -p) so multi-thousand-line prompts avoid
       the ARG_MAX cap."""
    state = _patch_popen(monkeypatch, events=[_envelope()])
    ai._record_outcome("auth")            # pre-existing failure category
    out = ai.call_claude(_CliCfg(), "SYS-PROMPT", "USER-MESSAGE",
                         model="claude-opus-4-7")
    assert out["text"] == "ok"
    assert out["model"] == "claude-opus-4-7"
    assert out["usage"]["input_tokens"] == 100
    assert out["usage"]["output_tokens"] == 50
    # The success cleared the prior failure category.
    assert ai.get_last_error() is None
    # Cmdline shape — streaming.
    cmd = state["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--verbose" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS-PROMPT"
    assert "--model" in cmd
    # User text on stdin (not in argv).
    assert state["stdin"] == "USER-MESSAGE"
    assert "USER-MESSAGE" not in cmd


def test_cli_backend_gates_tools(monkeypatch):
    """tools=[] passes an empty --allowedTools (no tools); a list passes the
       names; tools=None leaves the flag off (CLI default)."""
    state = _patch_popen(monkeypatch, events=[_envelope()])
    ai.call_claude(_CliCfg(), "s", "u", tools=[])
    cmd = state["cmd"]
    assert cmd[cmd.index("--allowedTools") + 1] == ""      # empty → no tools

    state = _patch_popen(monkeypatch, events=[_envelope()])
    ai.call_claude(_CliCfg(), "s", "u", tools=["Read", "Grep"])
    cmd = state["cmd"]
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep"

    state = _patch_popen(monkeypatch, events=[_envelope()])
    ai.call_claude(_CliCfg(), "s", "u")                    # tools=None default
    assert "--allowedTools" not in state["cmd"]
    assert "--disallowedTools" not in state["cmd"]


def test_cli_backend_hard_blocks_escape_tools_when_constrained(monkeypatch):
    """A constrained turn (tools given) ALSO passes --disallowedTools, since
       --allowedTools only governs auto-approval, not what may run. The
       denylist hard-blocks subagents (Task/Agent) and shell (Bash) — the
       fan-out path that wedged a review CLI — plus file-mutation and network
       tools. tools=None (CLI default) opts out of the denylist."""
    # A read-only review tool set still gets the hard denylist.
    state = _patch_popen(monkeypatch, events=[_envelope()])
    ai.call_claude(_CliCfg(), "s", "u", tools=["Read", "Grep", "Glob"])
    cmd = state["cmd"]
    blocked = cmd[cmd.index("--disallowedTools") + 1].split(",")
    for name in ("Task", "Agent", "Bash", "Write", "Edit"):
        assert name in blocked
    # ...and the empty (no-tools) prepare turn too.
    state = _patch_popen(monkeypatch, events=[_envelope()])
    ai.call_claude(_CliCfg(), "s", "u", tools=[])
    assert "--disallowedTools" in state["cmd"]


def test_cli_backend_logs_a_heartbeat_while_claude_thinks(monkeypatch, caplog):
    """A turn that outlasts the heartbeat interval (here, a stall before the
       events arrive) emits 'still working' elapsed-time lines so a console
       viewer isn't staring at a frozen log."""
    monkeypatch.setattr(ai, "_HEARTBEAT_SECONDS", 0.02)
    _patch_popen(monkeypatch, events=[_envelope()], delay=0.1)   # ~5 intervals
    with caplog.at_level("INFO", logger="hone.node.ai"):
        ai.call_claude(_CliCfg(), "SYS", "USER")
    assert any("still working" in r.message for r in caplog.records)


def test_cli_backend_no_heartbeat_for_a_fast_call(monkeypatch, caplog):
    """A turn shorter than the interval logs no heartbeat — the watchdog
       only fires for genuinely slow calls."""
    _patch_popen(monkeypatch, events=[_envelope()])
    with caplog.at_level("INFO", logger="hone.node.ai"):
        ai.call_claude(_CliCfg(), "SYS", "USER")
    assert not any("still working" in r.message for r in caplog.records)


def test_sdk_backend_logs_a_heartbeat_while_claude_thinks(monkeypatch, caplog):
    """The SDK path blocks on one HTTPS call, also silent — so it gets the
       same elapsed-time heartbeat when the turn outlasts the interval."""
    import time as _t
    import anthropic
    monkeypatch.setattr(ai, "_HEARTBEAT_SECONDS", 0.02)

    class _SlowMessages:
        def create(self, **kw):
            _t.sleep(0.1)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                      cache_read_input_tokens=0,
                                      cache_creation_input_tokens=0))

    class _SlowClient:
        def __init__(self, **kw): pass
        @property
        def messages(self): return _SlowMessages()

    monkeypatch.setattr(anthropic, "Anthropic", _SlowClient)
    with caplog.at_level("INFO", logger="hone.node.ai"):
        out = ai.call_claude(SimpleNamespaceCfg(), "SYS", "USER")
    assert out["text"] == "ok"
    assert any("still working" in r.message for r in caplog.records)


def test_cli_backend_sums_cache_tokens_into_input_tokens(monkeypatch):
    """The CLI envelope's `input_tokens` is the *non-cached* portion
       only. cache_creation_input_tokens (first-write to cache) and
       cache_read_input_tokens (served from cache) must be added in,
       otherwise a 30k-token prompt served from cache looks like a
       ~10-token request in the completion record.

       Tracks audit finding #1: token usage was severely under-
       reported because the wrapper was reading bare `input_tokens`
       and ignoring the cache split."""
    env = _envelope(input_tokens=50,
                     cache_creation_input_tokens=200,
                     cache_read_input_tokens=29750,
                     output_tokens=1000)
    _patch_popen(monkeypatch, events=[env])
    out = ai.call_claude(_CliCfg(), "SYS", "USER")
    assert out["usage"]["input_tokens"] == 30000      # 50 + 200 + 29750
    assert out["usage"]["output_tokens"] == 1000
    # No cache fields leak into usage — the completion-record schema
    # has additionalProperties:false on `usage`, cache splits live
    # in `meta` per docs.
    assert "cache_read_input_tokens" not in out["usage"]
    assert "cache_creation_input_tokens" not in out["usage"]


def test_cli_backend_uses_anthropic_model_from_cfg(monkeypatch):
    """When the call site doesn't pass `model=`, the dispatcher falls
       back to cfg.anthropic_model (sourced from $ANTHROPIC_MODEL) and
       passes it to `claude --model`. This is the operator-facing
       knob that lets .env pin the model without code changes."""
    state = _patch_popen(monkeypatch, events=[_envelope()])
    cfg = _CliCfg()
    cfg.anthropic_model = "claude-sonnet-4-6"
    ai.call_claude(cfg, "SYS", "USER")          # no explicit model=
    cmd = state["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"


def test_explicit_model_kwarg_beats_cfg_anthropic_model(monkeypatch):
    """Explicit model= at the call site wins over cfg.anthropic_model.
       Keeps the env knob from silently overriding per-call requests
       (none today, but future operations may want it)."""
    state = _patch_popen(monkeypatch, events=[_envelope()])
    cfg = _CliCfg()
    cfg.anthropic_model = "claude-sonnet-4-6"
    ai.call_claude(cfg, "SYS", "USER", model="claude-opus-4-7")
    cmd = state["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-7"


def test_cli_backend_strips_fences_in_the_result(monkeypatch):
    """Even though Claude is asked for raw JSON only, the CLI
       sometimes wraps the assistant text in markdown fences. The
       same _strip_fences helper that protects the SDK path also
       protects the CLI path."""
    body = json.dumps({"a": 1})
    wrapped = f"```json\n{body}\n```"
    _patch_popen(monkeypatch, events=[_envelope(text=wrapped)])
    out = ai.call_claude(_CliCfg(), "s", "u")
    assert out["text"] == body            # fence removed


def test_cli_backend_captures_assistant_and_tool_trace(monkeypatch):
    """The streamed assistant text + tool_use/tool_result events are
       captured into `trace` — the ordered record hone-core persists and
       presents in the web UI."""
    events = [
        _init_event(tools=["Read"]),
        _assistant_text_event("let me look"),
        _assistant_tool_event("Read", {"file_path": "drivers/net/foo.c"}),
        _tool_result_event(content="x" * 1234),
        _assistant_text_event('{"ok": true}'),
        _envelope(text='{"ok": true}'),
    ]
    _patch_popen(monkeypatch, events=events)
    out = ai.call_claude(_CliCfg(), "s", "u")
    steps = [s["step"] for s in out["trace"]]
    assert steps == ["assistant_text", "tool_use", "tool_result",
                     "assistant_text"]
    tool = next(s for s in out["trace"] if s["step"] == "tool_use")
    assert tool["name"] == "Read"
    assert tool["input"]["file_path"] == "drivers/net/foo.c"
    assert next(s for s in out["trace"]
                if s["step"] == "tool_result")["chars"] == 1234


def test_cli_backend_logs_tool_target_and_text_snippet(monkeypatch, caplog):
    """INFO lines show WHAT Claude is doing — a short snippet of assistant
       text and the tool's target file — not just lengths/names."""
    events = [
        _init_event(tools=["Read"]),
        _assistant_text_event("Let me read the changed file before judging."),
        _assistant_tool_event("Read", {"file_path": "drivers/net/foo.c"}),
        _envelope(text='{"ok": true}'),
    ]
    _patch_popen(monkeypatch, events=events)
    with caplog.at_level("INFO", logger="hone.node.ai"):
        ai.call_claude(_CliCfg(), "s", "u")
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "drivers/net/foo.c" in msgs                 # tool target shown
    assert "Let me read the changed file" in msgs      # text snippet shown


def test_cli_backend_translates_auth_stderr_to_auth_error(monkeypatch):
    """A `claude` CLI non-zero exit whose stderr mentions credentials /
       login surfaces as CallClaudeAuthError so the runner's existing
       config-fatal exit path triggers cleanly."""
    _patch_popen(monkeypatch, returncode=1,
                 stderr="Error: Please run `claude` to log in.")
    with pytest.raises(ai.CallClaudeAuthError) as ei:
        ai.call_claude(_CliCfg(), "s", "u")
    assert "claude" in str(ei.value).lower()
    assert ai.get_last_error() == "auth"
    assert ei.value.__cause__ is None


def test_cli_backend_classifies_rate_limit_for_health(monkeypatch):
    """A `rate limit` stderr → _LAST_ERROR='rate_limit' and a
       CallClaudeError (carrying the category) rather than a config-fatal
       auth error — the handler turns it into a submittable record."""
    _patch_popen(monkeypatch, returncode=1,
                 stderr="Error: rate limit exceeded; retry later")
    with pytest.raises(ai.CallClaudeError) as ei:
        ai.call_claude(_CliCfg(), "s", "u")
    assert ei.value.category == "rate_limit"
    assert ei.value.returncode == 1
    assert ai.get_last_error() == "rate_limit"


def test_cli_backend_handles_missing_binary(monkeypatch):
    """If the `claude` binary isn't in PATH (Dockerfile built without
       the CLI layer), surface the operator-facing message — same
       shape as a wrong API key."""
    _patch_popen(monkeypatch, file_not_found=True)
    with pytest.raises(ai.CallClaudeAuthError) as ei:
        ai.call_claude(_CliCfg(), "s", "u")
    assert "claude" in str(ei.value)
    assert "PATH" in str(ei.value)
    assert ai.get_last_error() == "auth"


def test_cli_backend_errors_when_stream_has_no_result(monkeypatch):
    """A clean exit whose stream never carried a `result` event (the CLI
       crashed mid-stream) bails with a CallClaudeError."""
    _patch_popen(monkeypatch, returncode=0,
                 events=[_init_event(), _assistant_text_event()])
    with pytest.raises(ai.CallClaudeError, match="without a result event"):
        ai.call_claude(_CliCfg(), "s", "u")
    assert ai.get_last_error() == "other"


def test_cli_backend_handles_error_result(monkeypatch):
    """A `result` event with is_error=true (CLI internal failure) raises
       a CallClaudeError rather than returning the assistant text."""
    _patch_popen(monkeypatch, returncode=0, events=[_envelope(is_error=True)])
    with pytest.raises(ai.CallClaudeError, match="error result"):
        ai.call_claude(_CliCfg(), "s", "u")
    assert ai.get_last_error() == "other"


def test_cli_backend_classifies_self_signed_cert_as_connection(monkeypatch):
    """A TLS 'self-signed certificate' failure surfaces as an assistant
       'API Error: …' message + a non-success result on a *clean* exit, not
       on stderr. It must classify as `connection` so the operator's health
       page points at the network/proxy cause, not the catch-all `other`."""
    msg = ("API Error: Unable to connect to API: Self-signed certificate "
           "detected. Check your proxy or corporate SSL certificates")
    _patch_popen(monkeypatch, returncode=0,
                 events=[_init_event(),
                         _assistant_text_event(text=msg),
                         _envelope(text=msg, is_error=True)])
    with pytest.raises(ai.CallClaudeError) as ei:
        ai.call_claude(_CliCfg(), "s", "u")
    assert ei.value.category == "connection"
    assert ai.get_last_error() == "connection"


def test_cli_backend_classifies_api_error_success_result(monkeypatch):
    """Defensive: even when the CLI marks the turn a *success* but its result
       text is an 'API Error: …' transport failure (no is_error flag), the
       call raises CallClaudeError with the real category rather than
       returning the error string as if it were the model's answer."""
    msg = "API Error: Unable to connect to API: Self-signed certificate detected"
    _patch_popen(monkeypatch, returncode=0,
                 events=[_init_event(), _envelope(text=msg, is_error=False)])
    with pytest.raises(ai.CallClaudeError) as ei:
        ai.call_claude(_CliCfg(), "s", "u")
    assert ei.value.category == "connection"
    assert "api error" in str(ei.value).lower()
    assert ai.get_last_error() == "connection"


def test_cli_backend_failure_preserves_partial_trace(monkeypatch):
    """A non-zero CLI exit carries everything the turn produced before it
       failed — the partial trace (assistant text + tool_use + tool_result),
       the stderr, the failure category, and the returncode — so the
       handler can submit it as a record instead of crashing the loop."""
    _patch_popen(
        monkeypatch, returncode=1, stderr="Error: kaboom",
        events=[_init_event(),
                _assistant_text_event(text="Looking at the patch."),
                _assistant_tool_event(name="Read",
                                      tool_input={"file_path": "mm/slab.c"}),
                _tool_result_event(content="x" * 42)])
    with pytest.raises(ai.CallClaudeError) as ei:
        ai.call_claude(_CliCfg(), "s", "u")
    exc = ei.value
    assert exc.category == "other"
    assert exc.returncode == 1
    assert "kaboom" in exc.stderr
    steps = [s["step"] for s in exc.trace]
    assert steps == ["assistant_text", "tool_use", "tool_result"]
    assert exc.trace[0]["text"] == "Looking at the patch."
    assert exc.trace[1]["name"] == "Read"
    assert exc.trace[2]["chars"] == 42


# --- pre-prompt CLI update (pinned image, current fleet) --------------------

def test_cli_backend_checks_for_updates_before_the_prompt(monkeypatch):
    """With HONE_CLAUDE_AUTOUPDATE on (the default), the CLI path runs
       `claude update` before launching the prompt subprocess."""
    monkeypatch.setenv("HONE_CLAUDE_AUTOUPDATE", "1")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        class R: returncode, stdout, stderr = 0, "already current", ""
        return R()
    monkeypatch.setattr(ai.subprocess, "run", fake_run)
    state = _patch_popen(monkeypatch, events=[_envelope()])
    ai.call_claude(_CliCfg(), "s", "u")
    assert ["claude", "update"] in calls
    assert state["cmd"][0] == "claude"            # the prompt still ran


def test_update_cli_is_skipped_when_opted_out(monkeypatch):
    """HONE_CLAUDE_AUTOUPDATE=0 (air-gapped deployments — and the test
       suite's autouse default) never spawns the update subprocess."""
    def boom(*a, **kw):
        raise AssertionError("update must not run when opted out")
    monkeypatch.setattr(ai.subprocess, "run", boom)
    ai._update_cli()                              # opt-out via conftest


def test_update_cli_swallows_every_failure_mode(monkeypatch):
    """Offline, a bad exit, a timeout, or no binary at all: the update is
       best-effort and the prompt proceeds on the current version."""
    monkeypatch.setenv("HONE_CLAUDE_AUTOUPDATE", "1")
    monkeypatch.delenv("HONE_CLAUDE_BIN_DIR", raising=False)

    def failing(rc=1):
        class R: returncode, stdout, stderr = rc, "", "registry unreachable"
        return lambda *a, **kw: R()
    monkeypatch.setattr(ai.subprocess, "run", failing())
    ai._update_cli()                              # rc=1 — no raise

    def raise_timeout(*a, **kw):
        raise ai.subprocess.TimeoutExpired(cmd="claude update", timeout=1)
    monkeypatch.setattr(ai.subprocess, "run", raise_timeout)
    ai._update_cli()                              # timeout — no raise

    def raise_missing(*a, **kw):
        raise FileNotFoundError("claude")
    monkeypatch.setattr(ai.subprocess, "run", raise_missing)
    ai._update_cli()                              # no binary — no raise


def test_ensure_persistent_cli_seeds_the_volume_copy_once(monkeypatch, tmp_path):
    """First use copies the image's pinned binary into
       HONE_CLAUDE_BIN_DIR (the persistent volume, first on PATH) so
       self-updates land there — no overlay-layer churn, and the updated
       CLI survives container recreates. A second call is a no-op."""
    src = tmp_path / "image" / "claude"
    src.parent.mkdir()
    src.write_bytes(b"pinned-binary")
    bin_dir = tmp_path / "data" / "bin"
    monkeypatch.setenv("HONE_CLAUDE_BIN_DIR", str(bin_dir))
    monkeypatch.setattr(ai.shutil, "which", lambda name: str(src))
    ai._ensure_persistent_cli()
    dst = bin_dir / "claude"
    assert dst.read_bytes() == b"pinned-binary"
    # Second call leaves an (updated) volume copy alone.
    dst.write_bytes(b"self-updated")
    ai._ensure_persistent_cli()
    assert dst.read_bytes() == b"self-updated"


# --- CLI version probe (health-snapshot provenance) --------------------------

def test_get_cli_version_probes_once_and_caches(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        class R: returncode, stdout, stderr = 0, "2.1.161 (Claude Code)\n", ""
        return R()
    monkeypatch.setattr(ai.subprocess, "run", fake_run)
    monkeypatch.setitem(ai._CLI_VERSION_CACHE, "fresh", False)
    assert ai.get_cli_version() == "2.1.161 (Claude Code)"
    assert ai.get_cli_version() == "2.1.161 (Claude Code)"
    assert calls == [["claude", "--version"]]      # one probe, then cached


def test_get_cli_version_none_when_binary_missing(monkeypatch):
    def raise_missing(*a, **kw):
        raise FileNotFoundError("claude")
    monkeypatch.setattr(ai.subprocess, "run", raise_missing)
    monkeypatch.setitem(ai._CLI_VERSION_CACHE, "fresh", False)
    assert ai.get_cli_version() is None


def test_successful_update_invalidates_the_version_cache(monkeypatch):
    """After `claude update` runs cleanly the cached version is re-probed
       on the next health tick — the snapshot must report the build
       actually in use."""
    monkeypatch.setenv("HONE_CLAUDE_AUTOUPDATE", "1")
    monkeypatch.delenv("HONE_CLAUDE_BIN_DIR", raising=False)

    def fake_run(cmd, **kw):
        class R: returncode, stdout, stderr = 0, "updated to 2.2.0", ""
        return R()
    monkeypatch.setattr(ai.subprocess, "run", fake_run)
    monkeypatch.setitem(ai._CLI_VERSION_CACHE, "fresh", True)
    ai._update_cli()
    assert ai._CLI_VERSION_CACHE["fresh"] is False
