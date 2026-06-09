"""hone-core — the operator web UI (see ../ARCHITECTURE.md → Operator web UI).

Server-rendered: Jinja2 + Bootstrap 5 + HTMX. Pages:
- `/login`              email+password or Google SSO login
- `/register`           self-service account registration (admin-approved)
- `/`                   the work queue (prepare + review + train items)
- `/patchsets/{root}`   per-patchset detail (corpus + reviews + queue history)
- `/nodes`              the node fleet + pending enrollments
- `/enroll`             approve a node's device-grant enrollment
- `/settings`           operator-tunable runtime config + list-tag gather filter
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
from core import auth, core_db, gather, methodology_format, patchview, runtime_config

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
        is_config_admin=False))
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
        core_db.create_user(db, email, display_name or email, "local",
                            password_hash=hashed)
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
        core_db.create_user(db, g_email, g_name, "google", google_sub=google_sub)
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
        is_config_admin=False))
    core_db.touch_last_login(db, user["id"])
    return RedirectResponse(next_url, status_code=303)


@router.get("/logout", include_in_schema=False)
async def logout(request: Request):
    auth.clear_session(request)
    return RedirectResponse("/login", status_code=303)


# ===========================================================================
# User management (config-token admin only)
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


@router.post("/users/{user_id}/delete", include_in_schema=False, dependencies=[Depends(auth.require_csrf)])
async def user_delete(request: Request, user_id: int,
                      user: auth.SessionUser = Depends(auth.require_config_admin)):
    core_db.delete_user(request.app.state.db, user_id)
    return RedirectResponse("/users", status_code=303)


def _types(raw):
    """Render a node's stored task_types JSON as a readable list."""
    try:
        vals = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return raw or "—"
    return ", ".join(vals) if vals else "—"


def _when(ts):
    """Render a unix timestamp as a readable UTC string."""
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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
        tooltip_parts.append(f"last seen {_when(s['last_activity_at'])}")
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
                     ("comments", "Comments", True))


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


def _queue_view(db, type, state, page, size):
    """Build the queue page's render context — items, chips, paging info.
       Shared by the full-page `GET /` and the HTMX partial swap."""
    type_int  = _WORK_TYPE_BY_NAME.get(type)
    state_int = _WORK_STATE_BY_NAME.get(state)
    if type_int is None:
        type = None
    if state_int is None:
        state = None

    counts = core_db.work_item_counts(db)         # {type_int: {state_int: n}}
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
    filtered_total = core_db.count_work_items(db, type=type_int,
                                                state=state_int)
    pages = max(1, -(-filtered_total // size))    # ceil division
    page = max(1, min(page or 1, pages))
    offset = (page - 1) * size

    # The queue's current URL is the destination the detail page's
    # `← Back` link returns to. Computed once; threaded onto every row.
    this_url = _queue_url(type=type, state=state,
                           page=page if page > 1 else None, size=size)
    back_qs = "?back=" + quote(this_url, safe="")

    items = []
    for w in core_db.list_work_items(db, type=type_int, state=state_int,
                                       limit=size, offset=offset):
        type_name  = core_db.WORK_ITEM_TYPE_NAMES.get(w["type"], "?")
        state_name = core_db.WORK_ITEM_STATE_NAMES.get(w["state"], "?")
        items.append({
            "id":             w["id"],
            "type":           type_name,
            "type_badge":     _TYPE_BADGE.get(type_name, "text-bg-light"),
            "state":          state_name,
            "state_badge":    _STATE_BADGE.get(state_name, "text-bg-light"),
            "subject":        w["subject"] or w["root_message_id"],
            "message_id":     w["message_id"],
            # Queue rows drill into the per-work-item detail page —
            # the queue is a list of work-items, so one click goes to
            # the work-item, not the patchset. The patchset is one
            # further click from inside the work-item header.
            "detail_url":     f"/work-items/{w['id']}{back_qs}",
            "claimed_by":     w["claimed_by"],
            "enqueued_display": _when(w["enqueued_at"]),
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
       per-patchset detail page."""
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
        state=_WORK_STATE_BY_NAME.get(state))
    if is_auto_poll and request.headers.get("x-queue-version") == version:
        # Idle poll: the operator's open page already shows the
        # latest state. Logged at DEBUG so the per-operator,
        # per-5s tick doesn't fill the operator's container log
        # at the default INFO level; raise the logger to DEBUG
        # if you want to watch the polling loop.
        log.debug("queue poll 204 — version unchanged (%s)", version)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    ctx = _queue_view(db, type, state, page, size)
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


def _patchset_view(db, root):
    """Build the per-patchset detail render context — patchset header,
       list tags, prepare-derived metadata, ai_review concerns, work-item
       history, and the message thread. Raises 404 when the root is
       unknown."""
    patchset = core_db.get_patchset(db, root)
    if patchset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no patchset with root_message_id {root!r}")
    patchset["sent_display"]      = _when(patchset.get("sent"))
    patchset["gathered_display"]  = _when(patchset.get("gathered_at"))
    patchset["state_display"] = core_db.PATCHSET_STATE_NAMES.get(
        patchset["state"], "?")

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

    messages = []
    for m in raw_messages:
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
            "headers":      rendered.headers,
            "body_html":    rendered.body_html,
        })

    work_item_back_qs = "?back=" + quote(f"/patchsets/{root}", safe="")
    work_items = []
    has_review_item = False
    for w in core_db.work_items_for_patchset(db, root):
        type_name  = core_db.WORK_ITEM_TYPE_NAMES.get(w["type"], "?")
        state_name = core_db.WORK_ITEM_STATE_NAMES.get(w["state"], "?")
        if w["type"] == core_db.WORK_ITEM_TYPE_REVIEW:
            has_review_item = True
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
        })

    # Review-request availability for the manual trigger. Review is no
    # longer auto-enqueued (see gather._ingest_ref); the operator asks for
    # it here. The button is offered only when prepare has produced the
    # patchset_metadata row (the enqueue gate) and there isn't already a
    # review work-item or a completed ai_review — so it dims once a review
    # exists or is in flight. `reason` explains a disabled button.
    if has_review_item:
        review_request = {"available": False, "reason": "queued"}
    elif ai_review is not None:
        review_request = {"available": False, "reason": "done"}
    elif metadata is None:
        review_request = {"available": False, "reason": "needs-prepare"}
    else:
        review_request = {"available": True, "reason": None}

    return {
        "patchset":   patchset,
        "tags":       tags,
        "metadata":   metadata,
        "ai_review":  ai_review,
        "review_patches":  review_patches,
        "series_concerns": series_concerns,
        "messages":   messages,
        "work_items": work_items,
        "review_request": review_request,
    }


_MSG_TYPE_BADGE = {"cover":   "text-bg-secondary",
                   "patch":   "text-bg-primary",
                   "comment": "text-bg-info"}


@router.get("/patchsets/{root_message_id:path}", response_class=HTMLResponse)
async def patchset_detail(request: Request, root_message_id: str,
                           back: str | None = None,
                           current_user: auth.SessionUser = Depends(auth.require_session)):
    """The per-patchset detail page — drill down into a row from the queue
       (or any other index that links here). `?back=` carries the URL the
       opener wants the `← Back` button to return to; same-origin paths
       only, fallback to `/`."""
    ctx = _patchset_view(request.app.state.db, root_message_id)
    ctx["back_url"] = _safe_back(back)
    ctx["current_user"] = current_user
    return templates.TemplateResponse(request, "patchset.html", ctx)


@router.post("/review-requests/{root_message_id:path}/delete", dependencies=[Depends(auth.require_csrf)])
async def delete_review(request: Request, root_message_id: str,
                         _: auth.SessionUser = Depends(auth.require_session)):
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
       would otherwise greedily swallow `<root>/delete`."""
    db = request.app.state.db
    if core_db.get_patchset(db, root_message_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no patchset with root_message_id "
                            f"{root_message_id!r}")
    core_db.delete_review(db, root_message_id)
    return RedirectResponse(f"/patchsets/{quote(root_message_id)}",
                             status_code=303)


@router.post("/review-requests/{root_message_id:path}", dependencies=[Depends(auth.require_csrf)])
async def request_review(request: Request, root_message_id: str,
                          _: auth.SessionUser = Depends(auth.require_session)):
    """Operator-triggered review enqueue. Review is not auto-enqueued
       (a gather run would flood the queue); the operator requests one
       patchset at a time here. Reuses core_db.maybe_enqueue_review — the
       same prepare-gated, idempotent enqueue the pipeline used to call —
       so a double-click or a patchset that already has a review is a safe
       no-op. Redirect-after-POST back to the detail page (the button
       dims once the review item exists)."""
    db = request.app.state.db
    # maybe_enqueue_review returns None (not KeyError) for an unknown or
    # not-yet-gathered patchset, so check existence explicitly to 404.
    if core_db.get_patchset(db, root_message_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no patchset with root_message_id "
                            f"{root_message_id!r}")
    core_db.maybe_enqueue_review(db, root_message_id)
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
        "free_disk":  _mb_display(health.get("free_disk_mb")),
        "repo_size":  _mb_display(health.get("refrepo_size_mb")),
        "error":      _ANTHROPIC_ERROR_LABELS.get(err, err) if err else None,
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
    if anth_err:
        bucket = "errored"
    elif last_seen and (now - last_seen) > stale_after:
        bucket = "stale"
    elif claim:
        bucket = "in_flight"
    else:
        bucket = "idle"
    freshness = (now - last_seen) if last_seen else None
    health_age = (now - health_at) if health_at else None
    running = (now - claim["claimed_at"]
                if claim and claim["claimed_at"] else None)
    return {
        "bucket":               bucket,
        "bucket_label":         dict(_NODE_BUCKETS).get(bucket, bucket),
        "freshness_display":    _relative_duration(freshness),
        "last_seen_tooltip":    _when(last_seen) if last_seen else "",
        "health_age_display":   _relative_duration(health_age),
        "running_time_display": _relative_duration(running),
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


def _nodes_view(db, runtime_cfg):
    """The view-model the /nodes table partial renders. Sorts each
       enrolled node into one of four buckets — errored / stale /
       in_flight / idle — and augments each row with the
       running-time and freshness fields the table columns show.

       Bucketing follows the same loudest-wins rule the fleet-pulse
       chip uses: a node carrying an anthropic-error wins over
       staleness, in-flight wins over plain idle. Each bucket is
       sorted by last_seen DESC so the most-recently-active row
       within the bucket appears first."""
    now = int(time.time())
    stale_after = (runtime_cfg.heartbeat_seconds
                    * _FLEET_STALE_HEARTBEAT_MULT)
    back_qs = "?back=" + quote("/nodes", safe="")

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
        })
        buckets[status["bucket"]].append(n)

    # Sort within each bucket — most-recently-seen first.
    for key in buckets:
        buckets[key].sort(key=lambda r: r.get("last_seen") or 0,
                           reverse=True)

    # Render-ready bucket list with counts; preserves the loudest-
    # first order from _NODE_BUCKETS.
    bucketed = [{"key": k, "label": label, "rows": buckets[k],
                  "count": len(buckets[k]),
                  "collapsed_by_default": k == "idle"}
                 for k, label in _NODE_BUCKETS]
    return {"buckets": bucketed,
             "total":   sum(b["count"] for b in bucketed),
             "node_state_active": core_db.NODE_STATE_ACTIVE}


@router.get("/nodes", response_class=HTMLResponse)
async def nodes(request: Request,
                current_user: auth.SessionUser = Depends(auth.require_session)):
    """The node fleet: the pending-enrollment queue and the enrolled
       nodes, sorted into health buckets (errored / stale / in-flight
       / idle). The bucketed table partial polls /nodes/fleet-table
       every 10s so an operator sees rows move between buckets and
       running-time tick without reloading."""
    db = request.app.state.db
    pending = []
    for e in core_db.list_pending_enrollments(db):
        e = dict(e)
        e["task_types_display"] = _types(e.get("task_types"))
        e["requested_display"]  = _when(e.get("created_at"))
        pending.append(e)
    ctx = {"pending": pending, "current_user": current_user,
            **_nodes_view(db, request.app.state.runtime_config)}
    return templates.TemplateResponse(request, "nodes.html", ctx)


@router.get("/nodes/fleet-table", response_class=HTMLResponse)
async def nodes_fleet_table(request: Request,
                              _: auth.SessionUser = Depends(auth.require_session)):
    """The bucketed enrolled-nodes table as an HTML partial — polled
       by the /nodes page every 10s. Skips the pending-enrollment
       table (static between approve/deny clicks; an unnecessary
       re-render would steal focus from any Approve button mid-
       hover)."""
    return templates.TemplateResponse(
        request, "_nodes_fleet_table.html",
        _nodes_view(request.app.state.db,
                     request.app.state.runtime_config))


# --- per-node detail ------------------------------------------------------

_WORK_TYPE_DISPLAY = core_db.WORK_ITEM_TYPE_NAMES
_WORK_STATE_DISPLAY = core_db.WORK_ITEM_STATE_NAMES


def _node_claimed_by_label(node):
    """The string the api layer writes into work_items.claimed_by for a
       node — the node's name when set, str(node.id) otherwise (see
       api.claim_task). Centralised so the detail page's per-node
       query matches what the claim path wrote."""
    return node.get("name") or str(node["id"])


def _node_live_panel_view(db, node_id, runtime_cfg):
    """Live-status fields the detail page's Node + Health cards render.
       Same bucket / running-time / freshness shape the /nodes table
       uses (via _node_status_fields), plus the bucket badge class
       and a pre-computed claim-link triple for the in-flight card.
       Returns None when the node is gone — the polling endpoint
       reads that as a 404."""
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
                       back=None):
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
    panel = _node_live_panel_view(db, node_id, runtime_cfg)
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
                             back=safe_back)
    ctx["back_url"] = safe_back or "/nodes"
    ctx["current_user"] = current_user
    return templates.TemplateResponse(request, "node_detail.html", ctx)


@router.get("/nodes/{node_id:int}/live", response_class=HTMLResponse)
async def node_detail_live_panel(request: Request, node_id: int,
                                  _: auth.SessionUser = Depends(auth.require_session)):
    """The live status panel for the per-node detail page — Node +
       Health cards with bucket badge, running time, and relative
       freshness. Polled every 10s by HTMX so an operator can leave
       the page open and watch the in-flight claim progress without
       reloading. The recent-claims and reviews tables below don't
       refresh on this poll — they're history, full reload is fine."""
    panel = _node_live_panel_view(request.app.state.db, node_id,
                                    request.app.state.runtime_config)
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
        "root_message_id":   w["root_message_id"],
        "patchset_subject":  patchset_subject,
        "patchset_url":      patchset_url,
        "node_url":          node_url,
        "claiming_node":     claiming_node,
        "claimed_at_display":   _when(w["claimed_at"]),
        "completed_at_display": _when(w["completed_at"]),
        "enqueued_at_display":  _when(w["enqueued_at"]),
        "lease_expires_display": _when(w["lease_expires"]),
        "heartbeat_at_display": _when(w["heartbeat_at"]),
        "record":            record if isinstance(record, dict) else None,
        "record_json":       json.dumps(record, indent=2)
                              if isinstance(record, dict) else None,
        "meta_schema_error":  meta.get("schema_error"),
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


@router.post("/work-items/{work_item_id:int}/release-deferred", dependencies=[Depends(auth.require_csrf)])
async def release_deferred(request: Request, work_item_id: int,
                           back: str | None = None,
                           _: auth.SessionUser = Depends(auth.require_session)):
    """Operator-triggered release of a DEFERRED work item back to the
       CLAIMABLE pool — the deferred badge on the detail page POSTs here.
       Reuses core_db.release_deferred: a no-op ('not_deferred') if the row
       has since been re-claimed or completed, so a double-click is safe.
       Redirect-after-POST back to the detail page (preserving ?back=)."""
    result = core_db.release_deferred(request.app.state.db, work_item_id)
    if result == "unknown":
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no work-item with id {work_item_id}")
    suffix = f"?back={quote(back, safe='')}" if back else ""
    return RedirectResponse(f"/work-items/{work_item_id}{suffix}",
                            status_code=303)


@router.post("/work-items/{work_item_id:int}/retry-unappliable", dependencies=[Depends(auth.require_csrf)])
async def retry_unappliable(request: Request, work_item_id: int,
                            back: str | None = None,
                            _: auth.SessionUser = Depends(auth.require_session)):
    """Operator-triggered retry of an UNAPPLIABLE work item back to the
       CLAIMABLE pool — the 'try again' badge on the detail page POSTs here.
       Reuses core_db.retry_unappliable: a no-op ('not_unappliable') if the
       row has since been re-claimed or completed, so a double-click is safe.
       Redirect-after-POST back to the detail page (preserving ?back=)."""
    result = core_db.retry_unappliable(request.app.state.db, work_item_id)
    if result == "unknown":
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no work-item with id {work_item_id}")
    suffix = f"?back={quote(back, safe='')}" if back else ""
    return RedirectResponse(f"/work-items/{work_item_id}{suffix}",
                            status_code=303)


@router.get("/enroll", response_class=HTMLResponse)
async def enroll(request: Request, code: str | None = None,
                 current_user: auth.SessionUser = Depends(auth.require_session)):
    """A node's enrollment verification page — the `verification_uri` it logs.
       The operator enters the user code and approves the node."""
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
            enr = dict(enr)
            enr["task_types_display"] = _types(enr.get("task_types"))
            enr["requested_display"]  = _when(enr.get("created_at"))
            ctx["enrollment"] = enr
    ctx["current_user"] = current_user
    return templates.TemplateResponse(request, "enroll.html", ctx)


@router.post("/nodes/enrollments/{user_code}/approve", dependencies=[Depends(auth.require_csrf)])
async def approve_enrollment(request: Request, user_code: str,
                              _: auth.SessionUser = Depends(auth.require_session)):
    """Approve a pending enrollment — the node joins the fleet. Errors
       (already decided / expired / unknown / name now conflicts with
       an active node) silently redirect; the operator sees the row's
       state on the refreshed page."""
    try:
        core_db.approve_enrollment(request.app.state.db, user_code,
                                   decided_by="operator")
    except (KeyError, ValueError, core_db.DuplicateNodeName):
        pass
    return RedirectResponse("/nodes", status_code=303)


@router.post("/nodes/{node_id}/delete", dependencies=[Depends(auth.require_csrf)])
async def delete_node(request: Request, node_id: int,
                       _: auth.SessionUser = Depends(auth.require_session)):
    """Hard-delete an enrolled node from the fleet — removes the row,
       deletes its tokens, NULLs the audit references. A no-op if the
       node was already deleted (e.g. a stale tab posting twice)."""
    core_db.delete_node(request.app.state.db, node_id)
    return RedirectResponse("/nodes", status_code=303)


@router.post("/nodes/enrollments/{user_code}/deny", dependencies=[Depends(auth.require_csrf)])
async def deny_enrollment(request: Request, user_code: str,
                           _: auth.SessionUser = Depends(auth.require_session)):
    """Deny a pending enrollment."""
    try:
        core_db.deny_enrollment(request.app.state.db, user_code,
                                decided_by="operator")
    except (KeyError, ValueError):
        pass
    return RedirectResponse("/nodes", status_code=303)


# --- settings --------------------------------------------------------------

def _deployment_view(cfg):
    """The read-only deployment-config rows for the Settings page (secrets
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
    """The list-tag rows for the Settings page — one switch per known tag."""
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


@router.get("/settings/lore-clone-status", response_class=HTMLResponse)
async def lore_clone_status(request: Request,
                             _: auth.SessionUser = Depends(auth.require_session)):
    """The lore-clone panel partial — rendered standalone for the
       Settings page's `hx-get` poll (every 5 s). Returns just the panel
       HTML so HTMX swaps it in place."""
    return templates.TemplateResponse(
        request, "_lore_clone_panel.html",
        {"lore_clone": _lore_clone_view(request.app.state.lore_clone)})


@router.post("/settings/lore-clone", response_class=HTMLResponse, dependencies=[Depends(auth.require_csrf)])
async def lore_clone_trigger(request: Request,
                              _: auth.SessionUser = Depends(auth.require_session)):
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
             "export_url":     "/settings/methodology/export"}


# Valid `?tab=...` values. Defined as a tuple so the order matches the
# template's nav-tab order (Gather first — most-frequent operator
# action). The four runtime-config groups (gather, work_queue,
# enrollment, merge_gate) each get their own tab + their own form so
# an operator can save one section without affecting the others.
_SETTINGS_TABS = ("gather", "work_queue", "enrollment", "merge_gate",
                   "methodology", "tags", "deployment")
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


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request,
                   current_user: auth.SessionUser = Depends(auth.require_session)):
    """View the deployment configuration and edit the operator-tunable
       settings. The page is organised into tabs (`?tab=gather` /
       `methodology` / `tags` / `deployment`); each tab renders only
       its own panel so the page isn't a wall of stacked sections.
       See ARCHITECTURE.md → Configuration & the Settings page."""
    rc = request.app.state.runtime_config.as_dict()
    available = gather.gather_api.available()
    values = {f"{g}.{k}": rc[g][k] for g, k, *_ in runtime_config.FIELDS}
    saved = request.query_params.get("saved")
    imported = request.query_params.get("methodology_imported")
    meth_error = request.query_params.get("methodology_error")
    return templates.TemplateResponse(request, "settings.html", {
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


@router.post("/settings", dependencies=[Depends(auth.require_csrf)])
async def save_settings(request: Request,
                         current_user: auth.SessionUser = Depends(auth.require_session)):
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
        return RedirectResponse(f"/settings?tab={tab}&saved=1",
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
    return templates.TemplateResponse(request, "settings.html", {
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


@router.post("/settings/gather/trigger", dependencies=[Depends(auth.require_csrf)])
async def trigger_gather(request: Request,
                          _: auth.SessionUser = Depends(auth.require_session)):
    """Wake the GATHER supervisor and fire every idle enabled source on
       its next tick, bypassing the per-source cadence check. Sources
       mid-cycle keep running; the trigger only affects idle ones. The
       supervisor wakes within one event-loop turn, so the button feels
       instant. Coalesces: rapid clicks collapse to one trigger because
       `asyncio.Event.set()` is idempotent until cleared."""
    trigger = getattr(request.app.state, "gather_trigger", None)
    if trigger is not None:
        trigger.set()
    return RedirectResponse("/settings?tab=gather&saved=triggered",
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


@router.get("/settings/methodology/export")
async def export_methodology(request: Request,
                              _: auth.SessionUser = Depends(auth.require_session)):
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


@router.post("/settings/methodology/import", dependencies=[Depends(auth.require_csrf)])
async def import_methodology(request: Request,
                              file: UploadFile = File(...),
                              _: auth.SessionUser = Depends(auth.require_session)):
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
            "/settings?tab=methodology&methodology_error=too_large", status_code=303)
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        log.warning("methodology import: parse failure: %s", exc)
        return RedirectResponse(
            "/settings?tab=methodology&methodology_error=parse", status_code=303)
    if not isinstance(document, dict):
        return RedirectResponse(
            "/settings?tab=methodology&methodology_error=shape", status_code=303)
    with open(_METHODOLOGY_SCHEMA_PATH, encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    try:
        jsonschema.validate(document, schema,
                             cls=jsonschema.Draft202012Validator)
    except jsonschema.ValidationError as exc:
        log.warning("methodology import: schema validation failed: %s",
                     exc.message)
        return RedirectResponse(
            "/settings?tab=methodology&methodology_error=schema", status_code=303)
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
                "/settings?tab=methodology&methodology_error=identical",
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
        f"/settings?tab=methodology&methodology_imported={version}", status_code=303)


@router.post("/settings/tags", dependencies=[Depends(auth.require_csrf)])
async def save_tag_filter(request: Request,
                           _: auth.SessionUser = Depends(auth.require_session)):
    """Persist the list-tag gather filter — every known tag is shown as a
       switch; ticked tags are the new enabled set. Tags not in the form fall
       back to disabled (an unticked checkbox isn't posted)."""
    db = request.app.state.db
    form = await request.form()
    posted = set(form.getlist("tag"))
    for row in core_db.list_tags(db):
        core_db.set_tag_enabled(db, row["tag"], row["tag"] in posted)
    return RedirectResponse("/settings?tab=tags&saved=tags",
                             status_code=303)
