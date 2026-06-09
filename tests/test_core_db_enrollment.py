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


def test_create_enrollment_rejects_a_name_already_taken(db):
    """A node enrolling with a name that's already active is rejected at
       the OAuth device-authorization step — no pending enrollment is
       created, the registering node gets a fast fail."""
    enr = core_db.create_enrollment(db, node_name="builder-7")
    core_db.approve_enrollment(db, enr["user_code"])
    with pytest.raises(core_db.DuplicateNodeName, match="builder-7"):
        core_db.create_enrollment(db, node_name="builder-7")


def test_create_enrollment_allows_name_when_existing_node_is_revoked(db):
    """A revoked node is a tombstone — it shouldn't block a fresh
       enrollment using the same name. The operator can also Delete
       the tombstone if they want it gone from the listing."""
    enr1 = core_db.create_enrollment(db, node_name="builder-7")
    node_id = core_db.approve_enrollment(db, enr1["user_code"])
    core_db.revoke_node(db, node_id)
    # The same name now enrolls cleanly.
    enr2 = core_db.create_enrollment(db, node_name="builder-7")
    assert enr2["device_code"] != enr1["device_code"]


def test_create_enrollment_allows_a_null_name(db):
    """A node that didn't self-identify (node_name=None) never
       conflicts with another such node — the duplicate check is
       gated on a non-empty name."""
    a = core_db.create_enrollment(db, node_name=None)
    b = core_db.create_enrollment(db, node_name=None)
    assert a["device_code"] != b["device_code"]


def test_approve_enrollment_rejects_a_now_conflicting_name(db):
    """Race-protection: two enrollments with the same name both got
       through device_authorization (e.g. submitted simultaneously,
       both passed the check). The first approval succeeds; the
       second hits the active-node guard at approve_enrollment time
       and raises DuplicateNodeName."""
    # Both enrollments are queued in pending state.
    enr1 = core_db.create_enrollment(db, node_name="builder-7")
    # Manually queue a second pending enrollment with the same name —
    # bypassing the create-time check to simulate the race window.
    db.execute(
        "INSERT INTO node_enrollments (device_code_hash,user_code,"
        "node_name,task_types,state,interval_seconds,created_at,expires_at) "
        "VALUES ('h','C-2','builder-7',NULL,?,5,?,?)",
        (core_db.NODE_ENROLLMENT_STATE_PENDING,
         int(__import__("time").time()),
         int(__import__("time").time()) + 900))
    db.commit()
    # First approval lands.
    core_db.approve_enrollment(db, enr1["user_code"])
    # Second approval now conflicts.
    with pytest.raises(core_db.DuplicateNodeName, match="builder-7"):
        core_db.approve_enrollment(db, "C-2")


def test_work_items_for_node_returns_recent_claims(db):
    """work_items_for_node returns rows the node has claimed, ordered
       by most-recent activity. The detail page renders this as the
       Recent claims table."""
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    # Plant a patchset and enqueue a prepare; claim it as "builder-7"
    # (the api layer writes the node name into claimed_by for a named
    # node — see api.claim_task).
    core_db.upsert_patchset(db, "<r1@x>", subject="s1", n_patches=1)
    core_db.upsert_patchset_metadata(
        db, "<r1@x>", mode="heuristic",
        tree_state={}, subsystem={"primary": "n"},
        patch_size={"bucket": "small"}, maintainer={},
        patch_type={"primary": "bugfix"},
        review_intensity={"bucket_overall": "light"},
        preparation_notes={})
    core_db.enqueue_review(db, "<r1@x>")
    claim = core_db.claim_work_item(
        db, "builder-7", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    assert claim is not None

    rows = core_db.work_items_for_node(db, "builder-7")
    assert len(rows) == 1
    assert rows[0]["root_message_id"] == "r1@x"
    assert rows[0]["subject"] == "s1"
    # Returns empty for an unknown claimed_by label.
    assert core_db.work_items_for_node(db, "no-such-node") == []


def test_work_items_for_node_paginates_with_offset(db):
    """offset + limit slice the claim history for the node-detail
       Recent-claims paginator; count_work_items_for_node gives the
       total the paginator's page math needs."""
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    # Plant 5 patchsets, enqueue + claim a review for each as "n1".
    for i in range(5):
        root = f"<r{i}@x>"
        core_db.upsert_patchset(db, root, subject=f"s{i}", n_patches=1)
        core_db.upsert_patchset_metadata(
            db, root, mode="heuristic", tree_state={},
            subsystem={"primary": "n"}, patch_size={"bucket": "small"},
            maintainer={}, patch_type={"primary": "bugfix"},
            review_intensity={"bucket_overall": "light"},
            preparation_notes={})
        core_db.enqueue_review(db, root)
        core_db.claim_work_item(db, "n1", methodology_version=1,
                                 types=(core_db.WORK_ITEM_TYPE_REVIEW,))

    assert core_db.count_work_items_for_node(db, "n1") == 5
    assert core_db.count_work_items_for_node(db, "nobody") == 0
    # First page of 2, then the next 2, then the last 1 — disjoint,
    # covering all five.
    p1 = core_db.work_items_for_node(db, "n1", limit=2, offset=0)
    p2 = core_db.work_items_for_node(db, "n1", limit=2, offset=2)
    p3 = core_db.work_items_for_node(db, "n1", limit=2, offset=4)
    assert [len(p) for p in (p1, p2, p3)] == [2, 2, 1]
    ids = {r["id"] for r in p1} | {r["id"] for r in p2} | {r["id"]
                                                             for r in p3}
    assert len(ids) == 5                              # no overlap


def test_ai_reviews_for_node_returns_audited_reviews(db):
    """ai_reviews_for_node returns rows whose node_id matches, with
       concerns decoded from JSON to a list."""
    enr = core_db.create_enrollment(db, node_name="builder-7")
    node_id = core_db.approve_enrollment(db, enr["user_code"])
    core_db.upsert_patchset(db, "<r1@x>", subject="s1", n_patches=1)
    core_db.upsert_ai_review(
        db, "<r1@x>", concerns=[{"concern_id": "c-1", "severity": "minor"}],
        model="claude-opus-4-7", node_id=node_id)

    rows = core_db.ai_reviews_for_node(db, node_id)
    assert len(rows) == 1
    assert rows[0]["root_message_id"] == "r1@x"
    assert rows[0]["concerns"] == [{"concern_id": "c-1", "severity": "minor"}]
    # Unrelated node → no rows.
    other = core_db.approve_enrollment(
        db, core_db.create_enrollment(db, node_name="builder-8")["user_code"])
    assert core_db.ai_reviews_for_node(db, other) == []


def test_update_node_health_round_trips_a_snapshot(db):
    """update_node_health writes the JSON snapshot + a timestamp; the
       getters return it as a decoded dict so the UI never touches
       json.loads."""
    enr = core_db.create_enrollment(db, node_name="builder-7")
    node_id = core_db.approve_enrollment(db, enr["user_code"])
    # Fresh node has no health yet.
    fresh = core_db.get_node(db, node_id)
    assert fresh["health"] is None and fresh["health_at"] is None
    # First report lands as a dict on the row.
    snapshot = {"free_disk_mb": 1000, "refrepo_size_mb": 4500,
                 "last_anthropic_error": None}
    assert core_db.update_node_health(db, node_id, snapshot) is True
    row = core_db.get_node(db, node_id)
    assert row["health"] == snapshot
    assert isinstance(row["health_at"], int) and row["health_at"] > 0
    # list_nodes also decodes.
    listed = core_db.list_nodes(db)
    assert listed[0]["health"] == snapshot


def test_update_node_health_overwrites_the_previous_snapshot(db):
    """Latest-snapshot semantics: a second report replaces the first
       (no history kept in the row)."""
    enr = core_db.create_enrollment(db, node_name="builder-7")
    node_id = core_db.approve_enrollment(db, enr["user_code"])
    core_db.update_node_health(db, node_id, {"free_disk_mb": 1000})
    core_db.update_node_health(db, node_id, {"free_disk_mb": 500,
                                              "last_anthropic_error": "auth"})
    assert core_db.get_node(db, node_id)["health"] == {
        "free_disk_mb": 500, "last_anthropic_error": "auth"}


def test_update_node_health_returns_false_for_unknown_id(db):
    """A snapshot POST race-loses to a delete_node: the helper
       returns False and the endpoint silently no-ops."""
    assert core_db.update_node_health(db, 99999, {"x": 1}) is False


def test_active_node_with_name_helper(db):
    enr = core_db.create_enrollment(db, node_name="builder-7")
    node_id = core_db.approve_enrollment(db, enr["user_code"])
    match = core_db.active_node_with_name(db, "builder-7")
    assert match is not None and match["id"] == node_id
    assert core_db.active_node_with_name(db, "builder-8") is None
    # Revoked → no longer matches.
    core_db.revoke_node(db, node_id)
    assert core_db.active_node_with_name(db, "builder-7") is None
    # NULL / empty name never matches.
    assert core_db.active_node_with_name(db, None) is None
    assert core_db.active_node_with_name(db, "") is None


def test_revoke_node_kills_its_tokens(db):
    node_id = _approved_node(db)
    tok = core_db.issue_tokens(db, node_id)
    core_db.revoke_node(db, node_id)
    assert core_db.resolve_access_token(db, tok["access_token"]) is None
    assert core_db.rotate_refresh_token(db, tok["refresh_token"]) is None


def test_delete_node_removes_the_row_and_its_tokens(db):
    """`delete_node` is the hard-delete companion to `revoke_node`:
       the row, the tokens, and the device-grant link all go. The
       audit references on ai_reviews are NULLed rather than dropped
       so the historical record survives the deletion."""
    node_id = _approved_node(db)
    tok = core_db.issue_tokens(db, node_id)
    # Plant an ai_reviews row referencing the node so we can check the
    # nullification.
    core_db.upsert_patchset(db, "<r1@x>", subject="t", n_patches=1)
    core_db.upsert_ai_review(db, "<r1@x>", concerns=[], node_id=node_id)

    assert core_db.delete_node(db, node_id) is True

    # The node row, its tokens, and any auth lookups are gone.
    assert core_db.get_node(db, node_id) is None
    assert core_db.resolve_access_token(db, tok["access_token"]) is None
    assert db.execute(
        "SELECT COUNT(*) FROM node_tokens WHERE node_id=?",
        (node_id,)).fetchone()[0] == 0
    # Audit refs preserved: the ai_review row remains; its node_id is
    # NULLed so the FK no longer points anywhere.
    rev = core_db.get_ai_review(db, "<r1@x>")
    assert rev is not None
    assert rev["node_id"] is None
    # Idempotent: a re-delete on the same id is a no-op returning False.
    assert core_db.delete_node(db, node_id) is False


def test_delete_node_returns_false_for_unknown_id(db):
    assert core_db.delete_node(db, 99999) is False


def test_delete_node_nulls_an_enrollment_reference(db):
    """An enrollment that has been approved + completed links to its
       resulting node via node_enrollments.node_id. Deleting that node
       must NULL the link (not raise an FK violation) so a re-enrolment
       afterwards can succeed without surgery."""
    node_id = _approved_node(db)
    # Find the enrollment row that resulted in node_id.
    enr_row = db.execute(
        "SELECT id FROM node_enrollments WHERE node_id=?",
        (node_id,)).fetchone()
    assert enr_row is not None

    assert core_db.delete_node(db, node_id) is True
    again = db.execute(
        "SELECT node_id FROM node_enrollments WHERE id=?",
        (enr_row["id"],)).fetchone()
    assert again is not None and again["node_id"] is None


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


def test_tag_pending_enrollment_first_lookup_wins_and_is_idempotent(db):
    """The first user to tag a pending enrollment owns the pairing; the
       same user re-tagging is a no-op success."""
    alice = core_db.create_user(db, "alice@x", "alice", "local")
    enr = core_db.create_enrollment(db, node_name="n1")
    tagged = core_db.tag_pending_enrollment(db, enr["user_code"], alice)
    assert tagged["requested_by_user_id"] == alice
    again = core_db.tag_pending_enrollment(db, enr["user_code"], alice)
    assert again["requested_by_user_id"] == alice


def test_tag_pending_enrollment_guard_refuses_a_second_user(db):
    """The tag is guarded inside the UPDATE itself (requested_by_user_id
       IS NULL OR = caller), so a second user is refused at the write —
       even a caller whose earlier read raced and saw the tag unset
       cannot overwrite the first user's pairing."""
    alice = core_db.create_user(db, "alice@x", "alice", "local")
    bob = core_db.create_user(db, "bob@x", "bob", "local")
    enr = core_db.create_enrollment(db, node_name="n1")
    core_db.tag_pending_enrollment(db, enr["user_code"], alice)
    assert core_db.tag_pending_enrollment(db, enr["user_code"], bob) is None
    row = db.execute(
        "SELECT requested_by_user_id FROM node_enrollments "
        "WHERE user_code=?", (enr["user_code"],)).fetchone()
    assert row["requested_by_user_id"] == alice


def test_tag_pending_enrollment_refuses_decided_or_unknown(db):
    """No tagging once the enrollment is decided; unknown codes give
       None too."""
    alice = core_db.create_user(db, "alice@x", "alice", "local")
    enr = core_db.create_enrollment(db, node_name="n1")
    core_db.deny_enrollment(db, enr["user_code"])
    assert core_db.tag_pending_enrollment(db, enr["user_code"], alice) is None
    assert core_db.tag_pending_enrollment(db, "ZZZZ-ZZZZ", alice) is None


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
