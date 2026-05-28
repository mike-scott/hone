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
import logging
import os
import time
from urllib.parse import quote

log = logging.getLogger("hone.ui")

from fastapi import (APIRouter, File, HTTPException, Request, Response,
                      UploadFile, status)
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from common.version import __version__ as VERSION
from core import core_db, gather, methodology_format, runtime_config

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
async def fleet_status_partial(request: Request):
    """The fleet-pulse chip as an HTML partial — polled by HTMX every
       10s from the top nav so the operator gets a live rollup
       without reloading the page they're on. Tiny SQL footprint:
       one COUNT-style scan over `nodes` plus one over claimed
       work_items, regardless of fleet size."""
    return templates.TemplateResponse(
        request, "_fleet_pulse.html",
        {"fleet": _fleet_pulse_view(request.app.state.db,
                                      request.app.state.runtime_config)})


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
        # Idle poll: the operator's open page already shows the
        # latest state. Logged at DEBUG so the per-operator,
        # per-5s tick doesn't fill the operator's container log
        # at the default INFO level; raise the logger to DEBUG
        # if you want to watch the polling loop.
        log.debug("queue poll 204 — version unchanged (%s)", version)
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

    work_item_back_qs = "?back=" + quote(f"/patchsets/{root}", safe="")
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
            "detail_url":    f"/work-items/{w['id']}{work_item_back_qs}",
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
        health = n.get("health") or {}
        anth_err = (health.get("last_anthropic_error")
                     if isinstance(health, dict) else None)
        last_seen = n.get("last_seen") or 0
        claim = claim_by_worker.get(n.get("name"))

        # Loudest-wins bucket assignment.
        if anth_err:
            bucket = "errored"
        elif last_seen and (now - last_seen) > stale_after:
            bucket = "stale"
        elif claim:
            bucket = "in_flight"
        else:
            bucket = "idle"

        freshness = (now - last_seen) if last_seen else None
        running   = (now - claim["claimed_at"]
                      if claim and claim["claimed_at"] else None)
        n.update({
            "task_types_display":  _types(n.get("task_types")),
            "state_display":       core_db.NODE_STATE_NAMES.get(
                                       n["state"], "?"),
            "freshness_display":   _relative_duration(freshness),
            "last_seen_tooltip":   _when(last_seen) if last_seen else "",
            "running_time_display":_relative_duration(running),
            "claim":               claim,
            "claim_subject":       (claim["subject"]
                                     or claim["root_message_id"]
                                     if claim else None),
            "claim_type":          (core_db.WORK_ITEM_TYPE_NAMES.get(
                                         claim["type"], "?")
                                     if claim else None),
            "claim_url":           (f"/work-items/{claim['id']}{back_qs}"
                                     if claim else None),
            "health_display":      _health_display(n.get("health")),
            "detail_url":          f"/nodes/{n['id']}{back_qs}",
        })
        buckets[bucket].append(n)

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
async def nodes(request: Request):
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
    ctx = {"pending": pending,
            **_nodes_view(db, request.app.state.runtime_config)}
    return templates.TemplateResponse(request, "nodes.html", ctx)


@router.get("/nodes/fleet-table", response_class=HTMLResponse)
async def nodes_fleet_table(request: Request):
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


def _node_detail_view(db, node_id):
    """Build the per-node detail render context — node row + health
       snapshot + recent claims + recent reviews. Raises 404 when
       the node is unknown (revoked tombstones are still resolvable;
       only a hard-deleted node 404s)."""
    node = core_db.get_node(db, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no node with id {node_id}")
    node["task_types_display"] = _types(node.get("task_types"))
    node["state_display"]      = core_db.NODE_STATE_NAMES.get(
                                     node["state"], "?")
    node["enrolled_display"]   = _when(node.get("enrolled_at"))
    node["last_seen_display"]  = _when(node.get("last_seen"))
    node["health_display"]     = _health_display(node.get("health"))
    node["health_at_display"]  = _when(node.get("health_at"))

    node_back_qs = f"?back={quote(f'/nodes/{node_id}', safe='')}"
    claims = []
    for w in core_db.work_items_for_node(db, _node_claimed_by_label(node)):
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
        "reviews": reviews,
        "node_state_active": core_db.NODE_STATE_ACTIVE,
    }


@router.get("/nodes/{node_id:int}", response_class=HTMLResponse)
async def node_detail(request: Request, node_id: int,
                       back: str | None = None):
    """The per-node detail page — drill down into a row from /nodes
       (or any other index that links here). `?back=` carries the URL
       the opener wants the ← Back button to return to; same-origin
       paths only via _safe_back, default `/nodes`."""
    ctx = _node_detail_view(request.app.state.db, node_id)
    ctx["back_url"] = _safe_back(back) if back else "/nodes"
    return templates.TemplateResponse(request, "node_detail.html", ctx)


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
    }


@router.get("/work-items/{work_item_id:int}", response_class=HTMLResponse)
async def work_item_detail(request: Request, work_item_id: int,
                            back: str | None = None):
    """The per-work-item detail page — every queue / patchset-history /
       node-claims table links here. `?back=` carries the opener's
       URL so the operator returns where they were; same-origin
       paths only via _safe_back, default `/`."""
    ctx = _work_item_view(request.app.state.db, work_item_id)
    ctx["back_url"] = _safe_back(back) if back else "/"
    return templates.TemplateResponse(request, "work_item.html", ctx)


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
async def settings(request: Request):
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
        "gather_triggered": saved == "triggered"})


@router.post("/settings")
async def save_settings(request: Request):
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
async def export_methodology(request: Request):
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


@router.post("/settings/methodology/import")
async def import_methodology(request: Request,
                              file: UploadFile = File(...)):
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
    return RedirectResponse("/settings?tab=tags&saved=tags",
                             status_code=303)
