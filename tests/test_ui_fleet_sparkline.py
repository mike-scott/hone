"""Tests for the /fleet-sparkline endpoint and its view-model."""
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


def _patchset(db, root="<r1@x>"):
    db.execute(
        "INSERT INTO patchsets (root_message_id,subject,submitter_email,"
        "sent,n_patches,gathered_at) VALUES (?,?,?,?,?,?)",
        (root, "s", "a@b", int(time.time()), 1, int(time.time())))
    db.commit()


def _completion(db, completed_at):
    db.execute(
        "INSERT INTO work_items (type,root_message_id,state,enqueued_at,"
        "completed_at) VALUES (?,?,?,?,?)",
        (core_db.WORK_ITEM_TYPE_PREPARE, "<r1@x>",
         core_db.WORK_ITEM_STATE_COMPLETED, completed_at - 30,
         completed_at))
    db.commit()


def test_endpoint_returns_self_replacing_sparkline_partial(ctx):
    """The endpoint serves the partial the base nav embeds. HTMX's
       outerHTML swap replaces the placeholder in place on each
       10s poll."""
    r = ctx.client.get("/fleet-sparkline")
    assert r.status_code == 200
    assert 'id="fleet-sparkline"' in r.text
    assert 'hx-get="/fleet-sparkline"' in r.text
    assert "<polyline" in r.text
    assert "<svg" in r.text


def test_empty_window_renders_a_flat_baseline(ctx):
    """With no completions in the last hour the SVG still has 60
       points — operator gets a stable layout (and the tooltip
       reads `0 claims completed in the last 60 min`)."""
    r = ctx.client.get("/fleet-sparkline")
    assert "0 claims completed in the last 60 min" in r.text
    # 60 bins → 60 "i,y" points in the polyline. Each point has a
    # comma; an empty / nbin window would not produce any commas.
    assert r.text.count(",") >= 60


def test_recent_completion_drives_the_tooltip_count(ctx):
    _patchset(ctx.db)
    _completion(ctx.db, completed_at=int(time.time()))
    r = ctx.client.get("/fleet-sparkline")
    assert "1 claim completed in the last 60 min" in r.text   # singular


def test_multiple_completions_render_plural_tooltip(ctx):
    _patchset(ctx.db)
    now = int(time.time())
    for offset in (0, 60, 120):
        _completion(ctx.db, completed_at=now - offset)
    r = ctx.client.get("/fleet-sparkline")
    assert "3 claims completed in the last 60 min" in r.text   # plural
