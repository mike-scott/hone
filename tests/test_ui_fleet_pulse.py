"""Tests for the fleet-pulse chip endpoint (GET /fleet-status) and
the view-model that drives its tone / label / tooltip. Covers the
loudest-wins branching (errored → danger, stale → warning, healthy
in-flight → success, idle → success, empty → muted) so future
changes to thresholds don't silently demote the chip."""
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
        (name, "[]", state, int(time.time()), last_seen,
         health_json, int(time.time())))
    db.commit()
    return cur.lastrowid


# --- endpoint contract -----------------------------------------------------

def test_fleet_status_endpoint_returns_chip_partial(ctx):
    """The endpoint serves the same `_fleet_pulse.html` partial the
       base nav embeds — HTMX outer-HTML swap replaces the
       placeholder in place."""
    r = ctx.client.get("/fleet-status")
    assert r.status_code == 200
    # Outer element is the badge anchor with id="fleet-pulse" so
    # HTMX's outerHTML swap finds and replaces the loading
    # placeholder cleanly.
    assert 'id="fleet-pulse"' in r.text
    assert 'href="/nodes"' in r.text                # click drills in
    assert 'hx-get="/fleet-status"' in r.text       # self-replacing poll


# --- empty fleet -----------------------------------------------------------

def test_chip_renders_muted_with_no_nodes(ctx):
    r = ctx.client.get("/fleet-status")
    assert "text-bg-secondary" in r.text            # muted tone
    assert "no nodes" in r.text


# --- healthy fleets --------------------------------------------------------

def test_chip_renders_success_for_idle_healthy_fleet(ctx):
    now = int(time.time())
    _node(ctx.db, name="n1", last_seen=now,
          health={"last_anthropic_error": None})
    r = ctx.client.get("/fleet-status")
    assert "text-bg-success" in r.text
    assert "idle" in r.text


def test_chip_renders_success_for_working_healthy_fleet(ctx):
    now = int(time.time())
    _node(ctx.db, name="n1", last_seen=now,
          health={"last_anthropic_error": None})
    # one in-flight claim
    ctx.db.execute(
        "INSERT INTO patchsets (root_message_id,subject,submitter_email,sent,"
        "n_patches,gathered_at) VALUES (?,?,?,?,?,?)",
        ("<r1@x>", "s", "a@b", now, 1, now))
    ctx.db.execute(
        "INSERT INTO work_items (type,root_message_id,state,enqueued_at) "
        "VALUES (?,?,?,?)",
        (core_db.WORK_ITEM_TYPE_PREPARE, "<r1@x>",
         core_db.WORK_ITEM_STATE_CLAIMED, now))
    ctx.db.commit()
    r = ctx.client.get("/fleet-status")
    assert "text-bg-success" in r.text
    assert "in flight" in r.text


# --- loud states win the colour -------------------------------------------

def test_chip_renders_warning_when_any_stale(ctx):
    now = int(time.time())
    _node(ctx.db, name="n1", last_seen=now,
          health={"last_anthropic_error": None})
    _node(ctx.db, name="n2", last_seen=now - 10_000,
          health={"last_anthropic_error": None})
    r = ctx.client.get("/fleet-status")
    assert "text-bg-warning" in r.text
    assert "1 stale" in r.text


def test_chip_renders_danger_when_any_errored(ctx):
    """An errored node makes the chip red regardless of how many
       healthy or stale peers it has — operator sees the loud
       signal first."""
    now = int(time.time())
    _node(ctx.db, name="n1", last_seen=now,
          health={"last_anthropic_error": None})
    _node(ctx.db, name="n2", last_seen=now - 10_000,
          health={"last_anthropic_error": None})
    _node(ctx.db, name="n3", last_seen=now,
          health={"last_anthropic_error": "auth"})
    r = ctx.client.get("/fleet-status")
    assert "text-bg-danger" in r.text
    assert "1 errored" in r.text


# --- view-model branches ---------------------------------------------------

def test_view_singular_grammar_for_one_node():
    """A one-node fleet says `1 node`, not `1 nodes`. Touches the
       label rendering (singular vs plural) without going through
       HTMX."""
    # Direct view-model exercise — no app needed, but we do need a
    # tiny DB + runtime config to call it.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = core_db.connect(f"{td}/h.db")
        _node(db, name="solo", last_seen=int(time.time()),
              health={"last_anthropic_error": None})
        rc = runtime_config.load(f"{td}/c.yaml")
        v = ui._fleet_pulse_view(db, rc)
    assert "1 node" in v["label"]
    assert "1 nodes" not in v["label"]
