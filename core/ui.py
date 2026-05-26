"""hone-core — the operator web UI (see ../ARCHITECTURE.md → Operator web UI).

Server-rendered: Jinja2 + Bootstrap 5 + HTMX. Pages:
- `/`                   the work queue (prepare + review + train items)
- `/patchsets/{root}`   per-patchset detail (corpus + reviews + queue history)
- `/nodes`              the node fleet + pending enrollments
- `/enroll`             approve a node's device-grant enrollment
- `/settings`           operator-tunable runtime config + list-tag gather filter

NOTE: the operator login (session-based, distinct from a node's bearer token)
is still TODO; these routes are currently unauthenticated, like the rest of
the UI skeleton.
"""
import datetime
import json
import os
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from common.version import __version__ as VERSION
from core import core_db, gather, runtime_config

_HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))
templates.env.globals["version"] = VERSION   # rendered in the base footer

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


# Page-size options for the queue paginator (small dropdown). 50 is the
# default — smaller than the framework's max list-limit of 200, more
# "page"-like for an operator skimming the queue.
_PAGE_SIZES = (25, 50, 100, 200)
_DEFAULT_PAGE_SIZE = 50


def _queue_url(*, type=None, state=None, page=None, size=None):
    """Build a `/?...` queue URL preserving the chosen axes + paging.
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
    return "/" + ("?" + "&".join(parts) if parts else "")


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
            "detail_url":     f"/patchsets/{quote(w['root_message_id'])}"
                              + back_qs,
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


@router.get("/", response_class=HTMLResponse)
async def queue(request: Request,
                type: str | None = None, state: str | None = None,
                page: int = 1, size: int = _DEFAULT_PAGE_SIZE):
    """The work queue — the operator UI's home page. `?type=` filters to
       one work-item type (prepare / review / train); `?state=` filters to
       one state. Unknown axis values are ignored. `?page=` and `?size=`
       page the listing (page is 1-indexed; size is clamped to one of
       _PAGE_SIZES — defaults to 50).

       Auto-poll short-circuit: the #queue-pane wrapper echoes the
       last-known `X-Queue-Version` on every HTMX request. When it
       matches the current filtered queue's version, we return 204 No
       Content so HTMX skips the swap and the server skips the template
       render. A full-page navigation (no HX-Request header) always
       renders — a real browser navigation isn't an idempotent poll."""
    db = request.app.state.db
    is_hx = request.headers.get("hx-request") == "true"
    type_int  = _WORK_TYPE_BY_NAME.get(type)
    state_int = _WORK_STATE_BY_NAME.get(state)
    version = core_db.queue_version(db, type=type_int, state=state_int)
    if is_hx and request.headers.get("x-queue-version") == version:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    ctx = _queue_view(db, type, state, page, size)
    ctx["queue_version"] = version
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
    ai_review = core_db.get_ai_review(db, root)

    messages = []
    for m in core_db.messages_for_patchset(db, root):
        messages.append({
            "message_id":   m["message_id"],
            "type":         core_db.MSG_TYPE_NAMES.get(m["type"], "?"),
            "type_badge":   _MSG_TYPE_BADGE.get(
                                core_db.MSG_TYPE_NAMES.get(m["type"]),
                                "text-bg-light"),
            "part_index":   m["part_index"],
            "subject":      m["subject"],
            "author":       m["author_name"] or m["author_email"] or "—",
            "sent_display": _when(m["sent"]),
            "body":         m["body"] or "",
        })

    work_items = []
    for w in core_db.work_items_for_patchset(db, root):
        type_name  = core_db.WORK_ITEM_TYPE_NAMES.get(w["type"], "?")
        state_name = core_db.WORK_ITEM_STATE_NAMES.get(w["state"], "?")
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
        })

    return {
        "patchset":   patchset,
        "tags":       tags,
        "metadata":   metadata,
        "ai_review":  ai_review,
        "messages":   messages,
        "work_items": work_items,
    }


_MSG_TYPE_BADGE = {"cover":   "text-bg-secondary",
                   "patch":   "text-bg-primary",
                   "comment": "text-bg-info"}


@router.get("/patchsets/{root_message_id:path}", response_class=HTMLResponse)
async def patchset_detail(request: Request, root_message_id: str,
                           back: str | None = None):
    """The per-patchset detail page — drill down into a row from the queue
       (or any other index that links here). `?back=` carries the URL the
       opener wants the `← Back` button to return to; same-origin paths
       only, fallback to `/`."""
    ctx = _patchset_view(request.app.state.db, root_message_id)
    ctx["back_url"] = _safe_back(back)
    return templates.TemplateResponse(request, "patchset.html", ctx)


# --- node management -------------------------------------------------------

@router.get("/nodes", response_class=HTMLResponse)
async def nodes(request: Request):
    """The node fleet: the pending-enrollment queue and the enrolled nodes."""
    db = request.app.state.db
    pending = []
    for e in core_db.list_pending_enrollments(db):
        e = dict(e)
        e["task_types_display"] = _types(e.get("task_types"))
        e["requested_display"]  = _when(e.get("created_at"))
        pending.append(e)

    fleet = []
    for n in core_db.list_nodes(db):
        n = dict(n)
        n["task_types_display"] = _types(n.get("task_types"))
        n["state_display"]      = core_db.NODE_STATE_NAMES.get(
                                      n["state"], "?")
        n["last_seen_display"]  = _when(n.get("last_seen"))
        fleet.append(n)

    return templates.TemplateResponse(request, "nodes.html", {
        "pending": pending, "nodes": fleet,
        "node_state_active": core_db.NODE_STATE_ACTIVE})


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
        elif enr["state"] != core_db.NODE_ENROLLMENT_STATE_PENDING:
            state_name = core_db.NODE_ENROLLMENT_STATE_NAMES.get(
                enr["state"], "?")
            ctx["error"] = f"That enrollment is already {state_name}."
        else:
            enr = dict(enr)
            enr["task_types_display"] = _types(enr.get("task_types"))
            enr["requested_display"]  = _when(enr.get("created_at"))
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


@router.post("/nodes/{node_id}/delete")
async def delete_node(request: Request, node_id: int):
    """Hard-delete an enrolled node from the fleet — removes the row,
       deletes its tokens, NULLs the audit references. A no-op if the
       node was already deleted (e.g. a stale tab posting twice)."""
    core_db.delete_node(request.app.state.db, node_id)
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
       in-memory `app.state.lore_clone` snapshot the autoclone task
       publishes (see core/main.py `_autoclone_lore`) and re-stats the
       archive each call so an out-of-band clone is also picked up."""
    state = dict(state)                           # snapshot — never mutate
    archive_path = state.get("archive_path") or ""
    # re-stat: an operator may have cloned the archive out-of-band; flip
    # the panel to "ready" the next time they refresh.
    if archive_path and os.path.isdir(os.path.join(archive_path, ".git")):
        if state["phase"] != "cloning":
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
async def lore_clone_status(request: Request):
    """The lore-clone panel partial — rendered standalone for the
       Settings page's `hx-get` poll (every 5 s). Returns just the panel
       HTML so HTMX swaps it in place."""
    return templates.TemplateResponse(
        request, "_lore_clone_panel.html",
        {"lore_clone": _lore_clone_view(request.app.state.lore_clone)})


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    """View the deployment configuration and edit the operator-tunable
       settings — runtime config + the list-tag gather filter
       (ARCHITECTURE.md → Configuration & the Settings page)."""
    rc = request.app.state.runtime_config.as_dict()
    available = gather.gather_api.available()
    values = {f"{g}.{k}": rc[g][k] for g, k, *_ in runtime_config.FIELDS}
    saved = request.query_params.get("saved")
    return templates.TemplateResponse(request, "settings.html", {
        "groups":     _settings_fields(values, available),
        "tags":       _tag_rows(request.app.state.db),
        "deployment": _deployment_view(request.app.state.config),
        "lore_clone": _lore_clone_view(request.app.state.lore_clone),
        "saved_settings":   saved == "1",
        "saved_tags":       saved == "tags",
        "gather_triggered": saved == "triggered"})


@router.post("/settings")
async def save_settings(request: Request):
    """Validate the runtime-config submission, persist it to config.yaml, and
       update the live config — no restart needed. Invalid input re-renders
       the form with the fields flagged; config.yaml is left untouched."""
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
        "groups":     _settings_fields(submitted, available, errors),
        "tags":       _tag_rows(request.app.state.db),
        "deployment": _deployment_view(request.app.state.config),
        "lore_clone": _lore_clone_view(request.app.state.lore_clone),
        "saved_settings":   False, "saved_tags": False,
        "gather_triggered": False}, status_code=400)


@router.post("/settings/gather/trigger")
async def trigger_gather(request: Request):
    """Wake the GATHER supervisor and fire every idle enabled source on
       its next tick, bypassing the per-source cadence check. Sources
       mid-cycle keep running; the trigger only affects idle ones. The
       supervisor wakes within one event-loop turn, so the button feels
       instant. Coalesces: rapid clicks collapse to one trigger because
       `asyncio.Event.set()` is idempotent until cleared."""
    trigger = getattr(request.app.state, "gather_trigger", None)
    if trigger is not None:
        trigger.set()
    return RedirectResponse("/settings?saved=triggered", status_code=303)


@router.post("/settings/tags")
async def save_tag_filter(request: Request):
    """Persist the list-tag gather filter — every known tag is shown as a
       switch; ticked tags are the new enabled set. Tags not in the form fall
       back to disabled (an unticked checkbox isn't posted)."""
    db = request.app.state.db
    form = await request.form()
    posted = set(form.getlist("tag"))
    for row in core_db.list_tags(db):
        core_db.set_tag_enabled(db, row["tag"], row["tag"] in posted)
    return RedirectResponse("/settings?saved=tags", status_code=303)
