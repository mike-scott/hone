"""Tests for the operator UI login + registration flows — specifically the
no-enumeration / no-timing-leak posture: the failed-login path must look
identical whether the email is registered or not, account-state messages
(pending / revoked) appear only after a *valid* password (so they don't
leak), and self-registration returns the same body whether the email is
new or already taken."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from core import auth, core_db, ui

_SESSION_SECRET = "test-session-secret-32-bytes-or-more-padding"
_ADMIN_TOKEN    = "admin-token-for-tests"


@pytest.fixture(scope="module")
def _shared_hash():
    """Argon2 hash a known password once per module — keeps the tests fast
       (each hash call is ~50ms)."""
    return auth.hash_password("correct-horse-battery-staple")


@pytest.fixture
def ctx(tmp_path, _shared_hash):
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    # Real SessionMiddleware so the success-path redirect can write a cookie
    # (the no-enumeration tests exercise the actual login_submit handler).
    app.add_middleware(SessionMiddleware, secret_key=_SESSION_SECRET,
                        session_cookie="hone_session")
    app.include_router(ui.router)
    app.state.db = db
    app.state.config = SimpleNamespace(
        admin_token=_ADMIN_TOKEN,
        google_client_id="",            # Google SSO disabled for these tests
        google_client_secret="")
    return SimpleNamespace(
        client=TestClient(app, follow_redirects=False),
        db=db,
        shared_hash=_shared_hash)


def _plant_user(ctx, *, email="alice@example.com", state="approved",
                display_name="Alice"):
    uid = core_db.create_user(ctx.db, email, display_name, "local",
                              password_hash=ctx.shared_hash)
    if state != "pending":
        core_db.set_user_state(ctx.db, uid, state)
    return uid


def _post_login(ctx, *, email, password):
    return ctx.client.post(
        "/login", data={"email": email, "password": password, "next": "/"})


def _post_register(ctx, *, email, password="correct-horse-battery-staple",
                   display_name="A New User"):
    return ctx.client.post("/register", data={
        "email": email, "password": password, "display_name": display_name})


_GENERIC = "Invalid email or password."


# --- login: no account enumeration via response body / status -------------

def test_login_unknown_email_returns_generic_401(ctx):
    r = _post_login(ctx, email="nobody@example.com", password="anything")
    assert r.status_code == 401
    assert _GENERIC in r.text


def test_login_wrong_password_returns_the_same_generic_401(ctx):
    """An attacker probing emails must not be able to distinguish 'no such
       user' from 'user exists, wrong password' via the response."""
    _plant_user(ctx, email="alice@example.com", state="approved")
    r = _post_login(ctx, email="alice@example.com", password="wrong")
    assert r.status_code == 401
    assert _GENERIC in r.text


def test_login_pending_user_with_wrong_password_returns_generic_401(ctx):
    """The 'awaiting approval' message is post-authentication — wrong
       password yields the same generic 401 a stranger gets."""
    _plant_user(ctx, email="pending@example.com", state="pending")
    r = _post_login(ctx, email="pending@example.com", password="wrong")
    assert r.status_code == 401
    assert _GENERIC in r.text
    assert "awaiting" not in r.text.lower()       # no state leak


def test_login_revoked_user_with_wrong_password_returns_generic_401(ctx):
    _plant_user(ctx, email="revoked@example.com", state="revoked")
    r = _post_login(ctx, email="revoked@example.com", password="wrong")
    assert r.status_code == 401
    assert _GENERIC in r.text
    assert "revoked" not in r.text.lower()        # no state leak


# --- login: state messages only after a valid password -------------------

def test_login_pending_user_with_correct_password_reveals_pending(ctx):
    """Once credentials are valid the user *is* authenticated; surfacing
       'awaiting approval' here is informative, not a leak (the attacker
       would need to know the user's password to elicit it)."""
    _plant_user(ctx, email="pending@example.com", state="pending")
    r = _post_login(ctx, email="pending@example.com",
                    password="correct-horse-battery-staple")
    assert r.status_code == 403
    assert "awaiting admin approval" in r.text.lower()


def test_login_revoked_user_with_correct_password_reveals_revoked(ctx):
    _plant_user(ctx, email="revoked@example.com", state="revoked")
    r = _post_login(ctx, email="revoked@example.com",
                    password="correct-horse-battery-staple")
    assert r.status_code == 403
    assert "revoked" in r.text.lower()


def test_login_approved_user_with_correct_password_logs_in(ctx):
    _plant_user(ctx, email="alice@example.com", state="approved")
    r = _post_login(ctx, email="alice@example.com",
                    password="correct-horse-battery-staple")
    assert r.status_code == 303
    assert r.headers["Location"] == "/"
    assert "hone_session" in r.headers.get("set-cookie", "")


# --- register: success page rendered whether or not the email exists -----

def test_register_new_email_creates_pending_user(ctx):
    r = _post_register(ctx, email="newcomer@example.com")
    assert r.status_code == 200
    row = core_db.get_user_by_email(ctx.db, "newcomer@example.com")
    assert row is not None and row["state"] == "pending"


def test_register_existing_email_renders_the_same_success_page(ctx):
    """If the email is already taken we silently no-op and render the same
       success page — body + status indistinguishable from a fresh signup,
       so the endpoint doesn't leak which addresses have accounts."""
    _plant_user(ctx, email="taken@example.com", state="approved")
    original = core_db.get_user_by_email(ctx.db, "taken@example.com")

    new_attempt = _post_register(ctx, email="taken@example.com",
                                 password="different-password-entirely")
    fresh = _post_register(ctx, email="freshly-new@example.com")

    # Identical body + status: no signal which one already existed.
    assert new_attempt.status_code == 200 == fresh.status_code
    assert new_attempt.text == fresh.text

    # And the existing user's password_hash is NOT overwritten.
    after = core_db.get_user_by_email(ctx.db, "taken@example.com")
    assert after["password_hash"] == original["password_hash"]
    assert after["state"] == "approved"


def test_register_keeps_input_validation_errors(ctx):
    """The no-enumeration cleanup must not paper over legitimate input
       validation — a malformed email or a too-short password is still
       reported normally, since neither reveals account existence."""
    bad_email = _post_register(ctx, email="not-an-email", password="x" * 12)
    assert bad_email.status_code == 422
    assert "valid email" in bad_email.text.lower()

    short_pw = _post_register(ctx, email="ok@example.com", password="short")
    assert short_pw.status_code == 422
    assert "at least 10" in short_pw.text


# --- dummy hash + admin path unaffected ----------------------------------

def test_dummy_password_hash_is_a_valid_argon2_hash_and_cached():
    h1 = auth.dummy_password_hash()
    h2 = auth.dummy_password_hash()
    assert h1 == h2                                 # cached singleton
    assert h1.startswith("$argon2")                 # real hash, not a sentinel
    assert auth.verify_password("placeholder-never-matches", h1) is True
    assert auth.verify_password("anything-else", h1) is False


def test_admin_token_login_still_works_with_any_email(ctx):
    """The HONE_ADMIN_TOKEN-as-password path is unchanged: a constant-time
       compare against the token, with the email field ignored."""
    r = _post_login(ctx, email="literally-anything@x", password=_ADMIN_TOKEN)
    assert r.status_code == 303
    assert "hone_session" in r.headers.get("set-cookie", "")
