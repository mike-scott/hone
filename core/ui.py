"""hone-core — the operator web UI (see ../ARCHITECTURE.md → Operator web UI).

Server-rendered: Jinja2 + Bootstrap 5 + HTMX. Skeleton: most page routes
render placeholders. Node management is built — the operator approves a node's
device-grant enrollment here (ARCHITECTURE.md → Auth, enrollment & transport).

NOTE: the operator login (session-based, distinct from a node's bearer token)
is still TODO; these routes are currently unauthenticated, like the rest of
the UI skeleton.
"""
import datetime
import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from core import core_db, gather, runtime_config

_HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

router = APIRouter(tags=["ui"])


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


_REVIEW_STATES = ("claimable", "claimed", "reviewed", "unappliable", "deferred")
_STATE_BADGE = {
    "claimable":   "text-bg-secondary",
    "claimed":     "text-bg-info",
    "reviewed":    "text-bg-success",
    "unappliable": "text-bg-danger",
    "deferred":    "text-bg-warning",
}


@router.get("/", response_class=HTMLResponse)
async def queue(request: Request, state: str | None = None):
    """The review queue — the operator UI's home page. `?state=` filters the
       listing to one review state; an unknown state is ignored."""
    db = request.app.state.db
    if state not in _REVIEW_STATES:
        state = None
    reviews = []
    for r in core_db.list_reviews(db, state=state):
        r = dict(r)
        r["enqueued_display"] = _when(r.get("enqueued_at"))
        r["state_badge"] = _STATE_BADGE.get(r["state"], "text-bg-light")
        reviews.append(r)
    counts = core_db.review_counts(db)
    return templates.TemplateResponse(request, "queue.html", {
        "counts": counts, "total": sum(counts.values()),
        "states": _REVIEW_STATES, "reviews": reviews, "filter": state})


@router.get("/submissions", response_class=HTMLResponse)
async def submissions(request: Request):
    return templates.TemplateResponse(request, "page.html",
                                      {"page_title": "Manual submissions"})


# --- node management -------------------------------------------------------

@router.get("/nodes", response_class=HTMLResponse)
async def nodes(request: Request):
    """The node fleet: the pending-enrollment queue and the enrolled nodes."""
    db = request.app.state.db
    pending = []
    for e in core_db.list_pending_enrollments(db):
        e = dict(e)
        e["task_types_display"] = _types(e.get("task_types"))
        e["requested_display"] = _when(e.get("created_at"))
        pending.append(e)

    fleet = []
    for n in core_db.list_nodes(db):
        n = dict(n)
        n["task_types_display"] = _types(n.get("task_types"))
        n["last_seen_display"] = _when(n.get("last_seen"))
        fleet.append(n)

    return templates.TemplateResponse(request, "nodes.html", {
        "pending": pending, "nodes": fleet})


@router.get("/enroll", response_class=HTMLResponse)
async def enroll(request: Request, code: str | None = None):
    """A node's enrollment verification page — the `verification_uri` it logs.
       The operator enters the user code and approves the node."""
    db = request.app.state.db
    ctx = {"code": code, "enrollment": None, "error": None}
    if code:
        enr = core_db.get_enrollment_by_user_code(db, code)
        if enr is None:
            ctx["error"] = f"No enrollment found for code {code}."
        elif enr["state"] != "pending":
            ctx["error"] = f"That enrollment is already {enr['state']}."
        else:
            enr = dict(enr)
            enr["task_types_display"] = _types(enr.get("task_types"))
            enr["requested_display"] = _when(enr.get("created_at"))
            ctx["enrollment"] = enr
    return templates.TemplateResponse(request, "enroll.html", ctx)


@router.post("/nodes/enrollments/{user_code}/approve")
async def approve_enrollment(request: Request, user_code: str):
    """Approve a pending enrollment — the node joins the fleet."""
    try:
        core_db.approve_enrollment(request.app.state.db, user_code,
                                   decided_by="operator")
    except (KeyError, ValueError):
        pass                               # already decided / expired / gone
    return RedirectResponse("/nodes", status_code=303)


@router.post("/nodes/enrollments/{user_code}/deny")
async def deny_enrollment(request: Request, user_code: str):
    """Deny a pending enrollment."""
    try:
        core_db.deny_enrollment(request.app.state.db, user_code,
                                decided_by="operator")
    except (KeyError, ValueError):
        pass
    return RedirectResponse("/nodes", status_code=303)


# --- remaining skeleton pages ----------------------------------------------

@router.get("/merge-gate", response_class=HTMLResponse)
async def merge_gate(request: Request):
    return templates.TemplateResponse(request, "page.html",
                                      {"page_title": "Merge gate"})


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
    """The Settings form fields, grouped. `field_values` maps 'group.key' to
       the value to show — an int/string for the text fields, a list of
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


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    """View the deployment configuration and edit the operator-tunable
       settings (ARCHITECTURE.md → Configuration & the Settings page)."""
    rc = request.app.state.runtime_config.as_dict()
    available = gather.gather_api.available()
    values = {f"{g}.{k}": rc[g][k] for g, k, *_ in runtime_config.FIELDS}
    return templates.TemplateResponse(request, "settings.html", {
        "groups": _settings_fields(values, available),
        "deployment": _deployment_view(request.app.state.config),
        "saved": request.query_params.get("saved") == "1"})


@router.post("/settings")
async def save_settings(request: Request):
    """Validate the submission, persist it to config.yaml, and update the
       live config — no restart needed. Invalid input re-renders the form with
       the fields flagged; config.yaml is left untouched."""
    form = await request.form()
    available = gather.gather_api.available()
    rc, errors = runtime_config.parse_form(form, valid_sources=available)
    if not errors:
        runtime_config.save(request.app.state.config.config_path, rc)
        request.app.state.runtime_config = rc
        return RedirectResponse("/settings?saved=1", status_code=303)
    submitted = {}
    for g, k, _label, _unit, kind in runtime_config.FIELDS:
        name = f"{g}.{k}"
        submitted[name] = (form.getlist(name) if kind == "sources"
                           else (form.get(name) or ""))
    return templates.TemplateResponse(request, "settings.html", {
        "groups": _settings_fields(submitted, available, errors),
        "deployment": _deployment_view(request.app.state.config),
        "saved": False}, status_code=400)
