"""Tests for the operator node-management / enrollment UI (core/ui.py),
driven through FastAPI's TestClient. TestClient follows the redirect-after-POST,
so a POST assertion lands on the refreshed /nodes page."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import core_db, ui


@pytest.fixture
def ctx(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.include_router(ui.router)
    app.state.db = db
    return SimpleNamespace(client=TestClient(app), db=db)


def test_nodes_page_when_empty(ctx):
    r = ctx.client.get("/nodes")
    assert r.status_code == 200 and "No node is waiting" in r.text


def test_pending_enrollment_is_listed(ctx):
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    r = ctx.client.get("/nodes")
    assert enr["user_code"] in r.text and "builder-7" in r.text


def test_enroll_page_looks_up_a_code(ctx):
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    r = ctx.client.get("/enroll", params={"code": enr["user_code"]})
    assert r.status_code == 200
    assert "builder-7" in r.text and "Approve" in r.text


def test_enroll_page_unknown_code(ctx):
    r = ctx.client.get("/enroll", params={"code": "ZZZZ-ZZZZ"})
    assert "No enrollment found" in r.text


def test_add_tenant(ctx):
    r = ctx.client.post("/nodes/tenants", data={"name": "Acme Corp"})
    assert r.status_code == 200 and "Acme Corp" in r.text
    assert len(core_db.list_clients(ctx.db)) == 1


def test_approve_enrollment_binds_the_node_to_the_tenant(ctx):
    cid = core_db.register_client(ctx.db, "Acme")
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    r = ctx.client.post(f"/nodes/enrollments/{enr['user_code']}/approve",
                        data={"client_id": cid})
    assert r.status_code == 200
    assert "No node is waiting" in r.text          # off the pending queue
    nodes = core_db.list_nodes(ctx.db)
    assert len(nodes) == 1 and nodes[0]["client_id"] == cid


def test_deny_enrollment(ctx):
    enr = core_db.create_enrollment(ctx.db)
    r = ctx.client.post(f"/nodes/enrollments/{enr['user_code']}/deny")
    assert r.status_code == 200
    assert core_db.get_enrollment_by_user_code(
        ctx.db, enr["user_code"])["state"] == "denied"
