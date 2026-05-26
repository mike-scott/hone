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
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7",
                                     task_types=["review"])
    r = ctx.client.get("/nodes")
    assert enr["user_code"] in r.text and "builder-7" in r.text
    assert "review" in r.text                            # capabilities cell


def test_enroll_page_looks_up_a_code(ctx):
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    r = ctx.client.get("/enroll", params={"code": enr["user_code"]})
    assert r.status_code == 200
    assert "builder-7" in r.text and "Approve" in r.text


def test_enroll_page_unknown_code(ctx):
    r = ctx.client.get("/enroll", params={"code": "ZZZZ-ZZZZ"})
    assert "No enrollment found" in r.text


def test_enroll_page_already_decided(ctx):
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    core_db.deny_enrollment(ctx.db, enr["user_code"])
    r = ctx.client.get("/enroll", params={"code": enr["user_code"]})
    assert "already denied" in r.text


def test_approve_enrollment(ctx):
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    r = ctx.client.post(f"/nodes/enrollments/{enr['user_code']}/approve")
    assert r.status_code == 200
    assert "No node is waiting" in r.text          # off the pending queue
    nodes = core_db.list_nodes(ctx.db)
    assert len(nodes) == 1 and nodes[0]["name"] == "builder-7"
    assert nodes[0]["state"] == core_db.NODE_STATE_ACTIVE


def test_deny_enrollment(ctx):
    enr = core_db.create_enrollment(ctx.db)
    r = ctx.client.post(f"/nodes/enrollments/{enr['user_code']}/deny")
    assert r.status_code == 200
    assert core_db.get_enrollment_by_user_code(
        ctx.db, enr["user_code"])["state"] \
        == core_db.NODE_ENROLLMENT_STATE_DENIED


def test_enrolled_node_renders_state_active(ctx):
    enr = core_db.create_enrollment(ctx.db, node_name="builder-9")
    core_db.approve_enrollment(ctx.db, enr["user_code"])
    r = ctx.client.get("/nodes")
    assert "builder-9" in r.text and "active" in r.text


# --- delete an enrolled node ---------------------------------------------

def test_nodes_page_renders_a_delete_form_per_enrolled_row(ctx):
    """Each enrolled-node row carries a POST form that targets the
       delete endpoint and hooks the shared confirm-modal via the
       `data-confirm` attribute. A fat-finger click on Delete is
       intercepted by the modal in base.html, themed to match the
       rest of the UI."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    body = ctx.client.get("/nodes").text
    assert f'action="/nodes/{node_id}/delete"' in body
    assert "Delete</button>" in body
    # The data-confirm attribute carries the message the modal shows;
    # data-confirm-title + data-confirm-button parameterise the modal
    # chrome. base.html ships the shared modal markup + handler.
    assert "data-confirm=" in body and "builder-7" in body
    assert 'data-confirm-button="Delete"' in body
    assert 'id="confirm-modal"' in body


def test_delete_node_via_ui_removes_the_node_and_redirects(ctx):
    """A POST to /nodes/{id}/delete drops the row, kills tokens, and
       lands back on the refreshed /nodes page where the row is gone."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    tok = core_db.issue_tokens(ctx.db, node_id)

    r = ctx.client.post(f"/nodes/{node_id}/delete")
    assert r.status_code == 200             # TestClient follows the redirect
    assert "builder-7" not in r.text        # off the enrolled list
    assert core_db.get_node(ctx.db, node_id) is None
    assert core_db.resolve_access_token(ctx.db, tok["access_token"]) is None


def test_delete_node_via_ui_is_idempotent_on_unknown_id(ctx):
    """A double-submit from a stale tab — or a click against an
       already-deleted node — must not 500; it just redirects to
       /nodes again."""
    r = ctx.client.post("/nodes/99999/delete")
    assert r.status_code == 200             # redirect followed, no crash
