"""Unit tests for the OAuth enrollment / token data layer in core/core_db.py."""
import pytest

from core import core_db


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _approved_node(db):
    """Create an enrollment and approve it; return the new node id."""
    enr = core_db.create_enrollment(db)
    return core_db.approve_enrollment(db, enr["user_code"])


def test_schema_migrated_to_head(db):
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(
        core_db._MIGRATIONS)
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"patchsets", "messages", "ai_reviews", "patchset_metadata",
            "review_evaluations", "list_tags", "patchset_tags",
            "methodology_versions", "methodology_candidates",
            "methodology_proposals", "eligibility_flags",
            "work_items", "draft_tasks",
            "training_sessions", "training_session_patchsets",
            "patchset_session_history",
            "nodes", "node_enrollments",
            "node_tokens", "gather_state"} <= tables


def test_reviewer_tracking_is_out_of_scope(db):
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "reviewers" not in tables
    assert "reviewer_emails" not in tables


def test_create_and_look_up_enrollment(db):
    enr = core_db.create_enrollment(db, node_name="n1", task_types=["review"])
    assert enr["device_code"] and enr["user_code"]
    by_dc = core_db.get_enrollment_by_device_code(db, enr["device_code"])
    assert by_dc["state"] == core_db.NODE_ENROLLMENT_STATE_PENDING
    assert by_dc["node_name"] == "n1"


def test_user_code_lookup_is_normalized(db):
    enr = core_db.create_enrollment(db)
    typed = enr["user_code"].lower().replace("-", "")     # operator sloppiness
    assert core_db.get_enrollment_by_user_code(db, typed) is not None


def test_device_code_is_stored_hashed(db):
    enr = core_db.create_enrollment(db)
    row = db.execute("SELECT device_code_hash FROM node_enrollments").fetchone()
    assert row["device_code_hash"] != enr["device_code"]


def test_approve_creates_a_node(db):
    node_id = _approved_node(db)
    node = core_db.get_node(db, node_id)
    assert node is not None
    assert node["state"] == core_db.NODE_STATE_ACTIVE


def test_approve_rejects_unknown_or_decided_enrollments(db):
    with pytest.raises(KeyError):
        core_db.approve_enrollment(db, "ZZZZ-ZZZZ")
    enr = core_db.create_enrollment(db)
    core_db.deny_enrollment(db, enr["user_code"])
    with pytest.raises(ValueError):
        core_db.approve_enrollment(db, enr["user_code"])


def test_issue_and_resolve_access_token(db):
    node_id = _approved_node(db)
    tok = core_db.issue_tokens(db, node_id)
    node = core_db.resolve_access_token(db, tok["access_token"])
    assert node and node["id"] == node_id
    assert core_db.resolve_access_token(db, "bogus-token") is None


def test_expired_access_token_is_rejected(db):
    node_id = _approved_node(db)
    tok = core_db.issue_tokens(db, node_id, access_ttl=-1)   # already expired
    assert core_db.resolve_access_token(db, tok["access_token"]) is None


def test_refresh_rotates_and_is_single_use(db):
    node_id = _approved_node(db)
    tok = core_db.issue_tokens(db, node_id)
    fresh = core_db.rotate_refresh_token(db, tok["refresh_token"])
    assert fresh and core_db.resolve_access_token(db, fresh["access_token"])
    assert core_db.resolve_access_token(db, tok["access_token"]) is None
    assert core_db.rotate_refresh_token(db, tok["refresh_token"]) is None


def test_revoke_node_kills_its_tokens(db):
    node_id = _approved_node(db)
    tok = core_db.issue_tokens(db, node_id)
    core_db.revoke_node(db, node_id)
    assert core_db.resolve_access_token(db, tok["access_token"]) is None
    assert core_db.rotate_refresh_token(db, tok["refresh_token"]) is None


def test_complete_enrollment_makes_the_device_code_single_use(db):
    enr = core_db.create_enrollment(db)
    eid = core_db.get_enrollment_by_device_code(db, enr["device_code"])["id"]
    core_db.complete_enrollment(db, eid)
    assert core_db.get_enrollment_by_device_code(
        db, enr["device_code"])["state"] \
        == core_db.NODE_ENROLLMENT_STATE_COMPLETED


def test_list_pending_enrollments(db):
    core_db.create_enrollment(db, node_name="n1")
    denied = core_db.create_enrollment(db, node_name="n2")
    core_db.deny_enrollment(db, denied["user_code"])
    pending = core_db.list_pending_enrollments(db)
    assert [e["node_name"] for e in pending] == ["n1"]


def test_enqueue_and_claim_one_review_per_patchset(db):
    # claim_work_item stamps methodology_version on the row at claim time,
    # which FK-references methodology_versions — so plant one.
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    core_db.upsert_patchset(db, "<root@x>", subject="t", n_patches=1)
    # review is gated on the prepare task having produced a metadata row
    core_db.upsert_patchset_metadata(
        db, "<root@x>", mode="heuristic",
        tree_state={}, subsystem={"primary": "net"},
        patch_size={"bucket": "small"}, maintainer={},
        patch_type={"primary": "bugfix"},
        review_intensity={"bucket_overall": "light"},
        preparation_notes={})
    wid1 = core_db.enqueue_review(db, "<root@x>")
    wid2 = core_db.enqueue_review(db, "<root@x>")            # idempotent
    assert wid1 == wid2
    claim = core_db.claim_work_item(db, worker_id="node-1",
                                     methodology_version=1,
                                     types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    assert claim and claim["root_message_id"] == "root@x"
    assert claim["type"] == core_db.WORK_ITEM_TYPE_REVIEW
    # the queue is drained for this type
    assert core_db.claim_work_item(
        db, worker_id="node-1",
        methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,)) is None
