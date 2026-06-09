"""Operator UI authentication helpers.

Session management (signed cookie via Starlette SessionMiddleware), password
hashing (Argon2id), and Google OAuth 2.0 authorization-code flow.

The config-token admin (HONE_ADMIN_TOKEN) never has a database row — their
session carries is_config_admin=True and id=None.  All other users are in the
`users` table and must be in state='approved' to hold an active session.
"""
import secrets
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
