"""Tests for core/main.py — the hone-core application entry point. Today
we cover the uvicorn access-log filter and the create_app() fail-closed
checks; the lifespan + container boot path is exercised end-to-end by the
smoke / integration runs."""
import logging

import pytest

from core.main import _QuietIdlePollFilter, create_app


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


# --- create_app: fail-closed on a missing session secret ---

def _set_required_env(monkeypatch, *, session_secret):
    """Set the env vars create_app() reads, with `session_secret` controllable."""
    monkeypatch.setenv("HONE_FLEET_SECRET", "fleet")
    monkeypatch.setenv("HONE_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("HONE_HOSTNAME", "core.example")
    if session_secret is None:
        monkeypatch.delenv("HONE_SESSION_SECRET", raising=False)
    else:
        monkeypatch.setenv("HONE_SESSION_SECRET", session_secret)


def test_create_app_refuses_to_start_without_a_session_secret(monkeypatch):
    """No HONE_SESSION_SECRET → hone-core refuses to construct the app. The
       alternative is a known-constant signing key that any reader of the
       source could use to forge an admin session cookie — there is no safe
       fallback, so we fail-closed at construction (before any middleware is
       added). The error message tells the operator exactly what to do."""
    _set_required_env(monkeypatch, session_secret=None)
    with pytest.raises(RuntimeError) as ei:
        create_app()
    msg = str(ei.value)
    assert "HONE_SESSION_SECRET" in msg
    assert "openssl rand -hex 32" in msg   # tells them how to generate one


def test_create_app_refuses_to_start_with_an_empty_session_secret(monkeypatch):
    """An empty string is the same failure mode as unset — don't accept it."""
    _set_required_env(monkeypatch, session_secret="")
    with pytest.raises(RuntimeError, match="HONE_SESSION_SECRET"):
        create_app()


def test_create_app_succeeds_with_a_session_secret(monkeypatch):
    """With a real secret set, app construction completes (the SessionMiddleware
       and routers are wired). Lifespan still has to run for the DB to come
       up — we don't exercise that here, just that create_app() doesn't raise."""
    _set_required_env(monkeypatch, session_secret="x" * 64)
    app = create_app()
    assert app.title == "hone-core"


# --- session_cookie_secure parse + wiring --------------------------------

def test_config_session_cookie_secure_defaults_to_true(monkeypatch):
    """Secure-by-default: the session cookie's Secure flag is on unless the
       operator explicitly opts out via HONE_SESSION_COOKIE_SECURE=false."""
    from core.config import Config
    _set_required_env(monkeypatch, session_secret="x" * 64)
    monkeypatch.delenv("HONE_SESSION_COOKIE_SECURE", raising=False)
    assert Config.from_env().session_cookie_secure is True


@pytest.mark.parametrize("env, expected", [
    ("true",  True),  ("True",  True),  ("1",   True),
    ("yes",   True),  ("on",    True),
    ("false", False), ("False", False), ("0",   False),
    ("no",    False), ("off",   False), ("",    False),
    ("anything-else", False),
])
def test_config_session_cookie_secure_parses_env_bool(monkeypatch, env, expected):
    from core.config import Config
    _set_required_env(monkeypatch, session_secret="x" * 64)
    monkeypatch.setenv("HONE_SESSION_COOKIE_SECURE", env)
    assert Config.from_env().session_cookie_secure is expected


def test_create_app_passes_session_cookie_secure_to_session_middleware(monkeypatch):
    """The Config flag actually drives the SessionMiddleware's https_only —
       not just stored on the Config dataclass and ignored on the way through."""
    from starlette.middleware.sessions import SessionMiddleware
    _set_required_env(monkeypatch, session_secret="x" * 64)

    monkeypatch.setenv("HONE_SESSION_COOKIE_SECURE", "true")
    app = create_app()
    sm = next(m for m in app.user_middleware if m.cls is SessionMiddleware)
    assert sm.kwargs["https_only"] is True

    monkeypatch.setenv("HONE_SESSION_COOKIE_SECURE", "false")
    app = create_app()
    sm = next(m for m in app.user_middleware if m.cls is SessionMiddleware)
    assert sm.kwargs["https_only"] is False


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
