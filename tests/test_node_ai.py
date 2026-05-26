"""Tests for node/ai.py — the Claude API wrapper's helpers. The actual
call_claude() roundtrip through the Anthropic SDK is exercised in
integration / end-to-end paths only; here we cover the resilience
helpers (fence stripping, JSON parsing) that the handlers depend on."""
import json

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
    """Minimum config shape call_claude reads — just the API key."""
    anthropic_api_key = "sk-test-placeholder"
