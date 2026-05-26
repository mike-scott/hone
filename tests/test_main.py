"""Tests for core/main.py — the hone-core application entry point. Today
we only cover the uvicorn access-log filter; the lifespan + container
boot path is exercised end-to-end by the smoke / integration runs."""
import logging

from core.main import _QuietIdlePollFilter


def _access_record(method, path, status):
    """Build a uvicorn.access-shaped LogRecord. uvicorn formats access
       lines with `record.args = (client, method, full_path, http_version,
       status)` and a `%s - "%s %s HTTP/%s" %d` format string."""
    rec = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", method, path, "1.1", status),
        exc_info=None)
    return rec


def test_filter_drops_queue_idle_poll_204s():
    """`GET / 204` (the 5-second polling short-circuit) is dropped —
       this is what was filling the operator's container log."""
    f = _QuietIdlePollFilter()
    assert f.filter(_access_record("GET", "/", 204)) is False
    assert f.filter(_access_record("GET", "/?type=review", 204)) is False
    assert f.filter(_access_record("GET", "/?type=review&page=2",
                                     204)) is False


def test_filter_keeps_real_queue_renders():
    """A real render (200) is operator-relevant — the filter doesn't
       touch it. Same for non-poll status codes (4xx / 5xx)."""
    f = _QuietIdlePollFilter()
    assert f.filter(_access_record("GET", "/", 200)) is True
    assert f.filter(_access_record("GET", "/?type=review", 200)) is True
    assert f.filter(_access_record("GET", "/", 500)) is True


def test_filter_keeps_other_endpoints():
    """204s on other paths (an enqueue endpoint returning No Content,
       a future health probe, etc.) stay visible. The filter is
       scoped to the queue page's polling URL pattern."""
    f = _QuietIdlePollFilter()
    assert f.filter(_access_record("GET", "/nodes", 204)) is True
    assert f.filter(_access_record("POST", "/", 204)) is True
    assert f.filter(_access_record("GET", "/v1/claims", 204)) is True


def test_filter_passes_through_records_with_unexpected_args():
    """A future uvicorn that emits a different access-log shape
       shouldn't have everything silently dropped — unrecognised
       records pass through. The filter is defensive about args
       it can't structurally read."""
    f = _QuietIdlePollFilter()
    rec = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="",
        lineno=0, msg="some other format", args=(), exc_info=None)
    assert f.filter(rec) is True
