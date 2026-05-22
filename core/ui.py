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

from core import core_db

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


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    return templates.TemplateResponse(request, "overview.html")


@router.get("/queue", response_class=HTMLResponse)
async def queue(request: Request):
    return templates.TemplateResponse(request, "page.html",
                                      {"page_title": "Queue management"})


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


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    return templates.TemplateResponse(request, "page.html",
                                      {"page_title": "Settings"})
