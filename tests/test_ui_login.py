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
    # Generous limiter so the existing happy-path tests don't trip; the
    # rate-limit-specific tests below use a tight instance directly.
    app.state.login_limiter = auth.FailedAttemptLimiter(
        max_failures=100, window_seconds=60)
    return SimpleNamespace(
        client=TestClient(app, follow_redirects=False),
        app=app,                               # for per-test limiter overrides
        db=db,
        shared_hash=_shared_hash)


def _plant_user(ctx, *, email="alice@example.com", state="approved",
                display_name="Alice"):
    uid = core_db.create_user(ctx.db, email, display_name, "local",
                              password_hash=ctx.shared_hash)
    if state != "pending":
        core_db.set_user_state(ctx.db, uid, state)
    return uid


import re as _re
_CSRF_RE = _re.compile(r'name="csrf_token" value="([^"]+)"')
_CSRF_META_RE = _re.compile(r'<meta name="csrf-token" content="([^"]+)"')


def _csrf(ctx, path):
    """Fetch the real CSRF token by rendering the page (the GET seeds the
       session cookie + hidden field), the way a browser does. If the page
       redirects — e.g. an already-logged-in user GET-ing /login now skips
       the form — fall back to the home page's <meta name="csrf-token">,
       which carries the same per-session token."""
    r = ctx.client.get(path)
    m = _CSRF_RE.search(r.text)
    if m:
        return m.group(1)
    r = ctx.client.get("/")
    m = _CSRF_META_RE.search(r.text)
    assert m, f"no csrf_token found on GET {path} (and / didn't render either)"
    return m.group(1)


def _post_login(ctx, *, email, password):
    return ctx.client.post("/login", data={
        "email": email, "password": password, "next": "/",
        "csrf_token": _csrf(ctx, "/login")})


def _post_register(ctx, *, email, password="correct-horse-battery-staple",
                   display_name="A New User"):
    return ctx.client.post("/register", data={
        "email": email, "password": password, "display_name": display_name,
        "csrf_token": _csrf(ctx, "/register")})


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


@pytest.mark.parametrize("bad_email", [
    # Internal whitespace (strip only removes leading/trailing) — the old
    # `re.match` would have let these through because [^@] matches \n / \t.
    "foo\nbar@example.com",
    "foo\tbar@example.com",
    "foo bar@example.com",
    # Control characters / DEL — would otherwise flow into log lines,
    # mailer calls, or anywhere else that takes the address downstream.
    "alice\x00@example.com",
    "alice@example.\x7fcom",
    "alice@exa\x0dmple.com",
    # Trailing junk after a valid-looking prefix — fullmatch catches it
    # where re.match (unanchored at end) would have passed it.
    "alice@example.comJUNK\nEXTRA",
])
def test_register_rejects_emails_with_control_chars_or_trailing_junk(
        ctx, bad_email):
    r = _post_register(ctx, email=bad_email, password="x" * 12)
    assert r.status_code == 422
    assert "valid email" in r.text.lower()


@pytest.mark.parametrize("good_email", [
    "alice@example.com",
    "bob.smith@example.co.uk",
    "carol+tag@example.com",
    "dave-99@sub.example.org",
])
def test_register_accepts_ordinary_email_shapes(ctx, good_email):
    r = _post_register(ctx, email=good_email, password="x" * 12)
    assert r.status_code == 200                  # the "submitted" success page


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


# --- GET /login: already-logged-in shortcut ----------------------------------

def test_login_page_redirects_when_already_authenticated(ctx):
    """A logged-in user landing on /login skips the form — they're already
       in. Default redirect target is '/'."""
    _plant_user(ctx, email="alice@example.com", state="approved")
    # Log in once.
    r = _post_login(ctx, email="alice@example.com",
                    password="correct-horse-battery-staple")
    assert r.status_code == 303
    # Second GET /login should now redirect (the user is authenticated).
    r = ctx.client.get("/login")
    assert r.status_code == 303
    assert r.headers["Location"] == "/"


def test_login_page_redirect_honours_next_param(ctx):
    """The opener-passed `?next=…` URL drives where the redirect lands, so
       a deep-link in a logged-in session doesn't lose its destination."""
    _plant_user(ctx, email="alice@example.com", state="approved")
    _post_login(ctx, email="alice@example.com",
                password="correct-horse-battery-staple")
    r = ctx.client.get("/login?next=/settings")
    assert r.status_code == 303
    assert r.headers["Location"] == "/settings"


def test_login_page_redirect_rejects_offsite_next(ctx):
    """The shared _safe_next guard still applies — an absolute / protocol-
       relative URL is dropped to '/' so the shortcut isn't an open redirect."""
    _plant_user(ctx, email="alice@example.com", state="approved")
    _post_login(ctx, email="alice@example.com",
                password="correct-horse-battery-staple")
    r = ctx.client.get("/login?next=https://attacker.example/")
    assert r.status_code == 303
    assert r.headers["Location"] == "/"


def test_login_page_renders_form_when_session_is_stale(ctx):
    """A cookie whose user is no longer approved must NOT redirect — the
       current_session_user re-check clears the stale cookie and lets the
       form render, so the user can log in afresh."""
    _plant_user(ctx, email="alice@example.com", state="approved")
    _post_login(ctx, email="alice@example.com",
                password="correct-horse-battery-staple")
    # Admin revokes them.
    user = core_db.get_user_by_email(ctx.db, "alice@example.com")
    core_db.set_user_state(ctx.db, user["id"], "revoked")
    r = ctx.client.get("/login")
    assert r.status_code == 200
    assert _CSRF_RE.search(r.text) is not None     # the form is back


def test_login_page_redirect_works_for_config_admin(ctx):
    """The HONE_ADMIN_TOKEN-as-password path also short-circuits — config
       admins are id=None and bypass the DB re-check."""
    r = _post_login(ctx, email="x@y", password=_ADMIN_TOKEN)
    assert r.status_code == 303
    r = ctx.client.get("/login")
    assert r.status_code == 303
    assert r.headers["Location"] == "/"


# --- per-IP failed-login throttle ---------------------------------------------

def _tight_limiter(ctx, *, max_failures=2, window_seconds=60):
    ctx.app.state.login_limiter = auth.FailedAttemptLimiter(
        max_failures=max_failures, window_seconds=window_seconds)


def test_login_returns_429_after_too_many_failed_attempts(ctx):
    _tight_limiter(ctx, max_failures=2)
    _plant_user(ctx, email="alice@example.com", state="approved")
    # Two credential misses → still 401; the third gets a generic 429.
    for _ in range(2):
        r = _post_login(ctx, email="alice@example.com", password="wrong")
        assert r.status_code == 401
    r = _post_login(ctx, email="alice@example.com", password="wrong")
    assert r.status_code == 429
    assert "Too many login attempts" in r.text


def test_login_throttle_blocks_correct_credentials_too(ctx):
    """Once an IP is locked, ALL further attempts return 429 — even ones
       carrying valid credentials. The lockout doesn't peek at the password,
       so an attacker can't tell from the response which guesses would have
       succeeded."""
    _tight_limiter(ctx, max_failures=2)
    _plant_user(ctx, email="alice@example.com", state="approved")
    # Burn the failure budget.
    for _ in range(2):
        _post_login(ctx, email="alice@example.com", password="wrong")
    r = _post_login(ctx, email="alice@example.com",
                    password="correct-horse-battery-staple")
    assert r.status_code == 429
    # The valid attempt did not log the user in either:
    assert "hone_session" not in r.headers.get("set-cookie", "")


def test_login_success_does_not_count_as_a_failure(ctx):
    """A legitimate user who logs in normally doesn't burn down their own
       budget — the limiter only counts the credential-miss path."""
    _tight_limiter(ctx, max_failures=2)
    _plant_user(ctx, email="alice@example.com", state="approved")
    # 5 successes well past the failure threshold; never trips.
    for _ in range(5):
        r = _post_login(ctx, email="alice@example.com",
                        password="correct-horse-battery-staple")
        assert r.status_code == 303


def test_login_pending_or_revoked_with_valid_password_does_not_count(ctx):
    """A pending or revoked user supplying the right password is past the
       credential check — those 403 responses must NOT consume the IP's
       failure budget, otherwise an attacker who knew a pending user's
       password could DoS that IP's ability to log in elsewhere."""
    _tight_limiter(ctx, max_failures=2)
    _plant_user(ctx, email="pending@example.com", state="pending")
    # 5 pending-403s — well past the limit, but the limiter shouldn't notice.
    for _ in range(5):
        r = _post_login(ctx, email="pending@example.com",
                        password="correct-horse-battery-staple")
        assert r.status_code == 403
    # The next credential MISS should still get a fresh 401, not 429.
    r = _post_login(ctx, email="someone-else@example.com", password="wrong")
    assert r.status_code == 401
