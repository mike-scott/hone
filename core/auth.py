"""Operator UI authentication helpers.

Session management (signed cookie via Starlette SessionMiddleware), password
hashing (Argon2id), and Google OAuth 2.0 authorization-code flow.

The config-token admin (HONE_ADMIN_TOKEN) never has a database row — their
session carries is_config_admin=True and id=None.  All other users are in the
`users` table and must be in state='approved' to hold an active session.
"""
import collections
import secrets
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from core import core_db

GOOGLE_AUTH_URL    = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL   = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_SESSION_KEY = "ui_user"


@dataclass
class SessionUser:
    id: Optional[int]   # None for the config-token admin
    email: str
    display_name: str
    is_config_admin: bool


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_session_user(request: Request) -> Optional[SessionUser]:
    data = request.session.get(_SESSION_KEY)
    if not data:
        return None
    try:
        return SessionUser(**data)
    except (TypeError, KeyError):
        return None


def set_session_user(request: Request, user: SessionUser):
    request.session[_SESSION_KEY] = asdict(user)


def clear_session(request: Request):
    request.session.pop(_SESSION_KEY, None)


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    from argon2 import PasswordHasher
    return PasswordHasher().hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
    try:
        return PasswordHasher().verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------------------------
# Failed-attempt rate limiter (login brute-force / CPU-DoS defence)
# ---------------------------------------------------------------------------

# Defaults — 10 failed logins inside any 60-second sliding window locks the
# source IP out until enough timestamps age out. Argon2 verify is ~50-100ms,
# so even before the limiter trips an attacker is throttled by hash cost;
# the limiter caps the wasted CPU and forces them to slow down.
LOGIN_MAX_FAILURES = 10
LOGIN_FAILURE_WINDOW_SECONDS = 60


class FailedAttemptLimiter:
    """Sliding-window failed-attempt limiter, keyed by an arbitrary string
       (login uses the client IP). record_failure() stamps `now`; is_locked()
       reports True once `max_failures` stamps sit inside `window_seconds`,
       and unlocks naturally as older stamps age out. Successful attempts
       record nothing — legitimate users don't throttle themselves.

       In-memory, intra-process — fine for hone-core's single-worker shape.
       Thread-safe so a sync handler called from uvicorn's worker thread
       pool can hit it concurrently with the event loop. The clock is
       injectable (`now=` ctor arg) so tests can advance it deterministically
       instead of sleeping."""

    def __init__(self, *, max_failures: int, window_seconds: float,
                 now=time.monotonic):
        if max_failures < 1:
            raise ValueError("max_failures must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.max = max_failures
        self.window = window_seconds
        self._now = now
        self._stamps: dict = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def _prune(self, key, now):
        dq = self._stamps.get(key)
        if not dq:
            return
        cutoff = now - self.window
        while dq and dq[0] < cutoff:
            dq.popleft()
        if not dq:
            del self._stamps[key]

    def is_locked(self, key) -> bool:
        now = self._now()
        with self._lock:
            self._prune(key, now)
            return len(self._stamps.get(key, ())) >= self.max

    def record_failure(self, key) -> None:
        now = self._now()
        with self._lock:
            self._prune(key, now)
            self._stamps[key].append(now)


_DUMMY_HASH: Optional[str] = None


def dummy_password_hash() -> str:
    """A pre-computed Argon2 hash used as the verify target when an email
       lookup misses — pays the same CPU/wall time as a real `verify_password`
       so the response timing of a failed login is independent of whether the
       email is registered. Computed lazily on first use and cached."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("placeholder-never-matches")
    return _DUMMY_HASH


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def _redirect_to_login(request: Request) -> HTTPException:
    next_url = urllib.parse.quote(str(request.url), safe="")
    return HTTPException(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": f"/login?next={next_url}"})


def require_session(request: Request) -> SessionUser:
    """Per-request session check, re-validated against the DB so revoke /
       delete / un-approve takes effect on the user's NEXT request instead
       of waiting for their cookie to expire. The config-token admin has no
       DB row (id is None) and bypasses the lookup — that identity is
       controlled by HONE_ADMIN_TOKEN only."""
    user = get_session_user(request)
    if user is None:
        raise _redirect_to_login(request)
    if user.is_config_admin:
        return user
    row = core_db.get_user_by_id(request.app.state.db, user.id)
    if row is None or row["state"] != "approved":
        # Tear down the stale cookie so the next request prompts a fresh
        # login instead of looping through the same revoked identity.
        clear_session(request)
        raise _redirect_to_login(request)
    return user


def require_config_admin(
        user: SessionUser = Depends(require_session)) -> SessionUser:
    if not user.is_config_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="admin access required")
    return user


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

_CSRF_KEY = "csrf_token"


def csrf_token(request: Request) -> str:
    """The per-session CSRF token, lazily generated on first access. Stored in
       request.session so the signed cookie binds it to this client; the token
       itself is then echoed back on state-changing forms (or HTMX requests)
       and matched in require_csrf.

       Rotating per-session is sufficient: the session cookie is itsdangerous-
       signed, so an attacker can't forge a token paired with a victim's
       session. SameSite=lax already blocks most cross-site form posts; the
       token closes the remaining gap (browser quirks, embedded contexts).

       Returns "" when SessionMiddleware isn't installed — keeps template
       rendering working in handler-focused tests that intentionally skip the
       middleware (CSRF enforcement itself is covered against the real
       dependency in tests/test_csrf.py)."""
    try:
        session = request.session
    except (AssertionError, AttributeError):
        return ""
    tok = session.get(_CSRF_KEY)
    if not tok:
        tok = secrets.token_urlsafe(32)
        session[_CSRF_KEY] = tok
    return tok


def csrf_field(request: Request):
    """Jinja global: render the hidden CSRF input for a <form>. The value is
       safe by construction (token_urlsafe) but we mark it explicitly so it
       isn't double-escaped if a future template wraps it in Markup."""
    from markupsafe import Markup
    return Markup(
        f'<input type="hidden" name="csrf_token" value="{csrf_token(request)}">')


async def require_csrf(request: Request):
    """Dependency: gate a state-changing route on a valid CSRF token. The token
       can ride in the `csrf_token` form field (standard <form> POSTs) OR an
       `X-CSRF-Token` header (HTMX / JSON requests). 403 on miss or mismatch.

       Multipart parsing is cached by Starlette, so the route handler's own
       `await request.form()` returns the same parsed dict — no double-read
       cost."""
    expected = request.session.get(_CSRF_KEY) or ""
    header = request.headers.get("X-CSRF-Token", "")
    submitted = header
    if not submitted:
        try:
            form = await request.form()
            submitted = str(form.get("csrf_token") or "")
        except Exception:
            submitted = ""
    if not expected or not submitted or not secrets.compare_digest(
            submitted, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="CSRF token missing or invalid")


# ---------------------------------------------------------------------------
# Google OAuth helpers
# ---------------------------------------------------------------------------

def google_auth_url(config, redirect_uri: str, state: str) -> str:
    params = {
        "client_id":     config.google_client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "online",
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


async def google_exchange_code(config, code: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     config.google_client_id,
            "client_secret": config.google_client_secret,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        })
        r.raise_for_status()
        return r.json()


async def google_fetch_userinfo(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json()   # {sub, email, name, ...}
