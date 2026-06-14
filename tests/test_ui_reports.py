"""Tests for the operator /reports page (core/ui.py reports_page)."""
import json
import re
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import core_db, ui


@pytest.fixture
def ctx(tmp_path, fake_admin_session):
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.include_router(ui.router)
    fake_admin_session(app)
    app.state.db = db
    return SimpleNamespace(client=TestClient(app), db=db)


def _plant_yesterday(db):
    """One completed review yesterday — enough for every chart to have
       a non-zero closed day."""
    ts = int(time.time()) - 86400
    db.execute(
        "INSERT INTO patchsets (root_message_id,subject,n_patches,"
        "gathered_at) VALUES ('r1@x','s',1,?)", (ts,))
    db.execute(
        "INSERT INTO work_items (type,root_message_id,state,enqueued_at,"
        "completed_at,claimed_by,record) VALUES (?,?,?,?,?,?,?)",
        (core_db.WORK_ITEM_TYPE_REVIEW, "r1@x",
         core_db.WORK_ITEM_STATE_COMPLETED, ts - 60, ts, "node-1",
         json.dumps({"usage": {"input_tokens": 11, "output_tokens": 3,
                               "duration_ms": 4000}})))
    db.commit()


def test_reports_page_renders_and_materializes(ctx):
    """The page view materializes the closed days (lazy, not per-view:
       the second GET writes nothing) and renders the chart canvases
       with their embedded Chart.js configs."""
    _plant_yesterday(ctx.db)
    r = ctx.client.get("/reports")
    assert r.status_code == 200
    body = r.text
    # All five charts present as canvas + embedded JSON config.
    assert body.count("<canvas") == 5
    cfg = json.loads(re.search(
        r'id="chart-cfg-ops_daily">(.*?)</script>', body, re.S).group(1))
    assert cfg["type"] == "bar"
    labels = [d["label"] for d in cfg["data"]["datasets"]]
    assert labels == ["Prepare", "Review", "Train"]
    # Yesterday's completed review is in the data; today is the partial
    # final bar.
    review = cfg["data"]["datasets"][1]
    assert sum(review["data"]) == 1
    assert cfg["data"]["labels"][-1] == "today"
    # Summary cards + the vendored chart lib.
    assert "Input tokens" in body and "chart.umd.min.js" in body

    # Materialized once: yesterday's row exists, and a second view
    # neither rewrites it nor changes its stamp.
    row = ctx.db.execute(
        "SELECT computed_at FROM daily_stats ORDER BY day DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    ctx.db.execute("UPDATE daily_stats SET computed_at=1")
    ctx.db.commit()
    assert ctx.client.get("/reports").status_code == 200
    stamps = {r[0] for r in ctx.db.execute(
        "SELECT computed_at FROM daily_stats")}
    assert stamps == {1}                       # untouched by the re-view


def test_reports_page_empty_db_shows_no_data(ctx):
    r = ctx.client.get("/reports")
    assert r.status_code == 200
    assert "No activity recorded yet" in r.text
    assert "<canvas" not in r.text


def test_reports_nav_entry_is_admin_only(ctx, tmp_path, fake_admin_session):
    """The navbar offers Reports to the admin; a regular user's pages
       don't show it (the route itself is require_config_admin-gated,
       same dependency as /site-settings)."""
    _plant_yesterday(ctx.db)
    assert 'href="/reports"' in ctx.client.get("/queue").text

    app = FastAPI()
    app.include_router(ui.router)
    fake_admin_session(app, is_config_admin=False)
    app.state.db = ctx.db
    assert 'href="/reports"' not in TestClient(app).get("/queue").text


# --- /reports/check-usage (per-check usage analytics) ---------------------

def test_check_usage_page_empty(ctx):
    """No coverage yet → the page renders its empty state, no chart canvas."""
    r = ctx.client.get("/reports/check-usage")
    assert r.status_code == 200
    assert "No review coverage recorded yet" in r.text
    assert "<canvas" not in r.text


def test_check_usage_page_renders_with_data(ctx):
    """A review with check_coverage renders the table row, the floating-CI
       chart canvas + embedded config, and honours the ?mv= version filter."""
    mv = core_db.add_methodology_version(ctx.db, {"name": "t", "checks": []})
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="s", n_patches=1)
    core_db.upsert_ai_review(
        ctx.db, "<r1@x>", concerns=[], methodology_version=mv,
        check_coverage=[{"id": "concurrency", "applicable": True,
                         "gate": "specific", "fired": True, "n_concerns": 1}])
    body = ctx.client.get("/reports/check-usage").text
    assert "Check usage" in body
    assert "concurrency" in body
    assert 'data-report-chart="check_usage"' in body
    cfg = json.loads(re.search(
        r'id="chart-cfg-check_usage">(.*?)</script>', body, re.S).group(1))
    assert cfg["type"] == "bar" and cfg["options"]["indexAxis"] == "y"
    # version cohort filter resolves
    assert ctx.client.get(f"/reports/check-usage?mv={mv}").status_code == 200


def test_check_usage_nav_entry_is_admin_only(ctx, tmp_path, fake_admin_session):
    assert 'href="/reports/check-usage"' in ctx.client.get("/queue").text
    app = FastAPI()
    app.include_router(ui.router)
    fake_admin_session(app, is_config_admin=False)
    app.state.db = ctx.db
    assert 'href="/reports/check-usage"' not in TestClient(app).get("/queue").text
