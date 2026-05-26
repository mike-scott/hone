"""Tests for POST /v1/claims/{id}/result — the submit_result handler.

The completion record IS the body; `task_type` selects the schema
branch (prepare / review / train / draft). hone-core validates against
common/schema/completion-record.schema.yaml, then routes by task_type:
prepare writes a patchset_metadata row + fires maybe_enqueue_review;
review writes an ai_review (no train enqueue — trains are session-driven);
train records the per-train comparison; draft completes the draft task
and queues each proposed proposal into methodology_proposals.

core_db is stubbed so the handler runs in isolation.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import api, core_db

HEADERS = {"Authorization": "Bearer good-token"}
USAGE   = {"input_tokens": 100, "output_tokens": 50, "duration_ms": 9000}
# The methodology version the fake row pretends was stamped at claim time.
# Picked deliberately != 1 so the assertion proves the value flowed off
# the row rather than being defaulted somewhere.
FAKE_MV = 7

# A minimally-valid review concern matching the current schema shape.
CONCERN = {"concern_id":          "rev-c-001",
           "stage_id":             "2",
           "candidate_or_check_id": "object-lifetime",
           "text":                 "use-after-free at frob()",
           "severity":             "critical",
           "is_preexisting":       False,
           "patch_scope":          {"kind":  "patch",
                                    "patches": ["<p1@x>"]},
           "locations":            [{"file": "drivers/x.c"}]}


@pytest.fixture
def client(monkeypatch):
    """A TestClient over the v1 router with core_db stubbed: a bearer
       token resolves to a fake node; work / draft submissions return
       'ok'; ai_review and patchset_metadata writes are captured for
       assertions."""
    state = SimpleNamespace(ai_review_written=None,
                            patchset_metadata_written=None,
                            review_re_enqueued=None,
                            draft_completed=None,
                            proposals_added=[])

    monkeypatch.setattr(core_db, "resolve_access_token",
                        lambda db, tok: {"id": 1}
                        if tok == "good-token" else None)
    monkeypatch.setattr(core_db, "submit_work_result",
                        lambda db, cid, *, state, record: "ok")
    monkeypatch.setattr(core_db, "complete_draft_task",
                        lambda db, cid, record: "ok")

    def fake_upsert_ai_review(db, root, *, concerns, **kw):
        state.ai_review_written = (root, concerns, kw)
        return 1

    def fake_upsert_patchset_metadata(db, root, *, mode, **fields):
        state.patchset_metadata_written = (root, mode, fields)

    def fake_maybe_enqueue_review(db, root):
        state.review_re_enqueued = root
        return None

    def fake_add_proposal(db, ptype, payload):
        state.proposals_added.append((ptype, payload))
        return len(state.proposals_added)

    monkeypatch.setattr(core_db, "upsert_ai_review", fake_upsert_ai_review)
    monkeypatch.setattr(core_db, "upsert_patchset_metadata",
                        fake_upsert_patchset_metadata)
    monkeypatch.setattr(core_db, "maybe_enqueue_review",
                        fake_maybe_enqueue_review)
    monkeypatch.setattr(core_db, "add_proposal", fake_add_proposal)

    # the work_items / draft_tasks row lookups the handler does after
    # submit_work_result / complete_draft_task. Both rows carry the
    # methodology_version that was stamped at claim/enqueue time — that's
    # what the handler now reads back to drive downstream writes.
    class _FakeRow(dict):
        def __getitem__(self, k):
            return super().__getitem__(k)

    class _FakeDB:
        def execute(self, sql, params=()):
            if "draft_tasks" in sql:
                row = _FakeRow(methodology_version=FAKE_MV)
            else:
                row = _FakeRow(root_message_id="<r1@x>",
                                methodology_version=FAKE_MV)

            class _C:
                def fetchone(s):
                    return row
            return _C()

    app = FastAPI()
    app.include_router(api.router)
    app.state.config = SimpleNamespace(fleet_secret="f", admin_token="a")
    app.state.db = _FakeDB()
    return SimpleNamespace(http=TestClient(app), state=state)


# --- review ----------------------------------------------------------------

def test_valid_review_result_writes_ai_review(client):
    """A reviewed outcome captures the concerns into ai_reviews. There is
       NO train enqueue (trains are session-driven now). The
       methodology_version stamped on the work_items row flows through to
       the ai_reviews write — not from the record. The audit-link
       node_id is the authenticated node from the bearer token, not
       a parse of the record's worker_id (which is the node name)."""
    r = client.http.post(
        "/v1/claims/c1/result",
        json={"task_type": "review", "worker_id": "builder-7",
              "outcome": "reviewed", "concerns": [CONCERN],
              "self_review_record": {"summary": "no challenges arose",
                                     "challenges": []},
              "model": "claude-opus-4-7", "usage": USAGE},
        headers=HEADERS)
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    root, concerns, kw = client.state.ai_review_written
    assert root == "<r1@x>"
    # Concerns are stored as a flat list, not wrapped in {"concerns": [...]}.
    assert concerns == [CONCERN]
    # Version is read off the work_items row (FAKE_MV), not the record.
    assert kw["methodology_version"] == FAKE_MV
    # The audit link is the authenticated node's id (the fixture's
    # fake node is id=1), NOT an int() of the record's worker_id
    # which is the name "builder-7" and could never parse cleanly.
    assert kw["node_id"] == 1


def test_review_unappliable_does_not_write_ai_review(client):
    r = client.http.post(
        "/v1/claims/c1/result",
        json={"task_type": "review", "worker_id": "1",
              "outcome": "unappliable",
              "reason": "base commit not obtainable",
              "model": "claude-opus-4-7", "usage": USAGE},
        headers=HEADERS)
    assert r.status_code == 200
    assert client.state.ai_review_written is None


def test_malformed_review_record_rejected(client):
    # outcome=reviewed but no `concerns` / `self_review_record` → schema 422
    r = client.http.post(
        "/v1/claims/c1/result",
        json={"task_type": "review", "worker_id": "1",
              "outcome": "reviewed",
              "model": "claude-opus-4-7", "usage": USAGE},
        headers=HEADERS)
    assert r.status_code == 422 and "schema validation" in r.json()["detail"]


def test_methodology_version_on_record_is_rejected(client):
    """The schema forbids methodology_version on the record (it lives on
       the row, set at claim time). A node that echoes it back is 422'd."""
    r = client.http.post(
        "/v1/claims/c1/result",
        json={"task_type": "review", "worker_id": "1",
              "methodology_version": 1,                 # forbidden
              "outcome": "unappliable",
              "reason": "base commit not obtainable",
              "model": "claude-opus-4-7", "usage": USAGE},
        headers=HEADERS)
    assert r.status_code == 422


# --- prepare ---------------------------------------------------------------

PREPARE_METADATA = {
    "patchset_id":       "<r1@x>",
    "tree_state":        {"tree_available": True,
                          "base_commit_source": "trailer",
                          "prerequisite_patch_ids": []},
    "subsystem":         {"primary": "drivers/net", "secondary": [],
                          "cross_cutting": False, "uncertain_paths": [],
                          "source": "tree"},
    "patch_size":        {"lines_added": 10, "lines_removed": 2,
                          "files_modified": 1, "files_added": 0,
                          "files_deleted": 0, "files_renamed": 0,
                          "hunks": 1, "bucket": "small", "series_length": 1,
                          "churn_ratio": {"max": None, "mean": None,
                                          "high_churn_file_count": None},
                          "source": "tree"},
    "maintainer":        {"authoritative_set": [],
                          "authoritative_reviewer_set": [],
                          "mailing_lists": [], "cc_list_size": 0,
                          "source": "thread"},
    "patch_type":        {"primary": "bugfix", "secondary": [],
                          "evidence": {"primary": "Fixes: trailer"},
                          "source": "thread"},
    "review_intensity":  {"bucket_overall": "light",
                          "reply_count": 1, "unique_reviewers": 1,
                          "trailer_only_count": 0, "light_count": 1,
                          "substantive_count": 0, "deep_count": 0,
                          "had_nack": False, "had_v_next": False,
                          "per_reply": [], "source": "thread"},
    "preparation_notes": {"warnings": [], "confidence": "medium",
                          "mode": "heuristic"},
}


def test_prepare_prepared_writes_metadata_and_fires_review_enqueue(client):
    body = {"task_type": "prepare", "worker_id": "1",
            "outcome": "prepared",
            "model": "claude-opus-4-7", "usage": USAGE,
            "self_review_record": {"summary": "ok", "challenges": []},
            **PREPARE_METADATA}
    r = client.http.post("/v1/claims/c1/result", json=body, headers=HEADERS)
    assert r.status_code == 200, r.json()
    assert client.state.patchset_metadata_written is not None
    root, mode, fields = client.state.patchset_metadata_written
    assert root == "<r1@x>" and mode == "heuristic"
    assert client.state.review_re_enqueued == "<r1@x>"
    # Version is read off the work_items row (FAKE_MV), not the record.
    assert fields["methodology_version"] == FAKE_MV


def test_prepare_uncharacterisable_does_not_write_metadata(client):
    r = client.http.post(
        "/v1/claims/c1/result",
        json={"task_type": "prepare", "worker_id": "1",
              "outcome": "uncharacterisable",
              "reason": "patchset malformed",
              "model": "claude-opus-4-7", "usage": USAGE},
        headers=HEADERS)
    assert r.status_code == 200
    assert client.state.patchset_metadata_written is None


# --- draft -----------------------------------------------------------------

def test_draft_drafted_queues_proposals(client):
    """Each `propose` disposition queues a corresponding methodology
       proposal (one add_proposal call per proposal)."""
    body = {"task_type": "draft", "worker_id": "1",
            "outcome": "drafted",
            "model": "claude-opus-4-7", "usage": USAGE,
            "eligibility_dispositions": [
                {"flag_id": "elig-1", "disposition": "propose",
                 "proposal_id": "prop-a"}],
            "proposals": [
                {"proposal_id": "prop-a",
                 "recommendation": "graduate",
                 "subject_kind": "candidate",
                 "subject_ids": ["c-test"],
                 "payload": {"candidate_id": "c-test",
                             "graduated_check_id": "c-test",
                             "graduated_text": "graduated body"},
                 "rationale": {"summary": "the catches mature",
                               "evidence_cited":
                                   {"from_eligibility_flag": "elig-1"},
                               "considered_alternatives": []},
                 "predicted_impact":
                     {"expected_fire_rate": 0.4,
                      "expected_unique_catch_rate": 0.3}}],
            "cross_proposal_dependencies": [],
            "node_notes": {"warnings": [], "confidence": "high",
                            "confidence_reason": "strong evidence",
                            "overflow_flags_deferred": []},
            "self_review_record": {"summary": "ok", "challenges": []}}
    r = client.http.post("/v1/claims/c1/result", json=body, headers=HEADERS)
    assert r.status_code == 200
    assert len(client.state.proposals_added) == 1
    ptype, payload = client.state.proposals_added[0]
    assert ptype == core_db.METHODOLOGY_PROPOSAL_TYPE_GRADUATE
    assert payload["recommendation"] == "graduate"
    # base_methodology_version is read off the draft_tasks row (FAKE_MV),
    # not echoed from the record.
    assert payload["base_methodology_version"] == FAKE_MV


def test_draft_failed_queues_no_proposals(client):
    r = client.http.post(
        "/v1/claims/c1/result",
        json={"task_type": "draft", "worker_id": "1",
              "outcome": "failed",
              "reason": "ran out of budget",
              "model": "claude-opus-4-7", "usage": USAGE},
        headers=HEADERS)
    assert r.status_code == 200
    assert client.state.proposals_added == []


# --- release endpoint -----------------------------------------------------

def test_release_endpoint_forwards_to_core_db(client, monkeypatch):
    """POST /v1/claims/{id}/release calls core_db.release_claim with
       the supplied reason and echoes its status back."""
    seen = []
    monkeypatch.setattr(
        core_db, "release_claim",
        lambda db, cid, *, reason=None: seen.append((cid, reason)) or "ok")
    r = client.http.post(
        "/v1/claims/c1/release",
        json={"reason": "Claude API key rejected"},
        headers=HEADERS)
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert seen == [("c1", "Claude API key rejected")]


def test_release_endpoint_accepts_an_empty_body(client, monkeypatch):
    """The reason is optional — a body-less release POST still works."""
    monkeypatch.setattr(
        core_db, "release_claim",
        lambda db, cid, *, reason=None: "ok")
    r = client.http.post("/v1/claims/c1/release", headers=HEADERS)
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_release_endpoint_propagates_lapsed(client, monkeypatch):
    """A claim that's already been reclaimed by lease expiry returns
       'lapsed' — the node can log and move on."""
    monkeypatch.setattr(
        core_db, "release_claim",
        lambda db, cid, *, reason=None: "lapsed")
    r = client.http.post("/v1/claims/c1/release", headers=HEADERS,
                          json={"reason": "x"})
    assert r.status_code == 200 and r.json() == {"status": "lapsed"}


def test_release_endpoint_requires_bearer_token(client):
    """Same auth surface as result / heartbeat — no bearer → 401."""
    r = client.http.post("/v1/claims/c1/release",
                          json={"reason": "x"})
    assert r.status_code == 401


# --- health endpoint ------------------------------------------------------

def test_health_endpoint_writes_the_snapshot(client, monkeypatch):
    """POST /v1/nodes/me/health calls update_node_health with the
       authenticated node's id and the request body verbatim."""
    seen = []
    monkeypatch.setattr(
        core_db, "update_node_health",
        lambda db, nid, snap: seen.append((nid, snap)) or True)
    snapshot = {"free_disk_mb": 1024,
                 "refrepo_size_mb": 4500,
                 "last_anthropic_error": "auth"}
    r = client.http.post("/v1/nodes/me/health",
                          json=snapshot, headers=HEADERS)
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert seen == [(1, snapshot)]              # fixture node id is 1


def test_health_endpoint_accepts_extra_fields(client, monkeypatch):
    """The wire is a loose JSON dict — a future node that adds new
       fields shouldn't 422 against an older hone-core."""
    seen = []
    monkeypatch.setattr(
        core_db, "update_node_health",
        lambda db, nid, snap: seen.append(snap) or True)
    r = client.http.post(
        "/v1/nodes/me/health",
        json={"free_disk_mb": 1, "new_future_field": "future-value"},
        headers=HEADERS)
    assert r.status_code == 200
    assert seen[0]["new_future_field"] == "future-value"


def test_health_endpoint_requires_bearer_token(client):
    r = client.http.post("/v1/nodes/me/health", json={"free_disk_mb": 1})
    assert r.status_code == 401


# --- discriminator + auth --------------------------------------------------

def test_missing_task_type_rejected(client):
    r = client.http.post(
        "/v1/claims/c1/result",
        json={"worker_id": "1",
              "outcome": "reviewed", "concerns": []},
        headers=HEADERS)
    assert r.status_code == 422


def test_missing_bearer_token_rejected(client):
    r = client.http.post(
        "/v1/claims/c1/result",
        json={"task_type": "review", "worker_id": "1",
              "outcome": "reviewed", "concerns": []})
    assert r.status_code == 401
