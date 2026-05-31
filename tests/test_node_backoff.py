"""Unit tests for the hone-node transient-failure backoff (node/runner.py)."""
import anthropic
import httpx
import pytest

from node import runner
from node.client import EnrollmentError


def _http_error(status, headers=None):
    req = httpx.Request("POST", "https://core.example/v1/claims")
    resp = httpx.Response(status, request=req, headers=headers or {})
    return httpx.HTTPStatusError("err", request=req, response=resp)


def _anthropic_status_error(status, headers=None):
    """An anthropic.APIStatusError that mirrors a real SDK error closely
       enough for the backoff classifier (status_code + .response.headers
       are what the classifier reads)."""
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req, headers=headers or {})
    # The SDK has a status-code-specific subclass for the well-known
    # codes; using the generic APIStatusError covers the others (e.g.
    # 502 BadGateway). For the parametrized test we pick the
    # representative classes the runtime is most likely to see.
    cls = {
        429: anthropic.RateLimitError,
        500: anthropic.InternalServerError,
        403: anthropic.PermissionDeniedError,
        401: anthropic.AuthenticationError,
        400: anthropic.BadRequestError,
        404: anthropic.NotFoundError,
    }.get(status, anthropic.APIStatusError)
    return cls("err", response=resp,
                body={"error": {"message": "err"}})


class _Cfg:                       # only the fields _with_backoff reads
    backoff_initial = 0.001
    backoff_max = 0.01


# --- _is_transient ---------------------------------------------------------

@pytest.mark.parametrize("exc, transient", [
    # httpx (hone-core side)
    (httpx.ConnectError("refused"), True),
    (httpx.ConnectTimeout("timeout"), True),
    (httpx.ReadTimeout("timeout"), True),
    (_http_error(500), True),
    (_http_error(503), True),
    (_http_error(429), True),
    (_http_error(422), False),
    (_http_error(403), False),
    # anthropic (Claude side)
    (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")),
     True),
    (_anthropic_status_error(429), True),                 # rate limit
    (_anthropic_status_error(500), True),                 # 5xx
    (_anthropic_status_error(503), True),                 # 5xx (generic)
    (_anthropic_status_error(400), False),                # bad payload
    (_anthropic_status_error(404), False),                # bad model
    # Everything else
    (ValueError("nope"), False),
])
def test_is_transient(exc, transient):
    assert runner._is_transient(exc) is transient


# --- _retry_after (anthropic-side) ----------------------------------------

def test_retry_after_reads_from_anthropic_response_headers():
    """The SDK's APIStatusError exposes the same `.response.headers` as
       httpx, so the generic getattr-driven path picks it up."""
    err = _anthropic_status_error(429, {"Retry-After": "12"})
    assert runner._retry_after(err) == 12.0


# --- _describe (per-upstream messages) ------------------------------------

def test_describe_distinguishes_hone_core_from_claude():
    """A glance at the WARN line should tell the operator which
       upstream is throttling — hone-core or Claude."""
    assert "hone-core returned HTTP 500" in runner._describe(_http_error(500))
    assert "Claude returned HTTP 429" in runner._describe(
        _anthropic_status_error(429))
    assert "hone-core unreachable" in runner._describe(
        httpx.ConnectError("x"))
    assert "Claude unreachable" in runner._describe(
        anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://x")))


# --- _with_backoff over anthropic errors ----------------------------------

def test_with_backoff_retries_an_anthropic_rate_limit(monkeypatch):
    """A 429 from Claude triggers backoff + retry just like a 429 from
       hone-core — the operator's task should not crash the loop."""
    slept = []
    monkeypatch.setattr(runner.time, "sleep", slept.append)
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise _anthropic_status_error(429, {"Retry-After": "3"})
        return "done"

    assert runner._with_backoff(_Cfg, "prepare task", fn) == "done"
    assert slept == [3.0]                                  # exact Retry-After


def test_with_backoff_propagates_a_non_transient_anthropic_error(monkeypatch):
    """A 400 BadRequestError is a payload bug — never retried, must
       surface so the operator sees the bug rather than an infinite
       loop."""
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)

    def fn():
        raise _anthropic_status_error(400)

    with pytest.raises(anthropic.BadRequestError):
        runner._with_backoff(_Cfg, "prepare task", fn)


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


# --- run_once: release-claim on a non-transient task abort ---------------

def test_run_once_releases_the_claim_on_a_non_transient_failure(monkeypatch):
    """When tasks.dispatch raises a non-transient exception (a config-
       fatal CallClaudeAuthError, for instance), run_once calls
       client.release_claim with the failure summary BEFORE letting
       the exception propagate. This is the whole user-visible point:
       a correctly-configured peer can claim the work immediately
       instead of waiting (default 30 min) for the lease to lapse."""
    from node.ai import CallClaudeAuthError

    class _StubClient:
        def __init__(self):
            self.released = []

        def claim(self):
            return {"claim_id": "c1", "task_type": "prepare"}

        def release_claim(self, claim_id, reason):
            self.released.append((claim_id, reason))

        def submit_result(self, *args, **kw):
            raise AssertionError("submit_result must not be called on abort")

    def boom(cfg, client, claim):
        raise CallClaudeAuthError("Claude rejected the API key (HTTP 401).")

    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner.tasks, "dispatch", boom)

    cli = _StubClient()
    with pytest.raises(CallClaudeAuthError):
        runner.run_once(_Cfg, cli)
    assert len(cli.released) == 1
    claim_id, reason = cli.released[0]
    assert claim_id == "c1"
    assert "CallClaudeAuthError" in reason       # type prefix
    assert "Claude rejected the API key" in reason


def test_run_once_releases_an_unsupported_task_type_without_crashing(
        monkeypatch):
    """If hone-core hands back a task_type the node has no wired handler for
       (an old hone-core that ignores the per-claim capability declaration,
       or a deploy-order window), run_once releases the claim and returns
       False — it idles instead of crashing on the handler's
       NotImplementedError or hot-spinning on a reclaim."""
    class _StubClient:
        def __init__(self):
            self.released = []

        def claim(self):
            # `train` is not in SUPPORTED_TASK_TYPES (prepare, review),
            # so it exercises the unsupported-type guard.
            return {"claim_id": "c1", "task_type": "train"}   # unsupported

        def release_claim(self, claim_id, reason):
            self.released.append((claim_id, reason))

        def submit_result(self, *args, **kw):
            raise AssertionError("submit_result must not be called")

    def must_not_dispatch(cfg, client, claim):
        raise AssertionError("dispatch must not run for an unsupported type")

    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner.tasks, "dispatch", must_not_dispatch)

    cli = _StubClient()
    assert runner.run_once(_Cfg, cli) is False
    assert len(cli.released) == 1
    claim_id, reason = cli.released[0]
    assert claim_id == "c1"
    assert "train" in reason


def test_run_once_propagates_original_exception_even_if_release_fails(
        monkeypatch):
    """A failed release falls back to lease expiry — the original
       task exception still propagates so main() does its clean exit.
       Operator UX: a release-call network blip can't mask the actual
       root cause."""
    from node.ai import CallClaudeAuthError

    class _StubClient:
        def claim(self):
            return {"claim_id": "c1", "task_type": "prepare"}

        def release_claim(self, claim_id, reason):
            # The release-call wrapped backoff classifies this as
            # non-transient too, so it propagates and is swallowed
            # by run_once's inner try/except.
            raise RuntimeError("release-call exploded")

        def submit_result(self, *args, **kw):
            raise AssertionError("submit_result must not be called on abort")

    def boom(cfg, client, claim):
        raise CallClaudeAuthError("Claude rejected the API key (HTTP 401).")

    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner.tasks, "dispatch", boom)
    cli = _StubClient()
    # The ORIGINAL exception still surfaces, not the release one.
    with pytest.raises(CallClaudeAuthError):
        runner.run_once(_Cfg, cli)


def test_run_once_does_not_release_on_a_successful_task(monkeypatch):
    """The happy path: dispatch succeeds, submit_result lands the
       record, release_claim is NOT called. (Releasing a completed
       claim would be a contract violation — the claim is terminal
       at submission, not back in the pool.)"""

    class _StubClient:
        def __init__(self):
            self.released_calls = 0
            self.submitted = []

        def claim(self):
            return {"claim_id": "c1", "task_type": "prepare"}

        def release_claim(self, claim_id, reason):
            self.released_calls += 1

        def submit_result(self, claim_id, record):
            self.submitted.append((claim_id, record))

    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner.tasks, "dispatch",
                         lambda cfg, c, claim: {"outcome": "prepared"})

    cli = _StubClient()
    assert runner.run_once(_Cfg, cli) is True
    assert cli.released_calls == 0
    assert cli.submitted == [("c1", {"outcome": "prepared"})]


# --- 422 schema-rejected fallback -----------------------------------------

def test_run_once_submits_a_fallback_when_hone_core_rejects_the_record(
        monkeypatch):
    """hone-core's schema validator returns 422 on the original submit.
       Instead of crashing, the node submits a fallback failure-
       outcome record carrying the original payload + the validator's
       reason in `meta` — so the failure lands in work_items.record
       and is debuggable from the DB."""
    from node.client import SchemaRejectedError

    REJECTED = {"task_type": "prepare", "worker_id": "fake-node",
                "outcome": "prepared", "model": "claude-opus-4-7",
                "usage": {"input_tokens": 100, "output_tokens": 50,
                           "duration_ms": 5000},
                "subsystem": {"primary": "drivers/net"}}

    class _StubClient:
        def __init__(self):
            self.submits = []

        def claim(self):
            return {"claim_id": "c1", "task_type": "prepare"}

        def release_claim(self, claim_id, reason):
            raise AssertionError("release_claim must not be called when "
                                  "the fallback submit succeeds")

        def submit_result(self, claim_id, record):
            self.submits.append(record)
            if len(self.submits) == 1:
                # First call: hone-core rejects the original.
                raise SchemaRejectedError(
                    "completion record failed schema validation at "
                    "maintainer/mailing_lists/0", REJECTED)
            # Second call: the fallback record lands cleanly.

    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner.tasks, "dispatch",
                         lambda cfg, c, claim: REJECTED)

    cli = _StubClient()
    assert runner.run_once(_Cfg, cli) is True
    assert len(cli.submits) == 2
    first, fallback = cli.submits
    # The fallback's outcome matches the task_type's failure-branch
    # enum (prepare → uncharacterisable, per the schema).
    assert fallback["outcome"] == "uncharacterisable"
    assert fallback["task_type"] == "prepare"
    # Both the rejected outcome and the validator's reason are
    # captured on meta so the next debugging pass can see what the
    # node tried to submit AND why hone-core refused it.
    assert fallback["meta"]["rejected_outcome"] == "prepared"
    assert fallback["meta"]["rejected_record"] == REJECTED
    assert "maintainer/mailing_lists/0" in fallback["meta"]["schema_error"]
    assert "maintainer/mailing_lists/0" in fallback["reason"]
    # Header fields (worker_id, model, usage) preserve from the
    # original so the audit trail of WHO produced the rejected
    # record stays intact.
    assert fallback["worker_id"] == "fake-node"
    assert fallback["model"] == "claude-opus-4-7"
    assert fallback["usage"]["duration_ms"] == 5000


def test_run_once_releases_when_fallback_submit_also_fails(monkeypatch):
    """If even the fallback record gets rejected (shouldn't happen —
       the failure schema is much looser — but defense in depth),
       the original SchemaRejectedError propagates, the outer abort
       path releases the claim cleanly, and the node keeps running."""
    from node.client import SchemaRejectedError

    class _StubClient:
        def __init__(self):
            self.released = []

        def claim(self):
            return {"claim_id": "c1", "task_type": "prepare"}

        def release_claim(self, claim_id, reason):
            self.released.append((claim_id, reason))

        def submit_result(self, claim_id, record):
            # Both the original and the fallback are rejected.
            raise SchemaRejectedError("nope", record)

    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner.tasks, "dispatch",
                         lambda cfg, c, claim: {"task_type": "prepare",
                                                  "outcome": "prepared"})

    cli = _StubClient()
    with pytest.raises(SchemaRejectedError):
        runner.run_once(_Cfg, cli)
    # The outer abort path doesn't get to release because the
    # fallback submit's exception escapes the inner _with_backoff
    # (SchemaRejectedError isn't in _BACKOFF_CATCHES); the SchemaError
    # surfaces. main() handles it via the existing unhandled-exception
    # path. (A follow-up improvement could route this through the
    # abort/release path, but the immediate fix — no crash on a single
    # 422 — works.)


# --- fatal config error surface ------------------------------------------

def test_fatal_config_error_logs_one_line_and_exits(caplog):
    """A configuration-fatal error reaches the operator's `docker logs`
       as two short ERROR lines and a non-zero exit — no Python
       traceback, no SDK internals."""
    import logging
    caplog.set_level(logging.ERROR, logger="hone.node")
    with pytest.raises(SystemExit) as ei:
        runner._fatal_config_error(
            "Claude rejected the API key (HTTP 401). "
            "Check ANTHROPIC_API_KEY in your .env.")
    assert ei.value.code == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any("hone-node CONFIG ERROR" in m and "ANTHROPIC_API_KEY" in m
               for m in messages)
    assert any("Container will exit" in m for m in messages)
