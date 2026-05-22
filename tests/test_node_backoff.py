"""Unit tests for the hone-node transient-failure backoff (node/runner.py)."""
import httpx
import pytest

from node import runner
from node.client import EnrollmentError


def _http_error(status, headers=None):
    req = httpx.Request("POST", "https://core.example/v1/claims")
    resp = httpx.Response(status, request=req, headers=headers or {})
    return httpx.HTTPStatusError("err", request=req, response=resp)


class _Cfg:                       # only the fields _with_backoff reads
    backoff_initial = 0.001
    backoff_max = 0.01


# --- _is_transient ---------------------------------------------------------

@pytest.mark.parametrize("exc, transient", [
    (httpx.ConnectError("refused"), True),
    (httpx.ConnectTimeout("timeout"), True),
    (httpx.ReadTimeout("timeout"), True),
    (_http_error(500), True),
    (_http_error(503), True),
    (_http_error(429), True),
    (_http_error(422), False),
    (_http_error(403), False),
    (ValueError("nope"), False),
])
def test_is_transient(exc, transient):
    assert runner._is_transient(exc) is transient


# --- _retry_after ----------------------------------------------------------

def test_retry_after_integer_seconds():
    assert runner._retry_after(_http_error(429, {"Retry-After": "7"})) == 7.0


def test_retry_after_http_date_falls_back_to_none():
    err = _http_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert runner._retry_after(err) is None


def test_retry_after_absent():
    assert runner._retry_after(_http_error(429)) is None
    assert runner._retry_after(httpx.ConnectError("x")) is None


# --- _with_backoff ---------------------------------------------------------

def test_retries_a_transient_failure_then_succeeds(monkeypatch):
    slept = []
    monkeypatch.setattr(runner.time, "sleep", slept.append)
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 4:
            raise httpx.ConnectError("core down")
        return "done"

    assert runner._with_backoff(_Cfg, "claim", fn) == "done"
    assert len(calls) == 4 and len(slept) == 3      # 3 failures -> 3 backoffs


def test_a_non_transient_failure_propagates_without_backoff(monkeypatch):
    slept = []
    monkeypatch.setattr(runner.time, "sleep", slept.append)

    def fn():
        raise _http_error(422)                      # a node bug — not retried

    with pytest.raises(httpx.HTTPStatusError):
        runner._with_backoff(_Cfg, "submit", fn)
    assert slept == []


def test_enrollment_error_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)

    def fn():
        raise EnrollmentError("node revoked")

    with pytest.raises(EnrollmentError):
        runner._with_backoff(_Cfg, "claim", fn)


def test_retry_after_is_honoured_over_the_jittered_backoff(monkeypatch):
    slept = []
    monkeypatch.setattr(runner.time, "sleep", slept.append)
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, {"Retry-After": "5"})
        return "ok"

    runner._with_backoff(_Cfg, "claim", fn)
    assert slept == [5.0]                           # the exact Retry-After
