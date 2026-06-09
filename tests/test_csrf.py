"""Tests for the CSRF gate on state-changing UI routes. Handler-focused tests
elsewhere bypass require_csrf via fake_admin_session — this file pins it
exercised against the real dependency, on a real app with SessionMiddleware,
so the gate itself can't silently rot."""
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from core import auth, core_db, ui

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture
def ctx(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.add_middleware(SessionMiddleware,
                       secret_key="test-secret-32-bytes-minimum-padding",
                       session_cookie="hone_session")
    app.include_router(ui.router)
    app.state.db = db
    app.state.config = SimpleNamespace(
        admin_token="adm", google_client_id="", google_client_secret="")
    app.state.login_limiter = auth.FailedAttemptLimiter(
        max_failures=100, window_seconds=60)
    return SimpleNamespace(
        client=TestClient(app, follow_redirects=False), db=db)


def _csrf(ctx, path):
    """GET the page to seed the session cookie and pluck the rendered token."""
    r = ctx.client.get(path)
    m = _CSRF_RE.search(r.text)
    assert m, f"no csrf_token found on GET {path}"
    return m.group(1)


# --- the gate itself ----------------------------------------------------------

def test_post_without_token_is_rejected(ctx):
    """A POST with no csrf_token field and no X-CSRF-Token header is denied —
       it doesn't even reach the route handler. 403, not 401."""
    ctx.client.get("/login")                          # seed the session
    r = ctx.client.post("/login", data={"email": "x@y", "password": "z"})
    assert r.status_code == 403
    assert "CSRF" in r.text


def test_post_with_wrong_token_is_rejected(ctx):
    ctx.client.get("/login")                          # seed the session
    r = ctx.client.post("/login", data={
        "email": "x@y", "password": "z", "csrf_token": "obviously-wrong"})
    assert r.status_code == 403


def test_post_with_token_in_header_is_accepted(ctx):
    """HTMX requests don't carry the token in the form body — they emit
       X-CSRF-Token via the base.html hook. The dependency must accept it."""
    token = _csrf(ctx, "/login")
    r = ctx.client.post("/login",
                         data={"email": "x@y", "password": "wrong"},
                         headers={"X-CSRF-Token": token})
    # Past the CSRF gate: lands on the credential-miss path (401), NOT 403.
    assert r.status_code == 401


def test_token_is_stable_across_requests_in_one_session(ctx):
    """The token persists per-session (so a form rendered on page A still
       validates on page B's submission), and the response cookie isn't
       a different token per render."""
    t1 = _csrf(ctx, "/login")
    t2 = _csrf(ctx, "/login")
    assert t1 == t2


def test_register_post_also_requires_a_token(ctx):
    """All state-changing UI POSTs are gated — register included, even though
       it's reachable without a prior login."""
    ctx.client.get("/register")                       # seed the session
    r = ctx.client.post("/register", data={
        "email": "new@example.com", "password": "x" * 12,
        "display_name": "X"})
    assert r.status_code == 403


def test_post_without_any_session_at_all_is_rejected(ctx):
    """Skipping the page render entirely (no session cookie) means there's no
       server-side token to compare to — request fails closed."""
    r = ctx.client.post("/login", data={
        "email": "x@y", "password": "z",
        "csrf_token": "some-random-value-the-attacker-guesses"})
    assert r.status_code == 403


# --- template integration -----------------------------------------------------

def test_login_page_renders_csrf_meta_and_hidden_field(ctx):
    """The base layout exposes the per-session token via <meta>, and every
       <form method=post> carries it as a hidden field. Sanity that the
       wiring reaches the rendered HTML, not just the python helpers."""
    body = ctx.client.get("/login").text
    # standalone page (no base.html) — just the hidden field
    assert _CSRF_RE.search(body) is not None


def test_authenticated_page_exposes_csrf_token_as_meta(tmp_path,
                                                        fake_admin_session):
    """An authenticated page (which extends base.html) exposes the per-session
       token via <meta name="csrf-token"> so the HTMX request hook can pick
       it up. We use fake_admin_session here because we're verifying the
       *template wiring*, not the gate."""
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.add_middleware(SessionMiddleware,
                       secret_key="x" * 32, session_cookie="hone_session")
    app.include_router(ui.router)
    app.state.db = db
    app.state.config = SimpleNamespace(
        admin_token="adm", google_client_id="", google_client_secret="")
    app.state.login_limiter = auth.FailedAttemptLimiter(
        max_failures=100, window_seconds=60)
    fake_admin_session(app)
    client = TestClient(app, follow_redirects=False)
    body = client.get("/").text
    assert re.search(r'<meta name="csrf-token" content="[^"]+"', body) \
        is not None
