"""hone-core — the operator web UI (see ../ARCHITECTURE.md → Operator web UI).

Server-rendered: Jinja2 + Bootstrap 5 + HTMX. Pages:
- `/login`              email+password or Google SSO login
- `/register`           self-service account registration (admin-approved)
- `/`                   the work queue (prepare + review + train items)
- `/patchsets/{root}`   per-patchset detail (corpus + reviews + queue history)
- `/nodes`              the node fleet + pending enrollments
- `/enroll`             approve a node's device-grant enrollment
- `/site-settings`           operator-tunable runtime config + list-tag gather filter
- `/users`              user management (config-token admin only)
"""
import datetime
import html
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import quote, unquote
from markupsafe import Markup

log = logging.getLogger("hone.ui")

from fastapi import (APIRouter, Depends, File, HTTPException, Request, Response,
                      UploadFile, status)
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from common.version import __version__ as VERSION
from core import (auth, core_db, gather, methodology_format, patchview,
                  reports, runtime_config, upload)

_HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))
templates.env.globals["version"] = VERSION   # rendered in the base footer


def _asset_v(filename):
    """Cache-busting token for a /static asset: its mtime. StaticFiles serves
       no Cache-Control, so browsers heuristically reuse a cached copy — append
       `?v={{ asset_v('app.css') }}` so a changed file always reloads."""
    try:
        return int(os.path.getmtime(os.path.join(_HERE, "static", filename)))
    except OSError:
        return VERSION


templates.env.globals["asset_v"] = _asset_v


_TICKED = re.compile(r"`([^`\n]+)`")


def _tickcode(text):
    """Render concern prose: HTML-escape the (untrusted) model text, then turn
       Markdown-style `…` spans into inline <code> with the backticks dropped.
       Escaping first means the wrapped run is already safe."""
    return Markup(_TICKED.sub(r"<code>\1</code>", html.escape(text or "")))


templates.env.filters["tickcode"] = _tickcode
# Templates render the hidden CSRF input via `{{ csrf_field(request) }}` on
# every state-changing form; the same token is also exposed in base.html as
# a <meta> tag for the HTMX request hook (X-CSRF-Token).
templates.env.globals["csrf_field"] = auth.csrf_field
templates.env.globals["csrf_token"] = auth.csrf_token

router = APIRouter(tags=["ui"])


# ===========================================================================
# Auth routes — login, register, Google SSO, logout (no session required)
# ===========================================================================

def _safe_next(next_url: str | None) -> str:
    """Return next_url if it looks like a safe local path, else '/'."""
    if not next_url:
        return "/"
    decoded = unquote(next_url)
    if decoded.startswith("/") and not decoded.startswith("//"):
        return decoded
    return "/"


def _notify_access_request(db, new_user_id, email):
    """Notify admins that a new account is awaiting approval. Best-effort — a
       notification failure must never break signup."""
    try:
        core_db.notify_admins(
            db, type=core_db.NOTIF_TYPE_USER_ACCESS,
            dedup_key=f"user_access:{new_user_id}",
            title=f"Access request: {email}",
            link=f"/users#user-{new_user_id}")
    except Exception:
        log.warning("access-request notification failed (non-fatal)",
                    exc_info=True)


def _google_redirect_uri(cfg) -> str:
    """The Google OAuth callback URL — derived from cfg.public_url, NOT
       request.base_url. The latter is built from the incoming Host header,
       and behind a proxy with a slack (or unset) X-Forwarded-Host policy an
       attacker who can spoof Host could redirect the authorization `code`
       to a domain they control. Google requires the URI in the authorization
       step to match the one in the token exchange, so both flows route
       through this single source."""
    return cfg.public_url.rstrip("/") + "/auth/google/callback"


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str | None = None,
                     error: str | None = None):
    cfg = request.app.state.config
    # Already-logged-in shortcut: a user with a still-valid session
    # (current_session_user re-validates against the DB, so a revoked cookie
    # is treated as no session at all) skips the form and proceeds where
    # they were going.
    if auth.current_session_user(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "next":           next or "",
        "error":          error or "",
        "google_enabled": bool(cfg.google_client_id),
    })


@router.post("/login", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def login_submit(request: Request):
    cfg = request.app.state.config
    db  = request.app.state.db
    form = await request.form()
    email    = (form.get("email")    or "").strip().lower()
    password = (form.get("password") or "")
    next_url = _safe_next(form.get("next"))

    def _login_response(error, status_code):
        return templates.TemplateResponse(request, "login.html", {
            "next": next_url, "google_enabled": bool(cfg.google_client_id),
            "error": error,
        }, status_code=status_code)

    # Per-IP failed-attempt limiter: caps brute-force / CPU-DoS against the
    # Argon2 verify path. Keyed on the request's client host (uvicorn's
    # --proxy-headers populates this from X-Forwarded-For when configured).
    # A locked IP gets the same 429 regardless of credentials — even valid
    # ones — so an attacker can't tell which password attempts would have
    # succeeded.
    limiter = request.app.state.login_limiter
    client_ip = (request.client.host if request.client else "") or "unknown"
    if limiter.is_locked(client_ip):
        return _login_response(
            "Too many login attempts. Please try again later.", 429)

    # Config-token admin — constant-time comparison against HONE_ADMIN_TOKEN.
    if cfg.admin_token and secrets.compare_digest(password, cfg.admin_token):
        auth.set_session_user(request, auth.SessionUser(
            id=None, email=email or "admin",
            display_name="Admin", is_config_admin=True))
        return RedirectResponse(next_url, status_code=303)

    # Regular DB user. Always run an Argon2 verify (against a precomputed
    # dummy hash if the email lookup misses or the user has no password) so
    # the wall-time of a failed login is roughly independent of whether the
    # email is registered — no enumeration through response timing. State-
    # specific messages (pending / revoked) are only revealed AFTER a valid
    # password, so they don't leak: an attacker who can produce them already
    # has the user's credentials.
    user = core_db.get_user_by_email(db, email)
    hashed = (user["password_hash"] if user and user["password_hash"]
              else auth.dummy_password_hash())
    password_ok = auth.verify_password(password, hashed)

    if not (user and password_ok):
        # Count any credential miss — unknown email, wrong password, missing
        # password hash. State-conditional outcomes (pending / revoked,
        # below) don't count because they only happen post-valid-password.
        limiter.record_failure(client_ip)
        return _login_response("Invalid email or password.", 401)
    if user["state"] == "pending":
        return _login_response(
            "Your account is awaiting admin approval.", 403)
    if user["state"] == "revoked":
        return _login_response("Your account has been revoked.", 403)

    auth.set_session_user(request, auth.SessionUser(
        id=user["id"], email=user["email"],
        display_name=user["display_name"] or user["email"],
        is_config_admin=bool(user["is_admin"]),
        is_maintainer=bool(user["is_maintainer"])))
    core_db.touch_last_login(db, user["id"])
    return RedirectResponse(next_url, status_code=303)


@router.get("/register", response_class=HTMLResponse, include_in_schema=False)
async def register_page(request: Request):
    cfg = request.app.state.config
    return templates.TemplateResponse(request, "register.html", {
        "google_enabled": bool(cfg.google_client_id),
        "error": "", "success": False,
    })


@router.post("/register", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def register_submit(request: Request):
    cfg = request.app.state.config
    db  = request.app.state.db
    form = await request.form()
    email        = (form.get("email")        or "").strip().lower()
    display_name = (form.get("display_name") or "").strip()
    password     = (form.get("password")     or "")

    def _fail(msg):
        return templates.TemplateResponse(request, "register.html", {
            "google_enabled": bool(cfg.google_client_id),
            "error": msg, "success": False,
            "email": email, "display_name": display_name,
        }, status_code=422)

    # fullmatch (anchored both ends) so trailing junk after a valid-looking
    # prefix can't slip through, and a negated class that rejects whitespace
    # + every C0 control char + DEL so an attacker can't embed CR/LF or NUL
    # in a value that later flows into a header, log line, or outbound mailer.
    if not email or not re.fullmatch(
            r"[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+\.[^@\s\x00-\x1f\x7f]+",
            email):
        return _fail("Please enter a valid email address.")
    if len(password) < 10:
        return _fail("Password must be at least 10 characters.")

    # Always hash the password (so the response time is independent of whether
    # the email is registered) and always render the same success page (so
    # the body/status don't leak which addresses have accounts). If the email
    # is already taken we silently no-op rather than overwriting the existing
    # row; the legitimate owner is unaffected, and an attacker probing the
    # endpoint can't tell whether their submission landed.
    hashed = auth.hash_password(password)
    if not core_db.get_user_by_email(db, email):
        new_id = core_db.create_user(db, email, display_name or email, "local",
                                     password_hash=hashed)
        _notify_access_request(db, new_id, email)
    return templates.TemplateResponse(request, "register.html", {
        "google_enabled": bool(cfg.google_client_id),
        "error": "", "success": True,
    })


@router.get("/auth/google", include_in_schema=False)
async def google_sso_start(request: Request, next: str | None = None):
    cfg = request.app.state.config
    if not cfg.google_client_id:
        raise HTTPException(404)
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    request.session["oauth_next"]  = _safe_next(next)
    redirect_uri = _google_redirect_uri(cfg)
    return RedirectResponse(auth.google_auth_url(cfg, redirect_uri, state))


@router.get("/auth/google/callback", response_class=HTMLResponse,
            include_in_schema=False)
async def google_sso_callback(request: Request,
                               code: str | None = None,
                               state: str | None = None,
                               error: str | None = None):
    cfg = request.app.state.config
    if not cfg.google_client_id:
        raise HTTPException(404)
    if error:
        return RedirectResponse(f"/login?error={quote(error)}")

    expected_state = request.session.pop("oauth_state", None)
    next_url = request.session.pop("oauth_next", "/")
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        return RedirectResponse("/login?error=invalid+state")

    db = request.app.state.db
    try:
        redirect_uri = _google_redirect_uri(cfg)
        tokens   = await auth.google_exchange_code(cfg, code, redirect_uri)
        userinfo = await auth.google_fetch_userinfo(tokens["access_token"])
    except Exception:
        log.exception("Google OAuth exchange failed")
        return RedirectResponse("/login?error=google+auth+failed")

    google_sub = userinfo.get("sub")
    g_email    = (userinfo.get("email") or "").lower().strip()
    g_name     = userinfo.get("name") or g_email

    # Find or create user.
    user = (core_db.get_user_by_google_sub(db, google_sub)
            or core_db.get_user_by_email(db, g_email))
    if user is None:
        new_id = core_db.create_user(db, g_email, g_name, "google",
                                     google_sub=google_sub)
        _notify_access_request(db, new_id, g_email)
        return templates.TemplateResponse(request, "login.html", {
            "next": next_url, "google_enabled": True,
            "error": "Your account has been created and is awaiting admin approval.",
        })

    # Attach google_sub if this was a previously-local account signing in via Google.
    if user["google_sub"] is None:
        db.execute("UPDATE users SET google_sub=? WHERE id=?",
                   (google_sub, user["id"]))
        db.commit()
        user = core_db.get_user_by_id(db, user["id"])

    if user["state"] == "pending":
        return templates.TemplateResponse(request, "login.html", {
            "next": next_url, "google_enabled": True,
            "error": "Your account is awaiting admin approval.",
        })
    if user["state"] == "revoked":
        return templates.TemplateResponse(request, "login.html", {
            "next": next_url, "google_enabled": True,
            "error": "Your account has been revoked.",
        })

    auth.set_session_user(request, auth.SessionUser(
        id=user["id"], email=user["email"],
        display_name=user["display_name"] or user["email"],
        is_config_admin=bool(user["is_admin"]),
        is_maintainer=bool(user["is_maintainer"])))
    core_db.touch_last_login(db, user["id"])
    return RedirectResponse(next_url, status_code=303)


@router.get("/logout", include_in_schema=False)
async def logout(request: Request):
    auth.clear_session(request)
    return RedirectResponse("/login", status_code=303)


# ===========================================================================
# User management (admin only — the config-token admin or a granted account)
# ===========================================================================

@router.get("/users", response_class=HTMLResponse, include_in_schema=False)
async def users_list(request: Request,
                     user: auth.SessionUser = Depends(auth.require_config_admin)):
    db = request.app.state.db
    rows = core_db.list_users(db)
    pending  = [r for r in rows if r["state"] == "pending"]
    approved = [r for r in rows if r["state"] == "approved"]
    revoked  = [r for r in rows if r["state"] == "revoked"]
    return templates.TemplateResponse(request, "users.html", {
        "current_user": user,
        "pending": pending, "approved": approved, "revoked": revoked,
        "when": _when,
    })


@router.post("/users/{user_id}/approve", include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def user_approve(request: Request, user_id: int,
                       user: auth.SessionUser = Depends(auth.require_config_admin)):
    core_db.set_user_state(request.app.state.db, user_id, "approved")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/revoke", include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def user_revoke(request: Request, user_id: int,
                      user: auth.SessionUser = Depends(auth.require_config_admin)):
    core_db.set_user_state(request.app.state.db, user_id, "revoked")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/grant-admin", include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def user_grant_admin(request: Request, user_id: int,
                           user: auth.SessionUser = Depends(auth.require_config_admin)):
    """Grant the admin permission to an account. Takes effect on the
       target's next request — auth.current_session_user re-derives the
       flag from the users row."""
    core_db.set_user_admin(request.app.state.db, user_id, True)
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/revoke-admin", include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def user_revoke_admin(request: Request, user_id: int,
                            user: auth.SessionUser = Depends(auth.require_config_admin)):
    """Remove the admin permission. Self-demotion is allowed — the
       config-token admin (HONE_ADMIN_TOKEN, no users row) is the
       permanent backstop that can always re-grant."""
    core_db.set_user_admin(request.app.state.db, user_id, False)
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/grant-maintainer", include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def user_grant_maintainer(request: Request, user_id: int,
                                user: auth.SessionUser = Depends(auth.require_config_admin)):
    """Grant the maintainer permission — corpus browsing and selecting
       patchsets for review. Takes effect on the target's next request."""
    core_db.set_user_maintainer(request.app.state.db, user_id, True)
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/revoke-maintainer", include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def user_revoke_maintainer(request: Request, user_id: int,
                                 user: auth.SessionUser = Depends(auth.require_config_admin)):
    """Remove the maintainer permission."""
    core_db.set_user_maintainer(request.app.state.db, user_id, False)
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/delete", include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def user_delete(request: Request, user_id: int,
                      user: auth.SessionUser = Depends(auth.require_config_admin)):
    core_db.delete_user(request.app.state.db, user_id)
    return RedirectResponse("/users", status_code=303)


# ===========================================================================
# User settings (the logged-in account's own page — any session user)
# ===========================================================================

def _user_settings_ctx(current_user, db, **extra):
    """Render context for user_settings.html. `account` is the live users
       row (None for the config-token admin, who has no row — the template
       shows a notice instead of forms)."""
    row = (core_db.get_user_by_id(db, current_user.id)
           if current_user.id is not None else None)
    ctx = {"current_user":   current_user,
           "account":        row,
           "is_local":       bool(row and row["auth_provider"] == "local"),
           "when":           _when,
           "profile_error":  None, "profile_saved":  False,
           "password_error": None, "password_saved": False,
           "notif_prefs":    _notification_pref_rows(current_user, db),
           "notif_saved":    False}
    ctx.update(extra)
    return ctx


@router.get("/user-settings", response_class=HTMLResponse, include_in_schema=False)
async def user_settings(request: Request,
                        current_user: auth.SessionUser = Depends(auth.require_session)):
    """The logged-in account's own settings: display name for every
       DB-backed account, password change for local-provider accounts
       (Google accounts authenticate at Google — no hone-core password).
       Distinct from /site-settings, which configures hone-core itself
       and is admin-only."""
    saved = request.query_params.get("saved")
    return templates.TemplateResponse(
        request, "user_settings.html",
        _user_settings_ctx(current_user, request.app.state.db,
                           profile_saved=saved == "profile",
                           password_saved=saved == "password",
                           notif_saved=saved == "notifications"))


@router.post("/user-settings/profile", include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def user_settings_profile(request: Request,
                                current_user: auth.SessionUser = Depends(auth.require_session)):
    """Update the account's display name. The navbar reads the name from
       the session cookie, so refresh the cookie in place — no re-login."""
    if current_user.id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "the config-token admin has no user account")
    db = request.app.state.db
    form = await request.form()
    name = (form.get("display_name") or "").strip()
    if not name:
        return templates.TemplateResponse(
            request, "user_settings.html",
            _user_settings_ctx(current_user, db,
                               profile_error="Display name cannot be empty."),
            status_code=422)
    core_db.set_user_display_name(db, current_user.id, name)
    current_user.display_name = name
    auth.set_session_user(request, current_user)
    return RedirectResponse("/user-settings?saved=profile", status_code=303)


@router.post("/user-settings/password", include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def user_settings_password(request: Request,
                                 current_user: auth.SessionUser = Depends(auth.require_session)):
    """Change the account password — local-provider accounts only.
       Requires the current password (an unattended session can't be
       hijacked into a silent credential change) and mirrors the
       register rule for the new one (≥ 10 chars)."""
    if current_user.id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "the config-token admin has no user account")
    db = request.app.state.db
    row = core_db.get_user_by_id(db, current_user.id)
    if row is None or row["auth_provider"] != "local":
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "password sign-in is not enabled for this account")
    form = await request.form()

    def _fail(msg):
        return templates.TemplateResponse(
            request, "user_settings.html",
            _user_settings_ctx(current_user, db, password_error=msg),
            status_code=422)

    if not auth.verify_password(form.get("current_password") or "",
                                row["password_hash"] or ""):
        return _fail("Current password is incorrect.")
    new = form.get("new_password") or ""
    if len(new) < 10:
        return _fail("New password must be at least 10 characters.")
    if new != (form.get("confirm_password") or ""):
        return _fail("New passwords do not match.")
    core_db.set_user_password_hash(db, current_user.id,
                                   auth.hash_password(new))
    return RedirectResponse("/user-settings?saved=password", status_code=303)


# ===========================================================================
# Notifications  (per-user in-app feed; core_db migration v13)
# ===========================================================================

_NOTIF_DROPDOWN_LIMIT = 6
_NOTIF_PAGE_LIMIT = 100

# type -> Bootstrap icon for the feed item.
_NOTIF_ICONS = {
    core_db.NOTIF_TYPE_REVIEW_READY:     "bi-clipboard-check",
    core_db.NOTIF_TYPE_REVIEW_FAILED:    "bi-exclamation-triangle",
    core_db.NOTIF_TYPE_PREPARE_FAILED:   "bi-exclamation-triangle",
    core_db.NOTIF_TYPE_NEW_COMMENT:      "bi-chat-left-text",
    core_db.NOTIF_TYPE_PATCHSET_SKIPPED: "bi-slash-circle",
    core_db.NOTIF_TYPE_NODE_HEALTH:      "bi-hdd-network",
    core_db.NOTIF_TYPE_USER_ACCESS:      "bi-person-plus",
}

# slug -> (label, help text) for the User-settings opt-in/out section.
_NOTIF_PREF_LABELS = {
    "review_ready":        ("Review ready", "An AI review of one of your patchsets completed."),
    "review_failed":       ("Review failed", "A review couldn't run (the series didn't apply)."),
    "prepare_failed":      ("Prepare failed", "A patchset couldn't be characterised."),
    "new_comment":         ("New lore comment", "A new comment landed on a patchset you track."),
    "patchset_skipped":    ("Patchset skipped", "A patchset you track was filtered out of the corpus."),
    "node_health_alert":   ("Node health alerts", "A node you own raised a health alert."),
    "user_access_request": ("Access requests (admin)", "A new account is awaiting approval."),
}


def _notif_view(items):
    """Decorate notification rows for templates: icon + relative time."""
    out = []
    for n in items:
        d = dict(n)
        d["icon"] = _NOTIF_ICONS.get(n["type"], "bi-bell")
        d["when"] = _when(n["created_at"])
        out.append(d)
    return out


@router.get("/notifications/badge", response_class=HTMLResponse, include_in_schema=False)
async def notifications_badge(request: Request,
                             current_user: auth.SessionUser = Depends(auth.require_session)):
    """The self-refreshing unread-count badge inside the nav bell."""
    count = core_db.unread_notification_count(request.app.state.db,
                                              current_user.id)
    return templates.TemplateResponse(request, "_notifications_badge.html",
                                      {"unread": count})


@router.get("/notifications/dropdown", response_class=HTMLResponse, include_in_schema=False)
async def notifications_dropdown(request: Request,
                                current_user: auth.SessionUser = Depends(auth.require_session)):
    """The recent-unread panel loaded under the bell on open."""
    db = request.app.state.db
    items = core_db.list_notifications(db, current_user.id,
                                       limit=_NOTIF_DROPDOWN_LIMIT,
                                       unread_only=True)
    return templates.TemplateResponse(
        request, "_notifications_dropdown.html",
        {"items": _notif_view(items),
         "unread": core_db.unread_notification_count(db, current_user.id)})


@router.get("/notifications", response_class=HTMLResponse, include_in_schema=False)
async def notifications_page(request: Request,
                            current_user: auth.SessionUser = Depends(auth.require_session)):
    """The full notifications feed (read + unread)."""
    db = request.app.state.db
    items = core_db.list_notifications(db, current_user.id,
                                       limit=_NOTIF_PAGE_LIMIT)
    return templates.TemplateResponse(
        request, "notifications.html",
        {"current_user": current_user, "items": _notif_view(items)})


@router.get("/notifications/{notif_id}/click", include_in_schema=False)
async def notification_click(request: Request, notif_id: int,
                            current_user: auth.SessionUser = Depends(auth.require_session)):
    """Click-through: mark the notification read, then redirect to its STORED
       link (server-side target → no open-redirect). 404 if it isn't the
       caller's. A plain GET so a notification is just a link."""
    db = request.app.state.db
    n = core_db.get_notification(db, current_user.id, notif_id)
    if n is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such notification")
    core_db.mark_notification_read(db, current_user.id, notif_id)
    return RedirectResponse(n["link"] or "/notifications", status_code=303)


@router.post("/notifications/{notif_id}/read", include_in_schema=False,
             dependencies=[Depends(auth.require_csrf)])
async def notification_read(request: Request, notif_id: int,
                           current_user: auth.SessionUser = Depends(auth.require_session)):
    db = request.app.state.db
    core_db.mark_notification_read(db, current_user.id, notif_id)
    core_db.prune_read_notifications(db, current_user.id)
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/read-all", include_in_schema=False,
             dependencies=[Depends(auth.require_csrf)])
async def notifications_read_all(request: Request,
                                current_user: auth.SessionUser = Depends(auth.require_session)):
    db = request.app.state.db
    core_db.mark_all_notifications_read(db, current_user.id)
    core_db.prune_read_notifications(db, current_user.id)
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/{notif_id}/dismiss", include_in_schema=False,
             dependencies=[Depends(auth.require_csrf)])
async def notification_dismiss(request: Request, notif_id: int,
                              current_user: auth.SessionUser = Depends(auth.require_session)):
    """Dismiss (delete) one of the caller's notifications. Idempotent — a
       no-op (still 303) when it's already gone or not theirs."""
    db = request.app.state.db
    core_db.delete_notification(db, current_user.id, notif_id)
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/clear-read", include_in_schema=False,
             dependencies=[Depends(auth.require_csrf)])
async def notifications_clear_read(request: Request,
                                  current_user: auth.SessionUser = Depends(auth.require_session)):
    """Dismiss (delete) all of the caller's read notifications at once."""
    db = request.app.state.db
    core_db.delete_read_notifications(db, current_user.id)
    return RedirectResponse("/notifications", status_code=303)


def _notification_pref_rows(current_user, db):
    """The notification toggles appropriate to this user's access level:
       every developer/node type for any DB account, plus the admin-only
       access-request type when the user is an admin. None for the
       config-token admin (no user row)."""
    if current_user.id is None:
        return []
    prefs = core_db.get_notification_prefs(db, current_user.id)
    rows = []
    for type_id, slug in core_db.NOTIF_TYPE_NAMES.items():
        # is_config_admin is the admin-permission flag (token admin, or a
        # users.is_admin grant) — the access-request feed is admin-only.
        if slug == "user_access_request" and not current_user.is_config_admin:
            continue
        label, help_text = _NOTIF_PREF_LABELS.get(slug, (slug, ""))
        rows.append({"slug": slug, "label": label, "help": help_text,
                     "enabled": prefs.get(slug, True)})
    return rows


@router.post("/user-settings/notifications", include_in_schema=False,
             dependencies=[Depends(auth.require_csrf)])
async def user_settings_notifications(request: Request,
                                      current_user: auth.SessionUser = Depends(auth.require_session)):
    """Save the account's notification opt-in/out map. A checkbox present in
       the form means ON; absent means OFF — evaluated only over the types
       appropriate to this user's access level."""
    if current_user.id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "the config-token admin has no user account")
    db = request.app.state.db
    form = await request.form()
    prefs = {row["slug"]: (form.get(f"notif_{row['slug']}") is not None)
             for row in _notification_pref_rows(current_user, db)}
    core_db.set_notification_prefs(db, current_user.id, prefs)
    return RedirectResponse("/user-settings?saved=notifications",
                            status_code=303)


# ===========================================================================
# Patchset upload (origin=uploaded — a submission, not corpus/training data)
# ===========================================================================

# Parsed-but-unconfirmed uploads, keyed by a one-shot token the preview
# page posts back. In-memory and process-local — a restart drops pending
# previews (the user just re-uploads), which is fine for a confirm step.
_UPLOAD_PENDING_TTL_SECONDS = 900
_UPLOAD_PENDING_CAP = 20


def _pending_uploads(request):
    """The app-wide pending-upload store, pruned of expired entries."""
    store = getattr(request.app.state, "pending_uploads", None)
    if store is None:
        store = request.app.state.pending_uploads = {}
    cutoff = time.time() - _UPLOAD_PENDING_TTL_SECONDS
    for tok in [t for t, v in store.items() if v["at"] < cutoff]:
        del store[tok]
    return store


def _upload_status(row, *, base_label="uploaded"):
    """The pipeline chip for a /my-patchsets row — uploaded → preparing
       → prepared → reviewing → reviewed, with the two failure states
       surfaced. Reads the booleans + latest work-item states
       list_user_patchsets computes. The patchset detail page shares
       this for its Pipeline field, with `base_label="gathered"` for
       corpus patchsets (same ladder, different ingest verb)."""
    if row["has_ai_review"]:
        return ("reviewed", "text-bg-success")
    for state, stage in ((row["review_state"], "review"),
                         (row["prepare_state"], "prepare")):
        if state == core_db.WORK_ITEM_STATE_UNAPPLIABLE:
            return (f"{stage} unappliable", "text-bg-danger")
        if state == core_db.WORK_ITEM_STATE_DEFERRED:
            return (f"{stage} deferred", "text-bg-warning")
    if row["review_state"] is not None:
        return ("reviewing", "text-bg-info")
    if row["has_metadata"]:
        return ("prepared", "text-bg-info")
    if row["prepare_state"] is not None:
        return ("preparing", "text-bg-secondary")
    return (base_label, "text-bg-secondary")


@router.get("/my-patchsets", response_class=HTMLResponse)
async def my_patchsets(request: Request,
                       current_user: auth.SessionUser = Depends(auth.require_session)):
    """The developer's dashboard — their uploads blended with the
       gathered series they claimed, as one pipeline view (status chip
       per row, the review one click away), with the Upload button and
       the claim suggestions ("series on lore that look like yours").
       Scoped like the queue: a regular user sees their own rows, an
       admin sees everyone's with an Owner column. Uploaded patchsets
       do NOT appear on the corpus home page — this page is where they
       live; claimed series live in both (the corpus row IS the
       claimed row)."""
    db = request.app.state.db
    scope_user_id = _queue_scope_user_id(current_user)
    owner_emails = _owner_email_map(db) if scope_user_id is None else {}
    items = []
    for r in core_db.list_user_patchsets(db, user_id=scope_user_id):
        from_lore = r["origin"] == core_db.PATCHSET_ORIGIN_GATHERED
        label, badge = _upload_status(
            r, base_label="gathered" if from_lore else "uploaded")
        items.append({
            "root":             r["root_message_id"],
            "subject":          r["subject"] or r["root_message_id"],
            "detail_url":       f"/patchsets/{quote(r['root_message_id'])}",
            "n_patches":        r["n_patches"],
            "series_version":   r["series_version"],
            "iterations":       r["iterations"],
            "from_lore":        from_lore,
            "status":           label,
            "status_badge":     badge,
            "uploaded_display": _when(r["gathered_at"]),
            "owner_email":      owner_emails.get(
                                    r["uploaded_by_user_id"]
                                    or r["first_claimant_user_id"]),
        })
    _attach_concerns(db, items)
    # The seam-remover: gathered series whose submitter address matches
    # this account, one click from being theirs. Suggested only on the
    # personal view — the admin everyone-view has no "yours".
    claimable = []
    if scope_user_id is not None:
        for c in core_db.claimable_patchsets(db, scope_user_id,
                                             current_user.email):
            prior = _claim_prior(db, c, current_user)
            claimable.append({
                "subject":          c["subject"] or c["root_message_id"],
                "detail_url":       f"/patchsets/"
                                    f"{quote(c['root_message_id'])}",
                "claim_url":        f"/patchsets/"
                                    f"{quote(c['root_message_id'])}/claim",
                "n_patches":        c["n_patches"],
                "series_version":   c["series_version"],
                "gathered_display": _when(c["gathered_at"]),
                "prior_subject":    (prior["subject"]
                                     or prior["root_message_id"])
                                    if prior else None,
            })
    return templates.TemplateResponse(request, "my_patchsets.html", {
        "current_user": current_user, "items": items,
        "claimable": claimable, "show_owner": scope_user_id is None})


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request,
                      current_user: auth.SessionUser = Depends(auth.require_session)):
    """The patchset-upload form — `git format-patch` output (one .patch /
       .eml per patch + optional cover), a series mbox, or a pasted diff.
       Uploads are submissions ("review my series"), kept apart from the
       gathered corpus by patchsets.origin; they are never training
       data."""
    return templates.TemplateResponse(request, "upload.html", {
        "current_user": current_user, "preview": None, "token": None})


def _may_refresh_collision(existing, current_user):
    """Whether this user may confirm-ingest over an existing corpus row:
       maintainers/admins (corpus maintenance), or the row's own uploader
       (the re-upload flow). Everyone else is offered the existing
       patchset instead — for them the ingest starts nothing and must
       not rewrite stored bodies they don't own."""
    if current_user.is_config_admin or current_user.is_maintainer:
        return True
    return (existing["origin"] == core_db.PATCHSET_ORIGIN_UPLOADED
            and existing["uploaded_by_user_id"] == current_user.id)


def _collision_context(existing, root_norm, *, claimable):
    """Template context for the blocked-collision callout — who holds the
       existing row drives the explanation the uploader reads, and a
       claimable gathered series turns the dead end into the claim
       doorway (`claimable` renders the Claim button)."""
    gathered = existing["origin"] == core_db.PATCHSET_ORIGIN_GATHERED
    return {
        "root": root_norm,
        "gathered": gathered,
        "claimable": claimable,
        "holder": ("hone already gathered this series from the mailing "
                   "list" if gathered
                   else "another user already uploaded this series"),
    }


@router.post("/upload", response_class=HTMLResponse, dependencies=[Depends(auth.require_csrf)])
async def upload_preview(request: Request,
                         current_user: auth.SessionUser = Depends(auth.require_session)):
    """Parse the upload and render the confirm preview — the series
       assembled in order, the detected base-commit, and every warning
       (no base trailer, ignored replies, synthetic Message-IDs). Nothing
       touches the corpus until the user confirms."""
    form = await request.form()
    blobs = []
    for f in form.getlist("files"):
        if getattr(f, "filename", None):
            blobs.append((f.filename, await f.read()))
    pasted = (form.get("pasted") or "")
    parsed = upload.parse_upload(blobs, pasted=pasted)

    token, prior, collision = None, None, None
    if parsed["ok"]:
        db = request.app.state.db
        root_norm = core_db.norm_msgid(parsed["root_message_id"])
        existing = db.execute(
            "SELECT root_message_id, origin, uploaded_by_user_id "
            "FROM patchsets "
            "WHERE root_message_id=?", (root_norm,)).fetchone()
        if existing is not None:
            # Same gate as the confirm-time invalidation below: a
            # re-upload of YOUR OWN upload re-runs the pipeline when the
            # content changed; a maintainer/admin collision only
            # refreshes bodies; anyone else gets no confirm at all —
            # the ingest would do nothing FOR THEM (no new pipeline, no
            # My-patchsets row) and must not rewrite stored bodies they
            # don't own. They get a pointer to the existing patchset
            # instead (_collision_context).
            if (existing["origin"] == core_db.PATCHSET_ORIGIN_UPLOADED
                    and (current_user.is_config_admin
                         or existing["uploaded_by_user_id"]
                            == current_user.id)):
                parsed["warnings"].append(
                    "a patchset with this root Message-ID is already in "
                    "the corpus — confirming refreshes its messages, and "
                    "if the content changed the prepared metadata and AI "
                    "review are dropped so the pipeline re-runs")
            elif _may_refresh_collision(existing, current_user):
                parsed["warnings"].append(
                    "a patchset with this root Message-ID is already in "
                    "the corpus — confirming refreshes its stored "
                    "messages only; it keeps its existing origin and "
                    "work history and starts no new prepare or review")
            else:
                collision = _collision_context(
                    existing, root_norm,
                    claimable=_may_claim_patchset(db, dict(existing),
                                                  current_user))
        # Iteration detection — only for a genuinely NEW root (a known
        # root is a re-upload of the same iteration, handled above).
        # Heuristic, so the preview offers it as a pre-checked opt-out;
        # the confirm handler stamps the link only if the box stays
        # ticked. Candidates are the uploader's own chain heads.
        if existing is None and current_user.id is not None:
            prior = upload.find_prior_iteration(
                core_db.unsuperseded_user_series(db, current_user.id),
                subject=parsed["subject"],
                change_id=parsed.get("change_id"))
        if collision is None:
            store = _pending_uploads(request)
            if len(store) >= _UPLOAD_PENDING_CAP:
                oldest = min(store, key=lambda t: store[t]["at"])
                del store[oldest]
            token = secrets.token_urlsafe(16)
            store[token] = {"parsed": parsed, "user_id": current_user.id,
                            "at": time.time(),
                            "prior_root": prior["root_message_id"]
                                          if prior else None}
    return templates.TemplateResponse(request, "upload.html", {
        "current_user": current_user, "preview": parsed, "token": token,
        "collision": collision,
        "prior": {"root": prior["root_message_id"],
                  "subject": prior["subject"],
                  "uploaded_display": _when(prior["gathered_at"])}
                 if parsed["ok"] and prior else None})


@router.post("/upload/confirm", dependencies=[Depends(auth.require_csrf)])
async def upload_confirm(request: Request,
                         current_user: auth.SessionUser = Depends(auth.require_session)):
    """Ingest a previewed upload through the same primitives gather uses
       (upsert_patchset / upsert_message / maybe_enqueue_prepare) — same
       dedup, same idempotency — stamped origin=uploaded with the
       uploader's id. The prepare work-item carries the uploader as its
       origin, so it routes onto their own nodes' queue; the review is
       auto-chained when prepare lands (api.submit_result). Redirects to
       the patchset's detail page."""
    form = await request.form()
    token = form.get("token") or ""
    entry = _pending_uploads(request).pop(token, None)
    if entry is None or entry["user_id"] != current_user.id:
        raise HTTPException(status.HTTP_410_GONE,
                            "upload preview expired — please re-upload")
    parsed = entry["parsed"]
    db = request.app.state.db

    # Re-upload invalidation: when this root is already in the corpus as
    # this user's own upload (or the caller is an admin) and the new
    # upload actually changes content — any new or edited cover/patch
    # body — drop the pipeline's derived artifacts before ingesting.
    # Prepared metadata and an AI review must never outlive the bodies
    # they were computed from; the maybe_enqueue_prepare below then
    # starts a fresh prepare (review re-chains when it lands). Compared
    # BEFORE the upserts overwrite the stored bodies. A collision with
    # someone else's row keeps today's refresh-only behaviour.
    root_norm = core_db.norm_msgid(parsed["root_message_id"])
    existing = db.execute(
        "SELECT origin, uploaded_by_user_id FROM patchsets "
        "WHERE root_message_id=?", (root_norm,)).fetchone()
    # The preview offers no confirm on a collision the user can't
    # refresh, so reaching here with one means the row appeared between
    # preview and confirm (a gather or another upload raced us). Land
    # the user on the existing patchset instead of ingesting.
    if existing is not None and not _may_refresh_collision(existing,
                                                           current_user):
        return RedirectResponse(f"/patchsets/{quote(root_norm)}",
                                status_code=303)
    if (existing is not None
            and existing["origin"] == core_db.PATCHSET_ORIGIN_UPLOADED
            and (current_user.is_config_admin
                 or existing["uploaded_by_user_id"] == current_user.id)):
        incoming = (([parsed["cover"]] if parsed["cover"] else [])
                    + parsed["patches"])
        for m in incoming:
            row = db.execute(
                "SELECT body FROM messages WHERE message_id=?",
                (core_db.norm_msgid(m["message_id"]),)).fetchone()
            if row is None or row["body"] != m["body"]:
                core_db.reset_patchset_pipeline(db, root_norm)
                break

    # Iteration linking — the preview offered the prior (entry carries
    # it) and the uploader left the box ticked. Re-check the prior is
    # still THIS USER'S head: a raced parallel confirm may have chained
    # onto it first, and a chain must stay linear PER USER — another
    # developer hanging their own v2 off the same shared lore series
    # doesn't consume it for this uploader.
    supersedes = None
    prior_root = entry.get("prior_root")
    if prior_root and form.get("link_iteration"):
        still_head = db.execute(
            "SELECT 1 FROM patchsets WHERE root_message_id=? "
            "AND NOT EXISTS (SELECT 1 FROM patchsets q "
            "  WHERE q.supersedes_root_message_id=? "
            "  AND ((q.origin=? AND q.uploaded_by_user_id=?) "
            "    OR EXISTS (SELECT 1 FROM patchset_claims c "
            "      WHERE c.root_message_id=q.root_message_id "
            "      AND c.user_id=?)))",
            (prior_root, prior_root, core_db.PATCHSET_ORIGIN_UPLOADED,
             current_user.id, current_user.id)).fetchone()
        if still_head:
            supersedes = prior_root

    root = core_db.upsert_patchset(
        db, parsed["root_message_id"],
        subject=parsed["subject"],
        submitter_email=parsed["submitter_email"] or None,
        sent=parsed["sent"],
        n_patches=parsed["n_patches"],
        base_commit=parsed["base_commit"],
        change_id=parsed.get("change_id"),
        series_version=parsed["series_version"],
        origin=core_db.PATCHSET_ORIGIN_UPLOADED,
        uploaded_by_user_id=current_user.id,
        supersedes_root_message_id=supersedes)
    cover = parsed["cover"]
    if cover is not None:
        core_db.upsert_message(
            db, cover["message_id"], root_message_id=root,
            type=core_db.MSG_TYPE_COVER, body=cover["body"], part_index=0,
            author_name=cover["author_name"] or None,
            author_email=cover["author_email"] or None,
            subject=cover["subject"], sent=cover["sent"])
    for p in parsed["patches"]:
        core_db.upsert_message(
            db, p["message_id"], root_message_id=root,
            type=core_db.MSG_TYPE_PATCH, body=p["body"],
            part_index=p["part_index"],
            parent_message_id=cover["message_id"] if cover else None,
            author_name=p["author_name"] or None,
            author_email=p["author_email"] or None,
            subject=p["subject"], sent=p["sent"])
    core_db.maybe_enqueue_prepare(db, root,
                                  requested_by_user_id=current_user.id)
    # The new iteration replaces the old one's pending work: retire the
    # superseded patchset's queued (unheld) prepare/review items so
    # nodes don't spend tokens on it. In-flight claimed work finishes
    # and lands as that iteration's history.
    if supersedes:
        core_db.cancel_unheld_pipeline_items(db, supersedes)
    return RedirectResponse(f"/patchsets/{quote(root)}", status_code=303)


def _types(raw):
    """Render a node's stored task_types JSON as a readable list."""
    try:
        vals = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return raw or "—"
    return ", ".join(vals) if vals else "—"


def _when_text(ts):
    """Render a unix timestamp as a plain UTC string — for attribute
       contexts (tooltips) where the <time> markup of _when can't
       render. Body-text call sites use _when."""
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _when(ts):
    """Render a unix timestamp as a <time> element: UTC text content
       with a machine-readable `datetime` attribute. A script in
       base.html rewrites the text to the browser's local timezone on
       load and after every HTMX swap — so all dates in the UI read in
       the viewer's local time with zero configuration. Degrades to the
       UTC string with JS off, and the title attribute always keeps
       UTC so hovering correlates with server logs."""
    if not ts:
        return "—"
    text = _when_text(ts)
    iso = datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Markup(
        f'<time datetime="{iso}" title="{text}">{text}</time>')


# --- fleet pulse (top-nav chip) -------------------------------------------

# Freshness cutoff multiplier — a node is `stale` if its last_seen is
# older than (heartbeat_seconds × this). 3× is forgiving enough to
# survive a single missed heartbeat without flipping the chip yellow.
_FLEET_STALE_HEARTBEAT_MULT = 3


def _fleet_pulse_view(db, runtime_cfg):
    """View-model the fleet-pulse partial renders. Compresses
       core_db.fleet_status into three render-ready fields:

         tone:    `success` / `warning` / `danger` / `muted` — drives
                  the badge colour. Bias is loudest-wins: any
                  errored → danger, else any stale → warning,
                  else (any healthy) → success, else no nodes →
                  muted ("dim grey").
         label:   short text shown in the chip itself. Kept compact
                  so the nav bar stays uncluttered on narrow viewports
                  and 30+ node fleets.
         tooltip: longer hover-text with every count + last-activity
                  timestamp."""
    stale_after = (runtime_cfg.heartbeat_seconds
                    * _FLEET_STALE_HEARTBEAT_MULT)
    s = core_db.fleet_status(db, stale_after_seconds=stale_after)
    total      = s["total"]
    healthy    = s["healthy"]
    errored    = s["errored"]
    stale      = s["stale"]
    in_flight  = s["in_flight"]
    if total == 0:
        tone  = "muted"
        label = "no nodes"
    elif errored:
        tone  = "danger"
        label = f"{errored} errored · {total} nodes"
    elif stale:
        tone  = "warning"
        label = f"{stale} stale · {total} nodes"
    elif in_flight:
        tone  = "success"
        label = (f"{total} node · {in_flight} in flight" if total == 1
                  else f"{total} nodes · {in_flight} in flight")
    else:
        tone  = "success"
        label = f"{total} node idle" if total == 1 else f"{total} nodes idle"
    tooltip_parts = [f"{healthy} healthy"]
    if errored:
        tooltip_parts.append(f"{errored} errored")
    if stale:
        tooltip_parts.append(f"{stale} stale")
    tooltip_parts.append(f"{in_flight} claim{'' if in_flight == 1 else 's'} "
                          "in flight")
    if s["last_activity_at"]:
        # _when_text, not _when — this string lands in a title attribute.
        tooltip_parts.append(f"last seen {_when_text(s['last_activity_at'])}")
    tooltip = " · ".join(tooltip_parts)
    return {"tone": tone, "label": label, "tooltip": tooltip}


@router.get("/fleet-status", response_class=HTMLResponse)
async def fleet_status_partial(request: Request,
                                _: auth.SessionUser = Depends(auth.require_session)):
    """The fleet-pulse chip as an HTML partial — polled by HTMX every
       10s from the top nav so the operator gets a live rollup
       without reloading the page they're on. Tiny SQL footprint:
       one COUNT-style scan over `nodes` plus one over claimed
       work_items, regardless of fleet size."""
    return templates.TemplateResponse(
        request, "_fleet_pulse.html",
        {"fleet": _fleet_pulse_view(request.app.state.db,
                                      request.app.state.runtime_config)})


# --- fleet throughput sparkline (top-nav) ---------------------------------

# Sparkline geometry. Viewbox is unitless — the SVG scales to the CSS
# size on the placeholder span. Width = nbins-1 so points 0..nbins-1
# span the full width; height = 10 with a 0.5 padding above/below so
# the topmost / bottommost strokes aren't clipped.
_SPARKLINE_HEIGHT  = 10
_SPARKLINE_PAD     = 0.5
_SPARKLINE_WINDOW_SECONDS = 3600
_SPARKLINE_BIN_SECONDS    = 60


def _fleet_sparkline_view(db):
    """View-model the sparkline partial renders. Computes the
       throughput bins, the peak (for vertical scaling), the total
       claim count over the window (for the tooltip), and the SVG
       polyline `points` string ready to drop into the template.

       Empty / all-zero windows render as a flat baseline — the
       operator still sees a stable, non-empty SVG so the layout
       doesn't jump when the first claim lands."""
    bins = core_db.fleet_throughput(
        db, window_seconds=_SPARKLINE_WINDOW_SECONDS,
        bin_seconds=_SPARKLINE_BIN_SECONDS)
    total = sum(bins)
    peak  = max(bins) or 1
    span  = _SPARKLINE_HEIGHT - 2 * _SPARKLINE_PAD
    # Invert Y so a higher count plots toward the top of the viewBox.
    points = " ".join(
        f"{i},{_SPARKLINE_PAD + span * (1 - v / peak):.2f}"
        for i, v in enumerate(bins))
    minutes = _SPARKLINE_WINDOW_SECONDS // 60
    return {"points":      points,
             "width":       max(1, len(bins) - 1),
             "height":      _SPARKLINE_HEIGHT,
             "total":       total,
             "peak":        peak if max(bins) else 0,
             "tooltip":     (f"{total} claim{'' if total == 1 else 's'} "
                              f"completed in the last {minutes} min")}


@router.get("/fleet-sparkline", response_class=HTMLResponse)
async def fleet_sparkline_partial(request: Request,
                                   _: auth.SessionUser = Depends(auth.require_session)):
    """The fleet throughput sparkline as an HTML partial — polled
       every 10s from the top nav. Tracks claims/min over a rolling
       hour so the operator sees a confirmation that work is
       actually moving, not just that the fleet is alive (which
       the pulse chip already tells them)."""
    return templates.TemplateResponse(
        request, "_fleet_sparkline.html",
        {"spark": _fleet_sparkline_view(request.app.state.db)})


# --- queue (home) ----------------------------------------------------------

_WORK_TYPE_BY_NAME  = {v: k for k, v in core_db.WORK_ITEM_TYPE_NAMES.items()}
_WORK_STATE_BY_NAME = {v: k for k, v in core_db.WORK_ITEM_STATE_NAMES.items()}

_STATE_BADGE = {
    "claimable":   "text-bg-secondary",
    "claimed":     "text-bg-info",
    "completed":   "text-bg-success",
    "unappliable": "text-bg-danger",
    "deferred":    "text-bg-warning",
}
_TYPE_BADGE = {"prepare": "text-bg-secondary",
               "review":  "text-bg-primary",
               "train":   "text-bg-dark"}

# Patchset state-filter keys (the chips above the table) — lifecycle flags
# plus the one base-state filter, "skipped". "gathered" is omitted: every
# patchset in the corpus has been gathered, so it carries no information.
_PATCHSET_FILTERS = ("prepared", "reviewed", "training", "skipped")
# Per-row lifecycle flags shown in the State column: (row key, abbreviation,
# full title, badge class). A patchset can carry several at once; they're
# abbreviated + colour-coded, with the full word on hover.
_PATCHSET_FLAGS = (("is_prepared", "P", "Prepared", "text-bg-info"),
                   ("is_reviewed", "R", "Reviewed", "text-bg-success"),
                   ("is_training", "T", "Training", "text-bg-warning"))
# Patchset-list columns in display order: (sort key, header label, sortable).
# State is not sortable — it's a multi-flag column, not a single value.
_PATCHSET_COLUMNS = (("subject",  "Patchset", True),
                     ("author",   "Author",   True),
                     ("date",     "Date",     True),
                     ("state",    "State",    False),
                     ("parts",    "Parts",    True),
                     ("comments", "Comments", True),
                     ("concerns", "Concerns", False))


# Page-size options for the queue paginator (small dropdown). 25 is
# the default — small enough to scan on a single screen and consistent
# with the eye-level fit operators expect from a skim view.
_PAGE_SIZES = (25, 50, 100, 200)
_DEFAULT_PAGE_SIZE = 25

# Page-size options for the node-detail Recent-claims paginator. A
# smaller default (10) than the queue — the claims list is a sidebar-
# style history on a page that already has the live cards + reviews
# above/below it, so a short slice keeps the page compact.
_CLAIMS_PAGE_SIZES = (10, 25, 50, 100)
_DEFAULT_CLAIMS_PAGE_SIZE = 10


def _queue_url(*, type=None, state=None, page=None, size=None):
    """Build a `/queue?...` queue URL preserving the chosen axes + paging.
       Filter chips drop page (changing filter resets to page 1); paging
       links keep filter + size; the size selector keeps filter + resets
       page to 1."""
    parts = []
    if type:
        parts.append(f"type={type}")
    if state:
        parts.append(f"state={state}")
    if page and page > 1:
        parts.append(f"page={page}")
    if size and size != _DEFAULT_PAGE_SIZE:
        parts.append(f"size={size}")
    return "/queue" + ("?" + "&".join(parts) if parts else "")


def _page_window(current, pages, radius=2):
    """The page-number window the paginator shows: `current ±radius`,
       clipped to [1, pages] — what Bootstrap's pagination renders as
       1 … 4 5 [6] 7 8 … 42. Returns the list of integers to show as
       direct page links (the ellipsis is decided in the template)."""
    if pages <= 0:
        return []
    lo = max(1, current - radius)
    hi = min(pages, current + radius)
    return list(range(lo, hi + 1))


def _queue_scope_user_id(current_user):
    """The queue's visibility scope: admins (config-token or granted)
       see every work item; a regular user sees only the items they
       requested. Returns the user id to filter on, or None for the
       unscoped admin view."""
    if current_user is None or current_user.is_config_admin:
        return None
    return current_user.id


def _queue_view(db, type, state, page, size, current_user=None):
    """Build the queue page's render context — items, chips, paging info.
       Shared by the full-page `GET /queue` and the HTMX partial swap.
       Scoped by _queue_scope_user_id: chips, counts, paging, and rows
       all describe the same visible subset."""
    scope_user_id = _queue_scope_user_id(current_user)
    type_int  = _WORK_TYPE_BY_NAME.get(type)
    state_int = _WORK_STATE_BY_NAME.get(state)
    if type_int is None:
        type = None
    if state_int is None:
        state = None

    counts = core_db.work_item_counts(             # {type_int: {state_int: n}}
        db, requested_by_user_id=scope_user_id)
    type_totals = {core_db.WORK_ITEM_TYPE_NAMES[t]: sum(by_state.values())
                   for t, by_state in counts.items()}
    if type_int is None:
        state_totals_int = {s: sum(counts[t].get(s, 0) for t in counts)
                            for s in core_db.WORK_ITEM_STATE_NAMES}
    else:
        state_totals_int = dict(counts.get(type_int, {}))
    state_totals = {core_db.WORK_ITEM_STATE_NAMES[s]: n
                    for s, n in state_totals_int.items()}
    grand_total = sum(type_totals.values()) if type_int is None \
        else type_totals[type]

    # Paginate: clamp size to the allowed set, then page to [1, pages].
    size = size if size in _PAGE_SIZES else _DEFAULT_PAGE_SIZE
    filtered_total = core_db.count_work_items(
        db, type=type_int, state=state_int,
        requested_by_user_id=scope_user_id)
    pages = max(1, -(-filtered_total // size))    # ceil division
    page = max(1, min(page or 1, pages))
    offset = (page - 1) * size

    # The queue's current URL is the destination the detail page's
    # `← Back` link returns to. Computed once; threaded onto every row.
    this_url = _queue_url(type=type, state=state,
                           page=page if page > 1 else None, size=size)
    back_qs = "?back=" + quote(this_url, safe="")

    owner_emails = _owner_email_map(db)
    items = []
    for w in core_db.list_work_items(db, type=type_int, state=state_int,
                                       requested_by_user_id=scope_user_id,
                                       limit=size, offset=offset):
        type_name  = core_db.WORK_ITEM_TYPE_NAMES.get(w["type"], "?")
        state_name = core_db.WORK_ITEM_STATE_NAMES.get(w["state"], "?")
        # Deferral badge suffix: "deferred ×3" while retrying on backoff,
        # "deferred ×5 · parked" once the cap is hit (admin re-arm only).
        defer_suffix = ""
        if state_name == "deferred" and (w.get("defer_count") or 0):
            defer_suffix = f" ×{w['defer_count']}"
            if w.get("lease_expires") is None:
                defer_suffix += " · parked"
        items.append({
            "id":             w["id"],
            "type":           type_name,
            "type_badge":     _TYPE_BADGE.get(type_name, "text-bg-light"),
            "state":          state_name,
            "defer_suffix":   defer_suffix,
            "state_badge":    _STATE_BADGE.get(state_name, "text-bg-light"),
            "subject":        w["subject"] or w["root_message_id"],
            "message_id":     w["message_id"],
            # Queue rows drill into the per-work-item detail page —
            # the queue is a list of work-items, so one click goes to
            # the work-item, not the patchset. The patchset is one
            # further click from inside the work-item header.
            "detail_url":     f"/work-items/{w['id']}{back_qs}",
            "claimed_by":     w["claimed_by"],
            "origin_email":   owner_emails.get(w["requested_by_user_id"]),
            "enqueued_display":  _when(w["enqueued_at"]),
            "started_display":   _when(w["claimed_at"]),
            "completed_display": _when(w["completed_at"]),
        })

    type_chips = [{"label": "All",
                   "url": _queue_url(state=state, size=size),
                   "count": grand_total, "active": type is None}]
    for name in ("prepare", "review", "train"):
        type_chips.append({"label": name,
                           "url": _queue_url(type=name, size=size),
                           "count": type_totals[name],
                           "active": type == name})

    state_chips = [{"label": "All",
                    "url": _queue_url(type=type, size=size),
                    "count": grand_total, "active": state is None}]
    for s_name in ("claimable", "claimed", "completed",
                   "unappliable", "deferred"):
        state_chips.append({"label": s_name,
                            "url": _queue_url(type=type, state=s_name,
                                              size=size),
                            "count": state_totals[s_name],
                            "active": state == s_name})

    # Paging URLs + window. The first/prev/next/last + numbered links
    # round-trip filter and size; the size dropdown keeps filter, drops
    # page (any size change resets to page 1).
    def _u(p):
        return _queue_url(type=type, state=state, page=p, size=size)
    paging = {
        "page":         page,
        "pages":        pages,
        "size":         size,
        "size_options": [{"value": s, "url": _queue_url(type=type,
                                                          state=state,
                                                          size=s)}
                         for s in _PAGE_SIZES],
        "total":        filtered_total,
        "start":        offset + 1 if items else 0,
        "end":          offset + len(items),
        "first_url":    _u(1),
        "prev_url":     _u(max(1, page - 1)),
        "next_url":     _u(min(pages, page + 1)),
        "last_url":     _u(pages),
        "has_prev":     page > 1,
        "has_next":     page < pages,
        "window":       [{"page": p, "url": _u(p),
                          "active": p == page}
                         for p in _page_window(page, pages)],
        "show_first_ellipsis": _page_window(page, pages)[:1] != [1]
                               if pages else False,
        "show_last_ellipsis":  (_page_window(page, pages)[-1:] != [pages]
                                if pages else False),
    }
    return {
        "items": items, "filter_type": type, "filter_state": state,
        "type_chips": type_chips, "state_chips": state_chips,
        "paging": paging, "this_url": this_url,
    }


def _short_list_tag(tag):
    """Display label for a mailing-list tag: the list name before the first
       dot (netdev.vger.kernel.org → netdev)."""
    return tag.split(".", 1)[0]


def _patchsets_url(*, q=None, state=None, comments=None, list_tag=None,
                   patch_type=None, sort=None, direction=None, page=None,
                   size=None):
    """Build a `/?...` patchset-list URL preserving search, the filter axes,
       sort, and paging. Defaults (sort=date, direction=desc, page=1,
       size=25) are omitted so the bare home URL stays clean. state /
       comments / list_tag / patch_type are independent axes — each
       round-trips when set."""
    parts = []
    if q:
        parts.append(f"q={quote(q, safe='')}")
    if state:
        parts.append(f"state={state}")
    if comments:
        parts.append(f"comments={comments}")
    if list_tag:
        parts.append(f"list_tag={quote(list_tag, safe='')}")
    if patch_type:
        parts.append(f"patch_type={quote(patch_type, safe='')}")
    if sort and sort != "date":
        parts.append(f"sort={sort}")
    if direction and direction != "desc":
        parts.append(f"direction={direction}")
    if page and page > 1:
        parts.append(f"page={page}")
    if size and size != _DEFAULT_PAGE_SIZE:
        parts.append(f"size={size}")
    return "/" + ("?" + "&".join(parts) if parts else "")


def _patchsets_view(db, q, state, comments, list_tag, patch_type,
                    sort, direction, page, size):
    """Build the patchset-list render context — search-box value, the
       independent filter axes (lifecycle state, comments, mailing list,
       patch type), sortable column headers, the page of rows, and paging.
       Rows carry a `detail_url` into the per-patchset page with `?back=`
       round-tripping to this listing."""
    q = (q or "").strip() or None
    if state not in _PATCHSET_FILTERS:
        state = None
    comments = "with" if comments == "with" else None
    # Value-axis options come from the data, so the dropdowns never offer a
    # choice with zero matches; an unknown value falls back to "all".
    tag_options = core_db.distinct_patchset_tags(db)
    type_options = core_db.distinct_patch_types(db)
    if list_tag not in tag_options:
        list_tag = None
    if patch_type not in type_options:
        patch_type = None
    if sort not in core_db.PATCHSET_SORT_COLUMNS:
        sort = "date"
    direction = "asc" if str(direction).lower() == "asc" else "desc"

    size = size if size in _PAGE_SIZES else _DEFAULT_PAGE_SIZE
    total = core_db.count_patchsets(db, q=q, state=state, comments=comments,
                                    list_tag=list_tag, patch_type=patch_type)
    pages = max(1, -(-total // size))             # ceil division
    page = max(1, min(page or 1, pages))
    offset = (page - 1) * size

    # All listing URLs preserve the full filter/sort/size context; callers
    # override just the axis they change (and omit page → reset to page 1).
    def url(**over):
        args = dict(q=q, state=state, comments=comments, list_tag=list_tag,
                    patch_type=patch_type, sort=sort, direction=direction,
                    size=size)
        args.update(over)
        return _patchsets_url(**args)

    this_url = url(page=page if page > 1 else None)
    back_qs = "?back=" + quote(this_url, safe="")

    items = []
    for p in core_db.list_patchsets_page(db, q=q, state=state, comments=comments,
                                          list_tag=list_tag, patch_type=patch_type,
                                          sort=sort, direction=direction,
                                          limit=size, offset=offset):
        items.append({
            "root":         p["root_message_id"],
            "subject":      p["subject"] or p["root_message_id"],
            "author":       p["author"] or "—",
            "sent_display": _when(p["sent"]),
            "skipped":      p["state"] == core_db.PATCHSET_STATE_SKIPPED,
            # P/R/T flags actually set on this patchset, in display order.
            "flags":        [{"abbr": ab, "title": ti, "badge": bg}
                             for key, ab, ti, bg in _PATCHSET_FLAGS if p[key]],
            "n_parts":      p["n_parts"],
            "n_comments":   p["n_comments"],
            "detail_url":   f"/patchsets/{quote(p['root_message_id'])}{back_qs}",
        })
    # The AI-review concerns summary for just this page of roots (one query).
    _attach_concerns(db, items)

    # Two independent filter rows. A chip change resets to page 1 (url()
    # omits page) but preserves the other axis, search, sort, and size.
    state_chips = [{"label": "All", "active": state is None, "url": url(state=None)}]
    for key in _PATCHSET_FILTERS:
        state_chips.append({"label": key.capitalize(), "active": state == key,
                            "url": url(state=key)})
    comment_chips = [
        {"label": "All", "active": comments is None, "url": url(comments=None)},
        {"label": "With comments", "active": comments == "with",
         "url": url(comments="with")}]

    # Value-axis dropdowns (mailing list, patch type): an "All" option plus
    # one per value present in the corpus, each navigating via data-url.
    list_select = [{"label": "All lists", "selected": list_tag is None,
                    "url": url(list_tag=None)}]
    for t in tag_options:
        list_select.append({"label": _short_list_tag(t), "selected": list_tag == t,
                            "url": url(list_tag=t)})
    type_select = [{"label": "All types", "selected": patch_type is None,
                    "url": url(patch_type=None)}]
    for t in type_options:
        type_select.append({"label": t.capitalize(), "selected": patch_type == t,
                            "url": url(patch_type=t)})

    # Sortable headers: clicking the active column flips direction; a fresh
    # column starts desc for date (newest first), asc otherwise. The State
    # column is multi-flag, so it renders as a plain (unsortable) header.
    columns = []
    for key, label, sortable in _PATCHSET_COLUMNS:
        if not sortable:
            columns.append({"key": key, "label": label, "sortable": False})
            continue
        if key == sort:
            next_dir, indicator = (("asc", "down") if direction == "desc"
                                   else ("desc", "up"))
        else:
            next_dir, indicator = ("desc" if key == "date" else "asc"), None
        columns.append({"key": key, "label": label, "sortable": True,
                        "indicator": indicator,
                        "url": url(sort=key, direction=next_dir)})

    win = _page_window(page, pages)
    paging = {
        "page": page, "pages": pages, "size": size, "total": total,
        "start": offset + 1 if items else 0, "end": offset + len(items),
        "first_url": url(page=1), "prev_url": url(page=max(1, page - 1)),
        "next_url": url(page=min(pages, page + 1)), "last_url": url(page=pages),
        "has_prev": page > 1, "has_next": page < pages,
        "size_options": [{"value": s, "url": url(size=s)} for s in _PAGE_SIZES],
        "window": [{"page": p, "url": url(page=p), "active": p == page}
                   for p in win],
        "show_first_ellipsis": win[:1] != [1] if win else False,
        "show_last_ellipsis":  win[-1:] != [pages] if win else False,
    }
    return {
        "items": items, "q": q or "", "filter_state": state,
        "filter_comments": comments, "filter_list_tag": list_tag,
        "filter_patch_type": patch_type, "sort": sort, "direction": direction,
        "state_chips": state_chips, "comment_chips": comment_chips,
        "list_select": list_select, "type_select": type_select,
        "columns": columns, "paging": paging,
    }


@router.get("/", response_class=HTMLResponse)
async def patchsets(request: Request,
                    q: str | None = None, state: str | None = None,
                    comments: str | None = None, list_tag: str | None = None,
                    patch_type: str | None = None,
                    sort: str = "date", direction: str = "desc",
                    page: int = 1, size: int = _DEFAULT_PAGE_SIZE,
                    current_user: auth.SessionUser = Depends(auth.require_session)):
    """The patchset corpus — the operator UI's home page. A search box
       (partial subject or author name) plus independent filter axes
       (lifecycle state, comments, mailing list, patch type), sortable
       columns, and a 25/50/100/200 paginator above and below the table.
       Default sort is newest patchset date first; each row links to the
       per-patchset detail page.

       Maintainers and admins only — the corpus is the training /
       review-selection population, not a general browsing surface. A
       regular user landing on / is sent to their own dashboard
       (/my-patchsets) rather than 403'd: it's the home page."""
    if not (current_user.is_config_admin or current_user.is_maintainer):
        return RedirectResponse("/my-patchsets", status_code=303)
    db = request.app.state.db
    ctx = _patchsets_view(db, q, state, comments, list_tag, patch_type,
                          sort, direction, page, size)
    ctx["current_user"] = current_user
    return templates.TemplateResponse(request, "patchsets.html", ctx)


@router.get("/queue", response_class=HTMLResponse)
async def queue(request: Request,
                type: str | None = None, state: str | None = None,
                page: int = 1, size: int = _DEFAULT_PAGE_SIZE,
                current_user: auth.SessionUser = Depends(auth.require_session)):
    """The work queue. `?type=` filters to one work-item type (prepare /
       review / train); `?state=` filters to one state. Unknown axis values
       are ignored. `?page=` and `?size=`
       page the listing (page is 1-indexed; size is clamped to one of
       _PAGE_SIZES — defaults to 25).

       Visibility is scoped: admins see the whole queue; a regular user
       sees only the items they requested (chips, counts, paging, and
       the poll version all describe that same subset — see
       _queue_scope_user_id).

       Auto-poll short-circuit: the #queue-pane wrapper echoes the
       last-known `X-Queue-Version` on every HTMX request. When it
       matches the current filtered queue's version, we return 204 No
       Content so HTMX skips the swap and the server skips the template
       render. A full-page navigation (no HX-Request header) always
       renders — a real browser navigation isn't an idempotent poll."""
    db = request.app.state.db
    is_hx = request.headers.get("hx-request") == "true"
    # `hx-headers` on the #queue-pane wrapper carries X-Queue-Version
    # on EVERY request the wrapper fires, including descendant clicks
    # (paginator, chips) that inherit it. The 204 short-circuit must
    # only apply to the wrapper's own auto-poll, otherwise a
    # pagination click with the current version would 204 and HTMX
    # would skip the swap — page 2 never renders. HTMX sets
    # HX-Trigger to the triggering element's id, which is
    # "queue-pane" for the wrapper's `every 5s` poll and the link's
    # id (typically absent) for clicks.
    is_auto_poll = (is_hx
                     and request.headers.get("hx-trigger") == "queue-pane")
    version = core_db.queue_version(
        db, type=_WORK_TYPE_BY_NAME.get(type),
        state=_WORK_STATE_BY_NAME.get(state),
        requested_by_user_id=_queue_scope_user_id(current_user))
    if is_auto_poll and request.headers.get("x-queue-version") == version:
        # Idle poll: the operator's open page already shows the
        # latest state. Logged at DEBUG so the per-operator,
        # per-5s tick doesn't fill the operator's container log
        # at the default INFO level; raise the logger to DEBUG
        # if you want to watch the polling loop.
        log.debug("queue poll 204 — version unchanged (%s)", version)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    ctx = _queue_view(db, type, state, page, size,
                      current_user=current_user)
    ctx["queue_version"] = version
    ctx["current_user"] = current_user
    # HTMX swap requests (pagination + the auto-poll wrapper) get just
    # the `_queue_pane.html` partial — chips + body + the self-renewing
    # `hx-get` wrapper — so the page chrome and the sidebar don't
    # re-render and the wrapper's polling URL stays in lockstep with
    # the filter / page state.
    template = "_queue_pane.html" if is_hx else "queue.html"
    return templates.TemplateResponse(request, template, ctx)


# --- per-patchset detail ---------------------------------------------------

def _safe_back(back):
    """Sanitise the `?back=` query param. Only same-origin paths are
       honoured (must start with `/`, must not start with `//` — which is
       a protocol-relative URL pointing elsewhere). Everything else falls
       back to the queue home."""
    if back and back.startswith("/") and not back.startswith("//"):
        return back
    return "/"


# AI-review severities, highest first — the order the per-patch finding-count
# header lists them, and a red→grey spectrum (see the .sev-* rules in app.css).
_SEVERITY_ORDER = ("critical", "major", "moderate", "minor", "nit")


def _concern_view(c):
    """A concern flattened for the inline-review template: its severity (which
       names the `sev-<tag>` colour for its badge + comment-box edge), the
       pre-existing flag, the prose, and the file/function pointers."""
    return {
        "severity":      c.get("severity") or "nit",
        "is_preexisting": bool(c.get("is_preexisting")),
        "stage_id":      c.get("stage_id"),
        "candidate_or_check_id": c.get("candidate_or_check_id"),
        "text":          c.get("text") or "",
        "locations":     c.get("locations") or [],
    }


def _severity_counts(concerns):
    """Per-severity finding tally for a patch header, in `_SEVERITY_ORDER`.
       `new` is patch-introduced; `pre` is pre-existing (shown parenthesised)."""
    counts = {s: {"new": 0, "pre": 0} for s in _SEVERITY_ORDER}
    for c in concerns:
        sev = c.get("severity")
        if sev in counts:
            counts[sev]["pre" if c.get("is_preexisting") else "new"] += 1
    return [{"severity": s, "new": counts[s]["new"], "pre": counts[s]["pre"]}
            for s in _SEVERITY_ORDER]


def _concerns_cell(concerns):
    """The listing 'concerns' column model for a patchset, from its AI-review
       concerns list (concerns_map.get(root), so None == no review). Three
       states, matching the patchset page:
         None  -> the patchset isn't reviewed: a blank column;
         []    -> reviewed, no concerns in any patch: 'No concerns found';
         [...] -> reviewed: the critical/major/moderate/minor/nit tally
                  (_severity_counts) summed across every patch in the series."""
    if concerns is None:
        return None
    return {"any": bool(concerns), "counts": _severity_counts(concerns)}


def _attach_concerns(db, items):
    """Stamp each list item's `concerns` cell (_concerns_cell) from one batched
       lookup over the page's roots. Each item must carry `root`."""
    cmap = core_db.ai_review_concerns_for_roots(db, [it["root"] for it in items])
    for it in items:
        it["concerns"] = _concerns_cell(cmap.get(it["root"]))


def _annotate_patch(body, concerns):
    """Inline-review render of one patch: its complete diff as classified line
       rows, with each concern's comment injected just below the last line of
       its `spans_lines_in_diff` (0-indexed from the patch's first `diff --git`
       line). A concern whose span can't be anchored (null, or out of range) is
       returned separately to pin at the card's foot. Returns (rows, unanchored)
       where a row is {"line": span_html} or {"concern": view}."""
    origin, spans = patchview.diff_line_spans(body)
    by_index, unanchored = {}, []
    for c in concerns:
        span = (c.get("patch_scope") or {}).get("spans_lines_in_diff")
        anchor = origin + span[1] if (span and origin is not None) else None
        if anchor is not None and 0 <= anchor < len(spans):
            by_index.setdefault(anchor, []).append(_concern_view(c))
        else:
            unanchored.append(_concern_view(c))
    rows = []
    for i, span in enumerate(spans):
        rows.append({"line": span})
        for view in by_index.get(i, []):
            rows.append({"concern": view})
    return rows, unanchored


def _patchset_view(db, root, viewer_user_id=None):
    """Build the per-patchset detail render context — patchset header,
       list tags, prepare-derived metadata, ai_review concerns, work-item
       history, and the message thread. Raises 404 when the root is
       unknown. `viewer_user_id` scopes the viewer-relative pieces (the
       superseded-by banner)."""
    patchset = core_db.get_patchset(db, root)
    if patchset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no patchset with root_message_id {root!r}")
    patchset["sent_display"]      = _when(patchset.get("sent"))
    patchset["gathered_display"]  = _when(patchset.get("gathered_at"))
    patchset["state_display"] = core_db.PATCHSET_STATE_NAMES.get(
        patchset["state"], "?")
    # Skipped is the abnormal ingest disposition — the only corpus state
    # worth a row on the detail page (a page rendering the patchset is
    # self-evidently looking at a gathered one).
    patchset["is_skipped"] = (patchset["state"]
                              == core_db.PATCHSET_STATE_SKIPPED)
    # Uploaded-origin patchsets are submissions, not corpus rows — badge
    # them so the shared detail page never reads as LKML data.
    patchset["is_uploaded"] = (patchset.get("origin")
                               == core_db.PATCHSET_ORIGIN_UPLOADED)
    patchset["uploaded_by_email"] = None
    if patchset["is_uploaded"] and patchset.get("uploaded_by_user_id"):
        u = core_db.get_user_by_id(db, patchset["uploaded_by_user_id"])
        patchset["uploaded_by_email"] = u["email"] if u else None

    # Iteration chain (uploaded patchsets): the row this one replaced
    # and — important on a STALE page — the row that replaced this one.
    # `iteration` is the 1-based position in the chain, walked through
    # the supersedes pointers (cycle-guarded; a cycle can't be created
    # through the UI but the page must not hang on a hand-edited DB).
    supersedes_link = superseded_by = None
    patchset["iteration"] = None
    norm_root = patchset["root_message_id"]
    sup = patchset.get("supersedes_root_message_id")
    if sup:
        row = db.execute("SELECT subject FROM patchsets "
                         "WHERE root_message_id=?", (sup,)).fetchone()
        supersedes_link = {"url": f"/patchsets/{quote(sup)}",
                           "subject": row["subject"] if row else sup}
        n, cur, seen = 1, sup, set()
        while cur and cur not in seen:
            seen.add(cur)
            row = db.execute(
                "SELECT supersedes_root_message_id FROM patchsets "
                "WHERE root_message_id=?", (cur,)).fetchone()
            n += 1
            cur = row["supersedes_root_message_id"] if row else None
        patchset["iteration"] = n
    # The STALE banner is viewer-relative under cooperative claiming:
    # several developers may each hang their own next iteration off this
    # shared series, and someone else's private upload is not THIS
    # viewer's successor. A gathered successor is a series fact and
    # shows for everyone.
    row = db.execute(
        "SELECT root_message_id, subject FROM patchsets q "
        "WHERE q.supersedes_root_message_id=? "
        "AND (q.origin=? "
        "  OR (q.origin=? AND q.uploaded_by_user_id=?) "
        "  OR EXISTS (SELECT 1 FROM patchset_claims c "
        "    WHERE c.root_message_id=q.root_message_id "
        "    AND c.user_id=?)) "
        "ORDER BY q.gathered_at DESC, q.root_message_id LIMIT 1",
        (norm_root, core_db.PATCHSET_ORIGIN_GATHERED,
         core_db.PATCHSET_ORIGIN_UPLOADED, viewer_user_id,
         viewer_user_id)).fetchone()
    if row:
        superseded_by = {"url": f"/patchsets/{quote(row['root_message_id'])}",
                         "subject": row["subject"] or row["root_message_id"]}

    tags = core_db.tags_for_patchset(db, root)
    metadata = core_db.get_patchset_metadata(db, root)
    # Read the thread once — both the thread render below and the inline-review
    # per-patch annotation draw from these raw bodies.
    raw_messages = list(core_db.messages_for_patchset(db, root))
    review_patches = []      # per-patch inline-annotated diffs (see below)
    series_concerns = []     # concerns not scoped to a single patch in-thread
    ai_review = core_db.get_ai_review(db, root)
    if ai_review is not None:
        # Surface the producing node's human handle next to the
        # review. The schema's FK on ai_reviews.node_id (and
        # delete_node's null-out-first behaviour) guarantee that
        # node_id is either NULL or resolves to an existing nodes
        # row — no dangling-id case to handle. A revoked node still
        # resolves; the review just gets attributed to the revoked
        # tombstone, which is correct.
        ai_review["reviewed_display"] = _when(ai_review["reviewed_at"])
        node = (core_db.get_node(db, ai_review["node_id"])
                if ai_review["node_id"] else None)
        ai_review["producer_label"] = (
            node.get("name") or f"id {node['id']}") if node else None
        # Build the inline-review view: each patch's complete diff with its
        # concerns injected at the line they sit on (anchored by
        # `spans_lines_in_diff`), under a per-patch finding-count header. A
        # concern is tied to a patch by its patch_scope (`kind: patch` + a
        # single Message-Id, normalised to match the stored id); series- and
        # cross-patch concerns — and any whose patch isn't in the thread —
        # collect in a series-wide block instead.
        patch_msgs = [m for m in raw_messages
                      if core_db.MSG_TYPE_NAMES.get(m["type"]) == "patch"]
        by_patch = {m["message_id"]: [] for m in patch_msgs}
        for c in ai_review["concerns"]:
            ps = c.get("patch_scope") or {}
            patches = ps.get("patches") or []
            tgt = (core_db.norm_msgid(patches[0])
                   if ps.get("kind") == "patch" and len(patches) == 1 else None)
            if tgt in by_patch:
                by_patch[tgt].append(c)
            else:
                series_concerns.append(_concern_view(c))
        for m in patch_msgs:
            cs = by_patch[m["message_id"]]
            rows, unanchored = _annotate_patch(m["body"] or "", cs)
            type_name = core_db.MSG_TYPE_NAMES.get(m["type"], "?")
            review_patches.append({
                "message_id":   m["message_id"],
                "type":         type_name,
                "type_badge":   _MSG_TYPE_BADGE.get(type_name, "text-bg-light"),
                "subject":      m["subject"],
                "part_index":   m["part_index"],
                "has_concerns": bool(cs),
                "counts":       _severity_counts(cs),
                "rows":         rows,
                "unanchored":   unanchored,
            })

    # Thread the display: cover and patches keep their flat series order,
    # while comments nest under the message they answer — gather stores
    # each mail's nearest In-Reply-To ancestor in parent_message_id, so a
    # reply to a review comment renders one level deeper than the comment
    # itself, immediately below it. A comment whose parent never made it
    # into the corpus (lost mail, off-thread reply) stays at top level in
    # plain sent order. `depth` drives the template's indent and is capped
    # so a pathological chain can't push the column off the page.
    replies = {}                 # parent message_id → [comment dicts]
    top_level = []
    in_thread = {m["message_id"] for m in raw_messages}
    for m in raw_messages:
        parent = m.get("parent_message_id")
        if (core_db.MSG_TYPE_NAMES.get(m["type"]) == "comment"
                and parent in in_thread and parent != m["message_id"]):
            replies.setdefault(parent, []).append(m)
        else:
            top_level.append(m)

    ordered, emitted = [], set()
    stack = [(m, 0) for m in reversed(top_level)]
    while stack:
        m, depth = stack.pop()
        ordered.append((m, depth))
        emitted.add(m["message_id"])
        stack += [(c, depth + 1) for c in reversed(replies.get(
            m["message_id"], [])) if c["message_id"] not in emitted]
    # A reply cycle (forged / mangled headers) leaves its members
    # unreachable from any top-level message — sweep them in flat rather
    # than dropping mail from the page.
    ordered += [(m, 0) for m in raw_messages
                if m["message_id"] not in emitted]

    messages = []
    for m, depth in ordered:
        type_name = core_db.MSG_TYPE_NAMES.get(m["type"], "?")
        rendered = patchview.render(m["body"] or "", is_patch=type_name == "patch")
        messages.append({
            "message_id":   m["message_id"],
            "type":         type_name,
            "type_badge":   _MSG_TYPE_BADGE.get(type_name, "text-bg-light"),
            "part_index":   m["part_index"],
            "subject":      m["subject"],
            "author":       m["author_name"] or m["author_email"] or "—",
            "sent_display": _when(m["sent"]),
            "depth":        min(depth, 8),
            "headers":      rendered.headers,
            "body_html":    rendered.body_html,
        })

    work_item_back_qs = "?back=" + quote(f"/patchsets/{root}", safe="")
    work_items = []
    has_review_item = False
    has_prepare_item = False
    latest = {}    # work-item type → (id, state) of the newest item
    owner_emails = _owner_email_map(db)
    for w in core_db.work_items_for_patchset(db, root):
        type_name  = core_db.WORK_ITEM_TYPE_NAMES.get(w["type"], "?")
        state_name = core_db.WORK_ITEM_STATE_NAMES.get(w["state"], "?")
        if w["type"] == core_db.WORK_ITEM_TYPE_REVIEW:
            has_review_item = True
        if w["type"] == core_db.WORK_ITEM_TYPE_PREPARE:
            has_prepare_item = True
        if w["id"] > latest.get(w["type"], (-1, None))[0]:
            latest[w["type"]] = (w["id"], w["state"])
        work_items.append({
            "id":            w["id"],
            "type":          type_name,
            "type_badge":    _TYPE_BADGE.get(type_name, "text-bg-light"),
            "state":         state_name,
            "state_badge":   _STATE_BADGE.get(state_name, "text-bg-light"),
            "claimed_by":    w["claimed_by"],
            "claimed_display":   _when(w["claimed_at"]),
            "completed_display": _when(w["completed_at"]),
            "enqueued_display":  _when(w["enqueued_at"]),
            "methodology_version": w["methodology_version"],
            "message_id":    w["message_id"],
            "session_role":  w["session_role"],
            "stratum_label": w["stratum_label"],
            "detail_url":    f"/work-items/{w['id']}{work_item_back_qs}",
            "requested_by_email": owner_emails.get(
                                     w.get("requested_by_user_id")),
        })

    # The Pipeline chip — the same derivation as /my-patchsets, fed from
    # the facts this page already loads (the latest prepare / review
    # work-item states come from the history walk above). A corpus
    # patchset's base state reads "gathered" rather than "uploaded".
    prep_id,  prep_state = latest.get(core_db.WORK_ITEM_TYPE_PREPARE,
                                      (None, None))
    rev_id,   rev_state  = latest.get(core_db.WORK_ITEM_TYPE_REVIEW,
                                      (None, None))
    label, badge = _upload_status(
        {"has_metadata":  metadata is not None,
         "has_ai_review": ai_review is not None,
         "prepare_state": prep_state,
         "review_state":  rev_state},
        base_label="uploaded" if patchset["is_uploaded"] else "gathered")
    pipeline = {"label": label, "badge": badge}

    # The pipeline action cluster: only what is actually executable in
    # the current state — the chip above carries the status, so there
    # are no dimmed placeholder buttons. At most one action renders.
    # `permission` is the visibility gate the template applies: "act"
    # for anyone _can_act_on_patchset allows (admin / maintainer / the
    # uploader of their own upload / a claimant), "curate" for the
    # destructive curation set (_can_curate_patchset — no claimants),
    # "admin" for operator-only work-item cancellation — mirroring what
    # the POST endpoints enforce. Cancels are
    # offered only while the item is UNHELD (claimable / deferred); an
    # in-flight claimed item keeps its lease, the same rule as the
    # work-item detail page.
    unheld = (core_db.WORK_ITEM_STATE_CLAIMABLE,
              core_db.WORK_ITEM_STATE_DEFERRED)
    pipeline_actions = []
    if metadata is None:
        if not has_prepare_item:
            # Prepare is normally pipeline-enqueued — this is the
            # recovery path when that didn't happen or it was cancelled.
            pipeline_actions.append({
                "kind": "request-prepare", "permission": "act",
                "url": f"/prepare-requests/{quote(root)}"})
        elif prep_state in unheld:
            pipeline_actions.append({
                "kind": "cancel-prepare", "permission": "admin",
                "url": f"/work-items/{prep_id}/cancel{work_item_back_qs}"})
    elif ai_review is None:
        if not has_review_item:
            # Review is operator-requested, never auto-enqueued (see
            # gather._ingest_ref) — prepare's metadata row gates it.
            pipeline_actions.append({
                "kind": "request-review", "permission": "act",
                "url": f"/review-requests/{quote(root)}"})
        elif rev_state in unheld:
            pipeline_actions.append({
                "kind": "cancel-review", "permission": "admin",
                "url": f"/work-items/{rev_id}/cancel{work_item_back_qs}"})
        elif rev_state == core_db.WORK_ITEM_STATE_UNAPPLIABLE:
            # Unappliable is terminal but can be stale — the tip-at-
            # submission base moves on, and node-side failures land
            # here too. Without this the badge is a dead end: enqueue_
            # review no-ops while the item exists, and the work-item
            # retry is admin-only. Same audience as request-review.
            pipeline_actions.append({
                "kind": "retry-review", "permission": "act",
                "url": f"/review-requests/{quote(root)}/retry"})
    else:
        # Deleting the shared review is curation, not a request — with
        # open cooperative claiming, claimants must not be able to wipe
        # a review others rely on (_can_curate_patchset).
        pipeline_actions.append({
            "kind": "delete-review", "permission": "curate",
            "url": f"/review-requests/{quote(root)}/delete"})

    return {
        "patchset":   patchset,
        "tags":       tags,
        "metadata":   metadata,
        "ai_review":  ai_review,
        "review_patches":  review_patches,
        "series_concerns": series_concerns,
        "messages":   messages,
        "work_items": work_items,
        "pipeline":   pipeline,
        "pipeline_actions": pipeline_actions,
        "supersedes_link": supersedes_link,
        "superseded_by":   superseded_by,
    }


_MSG_TYPE_BADGE = {"cover":   "text-bg-secondary",
                   "patch":   "text-bg-primary",
                   "comment": "text-bg-info"}


def _can_act_on_patchset(db, patchset, current_user):
    """The request-action rule (request prepare / review), shared by
       the POST handlers and the templates that show / hide their
       buttons — so enforcement and visibility can't drift. Maintainers
       and admins act on any patchset; a regular user on their own
       uploaded one (review is the upload's whole purpose) or on a
       gathered series they claimed — claiming is cooperative ("I'm
       working with this"), so any claimant may queue work, which runs
       on their own nodes and budget. Destructive curation is gated
       separately (_can_curate_patchset)."""
    if current_user is None:
        return False
    if current_user.is_config_admin or current_user.is_maintainer:
        return True
    ps = patchset or {}
    if current_user.id is None:
        return False
    if (ps.get("origin") == core_db.PATCHSET_ORIGIN_UPLOADED
            and ps.get("uploaded_by_user_id") == current_user.id):
        return True
    return core_db.user_has_claim(db, ps.get("root_message_id", ""),
                                  current_user.id)


def _can_curate_patchset(patchset, current_user):
    """The destructive-curation rule (delete review / delete patchset):
       maintainers, admins, or the uploader of their own upload. Mere
       claimants are excluded on purpose — with open cooperative
       claiming, any account could otherwise claim a corpus series and
       delete the shared review other people (and training) rely on."""
    if current_user is None:
        return False
    if current_user.is_config_admin or current_user.is_maintainer:
        return True
    ps = patchset or {}
    return (current_user.id is not None
            and ps.get("origin") == core_db.PATCHSET_ORIGIN_UPLOADED
            and ps.get("uploaded_by_user_id") == current_user.id)


@router.get("/patchsets/{root_message_id:path}", response_class=HTMLResponse)
async def patchset_detail(request: Request, root_message_id: str,
                           back: str | None = None,
                           current_user: auth.SessionUser = Depends(auth.require_session)):
    """The per-patchset detail page — drill down into a row from the queue
       (or any other index that links here). `?back=` carries the URL the
       opener wants the `← Back` button to return to; same-origin paths
       only, fallback to `/`."""
    db = request.app.state.db
    ctx = _patchset_view(db, root_message_id,
                         viewer_user_id=current_user.id)
    ps = ctx["patchset"]
    ctx["back_url"] = _safe_back(back)
    ctx["current_user"] = current_user
    ctx["can_act_on_patchset"] = _can_act_on_patchset(db, ps,
                                                      current_user)
    ctx["can_curate_patchset"] = _can_curate_patchset(ps, current_user)
    ctx["can_claim"] = _may_claim_patchset(db, ps, current_user)
    # The viewer sees their OWN association ("claimed by you"); the full
    # claimant list is maintainer / admin visibility — other developers'
    # dashboards are not each other's business.
    claimants = core_db.patchset_claimants(db, ps["root_message_id"])
    ctx["my_claim"] = (current_user.id is not None
                       and any(c["user_id"] == current_user.id
                               for c in claimants))
    ctx["claimants"] = (claimants if (current_user.is_config_admin
                                      or current_user.is_maintainer)
                        else [])
    ctx["can_revoke_claims"] = (bool(claimants)
                                and not ctx["my_claim"]
                                and (current_user.is_config_admin
                                     or current_user.is_maintainer))
    # The submitter-match flavor: pure presentation — the claim offer
    # itself is open to every account.
    ctx["submitter_matches"] = (
        (ps.get("submitter_email") or "").lower()
        == current_user.email.lower())
    ctx["claim_prior"] = None
    if ctx["can_claim"]:
        prior = _claim_prior(db, ps, current_user)
        if prior:
            ctx["claim_prior"] = {
                "url": f"/patchsets/{quote(prior['root_message_id'])}",
                "subject": prior["subject"] or prior["root_message_id"]}
    return templates.TemplateResponse(request, "patchset.html", ctx)


@router.post("/review-requests/{root_message_id:path}/delete", dependencies=[Depends(auth.require_csrf)])
async def delete_review(request: Request, root_message_id: str,
                         current_user: auth.SessionUser = Depends(auth.require_session)):
    """Operator-triggered deletion of a patchset's AI review and the review
       work-item(s) that produced it — the "Delete review" button on the
       detail page POSTs here. Reuses core_db.delete_review, which removes
       the ai_review, any review_evaluations referencing it, and the review
       work-items, so the manual trigger re-arms ("Request review" reappears
       once nothing review-related remains). 404 only when the patchset is
       unknown; a patchset with no review is a safe no-op. Redirect-after-
       POST back to the detail page.

       Registered before the catch-all request_review route below so this
       more specific `/delete` suffix wins — the `:path` converter there
       would otherwise greedily swallow `<root>/delete`.

       Gated by _can_curate_patchset — maintainers / admins, or the
       uploader for their own uploaded patchset. Claimants are excluded:
       with open cooperative claiming, the shared review of a corpus
       series must not be deletable by anyone who merely claimed it."""
    db = request.app.state.db
    ps = core_db.get_patchset(db, root_message_id)
    if ps is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no patchset with root_message_id "
                            f"{root_message_id!r}")
    if not _can_curate_patchset(ps, current_user):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "maintainer access required")
    core_db.delete_review(db, root_message_id)
    return RedirectResponse(f"/patchsets/{quote(root_message_id)}",
                             status_code=303)


@router.post("/review-requests/{root_message_id:path}/retry", dependencies=[Depends(auth.require_csrf)])
async def retry_review(request: Request, root_message_id: str,
                       current_user: auth.SessionUser = Depends(auth.require_session)):
    """Re-arm a patchset's UNAPPLIABLE review — the "Retry review"
       button on the detail page POSTs here. Unappliable can be stale
       (the tip-at-submission base moves on; a node-side failure
       fallback lands here too), and it is otherwise a dead end:
       enqueue_review no-ops while the work-item exists, and the
       work-item-page retry is admin-only. Gated by
       _can_act_on_patchset — the same audience as request_review —
       and core_db.retry_review re-stamps the item's origin to the
       retrier, so the re-run routes to THEIR nodes. Registered before
       the catch-all request_review route below, like `/delete`, so
       the `:path` converter doesn't swallow the `/retry` suffix.
       Redirect-after-POST back to the detail page."""
    db = request.app.state.db
    ps = core_db.get_patchset(db, root_message_id)
    if ps is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no patchset with root_message_id "
                            f"{root_message_id!r}")
    if not _can_act_on_patchset(db, ps, current_user):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "claim, upload or maintainer access required")
    core_db.retry_review(db, root_message_id,
                         requested_by_user_id=current_user.id)
    return RedirectResponse(f"/patchsets/{quote(root_message_id)}",
                            status_code=303)


@router.post("/patchsets/{root_message_id:path}/delete", dependencies=[Depends(auth.require_csrf)])
async def delete_patchset(request: Request, root_message_id: str,
                          current_user: auth.SessionUser = Depends(auth.require_session)):
    """Delete an UPLOADED patchset outright — the cleanup path for an
       abandoned upload iteration. Uploaded-origin only: gathered corpus
       rows are data, not submissions, and stay (re-gather couldn't
       restore a deleted one — gather's dedup never revisits a handled
       root, so a corpus delete would be silently permanent). Gated by
       _can_curate_patchset; core_db.delete_patchset removes the thread,
       work-items and derived artifacts, and splices any iteration chain
       through this row. Redirects to /my-patchsets — the page the
       deleted row lived on."""
    db = request.app.state.db
    ps = core_db.get_patchset(db, root_message_id)
    if ps is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no patchset with root_message_id "
                            f"{root_message_id!r}")
    if not _can_curate_patchset(ps, current_user):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "maintainer access required")
    if ps.get("origin") != core_db.PATCHSET_ORIGIN_UPLOADED:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "only uploaded patchsets can be deleted")
    core_db.delete_patchset(db, root_message_id)
    return RedirectResponse("/my-patchsets", status_code=303)


def _may_claim_patchset(db, patchset, current_user):
    """Claim eligibility, shared by the POST handler and the templates
       that offer the button. Claiming is cooperative — "I'm working
       with this series", not an authorship assertion — so ANY signed-in
       account may claim ANY gathered series it hasn't already claimed;
       many developers can hold claims on the same one, each seeing it
       blended into their own dashboard. (A submitter-address match is
       used elsewhere purely as a SUGGESTION signal, never a gate.)"""
    ps = patchset or {}
    return (current_user is not None and current_user.id is not None
            and ps.get("origin") == core_db.PATCHSET_ORIGIN_GATHERED
            and not core_db.user_has_claim(
                db, ps.get("root_message_id", ""), current_user.id))


def _claim_prior(db, patchset, current_user):
    """The chain head this claim looks like a new iteration of, or None
       — find_prior_iteration over the claimant's own heads (uploads and
       claimed series), the row being claimed excluded. Shared by the
       POST handler and the pages that render the link checkbox."""
    cands = [c for c in core_db.unsuperseded_user_series(db,
                                                         current_user.id)
             if c["root_message_id"] != patchset["root_message_id"]]
    return upload.find_prior_iteration(
        cands, subject=patchset.get("subject") or "",
        change_id=patchset.get("change_id"))


@router.post("/patchsets/{root_message_id:path}/claim", dependencies=[Depends(auth.require_csrf)])
async def claim_patchset(request: Request, root_message_id: str,
                         current_user: auth.SessionUser = Depends(auth.require_session)):
    """Claim a gathered series onto the signed-in developer's dashboard
       — the bridge from "my series is already on lore" to the pipeline
       actions. Adds a (series, user) association only: origin stays
       GATHERED, the stored bodies are untouched, and other developers'
       claims are unaffected (claiming is cooperative — any number of
       accounts may work with the same series). Re-claiming what you
       already hold is an idempotent no-op redirect. Registered before
       the wildcard-less routes for the same reason as /delete above."""
    db = request.app.state.db
    ps = core_db.get_patchset(db, root_message_id)
    if ps is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no patchset with root_message_id "
                            f"{root_message_id!r}")
    if ps.get("origin") != core_db.PATCHSET_ORIGIN_GATHERED \
            or current_user.id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "only gathered series can be claimed")
    # Claim-time iteration linking — the claim doorways offer it as a
    # pre-checked opt-out when one of the claimant's chain heads matches
    # (same heuristic, same consent rule as upload confirm). The prior
    # is recomputed here rather than trusted from the form: the
    # candidate set is heads-only at POST time, so a raced re-link
    # can't fork a chain.
    supersedes = None
    form = await request.form()
    if form.get("link_iteration"):
        prior = _claim_prior(db, ps, current_user)
        supersedes = prior["root_message_id"] if prior else None
    core_db.claim_patchset(db, root_message_id, current_user.id,
                           supersedes=supersedes)
    if supersedes:
        # The new iteration replaces the old one's pending work — same
        # retirement as upload-confirm linking; in-flight claimed work
        # finishes and lands as that iteration's history.
        core_db.cancel_unheld_pipeline_items(db, supersedes)
    return RedirectResponse(f"/patchsets/{quote(root_message_id)}",
                            status_code=303)


@router.post("/patchsets/{root_message_id:path}/unclaim", dependencies=[Depends(auth.require_csrf)])
async def unclaim_patchset(request: Request, root_message_id: str,
                           current_user: auth.SessionUser = Depends(auth.require_session)):
    """Release a claim — the claimant's own undo (their claim only), or
       a maintainer / admin revoke, which clears EVERY claim on the
       series (the cleanup escape hatch)."""
    db = request.app.state.db
    ps = core_db.get_patchset(db, root_message_id)
    if ps is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no patchset with root_message_id "
                            f"{root_message_id!r}")
    if (current_user.id is not None
            and core_db.user_has_claim(db, root_message_id,
                                       current_user.id)):
        core_db.unclaim_patchset(db, root_message_id, current_user.id)
    elif current_user.is_config_admin or current_user.is_maintainer:
        core_db.unclaim_patchset(db, root_message_id)
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "only a claimant or a maintainer can "
                            "release claims")
    return RedirectResponse(f"/patchsets/{quote(root_message_id)}",
                            status_code=303)


@router.post("/review-requests/{root_message_id:path}", dependencies=[Depends(auth.require_csrf)])
async def request_review(request: Request, root_message_id: str,
                          current_user: auth.SessionUser = Depends(auth.require_session)):
    """Operator-triggered review enqueue. Review is not auto-enqueued
       (a gather run would flood the queue); the operator requests one
       patchset at a time here. Reuses core_db.maybe_enqueue_review — the
       same prepare-gated, idempotent enqueue the pipeline used to call —
       so a double-click or a patchset that already has a review is a safe
       no-op. Redirect-after-POST back to the detail page (the button
       dims once the review item exists).

       Gated by _can_act_on_patchset: maintainers / admins, or the
       uploader for their own uploaded patchset (whose review normally
       auto-chains — this is their re-request path after a delete)."""
    db = request.app.state.db
    # maybe_enqueue_review returns None (not KeyError) for an unknown or
    # not-yet-gathered patchset, so check existence explicitly to 404.
    ps = core_db.get_patchset(db, root_message_id)
    if ps is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no patchset with root_message_id "
                            f"{root_message_id!r}")
    if not _can_act_on_patchset(db, ps, current_user):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "maintainer access required")
    # The user who clicked the button owns the resulting work item; this
    # routes it onto their own nodes' user queue. The config-token admin
    # has id=None — admin-triggered reviews are system items.
    core_db.maybe_enqueue_review(db, root_message_id,
                                  requested_by_user_id=current_user.id)
    return RedirectResponse(f"/patchsets/{quote(root_message_id)}",
                             status_code=303)


@router.post("/prepare-requests/{root_message_id:path}", dependencies=[Depends(auth.require_csrf)])
async def request_prepare(request: Request, root_message_id: str,
                           current_user: auth.SessionUser = Depends(auth.require_session)):
    """Operator-triggered prepare enqueue — request_review's companion
       for a patchset whose prepare never ran (or whose work-item was
       cancelled before completing). Reuses core_db.maybe_enqueue_prepare,
       the same gathered-gated, idempotent enqueue the pipeline calls, so
       a double-click or an already-prepared patchset is a safe no-op.
       Redirect-after-POST back to the detail page (the button dims once
       the prepare item exists).

       Gated by _can_act_on_patchset, same as request_review."""
    db = request.app.state.db
    ps = core_db.get_patchset(db, root_message_id)
    if ps is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no patchset with root_message_id "
                            f"{root_message_id!r}")
    if not _can_act_on_patchset(db, ps, current_user):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "maintainer access required")
    core_db.maybe_enqueue_prepare(db, root_message_id,
                                   requested_by_user_id=current_user.id)
    return RedirectResponse(f"/patchsets/{quote(root_message_id)}",
                             status_code=303)


# --- node health ----------------------------------------------------------

def _mb_display(n):
    """Render an MiB integer as a compact `123 MB` or `4.5 GB` string,
       or `—` when the value is missing (None — the node hasn't
       reported yet, or the volume isn't mounted)."""
    if n is None:
        return "—"
    if n >= 1024:
        return f"{n / 1024:.1f} GB"
    return f"{n} MB"


_ANTHROPIC_ERROR_LABELS = {
    "auth":        "auth key rejected",
    "rate_limit":  "rate-limited",
    "connection":  "API unreachable",
    "other":       "other API error",
}


def _token_budget_display(tb):
    """The node's token-budget usage as a "% of cap spent" string, e.g.
       "day 24% · week 61%". Only enabled windows (limit > 0) appear —
       the budget is opt-in on the node, so None (nothing to render)
       covers unconfigured nodes, pre-field snapshots, and malformed
       data alike. Deliberately NOT clamped at 100%: the node enforces
       between tasks only, so a window can legitimately read 104%.
       `exhausted` carries the node's own verdict ("daily" / "weekly")
       so the template can mark a budget-paused node."""
    if not isinstance(tb, dict):
        return None
    parts = []
    for label, tokens_key, limit_key in (("day", "day_tokens", "day_limit"),
                                         ("week", "week_tokens",
                                          "week_limit")):
        try:
            limit = int(tb.get(limit_key) or 0)
            tokens = int(tb.get(tokens_key) or 0)
        except (TypeError, ValueError):
            continue
        if limit > 0:
            parts.append(f"{label} {round(tokens * 100 / limit)}%")
    if not parts:
        return None
    return {"text": " · ".join(parts), "exhausted": tb.get("exhausted")}


def _count_display(n):
    """Render an integer count (objects, refs) with thousands separators,
       or `—` when it's missing or non-numeric (a None objects_added when
       the node's count-objects call failed, a pre-field snapshot)."""
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        return "—"
    return f"{int(n):,}"


def _ms_display(ms):
    """Render a millisecond duration as `820 ms` or `3.4 s`, or `—` when
       missing."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return "—"
    if ms >= 1000:
        return f"{ms / 1000:.1f} s"
    return f"{int(ms)} ms"


def _refrepo_health_display(health):
    """The reference-repo churn signals — ancestry anchors, the last by-SHA
       base fetch, the last full tree fetch (resolve_tip), the last gc —
       surfaced so an operator can see whether `gc --prune=now` keeps base
       fetches delta-cheap (anchors > 0, a fetch adds a few thousand
       objects) or has collapsed the shared history (anchors 0, a fetch
       re-pulls millions), and how heavy the churn-driving tree fetch is.
       Returns None when the snapshot predates this instrumentation, so the
       template adds no rows for an older node. Each sub-field is
       independently optional: a node that has fetched but not yet gc'd this
       process (or just restarted) shows what it has and omits the rest."""
    if not isinstance(health, dict):
        return None
    anchors = health.get("refrepo_tracking_refs")
    fetch = health.get("refrepo_fetch")
    resolve = health.get("refrepo_resolve")
    gc = health.get("refrepo_gc")
    if (anchors is None and fetch is None and resolve is None
            and gc is None):
        return None
    out = {}
    if anchors is not None:
        out["anchors"] = _count_display(anchors)
    if isinstance(fetch, dict):
        remote = fetch.get("remote") or "?"
        out["fetch"] = (f"+{_count_display(fetch.get('objects_added'))} objs "
                        f"from {remote} · {_ms_display(fetch.get('ms'))}")
    if isinstance(resolve, dict):
        tree = resolve.get("tree") or "?"
        out["resolve"] = (f"+{_count_display(resolve.get('objects_added'))} "
                          f"objs from {tree} · "
                          f"{_ms_display(resolve.get('ms'))}")
    if isinstance(gc, dict):
        # `fetches` is absent on pre-field snapshots; only append it when
        # present so an older node's gc row stays clean.
        fetches = gc.get("fetches")
        reclaimed = (f"{_mb_display(gc.get('size_mb_before'))} → "
                     f"{_mb_display(gc.get('size_mb_after'))} · "
                     f"{_ms_display(gc.get('ms'))} · "
                     f"{_count_display(gc.get('tracking_refs'))} anchors")
        if fetches is not None:
            reclaimed += f" · {_count_display(fetches)} fetches"
        out["gc"] = reclaimed
        out["gc_ok"] = gc.get("ok")
    return out or None


def _health_display(health):
    """Turn a stored node-health JSON snapshot into the small dict the
       template renders. Returns None when the node hasn't reported
       yet, so the template shows a single em-dash rather than
       three. Defensive against an old / partial snapshot (a node
       version that reported fewer fields than this hone-core
       version expects)."""
    if not isinstance(health, dict) or not health:
        return None
    err = health.get("last_anthropic_error")
    return {
        "free_disk":      _mb_display(health.get("free_disk_mb")),
        "repo_size":      _mb_display(health.get("refrepo_size_mb")),
        "error":          _ANTHROPIC_ERROR_LABELS.get(err, err) if err else None,
        # The node's `claude --version` string from its latest health
        # snapshot — None for sdk-backend nodes or pre-field snapshots.
        "claude_version": health.get("claude_version"),
        # %-of-cap spent per enabled budget window — None when the node
        # has no token budget configured (it's opt-in).
        "token_budget":   _token_budget_display(health.get("token_budget")),
        # Reference-repo churn signals (anchors / last fetch / last gc) —
        # None for snapshots predating the refrepo instrumentation.
        "refrepo":        _refrepo_health_display(health),
    }


# --- node management -------------------------------------------------------

def _relative_duration(seconds):
    """Compact duration string for a `now - timestamp` interval. Used
       by the nodes-table freshness and running-time columns where
       operators want a glanceable "how long" rather than a precise
       UTC string. Returns "—" for None / 0 so empty cells read as
       blank rather than `0s`."""
    if seconds is None or seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60):02d}m"
    return f"{int(seconds // 86400)}d {int((seconds % 86400) // 3600):02d}h"


# Bucket order on the /nodes page — loudest first, with idle
# collapsed at the bottom. Mirrors the fleet-pulse chip's
# loudest-wins logic so operators see the same priority across
# both surfaces.
_NODE_BUCKETS = (("errored",   "Errored"),
                  ("stale",     "Stale"),
                  ("in_flight", "In flight"),
                  ("idle",      "Idle"))


def _node_status_fields(node, claim, runtime_cfg, *, now=None, back_qs=""):
    """Compute the live-status fields shared by the /nodes bucketed
       table and the /nodes/{id} detail page — bucket assignment,
       relative freshness, running time, and the claim-link triple.
       Centralised so the two surfaces can't drift (an operator who
       sees `In flight` on the index expects the same label on the
       drill-down)."""
    now = int(time.time()) if now is None else now
    stale_after = (runtime_cfg.heartbeat_seconds
                    * _FLEET_STALE_HEARTBEAT_MULT)
    health = node.get("health") or {}
    anth_err = (health.get("last_anthropic_error")
                 if isinstance(health, dict) else None)
    last_seen = node.get("last_seen") or 0
    health_at = node.get("health_at") or 0
    # A claim whose lease has lapsed is no longer this node's work —
    # the node went silent past the whole lease window and the row will
    # be re-offered. It must not read as "in flight" (nor show a
    # forever-growing Running timer); the claim stays attached, flagged,
    # so the row can say "lease expired" instead of pretending.
    claim_expired = bool(claim and claim.get("lease_expires")
                         and claim["lease_expires"] <= now)
    if anth_err:
        bucket = "errored"
    elif last_seen and (now - last_seen) > stale_after:
        bucket = "stale"
    elif claim and not claim_expired:
        bucket = "in_flight"
    else:
        bucket = "idle"
    freshness = (now - last_seen) if last_seen else None
    health_age = (now - health_at) if health_at else None
    running = (now - claim["claimed_at"]
                if claim and claim["claimed_at"] and not claim_expired
                else None)
    return {
        "bucket":               bucket,
        "bucket_label":         dict(_NODE_BUCKETS).get(bucket, bucket),
        "freshness_display":    _relative_duration(freshness),
        "last_seen_tooltip":    (_when_text(last_seen)       # title attr
                                 if last_seen else ""),
        "health_age_display":   _relative_duration(health_age),
        "running_time_display": _relative_duration(running),
        "claim_expired":        claim_expired,
        "claim":                claim,
        "claim_subject":        (claim["subject"] or claim["root_message_id"]
                                  if claim else None),
        "claim_type":           (core_db.WORK_ITEM_TYPE_NAMES.get(
                                       claim["type"], "?")
                                  if claim else None),
        "claim_url":            (f"/work-items/{claim['id']}{back_qs}"
                                  if claim else None),
    }


# Bucket → Bootstrap badge class. Shared by the /nodes index and the
# /nodes/{id} detail card so the loud signal looks the same in both
# places.
_NODE_BUCKET_BADGE = {"errored":   "text-bg-danger",
                       "stale":     "text-bg-warning",
                       "in_flight": "text-bg-success",
                       "idle":      "text-bg-secondary"}


def _owner_email_map(db):
    """A {user_id: email} index for decorating node rows with the
       owner's email. Cheap (one scan of users), and the per-row lookup
       is a dict access."""
    return {u["id"]: u["email"] for u in core_db.list_users(db)}


def _can_manage_node(node, current_user):
    """True when `current_user` may mutate this node — its owner or the
       config-token admin. Used as the `is_owner_or_admin` flag in node
       view-models and as the gate inside mutation handlers."""
    if current_user is None:
        return False
    if current_user.is_config_admin:
        return True
    return node.get("owner_user_id") == current_user.id


def _nodes_view(db, runtime_cfg, current_user=None):
    """The view-model the /nodes table partial renders: ONE table with
       every active node, ordered loudest-first (errored → stale →
       in-flight → idle, last_seen DESC within each class) — the same
       loudest-wins rule the fleet-pulse chip uses, expressed as row
       order plus a per-row status badge rather than separate bucket
       tables, so no node (idle ones included) is ever hidden or
       collapsed.

       Every approved node shows up regardless of who's viewing — the
       per-row `is_owner_or_admin` flag is what gates the action
       controls in the template."""
    now = int(time.time())
    stale_after = (runtime_cfg.heartbeat_seconds
                    * _FLEET_STALE_HEARTBEAT_MULT)
    back_qs = "?back=" + quote("/nodes", safe="")
    owner_emails = _owner_email_map(db)

    # Build a name → in-flight claim index so we can attach the
    # claim to the right node row in a single pass below. One query,
    # bounded by the number of CLAIMED work items.
    claim_by_worker = {}
    for w in core_db.list_work_items(
            db, state=core_db.WORK_ITEM_STATE_CLAIMED, limit=10_000):
        if w["claimed_by"]:
            claim_by_worker.setdefault(w["claimed_by"], w)

    buckets = {k: [] for k, _ in _NODE_BUCKETS}
    for n in core_db.list_nodes(db):
        n = dict(n)
        if n["state"] != core_db.NODE_STATE_ACTIVE:
            continue                                  # revoked → hidden
        claim = claim_by_worker.get(n.get("name"))
        status = _node_status_fields(n, claim, runtime_cfg,
                                       now=now, back_qs=back_qs)
        n.update(status)
        n.update({
            "task_types_display": _types(n.get("task_types")),
            "state_display":      core_db.NODE_STATE_NAMES.get(
                                      n["state"], "?"),
            "bucket_badge":       _NODE_BUCKET_BADGE.get(
                                      status["bucket"], "text-bg-secondary"),
            "health_display":     _health_display(n.get("health")),
            "detail_url":         f"/nodes/{n['id']}{back_qs}",
            "owner_email":        owner_emails.get(n.get("owner_user_id")),
            "handles_system":     bool(n.get("handles_system", 1)),
            "is_owner_or_admin":  _can_manage_node(n, current_user),
        })
        buckets[status["bucket"]].append(n)

    # Sort within each status class — most-recently-seen first — then
    # flatten loudest-first into the single row list the table renders.
    for key in buckets:
        buckets[key].sort(key=lambda r: r.get("last_seen") or 0,
                           reverse=True)
    rows = [r for k, _ in _NODE_BUCKETS for r in buckets[k]]
    return {"rows":  rows,
             "total": len(rows),
             "node_state_active": core_db.NODE_STATE_ACTIVE}


@router.get("/nodes", response_class=HTMLResponse)
async def nodes(request: Request,
                current_user: auth.SessionUser = Depends(auth.require_session)):
    """The node fleet: the pending-enrollment queue and the enrolled
       nodes, sorted into health buckets (errored / stale / in-flight
       / idle). The bucketed table partial polls /nodes/fleet-table
       every 10s so an operator sees rows move between buckets and
       running-time tick without reloading.

       Pending enrollments are scoped: a regular user sees only the
       enrollments they tagged via /enroll (the lookup-er); the
       config-token admin sees every pending row. Approved nodes are
       global — every viewer sees every node, owner-specific controls
       just render conditionally."""
    db = request.app.state.db
    scope_user_id = (None if current_user.is_config_admin
                     else current_user.id)
    pending = []
    for e in core_db.list_pending_enrollments(
            db, requested_by_user_id=scope_user_id):
        e = dict(e)
        e["task_types_display"] = _types(e.get("task_types"))
        e["requested_display"]  = _when(e.get("created_at"))
        pending.append(e)
    ctx = {"pending": pending, "current_user": current_user,
            **_nodes_view(db, request.app.state.runtime_config,
                          current_user=current_user)}
    return templates.TemplateResponse(request, "nodes.html", ctx)


@router.get("/nodes/fleet-table", response_class=HTMLResponse)
async def nodes_fleet_table(request: Request,
                              current_user: auth.SessionUser = Depends(auth.require_session)):
    """The bucketed enrolled-nodes table as an HTML partial — polled
       by the /nodes page every 10s. Skips the pending-enrollment
       table (static between approve/deny clicks; an unnecessary
       re-render would steal focus from any Approve button mid-
       hover)."""
    return templates.TemplateResponse(
        request, "_nodes_fleet_table.html",
        {"current_user": current_user,
         **_nodes_view(request.app.state.db,
                       request.app.state.runtime_config,
                       current_user=current_user)})


# --- per-node detail ------------------------------------------------------

_WORK_TYPE_DISPLAY = core_db.WORK_ITEM_TYPE_NAMES
_WORK_STATE_DISPLAY = core_db.WORK_ITEM_STATE_NAMES


def _node_claimed_by_label(node):
    """The string the api layer writes into work_items.claimed_by for a
       node — the node's name when set, str(node.id) otherwise (see
       api.claim_task). Centralised so the detail page's per-node
       query matches what the claim path wrote."""
    return node.get("name") or str(node["id"])


def _node_live_panel_view(db, node_id, runtime_cfg, *, current_user=None):
    """Live-status fields the detail page's Node + Health cards render.
       Same bucket / running-time / freshness shape the /nodes table
       uses (via _node_status_fields), plus the bucket badge class
       and a pre-computed claim-link triple for the in-flight card.
       Returns None when the node is gone — the polling endpoint
       reads that as a 404.

       Decorates the row with `owner_email` and `is_owner_or_admin`
       so the template can render the ownership line and gate the
       per-node action controls. When `current_user` is None the
       flag is conservatively False."""
    node = core_db.get_node(db, node_id)
    if node is None:
        return None
    node_back_qs = f"?back={quote(f'/nodes/{node_id}', safe='')}"
    claim = None
    if node.get("name"):
        for w in core_db.work_items_for_node(db, node["name"], limit=10):
            if w["state"] == core_db.WORK_ITEM_STATE_CLAIMED:
                claim = w
                break
    status = _node_status_fields(node, claim, runtime_cfg,
                                   back_qs=node_back_qs)
    node.update(status)
    node["task_types_display"] = _types(node.get("task_types"))
    node["state_display"]      = core_db.NODE_STATE_NAMES.get(
                                     node["state"], "?")
    node["enrolled_display"]   = _when(node.get("enrolled_at"))
    node["last_seen_display"]  = _when(node.get("last_seen"))
    node["health_display"]     = _health_display(node.get("health"))
    node["health_at_display"]  = _when(node.get("health_at"))
    node["bucket_badge"]       = _NODE_BUCKET_BADGE.get(
                                     status["bucket"], "text-bg-secondary")
    owner_email = None
    if node.get("owner_user_id") is not None:
        owner_row = core_db.get_user_by_id(db, node["owner_user_id"])
        if owner_row is not None:
            owner_email = owner_row["email"]
    node["owner_email"]        = owner_email
    node["handles_system"]     = bool(node.get("handles_system", 1))
    node["is_owner_or_admin"]  = _can_manage_node(node, current_user)
    return {"node":              node,
             "node_state_active": core_db.NODE_STATE_ACTIVE}


def _claims_url(node_id, *, page=None, size=None, back=None):
    """Build a `/nodes/{id}?...` URL preserving the Recent-claims
       paging params (and the opener's `back`, so the ← Back button
       still works after paginating). Drops page when 1 and size
       when default to keep the bookmark/URL clean."""
    parts = []
    if page and page > 1:
        parts.append(f"claims_page={page}")
    if size and size != _DEFAULT_CLAIMS_PAGE_SIZE:
        parts.append(f"claims_size={size}")
    if back:
        parts.append(f"back={quote(back, safe='')}")
    qs = ("?" + "&".join(parts)) if parts else ""
    return f"/nodes/{node_id}{qs}"


def _node_detail_view(db, node_id, runtime_cfg, *,
                       claims_page=1, claims_size=_DEFAULT_CLAIMS_PAGE_SIZE,
                       back=None, current_user=None):
    """Build the per-node detail render context — node row + health
       snapshot + recent claims (paged) + recent reviews. Raises 404
       when the node is unknown (revoked tombstones are still
       resolvable; only a hard-deleted node 404s).

       The dynamic top region (Node + Health cards with bucket
       badge, running time, relative freshness) comes from
       _node_live_panel_view so the /nodes/{id}/live polling
       endpoint and the initial page render share one source.

       Recent claims paginate via ?claims_page / ?claims_size
       (defaults 1 / 10). The paginator uses full-page navigation
       — the claims history isn't live like the cards above it, so
       there's no HTMX swap target to thread through."""
    panel = _node_live_panel_view(db, node_id, runtime_cfg,
                                    current_user=current_user)
    if panel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no node with id {node_id}")
    node = panel["node"]
    worker = _node_claimed_by_label(node)
    node_back_qs = f"?back={quote(f'/nodes/{node_id}', safe='')}"

    # Paginate Recent claims. Clamp size to the allowed set, page to
    # [1, pages]. Mirrors _queue_view's paging math.
    size = claims_size if claims_size in _CLAIMS_PAGE_SIZES \
        else _DEFAULT_CLAIMS_PAGE_SIZE
    total = core_db.count_work_items_for_node(db, worker)
    pages = max(1, -(-total // size))
    page = max(1, min(claims_page or 1, pages))
    offset = (page - 1) * size

    claims = []
    for w in core_db.work_items_for_node(db, worker, limit=size,
                                           offset=offset):
        type_name  = _WORK_TYPE_DISPLAY.get(w["type"], "?")
        state_name = _WORK_STATE_DISPLAY.get(w["state"], "?")
        claims.append({
            "id":               w["id"],
            "type":             type_name,
            "type_badge":       _TYPE_BADGE.get(type_name, "text-bg-light"),
            "state":            state_name,
            "state_badge":      _STATE_BADGE.get(state_name,
                                                  "text-bg-light"),
            "subject":          w["subject"] or w["root_message_id"],
            "root_message_id":  w["root_message_id"],
            "claimed_display":   _when(w["claimed_at"]),
            "completed_display": _when(w["completed_at"]),
            "work_item_url":    f"/work-items/{w['id']}{node_back_qs}",
            "patchset_url":     f"/patchsets/{quote(w['root_message_id'])}"
                                + node_back_qs,
        })

    def _u(p):
        return _claims_url(node_id, page=p, size=size, back=back)
    window = _page_window(page, pages)
    claims_paging = {
        "page":         page,
        "pages":        pages,
        "size":         size,
        "total":        total,
        "start":        offset + 1 if claims else 0,
        "end":          offset + len(claims),
        "size_options": [{"value": s,
                           "url": _claims_url(node_id, size=s, back=back)}
                          for s in _CLAIMS_PAGE_SIZES],
        "first_url":    _u(1),
        "prev_url":     _u(max(1, page - 1)),
        "next_url":     _u(min(pages, page + 1)),
        "last_url":     _u(pages),
        "has_prev":     page > 1,
        "has_next":     page < pages,
        "window":       [{"page": p, "url": _u(p), "active": p == page}
                         for p in window],
        "show_first_ellipsis": bool(window) and window[0] != 1,
        "show_last_ellipsis":  bool(window) and window[-1] != pages,
    }

    reviews = []
    for r in core_db.ai_reviews_for_node(db, node_id):
        reviews.append({
            "id":               r["id"],
            "root_message_id":  r["root_message_id"],
            "subject":          r["subject"] or r["root_message_id"],
            "model":            r["model"],
            "concerns_count":   len(r["concerns"]),
            "recorded_display": _when(r["recorded_at"]),
            "patchset_url":     f"/patchsets/{quote(r['root_message_id'])}"
                                + f"?back={quote(f'/nodes/{node_id}', safe='')}",
        })

    return {
        "node":    node,
        "claims":  claims,
        "claims_paging": claims_paging,
        "reviews": reviews,
        "node_state_active": core_db.NODE_STATE_ACTIVE,
    }


@router.get("/nodes/{node_id:int}", response_class=HTMLResponse)
async def node_detail(request: Request, node_id: int,
                       back: str | None = None,
                       claims_page: int = 1,
                       claims_size: int = _DEFAULT_CLAIMS_PAGE_SIZE,
                       current_user: auth.SessionUser = Depends(auth.require_session)):
    """The per-node detail page — drill down into a row from /nodes
       (or any other index that links here). `?back=` carries the URL
       the opener wants the ← Back button to return to; same-origin
       paths only via _safe_back, default `/nodes`. `?claims_page` /
       `?claims_size` page the Recent-claims table."""
    safe_back = _safe_back(back) if back else None
    ctx = _node_detail_view(request.app.state.db, node_id,
                             request.app.state.runtime_config,
                             claims_page=claims_page, claims_size=claims_size,
                             back=safe_back, current_user=current_user)
    ctx["back_url"] = safe_back or "/nodes"
    ctx["current_user"] = current_user
    return templates.TemplateResponse(request, "node_detail.html", ctx)


@router.get("/nodes/{node_id:int}/live", response_class=HTMLResponse)
async def node_detail_live_panel(request: Request, node_id: int,
                                  current_user: auth.SessionUser = Depends(auth.require_session)):
    """The live status panel for the per-node detail page — Node +
       Health cards with bucket badge, running time, and relative
       freshness. Polled every 10s by HTMX so an operator can leave
       the page open and watch the in-flight claim progress without
       reloading. The recent-claims and reviews tables below don't
       refresh on this poll — they're history, full reload is fine."""
    panel = _node_live_panel_view(request.app.state.db, node_id,
                                    request.app.state.runtime_config,
                                    current_user=current_user)
    if panel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no node with id {node_id}")
    return templates.TemplateResponse(
        request, "_node_live_panel.html", panel)


# --- per-work-item detail ------------------------------------------------

def _work_item_view(db, work_item_id):
    """Build the per-work-item detail render context. Raises 404 on
       an unknown id. Resolves the patchset (subject) and the claiming
       node (if claimed_by names a current node) so the template can
       render cross-links — same `?back=` round-trip the patchset and
       node detail pages use."""
    w = core_db.get_work_item(db, work_item_id)
    if w is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no work-item with id {work_item_id}")
    type_name  = core_db.WORK_ITEM_TYPE_NAMES.get(w["type"], "?")
    state_name = core_db.WORK_ITEM_STATE_NAMES.get(w["state"], "?")

    patchset = core_db.get_patchset(db, w["root_message_id"])
    patchset_subject = patchset["subject"] if patchset else w["root_message_id"]

    # claimed_by is the human label (node name when set, str(id) otherwise);
    # try to resolve back to a node row so the template can link. Falls
    # through to None for an unnamed-id label, a deleted node, or
    # null claimed_by.
    claiming_node = None
    if w.get("claimed_by"):
        row = db.execute(
            "SELECT id, name, state FROM nodes WHERE name=? OR id=?",
            (w["claimed_by"],
             int(w["claimed_by"]) if w["claimed_by"].isdigit() else -1)
        ).fetchone()
        if row is not None:
            claiming_node = dict(row)

    record = w.get("record") or {}
    # The interesting `meta.*` fields surfaced explicitly in the
    # template so an operator scanning a failed record doesn't have
    # to expand the collapsible JSON to see the schema reason or
    # raw response text.
    meta = (record.get("meta") or {}) if isinstance(record, dict) else {}

    # URL-encoded back-link for cross-links into other detail pages
    # (patchset, node). Threaded once into the context so the template
    # doesn't have to know about quote() escaping.
    self_back_qs = "?back=" + quote(f"/work-items/{w['id']}", safe="")
    patchset_url = (f"/patchsets/{quote(w['root_message_id'])}"
                    + self_back_qs)
    node_url = (f"/nodes/{claiming_node['id']}{self_back_qs}"
                if claiming_node else None)

    return {
        "work_item":         w,
        "id":                w["id"],
        "type":              type_name,
        "type_badge":        _TYPE_BADGE.get(type_name, "text-bg-light"),
        "state":             state_name,
        "state_badge":       _STATE_BADGE.get(state_name, "text-bg-light"),
        # Deferral retry bookkeeping: ×N on the badge; "parked" once the
        # row hit DEFER_CAP (lease_expires NULL — never re-offered until
        # an admin releases it, which resets the budget).
        "defer_count":       w.get("defer_count") or 0,
        "defer_parked":      (w["state"] == core_db.WORK_ITEM_STATE_DEFERRED
                              and w.get("lease_expires") is None
                              and (w.get("defer_count") or 0) > 0),
        "root_message_id":   w["root_message_id"],
        "patchset_subject":  patchset_subject,
        "patchset_url":      patchset_url,
        "node_url":          node_url,
        # Origin: the requesting user's email for a USER item, None for
        # a SYSTEM one — the same attribution the queue's Origin column
        # shows.
        "origin_email":      _owner_email_map(db).get(
                                 w.get("requested_by_user_id")),
        "claiming_node":     claiming_node,
        "claimed_at_display":   _when(w["claimed_at"]),
        "completed_at_display": _when(w["completed_at"]),
        "enqueued_at_display":  _when(w["enqueued_at"]),
        "lease_expires_display": _when(w["lease_expires"]),
        "heartbeat_at_display": _when(w["heartbeat_at"]),
        "record":            record if isinstance(record, dict) else None,
        "record_json":       json.dumps(record, indent=2)
                              if isinstance(record, dict) else None,
        # A record on a non-terminal row is the SUPERSEDED attempt's: a
        # retry/release re-armed the item but deliberately keeps the old
        # record visible (it's the only surviving evidence of why the
        # attempt failed) until the re-run's submission overwrites it.
        # The template labels it so it doesn't read as a current result.
        "record_superseded": bool(record) and w["state"] in (
                                  core_db.WORK_ITEM_STATE_CLAIMABLE,
                                  core_db.WORK_ITEM_STATE_CLAIMED),
        "meta_schema_error":  meta.get("schema_error"),
        "meta_claude_cli":    meta.get("claude_cli_version"),
        "meta_raw_response":  meta.get("raw_response"),
        "meta_raw_truncated": meta.get("raw_response_truncated"),
        # The captured Claude turn (node/ai.py → meta.trace): assistant text,
        # tool uses, tool results — rendered as the "Agent messages" timeline.
        "meta_trace":         meta.get("trace") or [],
    }


@router.get("/work-items/{work_item_id:int}", response_class=HTMLResponse)
async def work_item_detail(request: Request, work_item_id: int,
                            back: str | None = None,
                            current_user: auth.SessionUser = Depends(auth.require_session)):
    """The per-work-item detail page — every queue / patchset-history /
       node-claims table links here. `?back=` carries the opener's
       URL so the operator returns where they were; same-origin
       paths only via _safe_back, default `/`."""
    ctx = _work_item_view(request.app.state.db, work_item_id)
    ctx["back_url"] = _safe_back(back) if back else "/"
    ctx["current_user"] = current_user
    return templates.TemplateResponse(request, "work_item.html", ctx)


def _require_work_item_action(db, work_item_id, current_user):
    """Shared gate for the work-item re-arm badges (release-deferred,
       retry-unappliable): ADMIN only. A re-arm mutates fleet-level
       scheduling on a row that keeps its ORIGINAL origin — it acts on
       whoever's behalf the item was first enqueued — so it's an
       operator decision, not a per-user one (unlike the review
       request/delete buttons, which gate via _can_act_on_patchset).
       404 on an unknown item, 403 when the caller isn't an admin."""
    wi = db.execute("SELECT id FROM work_items WHERE id=?",
                    (work_item_id,)).fetchone()
    if wi is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no work-item with id {work_item_id}")
    if current_user is None or not current_user.is_config_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "admin access required")


@router.post("/work-items/{work_item_id:int}/cancel", dependencies=[Depends(auth.require_csrf)])
async def cancel_work_item(request: Request, work_item_id: int,
                           back: str | None = None,
                           current_user: auth.SessionUser = Depends(auth.require_session)):
    """Admin-triggered cancellation of an unheld (claimable / deferred)
       work item — the Cancel button on the detail page POSTs here. The
       row is deleted, so the redirect goes back to the opener (the
       queue, by default) rather than the now-gone detail page. A
       claimable review's cancellation re-arms the patchset's Request-
       review button. Admin-only, via _require_work_item_action."""
    db = request.app.state.db
    _require_work_item_action(db, work_item_id, current_user)
    result = core_db.cancel_work_item(db, work_item_id)
    if result == "unknown":
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no work-item with id {work_item_id}")
    if result == "not_cancellable":
        # The row was claimed/completed between render and click — send
        # the admin back to the detail page to see its current state.
        return RedirectResponse(f"/work-items/{work_item_id}",
                                status_code=303)
    return RedirectResponse(_safe_back(back) if back else "/queue",
                            status_code=303)


@router.post("/work-items/{work_item_id:int}/release-deferred", dependencies=[Depends(auth.require_csrf)])
async def release_deferred(request: Request, work_item_id: int,
                           back: str | None = None,
                           current_user: auth.SessionUser = Depends(auth.require_session)):
    """Operator-triggered release of a DEFERRED work item back to the
       CLAIMABLE pool — the deferred badge on the detail page POSTs here.
       Reuses core_db.release_deferred: a no-op ('not_deferred') if the row
       has since been re-claimed or completed, so a double-click is safe.
       Redirect-after-POST back to the detail page (preserving ?back=).
       Admin-only, via _require_work_item_action."""
    db = request.app.state.db
    _require_work_item_action(db, work_item_id, current_user)
    result = core_db.release_deferred(db, work_item_id)
    if result == "unknown":
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no work-item with id {work_item_id}")
    suffix = f"?back={quote(back, safe='')}" if back else ""
    return RedirectResponse(f"/work-items/{work_item_id}{suffix}",
                            status_code=303)


@router.post("/work-items/{work_item_id:int}/retry-unappliable", dependencies=[Depends(auth.require_csrf)])
async def retry_unappliable(request: Request, work_item_id: int,
                            back: str | None = None,
                            current_user: auth.SessionUser = Depends(auth.require_session)):
    """Operator-triggered retry of an UNAPPLIABLE work item back to the
       CLAIMABLE pool — the 'try again' badge on the detail page POSTs here.
       Reuses core_db.retry_unappliable: a no-op ('not_unappliable') if the
       row has since been re-claimed or completed, so a double-click is safe.
       Redirect-after-POST back to the detail page (preserving ?back=).
       Admin-only, via _require_work_item_action."""
    db = request.app.state.db
    _require_work_item_action(db, work_item_id, current_user)
    result = core_db.retry_unappliable(db, work_item_id)
    if result == "unknown":
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no work-item with id {work_item_id}")
    suffix = f"?back={quote(back, safe='')}" if back else ""
    return RedirectResponse(f"/work-items/{work_item_id}{suffix}",
                            status_code=303)


@router.get("/enroll", response_class=HTMLResponse)
async def enroll(request: Request, code: str | None = None,
                 current_user: auth.SessionUser = Depends(auth.require_session)):
    """A node's enrollment verification page — the `verification_uri`
       it logs. Pairing flow:

       1. The user enters their node's user_code on /enroll.
       2. On the first successful lookup we stamp
          node_enrollments.requested_by_user_id = current_user.id
          (tag_pending_enrollment). The pending row from then on shows
          on this user's /nodes only.
       3. A different user looking up the same code afterwards sees an
          "already paired" error — first-lookup-wins.

       The config-token admin (id=None) bypasses tagging — admins can
       look up and approve any pending enrollment without claiming it
       to themselves; the resulting node is created ownerless."""
    db = request.app.state.db
    ctx = {"code": code, "enrollment": None, "error": None}
    if code:
        enr = core_db.get_enrollment_by_user_code(db, code)
        if enr is None:
            ctx["error"] = f"No enrollment found for code {code}."
        elif enr["state"] != core_db.NODE_ENROLLMENT_STATE_PENDING:
            state_name = core_db.NODE_ENROLLMENT_STATE_NAMES.get(
                enr["state"], "?")
            ctx["error"] = f"That enrollment is already {state_name}."
        else:
            if current_user.is_config_admin:
                tagged = dict(enr)
            else:
                tagged = core_db.tag_pending_enrollment(
                    db, code, current_user.id)
                if tagged is None:
                    # The only way tag fails for a still-pending,
                    # unexpired row is another user already owns it.
                    ctx["error"] = ("That enrollment has already been "
                                    "paired with another user.")
                    tagged = None
            if tagged is not None:
                tagged["task_types_display"] = _types(tagged.get("task_types"))
                tagged["requested_display"]  = _when(tagged.get("created_at"))
                ctx["enrollment"] = tagged
    ctx["current_user"] = current_user
    return templates.TemplateResponse(request, "enroll.html", ctx)


@router.post("/nodes/enrollments/{user_code}/approve", dependencies=[Depends(auth.require_csrf)])
async def approve_enrollment(request: Request, user_code: str,
                              current_user: auth.SessionUser = Depends(auth.require_session)):
    """Approve a pending enrollment — the node joins the fleet, owned
       by the user who paired it. Regular users may only approve a
       pending enrollment they previously tagged via /enroll (the
       lookup-er); the config-token admin can approve any pending
       enrollment. Ownership follows the pairing, not the approver:
       an admin approving an enrollment a user tagged still creates
       the node owned by that user — admin approval is a shortcut,
       not an ownership grab. Only an untagged enrollment (admin
       looked it up directly) yields an ownerless system-only node.

       Errors (already decided / expired / unknown / name now
       conflicts with an active node / not yours to approve) silently
       redirect; the operator sees the row's state on the refreshed
       page."""
    db = request.app.state.db
    enr = core_db.get_enrollment_by_user_code(db, user_code)
    if enr is None or enr["state"] != core_db.NODE_ENROLLMENT_STATE_PENDING:
        return RedirectResponse("/nodes", status_code=303)
    if (not current_user.is_config_admin
            and enr.get("requested_by_user_id") != current_user.id):
        # Not the user who paired the device; not allowed to approve.
        return RedirectResponse("/nodes", status_code=303)
    # For a regular user the gate above pins requested_by_user_id to
    # current_user.id, so the tag is the owner in both paths.
    owner_user_id = enr.get("requested_by_user_id")
    try:
        core_db.approve_enrollment(
            db, user_code,
            decided_by=current_user.email,
            owner_user_id=owner_user_id)
    except (KeyError, ValueError, core_db.DuplicateNodeName):
        pass
    return RedirectResponse("/nodes", status_code=303)


@router.post("/nodes/{node_id}/delete", dependencies=[Depends(auth.require_csrf)])
async def delete_node(request: Request, node_id: int,
                       current_user: auth.SessionUser = Depends(auth.require_session)):
    """Hard-delete an enrolled node from the fleet — removes the row,
       deletes its tokens, NULLs the audit references. Gated on
       ownership: only the node's owner or the config-token admin may
       delete it. A 404 if the node is unknown, 403 if the caller
       isn't the owner. A no-op if the node was already deleted (e.g.
       a stale tab posting twice) — under the gate, that means the
       row is gone and we send the operator back to /nodes."""
    db = request.app.state.db
    node = core_db.get_node(db, node_id)
    if node is None:
        # Already deleted — no information leak (caller has access to
        # the fleet listing anyway).
        return RedirectResponse("/nodes", status_code=303)
    if not _can_manage_node(node, current_user):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "only the node owner may delete it")
    core_db.delete_node(db, node_id)
    return RedirectResponse("/nodes", status_code=303)


@router.post("/nodes/{node_id}/configure", dependencies=[Depends(auth.require_csrf)])
async def configure_node(request: Request, node_id: int,
                          current_user: auth.SessionUser = Depends(auth.require_session)):
    """Owner-controlled per-node settings — currently just the
       handles_system flag (whether the node falls back to the system
       work pool once its owner's user queue is empty). Form posts a
       single `handles_system=on|off` field; missing == off (HTML
       checkbox semantics). Gated on ownership (owner or
       config-token admin); 403 otherwise."""
    db = request.app.state.db
    node = core_db.get_node(db, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no node with id {node_id}")
    if not _can_manage_node(node, current_user):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "only the node owner may configure it")
    form = await request.form()
    handles_system = (form.get("handles_system") or "").lower() in (
        "on", "1", "true", "yes")
    core_db.set_node_handles_system(db, node_id, handles_system)
    return RedirectResponse(f"/nodes/{node_id}", status_code=303)


@router.post("/nodes/{node_id}/owner", dependencies=[Depends(auth.require_csrf)])
async def change_node_owner(request: Request, node_id: int,
                             _: auth.SessionUser = Depends(auth.require_config_admin)):
    """Reassign a node's owner — admin-only. Posts `owner_email`; an
       empty field makes the node ownerless (system-only). An unknown
       email is a 404 (the form is admin-driven so a typo deserves a
       loud error)."""
    db = request.app.state.db
    if core_db.get_node(db, node_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no node with id {node_id}")
    form = await request.form()
    raw = (form.get("owner_email") or "").strip()
    if not raw:
        core_db.set_node_owner(db, node_id, None)
    else:
        user = core_db.get_user_by_email(db, raw)
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"no user with email {raw!r}")
        core_db.set_node_owner(db, node_id, user["id"])
    return RedirectResponse(f"/nodes/{node_id}", status_code=303)


@router.post("/nodes/enrollments/{user_code}/deny", dependencies=[Depends(auth.require_csrf)])
async def deny_enrollment(request: Request, user_code: str,
                           current_user: auth.SessionUser = Depends(auth.require_session)):
    """Deny a pending enrollment — same ownership rule as approve:
       only the user who paired the device (or the config-token admin)
       can deny it. Silent on errors (unknown / non-pending) to keep
       the flow idempotent under double-submit."""
    db = request.app.state.db
    enr = core_db.get_enrollment_by_user_code(db, user_code)
    if enr is None or enr["state"] != core_db.NODE_ENROLLMENT_STATE_PENDING:
        return RedirectResponse("/nodes", status_code=303)
    if (not current_user.is_config_admin
            and enr.get("requested_by_user_id") != current_user.id):
        return RedirectResponse("/nodes", status_code=303)
    try:
        core_db.deny_enrollment(db, user_code,
                                decided_by=current_user.email)
    except (KeyError, ValueError):
        pass
    return RedirectResponse("/nodes", status_code=303)


# --- settings --------------------------------------------------------------

def _deployment_view(cfg):
    """The read-only deployment-config rows for the Site-settings page (secrets
       masked — they are set at container start, not editable here)."""
    def masked(v):
        return "•••••••• (set)" if v else "(unset)"
    return [
        ("Hostname", cfg.hostname),
        ("Public URL", cfg.public_url),
        ("HTTP port", cfg.http_port),
        ("Data directory", cfg.data_dir),
        ("Fleet secret", masked(cfg.fleet_secret)),
        ("Admin token", masked(cfg.admin_token)),
    ]


def _settings_fields(field_values, available, errors=None):
    """The runtime-config form fields, grouped. `field_values` maps 'group.key'
       to the value to show — an int/string for the text fields, a list of
       enabled sources for the sources toggle list. `available` is every
       installed gather module (one toggle each); `errors` maps a field to a
       validation message."""
    errors = errors or {}
    groups = {}
    for group, key, label, unit, kind in runtime_config.FIELDS:
        name = f"{group}.{key}"
        field = {"name": name, "label": label, "unit": unit, "kind": kind,
                 "error": errors.get(name)}
        if kind == "sources":
            chosen = set(field_values.get(name) or [])
            field["options"] = [{"value": s, "on": s in chosen}
                                for s in available]
        else:
            field["value"] = field_values.get(name, "")
        groups.setdefault(group, []).append(field)
    return list(groups.items())


def _tag_rows(db):
    """The list-tag rows for the Site-settings page — one switch per known tag."""
    rows = []
    for t in core_db.list_tags(db):
        rows.append({
            "tag":         t["tag"],
            "description": t["description"] or "",
            "enabled":     bool(t["enabled"]),
            "origin":      core_db.LIST_TAG_ORIGIN_NAMES.get(
                               t["origin"], "?")})
    return rows


def _elapsed(seconds):
    """Render a seconds count as a compact "Xm Ys" / "Xs" string."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


# Help text for the "lore archive: absent" panel (no autoclone) — the same
# guidance SOURCES.md / .env.example give, condensed for the UI.
_LORE_CLONE_HELP = (
    "Run `python3 core/gather-modules/lore.py clone` to provision the "
    "lore archive, or set HONE_LORE_AUTOCLONE=1 in core/.env to have "
    "hone-core clone it in the background on startup.")


def _lore_clone_view(state):
    """The view-model the lore-clone Settings panel renders. Reads the
       in-memory `app.state.lore_clone` snapshot the provision task
       publishes (see core/main.py `_run_lore_clone`) and re-stats the
       archive each call so an out-of-band clone is also picked up."""
    state = dict(state)                           # snapshot — never mutate
    archive_path = state.get("archive_path") or ""
    Lore = gather.gather_api.load("lore").__class__
    # re-stat (multi-list aware): an operator may have cloned out-of-band;
    # flip the panel to "ready" the next time they refresh. is_provisioned()
    # covers the configured set; the archive_path/.git check also catches a
    # clone dropped straight into the recorded path.
    present = (Lore.is_provisioned()
               or bool(archive_path
                       and os.path.isdir(os.path.join(archive_path, ".git"))))
    if state["phase"] != "cloning" and present:
        state["phase"] = "ready"
        state["archive_present"] = True

    now = time.time()
    started, completed = state.get("started_at"), state.get("completed_at")
    if state["phase"] == "cloning" and started:
        elapsed_display = _elapsed(now - started)
    elif completed and started:
        elapsed_display = _elapsed(completed - started)
    else:
        elapsed_display = None
    return {
        "phase":             state["phase"],          # absent|cloning|ready|error
        "percent":           state.get("percent", 0),
        "git_phase":         state.get("git_phase"),
        "last_line":         state.get("last_line"),
        "elapsed_display":   elapsed_display,
        "error":             state.get("error"),
        "archive_path":      archive_path,
        "autoclone_enabled": state.get("autoclone_enabled", False),
        "help":              _LORE_CLONE_HELP,
    }


@router.get("/site-settings/lore-clone-status", response_class=HTMLResponse)
async def lore_clone_status(request: Request,
                             _: auth.SessionUser = Depends(auth.require_config_admin)):
    """The lore-clone panel partial — rendered standalone for the
       Site-settings page's `hx-get` poll (every 5 s). Returns just the panel
       HTML so HTMX swaps it in place."""
    return templates.TemplateResponse(
        request, "_lore_clone_panel.html",
        {"lore_clone": _lore_clone_view(request.app.state.lore_clone)})


@router.post("/site-settings/lore-clone", response_class=HTMLResponse, dependencies=[Depends(auth.require_csrf)])
async def lore_clone_trigger(request: Request,
                              _: auth.SessionUser = Depends(auth.require_config_admin)):
    """Operator-triggered lore provision (the Settings 'Provision now'
       button). Kicks off a background clone unless one is already running,
       then returns the panel partial — now showing 'cloning', which starts
       the 5 s poll that tracks it to ready/error."""
    request.app.state.trigger_lore_clone()
    return templates.TemplateResponse(
        request, "_lore_clone_panel.html",
        {"lore_clone": _lore_clone_view(request.app.state.lore_clone)})


_METHODOLOGY_SCHEMA_PATH = os.path.join(
    os.path.dirname(_HERE), "common", "schema", "methodology.schema.yaml")

# Cap on uploaded YAML size — the packaged default-methodology.yaml is ~70 KB
# at present; 1 MiB leaves plenty of room for growth and stops accidental
# multi-megabyte uploads from reaching the YAML parser.
_METHODOLOGY_UPLOAD_CAP_BYTES = 1 * 1024 * 1024


_METHODOLOGY_ERROR_MESSAGES = {
    "parse":     "Could not parse the upload as YAML.",
    "shape":     "Top-level value must be a YAML mapping (object).",
    "schema":    "Document failed methodology-schema validation — see "
                 "container logs for the exact field path.",
    "too_large": ("Upload exceeds the methodology size cap "
                  f"({_METHODOLOGY_UPLOAD_CAP_BYTES // 1024} KiB)."),
    "identical": "Upload is byte-identical to the current active "
                 "methodology — no new version created.",
}


def _methodology_view(db):
    """The view-model the Methodology panel renders — active version,
       export URL, and the upload cap (rendered next to the file
       input). The active methodology lives in the DB
       (methodology_versions table); the UI never reads from disk."""
    active = core_db.active_methodology(db)
    base = {"upload_cap_kib": _METHODOLOGY_UPLOAD_CAP_BYTES // 1024}
    if active is None:
        return {**base, "version": None, "export_url": None}
    version, _document = active
    return {**base, "version": version,
             "export_url":     "/site-settings/methodology/export"}


# Valid `?tab=...` values. Defined as a tuple so the order matches the
# template's nav-tab order (Gather first — most-frequent operator
# action). The four runtime-config groups (gather, work_queue,
# enrollment, merge_gate) each get their own tab + their own form so
# an operator can save one section without affecting the others.
_SETTINGS_TABS = ("gather", "work_queue", "enrollment", "merge_gate",
                   "methodology", "tags", "deployment")
# --- operator reports -------------------------------------------------------

# Chart series specs: (daily_stats column, display label, hex color).
# Fixed hexes (Bootstrap's palette values) — CSS theme variables can't
# reach a <canvas>.
_OPS_SEGMENTS = (("ops_prepare", "Prepare", "#0d6efd"),
                 ("ops_review",  "Review",  "#198754"),
                 ("ops_train",   "Train",   "#ffc107"))
_OUTCOME_SEGMENTS = (("ops_completed",   "Completed",   "#198754"),
                     ("ops_deferred",    "Deferred",    "#ffc107"),
                     ("ops_unappliable", "Unappliable", "#dc3545"))
_USER_SEGMENTS = (("patchsets_uploaded", "Uploads",            "#0dcaf0"),
                  ("ops_user_origin",    "User-requested ops", "#6610f2"),
                  ("active_users",       "Active users",       "#6c757d"))
# Weekly active-users is the PEAK day, not a sum — say so in the legend.
_USER_WEEK_SEGMENTS = (
    ("patchsets_uploaded", "Uploads",                 "#0dcaf0"),
    ("ops_user_origin",    "User-requested ops",      "#6610f2"),
    ("active_users",       "Active users (peak day)", "#6c757d"))

_REPORT_DAYS = 30
_REPORT_WEEKS = 12


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request,
                       current_user: auth.SessionUser = Depends(auth.require_config_admin)):
    """The operator reports — daily/weekly activity rollups. Closed days
       come from the daily_stats table; reports.ensure_daily_stats
       materializes any days that closed since the last view (a no-op
       MAX() probe when up to date), so nothing historical is recomputed
       per page view. The single live computation is "today so far",
       rendered as a visually distinct partial bar."""
    db = request.app.state.db
    reports.ensure_daily_stats(db)
    today = reports.compute_day_stats(db, reports.today_utc())
    daily = reports.load_daily_stats(db, days=_REPORT_DAYS)
    weekly = reports.weekly_rollup(
        reports.load_daily_stats(db, days=_REPORT_WEEKS * 7),
        weeks=_REPORT_WEEKS, today=today)
    drows = reports.chart_rows_daily(daily, today)
    totals = reports.summary_totals(daily, today)
    avg_ms = totals["avg_duration_ms"]
    charts = {
        "ops_daily":      reports.stacked_chart_config(drows, _OPS_SEGMENTS),
        "ops_weekly":     reports.stacked_chart_config(weekly, _OPS_SEGMENTS),
        "outcomes_daily": reports.stacked_chart_config(drows,
                                                       _OUTCOME_SEGMENTS),
        "users_daily":    reports.stacked_chart_config(drows, _USER_SEGMENTS,
                                                       stacked=False),
        "users_weekly":   reports.stacked_chart_config(weekly,
                                                       _USER_WEEK_SEGMENTS,
                                                       stacked=False),
    }
    return templates.TemplateResponse(request, "reports.html", {
        "current_user": current_user,
        "charts": charts,
        "totals": totals,
        "avg_duration_display": (f"{avg_ms / 1000:.1f} s"
                                 if avg_ms is not None else "—"),
        "daily_rows": drows,
        "weekly_rows": weekly,
        "report_days": _REPORT_DAYS,
        "report_weeks": _REPORT_WEEKS,
        "has_data": bool(totals["ops_total"] or totals["ops_enqueued"]
                         or totals["patchsets_gathered"]
                         or totals["patchsets_uploaded"]
                         or totals["active_users"]),
    })


@router.get("/reports/check-usage", response_class=HTMLResponse)
async def check_usage_page(
        request: Request,
        current_user: auth.SessionUser = Depends(auth.require_config_admin)):
    """Which methodology review checks fire, over the per-review coverage
       (ai_reviews.check_coverage). Scoped to one methodology version via
       ?mv=N (the check set differs across versions, so rates aren't pooled);
       defaults to all versions. Admin-only, like /reports."""
    mv_raw = request.query_params.get("mv")
    mv = int(mv_raw) if mv_raw and mv_raw.isdigit() else None
    stats = reports.check_usage_stats(request.app.state.db,
                                      methodology_version=mv)
    return templates.TemplateResponse(request, "check_usage.html", {
        "current_user": current_user, **stats})


_DEFAULT_SETTINGS_TAB = "gather"

# Tabs that render a runtime-config form. The form_group attribute on
# each tab's form (hidden _group field) selects which subset of
# runtime_config.FIELDS the POST handler validates.
_RUNTIME_CONFIG_GROUPS = ("gather", "work_queue", "enrollment", "merge_gate")


def _resolve_settings_tab(request):
    """Pluck `?tab=...` from the query string, validate against the
       known set, fall back to the default. Server-side validation
       keeps a typo'd URL from rendering an empty page."""
    t = request.query_params.get("tab") or _DEFAULT_SETTINGS_TAB
    return t if t in _SETTINGS_TABS else _DEFAULT_SETTINGS_TAB


@router.get("/site-settings", response_class=HTMLResponse)
async def site_settings(request: Request,
                   current_user: auth.SessionUser = Depends(auth.require_config_admin)):
    """View the deployment configuration and edit the operator-tunable
       settings — config-token admin only, like every /site-settings route:
       these knobs mutate hone-core's behaviour for everyone. The page
       is organised into tabs (`?tab=gather` / `methodology` / `tags` /
       `deployment`); each tab renders only its own panel so the page
       isn't a wall of stacked sections. See ARCHITECTURE.md →
       Configuration & the Site-settings page."""
    rc = request.app.state.runtime_config.as_dict()
    available = gather.gather_api.available()
    values = {f"{g}.{k}": rc[g][k] for g, k, *_ in runtime_config.FIELDS}
    saved = request.query_params.get("saved")
    imported = request.query_params.get("methodology_imported")
    meth_error = request.query_params.get("methodology_error")
    return templates.TemplateResponse(request, "site_settings.html", {
        "tab":        _resolve_settings_tab(request),
        "groups":     _settings_fields(values, available),
        "tags":       _tag_rows(request.app.state.db),
        "deployment": _deployment_view(request.app.state.config),
        "lore_clone": _lore_clone_view(request.app.state.lore_clone),
        "methodology": _methodology_view(request.app.state.db),
        "methodology_imported": imported,
        "methodology_error":
            _METHODOLOGY_ERROR_MESSAGES.get(meth_error or ""),
        "saved_settings":   saved == "1",
        "saved_tags":       saved == "tags",
        "gather_triggered": saved == "triggered",
        "current_user": current_user})


@router.post("/site-settings", dependencies=[Depends(auth.require_csrf)])
async def save_site_settings(request: Request,
                         current_user: auth.SessionUser = Depends(auth.require_config_admin)):
    """Validate a runtime-config submission, persist it to config.yaml, and
       update the live config — no restart needed. Invalid input re-renders
       the form with the fields flagged; config.yaml is left untouched.

       Per-tab partial submission: each runtime-config tab posts a
       hidden `_group` field naming which subset of FIELDS to
       validate. The other groups keep their current values via the
       merge in runtime_config.parse_form. With `_group` absent (or
       unknown), every field is validated — preserved so callers
       that posted the whole form continue to work."""
    form = await request.form()
    available = gather.gather_api.available()
    submitted_group = form.get("_group", "")
    groups_filter = ({submitted_group}
                      if submitted_group in _RUNTIME_CONFIG_GROUPS
                      else None)
    # Tab to land on for redirects + re-renders. With no _group we
    # fall back to gather (the legacy single-form behavior).
    tab = submitted_group if groups_filter else "gather"
    rc, errors = runtime_config.parse_form(
        form, valid_sources=available,
        current=request.app.state.runtime_config,
        groups=groups_filter)
    if not errors:
        runtime_config.save(request.app.state.config.config_path, rc)
        request.app.state.runtime_config = rc
        return RedirectResponse(f"/site-settings?tab={tab}&saved=1",
                                 status_code=303)
    submitted = {}
    for g, k, _label, _unit, kind in runtime_config.FIELDS:
        name = f"{g}.{k}"
        submitted[name] = (form.getlist(name) if kind == "sources"
                           else (form.get(name) or ""))
    # If the submission was a legacy whole-form post and the error
    # is in a non-default group, land the re-render on THAT tab so
    # the operator sees their flagged field rather than an unrelated
    # gather form. Per-tab submissions already pin `tab` correctly.
    if groups_filter is None and errors:
        first_errored_group = next(iter(errors)).split(".", 1)[0]
        if first_errored_group in _RUNTIME_CONFIG_GROUPS:
            tab = first_errored_group
    return templates.TemplateResponse(request, "site_settings.html", {
        "tab":        tab,
        "groups":     _settings_fields(submitted, available, errors),
        "tags":       _tag_rows(request.app.state.db),
        "deployment": _deployment_view(request.app.state.config),
        "lore_clone": _lore_clone_view(request.app.state.lore_clone),
        "methodology": _methodology_view(request.app.state.db),
        "methodology_imported": None, "methodology_error": None,
        "saved_settings":   False, "saved_tags": False,
        "gather_triggered": False,
        "current_user": current_user}, status_code=400)


@router.post("/site-settings/gather/trigger", dependencies=[Depends(auth.require_csrf)])
async def trigger_gather(request: Request,
                          _: auth.SessionUser = Depends(auth.require_config_admin)):
    """Wake the GATHER supervisor and fire every idle enabled source on
       its next tick, bypassing the per-source cadence check. Sources
       mid-cycle keep running; the trigger only affects idle ones. The
       supervisor wakes within one event-loop turn, so the button feels
       instant. Coalesces: rapid clicks collapse to one trigger because
       `asyncio.Event.set()` is idempotent until cleared."""
    trigger = getattr(request.app.state, "gather_trigger", None)
    if trigger is not None:
        trigger.set()
    return RedirectResponse("/site-settings?tab=gather&saved=triggered",
                             status_code=303)


# --- methodology import / export ------------------------------------------

def _build_methodology_dumper():
    """Subclass yaml.SafeDumper so an exported methodology looks like
       the source default-methodology.yaml — diffable by eye, hand-
       editable. Two overrides:

         - increase_indent(flow=False, indentless=False) → False
           forces list items to be indented under their parent key
           (`  - id: foo`), instead of PyYAML's default left-hugging
           form (`- id: foo`).
         - str representer emits a literal block scalar (`|`) for any
           string containing a newline, instead of PyYAML's default
           double-quoted form with `\\n` escapes. Single-line strings
           keep the default unquoted style.

       PyYAML doesn't expose dumper config that achieves this via
       safe_dump kwargs; the subclass is the documented path."""
    import yaml

    class _IndentedDumper(yaml.SafeDumper):
        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow, False)

    def _str_representer(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar(
                "tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    _IndentedDumper.add_representer(str, _str_representer)
    return _IndentedDumper


def _dump_methodology_yaml(document):
    """Serialize a methodology dict as YAML matching the style of
       core/default-methodology.yaml. Shared between the export
       endpoint and any future "render methodology as YAML" use
       cases (e.g. a CLI dump command).

       Prose wrapping is owned upstream by core/methodology_format
       (mdformat at PROSE_WRAP_COLUMN). The custom representer
       above forces every multi-line string to `|` literal block
       style, which preserves those line breaks verbatim — PyYAML's
       own `width` knob is moot for literal blocks and would not
       fire on any single-line field in the methodology either."""
    import yaml
    return yaml.dump(document, Dumper=_build_methodology_dumper(),
                      sort_keys=False, default_flow_style=False,
                      allow_unicode=True)


@router.get("/site-settings/methodology/export")
async def export_methodology(request: Request,
                              _: auth.SessionUser = Depends(auth.require_config_admin)):
    """Download the active methodology as YAML. Filename carries the
       DB version so an operator keeping a few revisions on disk can
       tell them apart without diffing. Style mirrors
       core/default-methodology.yaml (literal block scalars,
       indented list items) and prose is reflowed to
       methodology_format.PROSE_WRAP_COLUMN — see
       core/methodology_format for the canonicalization rules.

       Defensive normalization on read: a DB row from before the
       canonicalizer landed gets reflowed for the download so the
       operator sees consistent output regardless of when v1 was
       bootstrapped. Idempotent — already-normalized content passes
       through unchanged."""
    db = request.app.state.db
    active = core_db.active_methodology(db)
    if active is None:
        raise HTTPException(status_code=404, detail="no active methodology")
    version, document = active
    document = methodology_format.normalize_methodology(document)
    return PlainTextResponse(
        _dump_methodology_yaml(document),
        media_type="application/x-yaml",
        headers={"Content-Disposition":
                  f'attachment; filename="methodology-v{version}.yaml"'})


def _canonical_methodology_bytes(document):
    """Stable byte-representation of a methodology dict for equality
       comparison. JSON with sort_keys=True is canonical for the pure
       data shapes a methodology contains (no datetimes, no custom
       objects — yaml.safe_load returns dict/list/str/int/bool/None).
       Used by import_methodology to detect "this is byte-identical
       to the active version, don't create a duplicate row"."""
    return json.dumps(document, sort_keys=True,
                       ensure_ascii=False).encode("utf-8")


@router.post("/site-settings/methodology/import", dependencies=[Depends(auth.require_csrf)])
async def import_methodology(request: Request,
                              file: UploadFile = File(...),
                              _: auth.SessionUser = Depends(auth.require_config_admin)):
    """Upload a methodology YAML. Validates against
       common/schema/methodology.schema.yaml, then adds it to the DB
       as a new active version (superseding the current active row in
       methodology_versions). The DB row is the persistent store —
       hone-core boots from the same DB on the next restart, so the
       imported methodology survives without a sidecar disk file.

       Two version-related behaviors:

         - **Content-identical reject**: if the upload is byte-
           identical (canonical JSON) to the active version, the
           import is refused with a flash message and NO new DB row
           is created. Prevents accidental no-op duplicates when an
           operator re-uploads the file they just exported.

         - **doc.version auto-bump**: the document's top-level
           `version` field is set to
           `max(active.version, uploaded.version) + 1` before
           storage. The schema describes doc.version as something
           hone-core controls (bumped on every accepted merge-gate
           change); an import IS a merge-gate-equivalent change, so
           hone-core takes ownership rather than trusting the value
           the operator put in the file. This means the operator
           need not hand-bump the field when editing offline."""
    import jsonschema
    import yaml
    raw = await file.read(_METHODOLOGY_UPLOAD_CAP_BYTES + 1)
    if len(raw) > _METHODOLOGY_UPLOAD_CAP_BYTES:
        return RedirectResponse(
            "/site-settings?tab=methodology&methodology_error=too_large", status_code=303)
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        log.warning("methodology import: parse failure: %s", exc)
        return RedirectResponse(
            "/site-settings?tab=methodology&methodology_error=parse", status_code=303)
    if not isinstance(document, dict):
        return RedirectResponse(
            "/site-settings?tab=methodology&methodology_error=shape", status_code=303)
    with open(_METHODOLOGY_SCHEMA_PATH, encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    try:
        jsonschema.validate(document, schema,
                             cls=jsonschema.Draft202012Validator)
    except jsonschema.ValidationError as exc:
        log.warning("methodology import: schema validation failed: %s",
                     exc.message)
        return RedirectResponse(
            "/site-settings?tab=methodology&methodology_error=schema", status_code=303)
    # Canonicalize prose fields BEFORE the content-identical
    # check, so an operator who re-uploads the export they edited
    # (with different whitespace / line breaks but the same
    # semantics) gets the identical-rejection rather than spawning
    # a near-duplicate row. See
    # core/methodology_format.normalize_methodology.
    document = methodology_format.normalize_methodology(document)
    db = request.app.state.db
    active = core_db.active_methodology(db)
    if active is not None:
        _active_db_version, active_doc = active
        # Compare both sides in normalized form — protects the
        # identical-reject path during the transition where an
        # already-active DB row pre-dates this canonicalizer.
        active_doc_norm = methodology_format.normalize_methodology(active_doc)
        if (_canonical_methodology_bytes(document)
                == _canonical_methodology_bytes(active_doc_norm)):
            log.info("methodology import: byte-identical to active "
                      "v%d; refusing to create a duplicate row",
                      _active_db_version)
            return RedirectResponse(
                "/site-settings?tab=methodology&methodology_error=identical",
                status_code=303)
        # Auto-bump doc.version: hone-core takes ownership of the
        # field rather than trusting the operator's value.
        active_doc_version   = active_doc.get("version", 1)
        uploaded_doc_version = document.get("version", 1)
        document["version"]  = max(active_doc_version,
                                    uploaded_doc_version) + 1
    note = f"imported from {file.filename or 'upload'}"
    version = core_db.add_methodology_version(db, document, note=note)
    log.info("methodology imported: db_version=%d doc_version=%d from=%s",
              version, document.get("version"), file.filename)
    return RedirectResponse(
        f"/site-settings?tab=methodology&methodology_imported={version}", status_code=303)


@router.post("/site-settings/tags", dependencies=[Depends(auth.require_csrf)])
async def save_tag_filter(request: Request,
                           _: auth.SessionUser = Depends(auth.require_config_admin)):
    """Persist the list-tag gather filter — every known tag is shown as a
       switch; ticked tags are the new enabled set. Tags not in the form fall
       back to disabled (an unticked checkbox isn't posted)."""
    db = request.app.state.db
    form = await request.form()
    posted = set(form.getlist("tag"))
    for row in core_db.list_tags(db):
        core_db.set_tag_enabled(db, row["tag"], row["tag"] in posted)
    return RedirectResponse("/site-settings?tab=tags&saved=tags",
                             status_code=303)
