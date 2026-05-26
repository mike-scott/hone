"""Tests for the /v1/claims endpoint and the api.py payload builders.

`test_api_submit_result.py` covers the result-submission path against a
heavily-stubbed core_db. This file exercises the dispatch + payload
assembly against a real in-memory database so the four payload shapes are
constructed end-to-end."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import api, core_db

HEADERS = {"Authorization": "Bearer good-token"}


def _plant_metadata(db, root):
    core_db.upsert_patchset_metadata(
        db, root, mode="heuristic",
        tree_state={"tree_available": True},
        subsystem={"primary": "drivers/net"},
        patch_size={"bucket": "small"},
        maintainer={"primary": "alice@k.org"},
        patch_type={"primary": "bugfix"},
        review_intensity={"bucket_overall": "light", "per_reply": []},
        preparation_notes={"mode": "heuristic"})


@pytest.fixture
def ctx(tmp_path):
    """A FastAPI test client over the real v1 router with a real db
       (in-memory-but-on-disk so the migrations run), a stub node
       resolver, and a bootstrapped methodology."""
    db = core_db.connect(str(tmp_path / "hone.db"))
    core_db.add_methodology_version(
        db, {"name": "test", "version": 1,
             "principles": [{"id": "p1", "title": "P", "body": "..."}],
             "stages": [{"id": "0", "title": "S", "applies": "x",
                          "body": "..."}],
             "checks": [],
             "operations": {"prepare":  {"guidance": "g-p", "return": "r-p"},
                            "review":   {"guidance": "g-r", "return": "r-r"},
                            "train":    {"guidance": "g-t", "return": "r-t"},
                            "draft":    {"guidance": "g-d", "return": "r-d"}}})
    app = FastAPI()
    app.include_router(api.router)
    app.state.db = db
    app.state.config = SimpleNamespace(fleet_secret="f", admin_token="a")
    # the bearer-token authenticator resolves to a fake node
    import json as _json
    node = {"id": 1, "task_types":
            _json.dumps(["prepare", "review", "train", "draft"])}

    def fake_resolve(_db, tok):
        return node if tok == "good-token" else None

    app.state.config = SimpleNamespace(fleet_secret="f", admin_token="a")
    app.dependency_overrides = {}
    # We can't monkeypatch on a session-scope fixture cheaply; do it via
    # the require_node dependency override.
    app.dependency_overrides[api.require_node] = lambda: node
    return SimpleNamespace(http=TestClient(app), db=db, node=node)


# --- _compile_methodology (pure function — narrow slice for prepare) ------

def test_compile_methodology_prepare_narrows_to_principles_only():
    """The prepare task gets `core: { principles }` only — no stages,
       checks, documentation_review, or report_finalization. See
       docs/ARCHITECTURE.md → Methodology storage. The methodology
       document stores these fields at the top level (per
       common/schema/methodology.schema.yaml)."""
    doc = {"principles":          [{"id": "p1"}],
            "stages":              [{"id": "0"}],
            "checks":              [{"id": "c1"}],
            "documentation_review": {"body": "..."},
            "report_finalization": {"body": "..."},
            "operations": {"prepare": {"guidance": "g", "return": "r"},
                            "review":  {"guidance": "g2", "return": "r2"}}}
    out = api._compile_methodology(doc, "prepare")
    assert out["core"] == {"principles": [{"id": "p1"}]}
    assert out["operations"] == {"prepare": {"guidance": "g", "return": "r"}}


def test_compile_methodology_review_gets_the_full_core():
    """Review / train / draft get the full `core` block — every governing
       field present in the document, lifted under the synthetic `core`
       wrapper."""
    doc = {"principles": [{"id": "p1"}],
            "stages":     [{"id": "0"}],
            "checks":     [{"id": "c1"}],
            "operations": {"review": {"guidance": "g", "return": "r"}}}
    out = api._compile_methodology(doc, "review")
    assert "stages" in out["core"] and "checks" in out["core"]


def test_compile_methodology_omits_other_operations():
    """Only the asked-for operation's prompt rides along in the slice."""
    doc = {"principles": [],
            "operations": {"review": {"guidance": "gr", "return": "rr"},
                            "train":  {"guidance": "gt", "return": "rt"}}}
    out = api._compile_methodology(doc, "review")
    assert out["operations"] == {"review": {"guidance": "gr",
                                              "return": "rr"}}


# --- /v1/claims endpoint --------------------------------------------------

def test_claim_returns_204_when_queue_empty(ctx):
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 204


def test_claim_serves_a_prepare_task_with_the_narrow_methodology(ctx):
    """A claimable prepare task surfaces as a 200 with the prepare-shaped
       payload + a `core` slice that carries only `principles`."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_prepare(ctx.db, "<r1@x>")
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    payload = r.json()
    assert payload["task_type"] == "prepare"
    assert payload["claim_id"]
    assert set(payload["methodology"]["core"].keys()) == {"principles"}
    assert payload["patchset"]["root_message_id"] == "r1@x"
    assert payload["patchset"]["declared_base_commit"] is None
    assert any(p["message_id"] == "p1@x" for p in payload["patches"])


def test_claim_stamps_methodology_version_on_the_work_items_row(ctx):
    """The active methodology version is frozen on the work_items row at
       claim time — not at result-submission time. So a row that's still
       in the CLAIMED state already carries its methodology_version, and
       the column matches what the payload advertised."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_prepare(ctx.db, "<r1@x>")
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    payload = r.json()
    row = ctx.db.execute(
        "SELECT state, methodology_version FROM work_items WHERE claim_id=?",
        (payload["claim_id"],)).fetchone()
    assert row["state"] == core_db.WORK_ITEM_STATE_CLAIMED
    assert row["methodology_version"] == payload["methodology_version"]
    assert row["methodology_version"] == 1            # the only active version


def test_claim_serves_a_review_task_with_full_core_and_patchset_metadata(ctx):
    """A review claim payload carries the patchset_metadata produced by
       prepare and gets the full `core` block (not narrowed)."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    _plant_metadata(ctx.db, "<r1@x>")
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_review(ctx.db, "<r1@x>")
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    payload = r.json()
    assert payload["task_type"] == "review"
    assert "stages" in payload["methodology"]["core"]
    assert payload["patchset_metadata"]["subsystem"] == {
        "primary": "drivers/net"}
    assert payload["patchset_metadata"]["patch_size"] == {"bucket": "small"}


def test_claim_serves_a_train_task_with_session_fields_and_named_comment(ctx):
    """A train claim payload echoes the session metadata and names the
       specific comment via comment_message_id, not a "latest comment"
       guess."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    _plant_metadata(ctx.db, "<r1@x>")
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.upsert_message(ctx.db, "<c1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_COMMENT, body="LGTM",
                           parent_message_id="<p1@x>",
                           author_name="A", author_email="a@k.org")
    core_db.upsert_ai_review(ctx.db, "<r1@x>", concerns=[])
    sid = core_db.create_session_draft(ctx.db, "standard")
    core_db.add_session_patchset(ctx.db, sid, "<r1@x>",
                                  role=core_db.SESSION_ROLE_POOL,
                                  stratum_label="net:light")
    core_db.enqueue_session_train(
        ctx.db, session_id=sid, root_message_id="<r1@x>",
        patch_message_id="<p1@x>", comment_message_id="<c1@x>",
        session_role=core_db.SESSION_ROLE_POOL, stratum_label="net:light")
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    payload = r.json()
    assert payload["task_type"] == "train"
    assert payload["training_session_id"] == sid
    assert payload["session_role"] == "pool"
    assert payload["stratum_label"] == "net:light"
    assert payload["patch"]["message_id"] == "p1@x"
    assert payload["comment"]["message_id"] == "c1@x"
    assert payload["comment"]["body"] == "LGTM"
    assert payload["ai_review"] == {"concerns": []}


def test_claim_serves_a_draft_task_when_no_work_items_available(ctx):
    """When the work_items queue is empty, the dispatcher falls through to
       the draft queue and serves a draft claim."""
    core_db.enqueue_draft_task(
        ctx.db, [{"flag_id": "elig-1", "kind": "graduate"}],
        methodology_version=1)
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    payload = r.json()
    assert payload["task_type"] == "draft"
    assert payload["eligibility_flags"] == [
        {"flag_id": "elig-1", "kind": "graduate"}]
    # draft task gets the full core slice, draft-only operation prompt.
    assert payload["methodology"]["operations"].keys() == {"draft"}


def test_claim_503s_when_methodology_not_bootstrapped(tmp_path):
    """The claim dispatcher requires an active methodology to compile the
       slice; without one it returns 503."""
    db = core_db.connect(str(tmp_path / "hone.db"))                # no bootstrap
    app = FastAPI()
    app.include_router(api.router)
    app.state.db = db
    app.state.config = SimpleNamespace(fleet_secret="f", admin_token="a")
    app.dependency_overrides[api.require_node] = lambda: {
        "id": 1, "task_types": None}
    r = TestClient(app).post("/v1/claims", headers=HEADERS)
    assert r.status_code == 503


def test_claim_respects_node_task_types_filter(ctx):
    """A node declaring only `prepare` in its task_types must NOT be
       offered a review/train claim, and must NOT receive a draft task."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    _plant_metadata(ctx.db, "<r1@x>")
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_review(ctx.db, "<r1@x>")
    core_db.enqueue_draft_task(ctx.db, [{"flag_id": 1}])

    # Tighten the node's declared task_types to prepare-only.
    import json as _json
    ctx.node["task_types"] = _json.dumps(["prepare"])
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    # No prepare task is queued; the review and draft are out of scope; 204.
    assert r.status_code == 204
