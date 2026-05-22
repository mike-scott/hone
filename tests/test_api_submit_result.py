"""Unit tests for POST /v1/claims/{claim_id}/result — the completion-record
schema validation wired into core/api.py's submit_result handler.

core_db is stubbed (monkeypatched), so these tests exercise the handler's
auth, validation, and dispatch in isolation from the database.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import api

HEADERS = {"Authorization": "Bearer good-token"}
USAGE = {"tokens": 100, "tool_uses": 3, "duration_ms": 9000}


@pytest.fixture
def client(monkeypatch):
    """A TestClient over the v1 router with core_db and bearer auth stubbed."""
    monkeypatch.setattr(api.core_db, "resolve_access_token",
                        lambda db, tok: {"id": 1, "client_id": 1}
                        if tok == "good-token" else None)
    monkeypatch.setattr(api.core_db, "get_client",
                        lambda db, cid: {"id": cid, "state": "active"})
    monkeypatch.setattr(api.core_db, "complete_review",
                        lambda db, cid, state, record, mv=None: "ok")
    monkeypatch.setattr(api.core_db, "complete_maintenance_task",
                        lambda db, cid, result: "ok")
    app = FastAPI()
    app.include_router(api.router)
    app.state.config = type("Cfg", (), {"fleet_secret": "test-fleet",
                                        "admin_token": "test-admin"})()
    app.state.db = object()
    return TestClient(app)


def _post(client, body):
    return client.post("/v1/claims/claim-1/result", json=body, headers=HEADERS)


def _review_record(**over):
    rec = {"worker_id": "node-1", "methodology_version": 1,
           "outcome": "reviewed", "verdict": "clean", "findings": [],
           "coverage": [{"id": "2", "status": "applied"}],
           "candidate_outcomes": [], "source_comparison": [],
           "residual_risk": "None identified.", "usage": USAGE}
    rec.update(over)
    return rec


# --- valid submissions -----------------------------------------------------

def test_valid_review_result_accepted(client):
    resp = _post(client, {"task_type": "review", "state": "reviewed",
                          "methodology_version": 1,
                          "record": _review_record()})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_valid_deferred_review_accepted(client):
    record = {"worker_id": "n", "methodology_version": 1,
              "outcome": "deferred", "reason": "base tree unobtainable"}
    resp = _post(client, {"task_type": "review", "state": "deferred",
                          "methodology_version": 1, "record": record})
    assert resp.status_code == 200


def test_valid_maintenance_result_accepted(client):
    record = {"worker_id": "n", "methodology_version": 1,
              "outcome": "completed", "proposals": [], "usage": USAGE}
    resp = _post(client, {"task_type": "maintenance", "result": record})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- schema rejection ------------------------------------------------------

def test_malformed_review_record_rejected(client):
    """A reviewed record missing required fields is 422, not stored."""
    bad = {"worker_id": "n", "methodology_version": 1,
           "outcome": "reviewed", "verdict": "clean"}        # no coverage etc.
    resp = _post(client, {"task_type": "review", "state": "reviewed",
                          "methodology_version": 1, "record": bad})
    assert resp.status_code == 422
    assert "schema validation" in resp.text


def test_maintenance_shaped_record_on_review_claim_rejected(client):
    """Branch isolation: a maintenance record cannot close a review claim."""
    maint = {"worker_id": "n", "methodology_version": 1,
             "outcome": "completed", "proposals": [], "usage": USAGE}
    resp = _post(client, {"task_type": "review", "state": "reviewed",
                          "methodology_version": 1, "record": maint})
    assert resp.status_code == 422
    assert "schema validation" in resp.text


def test_malformed_maintenance_record_rejected(client):
    bad = {"worker_id": "n", "outcome": "completed", "proposals": []}
    resp = _post(client, {"task_type": "maintenance", "result": bad})
    assert resp.status_code == 422
    assert "schema validation" in resp.text


def test_state_outcome_mismatch_rejected(client):
    """The envelope `state` must match the record's `outcome`."""
    record = {"worker_id": "n", "methodology_version": 1,
              "outcome": "deferred", "reason": "x"}
    resp = _post(client, {"task_type": "review", "state": "reviewed",
                          "methodology_version": 1, "record": record})
    assert resp.status_code == 422
    assert "does not match" in resp.text


# --- envelope / auth basics ------------------------------------------------

def test_missing_record_rejected(client):
    resp = _post(client, {"task_type": "review", "state": "reviewed"})
    assert resp.status_code == 422


def test_unknown_task_type_rejected(client):
    resp = _post(client, {"task_type": "audit"})
    assert resp.status_code == 422


def test_missing_bearer_token_rejected(client):
    resp = client.post("/v1/claims/claim-1/result",
                        json={"task_type": "maintenance", "result": {}})
    assert resp.status_code == 401
