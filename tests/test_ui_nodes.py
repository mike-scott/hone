"""Tests for the /nodes page — bucketed enrolled-nodes view and the
HTMX-polled /nodes/fleet-table partial. Covers the loudest-wins
bucketing (errored > stale > in-flight > idle), the running-time
column for in-flight rows, the relative freshness column, and the
HTMX self-replacing target."""
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import core_db, runtime_config, ui


@pytest.fixture
def ctx(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.include_router(ui.router)
    app.state.db = db
    app.state.runtime_config = runtime_config.load(
        str(tmp_path / "config.yaml"))
    return SimpleNamespace(client=TestClient(app), app=app, db=db)


def _node(db, *, name, last_seen, state=None, health=None):
    state = state if state is not None else core_db.NODE_STATE_ACTIVE
    health_json = json.dumps(health) if health is not None else None
    cur = db.execute(
        "INSERT INTO nodes (name,task_types,state,enrolled_at,last_seen,"
        "health,health_at) VALUES (?,?,?,?,?,?,?)",
        (name, '["prepare"]', state, int(time.time()), last_seen,
         health_json, int(time.time())))
    db.commit()
    return cur.lastrowid


def _patchset(db, root):
    """Seed a minimal patchset so a work_items row can FK against it."""
    db.execute(
        "INSERT INTO patchsets (root_message_id,subject,submitter_email,"
        "sent,n_patches,gathered_at) VALUES (?,?,?,?,?,?)",
        (root, "subj", "a@b", int(time.time()), 1, int(time.time())))
    db.commit()


def _claim(db, *, claimed_by, claimed_at, root="<r1@x>"):
    """Create a CLAIMED work_item owned by the named node — the table
       joins by `claimed_by == node.name`."""
    if not db.execute("SELECT 1 FROM patchsets WHERE root_message_id=?",
                       (root,)).fetchone():
        _patchset(db, root)
    db.execute(
        "INSERT INTO work_items (type,root_message_id,state,claimed_by,"
        "claimed_at,enqueued_at) VALUES (?,?,?,?,?,?)",
        (core_db.WORK_ITEM_TYPE_PREPARE, root,
         core_db.WORK_ITEM_STATE_CLAIMED, claimed_by, claimed_at,
         claimed_at - 30))
    db.commit()


# --- bucket assignment -----------------------------------------------------

def test_nodes_page_renders_buckets_in_loudest_order(ctx):
    """Errored / Stale / In flight / Idle, each with its own table
       and count. Empty buckets are hidden. Order is loudest-first
       so the operator sees red before they scroll."""
    now = int(time.time())
    _node(ctx.db, name="err-1",   last_seen=now,
          health={"last_anthropic_error": "auth"})
    _node(ctx.db, name="stale-1", last_seen=now - 10_000,
          health={"last_anthropic_error": None})
    _node(ctx.db, name="busy-1",  last_seen=now,
          health={"last_anthropic_error": None})
    _claim(ctx.db, claimed_by="busy-1", claimed_at=now - 47)
    _node(ctx.db, name="idle-1",  last_seen=now,
          health={"last_anthropic_error": None})
    body = ctx.client.get("/nodes").text
    err_pos = body.index("Errored")
    stl_pos = body.index("Stale")
    flt_pos = body.index("In flight")
    idl_pos = body.index("Idle")
    assert err_pos < stl_pos < flt_pos < idl_pos


def test_errored_node_appears_in_errored_not_stale(ctx):
    """A node that's BOTH stale (last_seen old) AND carrying an
       anthropic error lands in the errored bucket only. Mirrors the
       fleet-pulse rollup's loudest-wins rule."""
    now = int(time.time())
    _node(ctx.db, name="loud", last_seen=now - 10_000,
          health={"last_anthropic_error": "rate_limit"})
    body = ctx.client.get("/nodes").text
    # Errored bucket header has the row's name underneath; stale
    # bucket either absent or doesn't carry this row.
    assert "Errored" in body and "loud" in body
    assert "Stale" not in body          # no stale bucket rendered


def test_in_flight_bucket_shows_running_time_column(ctx):
    """The In flight table is the only one with a Running column;
       its values format as `47s` / `2m 12s` for the running claim."""
    now = int(time.time())
    _node(ctx.db, name="busy", last_seen=now,
          health={"last_anthropic_error": None})
    _claim(ctx.db, claimed_by="busy", claimed_at=now - 47)
    body = ctx.client.get("/nodes").text
    assert "Running" in body            # column header
    assert "47s" in body                # rendered duration


def test_idle_bucket_is_collapsed_by_default(ctx):
    """Idle is the bulk of the table at scale — `<details>` keeps it
       out of the way until the operator wants to inspect it."""
    now = int(time.time())
    _node(ctx.db, name="idle-1", last_seen=now,
          health={"last_anthropic_error": None})
    body = ctx.client.get("/nodes").text
    # `<details>` element with the Idle summary, NOT open by default.
    assert "<details" in body
    assert "Idle" in body
    assert "<details open" not in body  # collapsed


def test_revoked_nodes_are_hidden_from_buckets(ctx):
    """Revoked nodes are no longer part of the fleet — they shouldn't
       appear in any bucket. Matches fleet_status's behavior."""
    now = int(time.time())
    _node(ctx.db, name="gone", last_seen=now,
          state=core_db.NODE_STATE_REVOKED,
          health={"last_anthropic_error": None})
    body = ctx.client.get("/nodes").text
    assert "gone" not in body
    assert "No nodes enrolled yet" in body


# --- relative freshness ----------------------------------------------------

def test_last_seen_column_renders_relative_duration(ctx):
    """Last seen is a compact relative duration, not an absolute UTC
       string — operators read `4s` faster than `2026-05-27 18:14
       UTC`. Absolute timestamp moves to the cell's title tooltip."""
    now = int(time.time())
    _node(ctx.db, name="recent", last_seen=now - 4,
          health={"last_anthropic_error": None})
    body = ctx.client.get("/nodes").text
    assert "<code>4s</code>" in body    # relative cell value
    assert "title=\"" in body           # absolute lives in title


# --- HTMX polling contract -------------------------------------------------

def test_fleet_table_partial_endpoint_returns_self_replacing_block(ctx):
    """The /nodes/fleet-table partial wraps itself in the same
       `id="fleet-table"` outer div with hx-get pointing back at the
       same URL — so HTMX's outerHTML swap re-installs the polling
       handler with each cycle."""
    r = ctx.client.get("/nodes/fleet-table")
    assert r.status_code == 200
    assert 'id="fleet-table"' in r.text
    assert 'hx-get="/nodes/fleet-table"' in r.text
    assert 'hx-trigger="every 10s"' in r.text


def test_nodes_page_includes_the_polled_partial(ctx):
    """The full /nodes page includes the same fleet-table block, so
       the first render matches what the poll will subsequently
       refresh. Prevents a layout shift on the first poll."""
    body = ctx.client.get("/nodes").text
    assert 'id="fleet-table"' in body
    assert 'hx-get="/nodes/fleet-table"' in body


# --- ordering within bucket ------------------------------------------------

def test_rows_within_bucket_sort_by_last_seen_desc(ctx):
    """Within each bucket the most-recently-seen row appears first —
       so the operator's eye lands on what's most active."""
    now = int(time.time())
    _node(ctx.db, name="older",  last_seen=now - 100,
          health={"last_anthropic_error": None})
    _node(ctx.db, name="newer",  last_seen=now - 5,
          health={"last_anthropic_error": None})
    _node(ctx.db, name="newest", last_seen=now - 1,
          health={"last_anthropic_error": None})
    body = ctx.client.get("/nodes").text
    # All three are idle. Check the row order in the rendered HTML.
    assert (body.index("newest") < body.index("newer") < body.index("older"))
