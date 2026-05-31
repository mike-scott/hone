"""Tests for the work-queue auto-enqueue triggers in core_db.

Only prepare and review are auto-enqueued by gather; trains are
session-driven and created exclusively by the session orchestrator at
materialisation (see test_core_db_sessions for that path).
"""
import pytest

from core import core_db


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _planted_patchset(db, root, n_patches):
    """Plant a gathered patchset with `n_patches` patch messages."""
    core_db.upsert_patchset(db, root, n_patches=n_patches)
    for i in range(1, n_patches + 1):
        core_db.upsert_message(db, f"<m{i}@{root.strip('<>')}>",
                               root_message_id=root,
                               type=core_db.MSG_TYPE_PATCH,
                               part_index=i if n_patches > 1 else None,
                               body=f"patch {i}")


def _planted_metadata(db, root):
    """Plant a minimal patchset_metadata row — the prepare-gate for
       maybe_enqueue_review."""
    core_db.upsert_patchset_metadata(
        db, root, mode="heuristic",
        tree_state={}, subsystem={"primary": "net"},
        patch_size={"bucket": "small"}, maintainer={},
        patch_type={"primary": "bugfix"},
        review_intensity={"bucket_overall": "light"},
        preparation_notes={})


# --- maybe_enqueue_prepare -------------------------------------------------

def test_prepare_enqueued_for_gathered_patchset(db):
    core_db.upsert_patchset(db, "<r1@x>", n_patches=1)
    wid = core_db.maybe_enqueue_prepare(db, "<r1@x>")
    assert wid is not None


def test_prepare_enqueue_is_idempotent(db):
    core_db.upsert_patchset(db, "<r1@x>", n_patches=1)
    wid1 = core_db.maybe_enqueue_prepare(db, "<r1@x>")
    wid2 = core_db.maybe_enqueue_prepare(db, "<r1@x>")
    assert wid1 is not None and wid1 == wid2


def test_prepare_not_enqueued_when_patchset_unknown(db):
    assert core_db.maybe_enqueue_prepare(db, "<nobody@x>") is None


# --- maybe_enqueue_review --------------------------------------------------

def test_review_gated_on_patchset_metadata(db):
    """A patchset whose prepare task hasn't produced metadata yet must not
       have a review enqueued, even when every patch message is present."""
    _planted_patchset(db, "<r1@x>", n_patches=2)
    assert core_db.maybe_enqueue_review(db, "<r1@x>") is None
    _planted_metadata(db, "<r1@x>")
    assert core_db.maybe_enqueue_review(db, "<r1@x>") is not None


def test_review_enqueued_when_all_patches_present(db):
    core_db.upsert_patchset(db, "<r1@x>", n_patches=3)
    _planted_metadata(db, "<r1@x>")
    core_db.upsert_message(db, "<m1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, part_index=1,
                           body="...")
    core_db.upsert_message(db, "<m2@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, part_index=2,
                           body="...")
    # only 2 of 3 → no review yet
    assert core_db.maybe_enqueue_review(db, "<r1@x>") is None

    core_db.upsert_message(db, "<m3@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, part_index=3,
                           body="...")
    wid = core_db.maybe_enqueue_review(db, "<r1@x>")
    assert wid is not None


def test_review_enqueue_is_idempotent(db):
    _planted_patchset(db, "<r1@x>", n_patches=2)
    _planted_metadata(db, "<r1@x>")
    wid1 = core_db.maybe_enqueue_review(db, "<r1@x>")
    wid2 = core_db.maybe_enqueue_review(db, "<r1@x>")
    assert wid1 is not None and wid1 == wid2


def test_review_not_enqueued_for_skipped_patchset(db):
    core_db.mark_skipped(db, "<r1@x>", "filtered")
    assert core_db.maybe_enqueue_review(db, "<r1@x>") is None


def test_review_not_enqueued_when_patchset_unknown(db):
    assert core_db.maybe_enqueue_review(db, "<nobody@x>") is None


# --- session-driven train creation -----------------------------------------

def test_train_work_item_requires_session(db):
    """Direct SQL insert of a train without session fields is rejected by
       the work_items CHECK constraint."""
    import sqlite3
    _planted_patchset(db, "<r1@x>", n_patches=1)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO work_items (type, root_message_id, message_id, "
            "state, enqueued_at) VALUES (?, ?, ?, ?, ?)",
            (core_db.WORK_ITEM_TYPE_TRAIN, "<r1@x>", "<m1@r1@x>",
             core_db.WORK_ITEM_STATE_CLAIMABLE, 0))


def test_enqueue_session_train_creates_session_bound_work_item(db):
    """The session orchestrator's path: a train is created bound to a
       session at insertion, with the comment_message_id naming the
       specific comment evaluated."""
    _planted_patchset(db, "<r1@x>", n_patches=1)
    _planted_metadata(db, "<r1@x>")
    core_db.upsert_message(db, "<c1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_COMMENT,
                           parent_message_id="<m1@r1@x>",
                           body="nack!")
    core_db.upsert_ai_review(db, "<r1@x>", concerns=[])
    sid = core_db.create_session_draft(db, "standard")
    train_id = core_db.enqueue_session_train(
        db, session_id=sid, root_message_id="<r1@x>",
        patch_message_id="<m1@r1@x>", comment_message_id="<c1@x>",
        session_role=core_db.SESSION_ROLE_POOL, stratum_label="net:light")
    assert train_id is not None
    items = core_db.list_work_items(db, type=core_db.WORK_ITEM_TYPE_TRAIN)
    assert len(items) == 1
    # The created row carries every session field + the comment id.
    row = db.execute(
        "SELECT training_session_id, session_role, stratum_label, "
        "comment_message_id FROM work_items WHERE id=?",
        (train_id,)).fetchone()
    assert row["training_session_id"] == sid
    assert row["session_role"] == core_db.SESSION_ROLE_POOL
    assert row["stratum_label"] == "net:light"
    assert row["comment_message_id"] == "c1@x"


def test_enqueue_session_train_requires_hone_node_ai_review(db):
    _planted_patchset(db, "<r1@x>", n_patches=1)
    core_db.upsert_message(db, "<c1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_COMMENT,
                           parent_message_id="<m1@r1@x>", body="…")
    sid = core_db.create_session_draft(db, "standard")
    with pytest.raises(ValueError):
        core_db.enqueue_session_train(
            db, session_id=sid, root_message_id="<r1@x>",
            patch_message_id="<m1@r1@x>", comment_message_id="<c1@x>",
            session_role=core_db.SESSION_ROLE_POOL, stratum_label="x")


# --- patchset_metadata (prepare-task output) ------------------------------

def test_upsert_patchset_metadata_round_trips_all_seven_fields(db):
    _planted_patchset(db, "<r1@x>", n_patches=1)
    fields = dict(tree_state={"tree_available": True},
                  subsystem={"primary": "net"},
                  patch_size={"bucket": "medium"},
                  maintainer={"primary": "alice@k.org"},
                  patch_type={"primary": "bugfix"},
                  review_intensity={"bucket_overall": "heavy"},
                  preparation_notes={"mode": "authoritative"})
    core_db.upsert_patchset_metadata(
        db, "<r1@x>", mode="authoritative",
        methodology_version=None, node_tree_revision="abc123", **fields)
    got = core_db.get_patchset_metadata(db, "<r1@x>")
    assert got["mode"] == "authoritative"
    assert got["node_tree_revision"] == "abc123"
    # Each JSON field decoded to a dict.
    for k, v in fields.items():
        assert got[k] == v


def test_upsert_patchset_metadata_overwrites_an_existing_row(db):
    """Re-preparation against a fresher tree must update the row in place."""
    _planted_patchset(db, "<r1@x>", n_patches=1)
    base_fields = dict(tree_state={}, subsystem={"primary": "net"},
                        patch_size={"bucket": "small"}, maintainer={},
                        patch_type={"primary": "bugfix"},
                        review_intensity={"bucket_overall": "light"},
                        preparation_notes={})
    core_db.upsert_patchset_metadata(
        db, "<r1@x>", mode="heuristic", **base_fields)
    base_fields["subsystem"] = {"primary": "drivers/net"}    # authoritative
    core_db.upsert_patchset_metadata(
        db, "<r1@x>", mode="authoritative", **base_fields)
    got = core_db.get_patchset_metadata(db, "<r1@x>")
    assert got["mode"] == "authoritative"
    assert got["subsystem"]["primary"] == "drivers/net"


def test_upsert_patchset_metadata_rejects_bad_mode(db):
    _planted_patchset(db, "<r1@x>", n_patches=1)
    with pytest.raises(ValueError):
        core_db.upsert_patchset_metadata(
            db, "<r1@x>", mode="speculative",
            tree_state={}, subsystem={}, patch_size={}, maintainer={},
            patch_type={}, review_intensity={}, preparation_notes={})


def test_upsert_patchset_metadata_rejects_missing_fields(db):
    _planted_patchset(db, "<r1@x>", n_patches=1)
    with pytest.raises(ValueError, match="missing metadata fields"):
        core_db.upsert_patchset_metadata(
            db, "<r1@x>", mode="heuristic",
            tree_state={})       # six other required fields absent


def test_get_patchset_metadata_returns_none_for_unprepared(db):
    _planted_patchset(db, "<r1@x>", n_patches=1)
    assert core_db.get_patchset_metadata(db, "<r1@x>") is None


# --- review_evaluations (per (patchset, session) aggregation) -------------

def test_write_and_get_review_evaluation_round_trip(db):
    _planted_patchset(db, "<r1@x>", n_patches=1)
    _planted_metadata(db, "<r1@x>")
    ai_id = core_db.upsert_ai_review(db, "<r1@x>", concerns=[])
    sid = core_db.create_session_draft(db, "standard")
    core_db.write_review_evaluation(
        db, "<r1@x>", ai_id, sid,
        trains_consumed=5, coverage_rate=0.73,
        severity_weighted_coverage_rate=0.81, fp_rate=0.12,
        preexisting_unmatched_count=2, redundancy_pairs=1,
        had_missed_critical=False, had_missed_major=True,
        per_concern_verdicts=[{"concern_id": "rev-c-1",
                                "verdict": "matched_any"}],
        per_candidate_review_stats=[{"candidate_id": "c-1",
                                      "fired": True}],
        notes={"warnings": ["test"]})
    ev = core_db.get_review_evaluation(db, "<r1@x>", ai_id, sid)
    assert ev["session_id"] == sid
    assert ev["coverage_rate"] == 0.73
    assert ev["had_missed_major"] is True
    # JSON columns decoded.
    assert ev["per_concern_verdicts"] == [
        {"concern_id": "rev-c-1", "verdict": "matched_any"}]
    assert ev["notes"] == {"warnings": ["test"]}


def test_review_evaluations_for_patchset_returns_all_sessions(db):
    """A patchset re-used across two sessions produces two eval rows; the
       per-patchset listing returns both ordered by evaluated_at."""
    _planted_patchset(db, "<r1@x>", n_patches=1)
    _planted_metadata(db, "<r1@x>")
    ai_id = core_db.upsert_ai_review(db, "<r1@x>", concerns=[])
    s1 = core_db.create_session_draft(db, "standard")
    s2 = core_db.create_session_draft(db, "exploratory")
    core_db.write_review_evaluation(db, "<r1@x>", ai_id, s1,
                                     coverage_rate=0.4)
    core_db.write_review_evaluation(db, "<r1@x>", ai_id, s2,
                                     coverage_rate=0.8)
    rows = core_db.review_evaluations_for_patchset(db, "<r1@x>")
    assert len(rows) == 2
    assert {r["session_id"] for r in rows} == {s1, s2}


def test_write_review_evaluation_overwrites_on_same_triple(db):
    """Re-aggregation within the same (patchset, ai_review, session)
       atomically overwrites the row — the eval is always the latest pass."""
    _planted_patchset(db, "<r1@x>", n_patches=1)
    _planted_metadata(db, "<r1@x>")
    ai_id = core_db.upsert_ai_review(db, "<r1@x>", concerns=[])
    sid = core_db.create_session_draft(db, "standard")
    core_db.write_review_evaluation(db, "<r1@x>", ai_id, sid,
                                     coverage_rate=0.4)
    core_db.write_review_evaluation(db, "<r1@x>", ai_id, sid,
                                     coverage_rate=0.9)
    assert core_db.get_review_evaluation(
        db, "<r1@x>", ai_id, sid)["coverage_rate"] == 0.9
    assert db.execute(
        "SELECT COUNT(*) FROM review_evaluations").fetchone()[0] == 1


# --- heartbeat (works for both work_items and draft_tasks) ----------------

def test_heartbeat_extends_a_work_item_lease(db):
    """heartbeat looks across both work_items and draft_tasks; updating a
       work_items claim returns True."""
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    _planted_patchset(db, "<r1@x>", n_patches=1)
    core_db.maybe_enqueue_prepare(db, "<r1@x>")
    claim = core_db.claim_work_item(db, "worker-1",
                                     methodology_version=1,
                                     types=(core_db.WORK_ITEM_TYPE_PREPARE,))
    before = db.execute(
        "SELECT lease_expires FROM work_items WHERE claim_id=?",
        (claim["claim_id"],)).fetchone()["lease_expires"]
    assert core_db.heartbeat(db, claim["claim_id"], lease_seconds=7200)
    after = db.execute(
        "SELECT lease_expires FROM work_items WHERE claim_id=?",
        (claim["claim_id"],)).fetchone()["lease_expires"]
    assert after > before


def test_heartbeat_extends_a_draft_task_lease(db):
    """The same heartbeat call routes to draft_tasks when the claim is one."""
    core_db.enqueue_draft_task(db, [{"flag_id": 1}])
    claim = core_db.claim_draft_task(db, "worker-1", lease_seconds=600)
    assert core_db.heartbeat(db, claim["claim_id"], lease_seconds=7200)
    # The draft_tasks row's lease was advanced.
    row = db.execute(
        "SELECT lease_expires FROM draft_tasks WHERE claim_id=?",
        (claim["claim_id"],)).fetchone()
    assert row["lease_expires"] is not None


def test_heartbeat_returns_false_for_unknown_claim(db):
    assert core_db.heartbeat(db, "nope-cid") is False


# --- reclaim_expired (crash recovery) -------------------------------------

def test_reclaim_expired_returns_separate_counts_for_each_queue(db):
    """reclaim_expired sweeps both work_items and draft_tasks; the returned
       tuple is (work_items_reclaimed, draft_tasks_reclaimed)."""
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    _planted_patchset(db, "<r1@x>", n_patches=1)
    core_db.maybe_enqueue_prepare(db, "<r1@x>")
    core_db.enqueue_draft_task(db, [{"flag_id": 1}])
    # Claim both with lease_seconds=0 so they're already expired.
    core_db.claim_work_item(db, "w-1",
                              methodology_version=1,
                              types=(core_db.WORK_ITEM_TYPE_PREPARE,),
                              lease_seconds=0)
    core_db.claim_draft_task(db, "w-2", lease_seconds=0)
    import time as _t
    _t.sleep(0.01)
    w, d = core_db.reclaim_expired(db)
    assert w == 1 and d == 1
    # Both rows are now claimable again.
    assert db.execute(
        "SELECT state FROM work_items WHERE root_message_id=?",
        ("r1@x",)).fetchone()["state"] == core_db.WORK_ITEM_STATE_CLAIMABLE


# --- release_claim (node-initiated abort) ---------------------------------

def test_release_claim_returns_a_claimed_row_to_the_pool(db):
    """release_claim flips state CLAIMED → CLAIMABLE and clears every
       claim-time field — the row looks like a fresh enqueue, ready
       for the next claimer with no lease wait."""
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    _planted_patchset(db, "<r1@x>", n_patches=1)
    core_db.maybe_enqueue_prepare(db, "<r1@x>")
    claim = core_db.claim_work_item(
        db, "worker-1", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_PREPARE,))
    assert core_db.release_claim(
        db, claim["claim_id"], reason="api key rejected") == "ok"
    row = db.execute(
        "SELECT state, claim_id, claimed_by, claimed_at, lease_expires, "
        "heartbeat_at, methodology_version "
        "FROM work_items WHERE id=?", (claim["id"],)).fetchone()
    assert row["state"] == core_db.WORK_ITEM_STATE_CLAIMABLE
    for col in ("claim_id", "claimed_by", "claimed_at", "lease_expires",
                 "heartbeat_at", "methodology_version"):
        assert row[col] is None, f"{col} should be NULL on release"


def test_release_claim_is_safe_to_retry(db):
    """The first release nulls the claim_id, so a second call against
       the same id can't find the row and returns 'lapsed' — the
       work-item is already back in the pool. The node treats
       'lapsed' the same as a successful retry, so a network blip
       between the release request and its 200 doesn't strand the
       node in a bad state."""
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    _planted_patchset(db, "<r1@x>", n_patches=1)
    core_db.maybe_enqueue_prepare(db, "<r1@x>")
    claim = core_db.claim_work_item(
        db, "w-1", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_PREPARE,))
    assert core_db.release_claim(db, claim["claim_id"]) == "ok"
    assert core_db.release_claim(db, claim["claim_id"]) == "lapsed"


def test_release_claim_returns_lapsed_for_unknown_id(db):
    """An unknown claim_id (already reclaimed by lease expiry, or
       never issued) returns 'lapsed' — symmetric with submit_result."""
    assert core_db.release_claim(db, "no-such-claim") == "lapsed"


# --- deferred re-arm (claim picks up lease-elapsed deferred items) ----------

def test_claim_rearms_a_lease_elapsed_deferred_item(db):
    """A deferred work item re-arms once its lease elapses: claim_work_item
       re-offers it just like a lease-expired claim. (A deferred review whose
       base wasn't fetchable should be retried later — e.g. once the tree has
       advanced — not stuck forever.)"""
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    _planted_patchset(db, "<ps-d@x>", n_patches=1)
    core_db.maybe_enqueue_prepare(db, "<ps-d@x>")
    c = core_db.claim_work_item(db, "node-x", methodology_version=1)
    # node defers it (base tree unobtainable); submit_work_result leaves
    # lease_expires intact, so push it into the past to simulate elapse.
    core_db.submit_work_result(db, c["claim_id"],
                               state=core_db.WORK_ITEM_STATE_DEFERRED,
                               record={"outcome": "deferred"})
    db.execute("UPDATE work_items SET lease_expires=? WHERE claim_id=?",
               (1, c["claim_id"]))
    again = core_db.claim_work_item(db, "node-y", methodology_version=1)
    assert again is not None and again["id"] == c["id"]
    row = db.execute("SELECT state, claimed_by FROM work_items WHERE id=?",
                     (c["id"],)).fetchone()
    assert row["state"] == core_db.WORK_ITEM_STATE_CLAIMED
    assert row["claimed_by"] == "node-y"


def test_claim_does_not_rearm_a_deferred_item_before_lease_elapses(db):
    """A still-within-lease deferred item is held, not re-offered — the
       lease is the backoff that stops a deferred review hot-looping."""
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    _planted_patchset(db, "<ps-e@x>", n_patches=1)
    core_db.maybe_enqueue_prepare(db, "<ps-e@x>")
    c = core_db.claim_work_item(db, "node-x", methodology_version=1,
                                lease_seconds=1800)
    core_db.submit_work_result(db, c["claim_id"],
                               state=core_db.WORK_ITEM_STATE_DEFERRED,
                               record={"outcome": "deferred"})
    # lease_expires is still ~30min out (untouched by the defer submit)
    assert core_db.claim_work_item(db, "node-y", methodology_version=1) is None


# --- release_deferred (operator manual re-arm from the UI) ------------------

def _deferred_item(db, root="<ps-rd@x>"):
    """Plant a patchset, enqueue+claim its prepare, and submit a deferred
       result — leaving one DEFERRED work item. Returns its id."""
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    _planted_patchset(db, root, n_patches=1)
    core_db.maybe_enqueue_prepare(db, root)
    c = core_db.claim_work_item(db, "node-x", methodology_version=1)
    core_db.submit_work_result(db, c["claim_id"],
                               state=core_db.WORK_ITEM_STATE_DEFERRED,
                               record={"outcome": "deferred"})
    return c["id"]


def test_release_deferred_reverts_to_claimable_and_clears_claim_fields(db):
    item_id = _deferred_item(db)
    assert core_db.release_deferred(db, item_id) == "ok"
    row = db.execute(
        "SELECT state, claim_id, claimed_by, claimed_at, lease_expires, "
        "heartbeat_at, methodology_version FROM work_items WHERE id=?",
        (item_id,)).fetchone()
    assert row["state"] == core_db.WORK_ITEM_STATE_CLAIMABLE
    assert row["claim_id"] is None and row["claimed_by"] is None
    assert row["claimed_at"] is None and row["lease_expires"] is None
    assert row["heartbeat_at"] is None and row["methodology_version"] is None


def test_release_deferred_is_a_no_op_for_a_non_deferred_item(db):
    """A claimed (or any non-deferred) item is left untouched — guards a
       double click after the row was already re-claimed."""
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    _planted_patchset(db, "<ps-rd2@x>", n_patches=1)
    core_db.maybe_enqueue_prepare(db, "<ps-rd2@x>")
    c = core_db.claim_work_item(db, "node-x", methodology_version=1)
    assert core_db.release_deferred(db, c["id"]) == "not_deferred"
    row = db.execute("SELECT state, claimed_by FROM work_items WHERE id=?",
                     (c["id"],)).fetchone()
    assert row["state"] == core_db.WORK_ITEM_STATE_CLAIMED
    assert row["claimed_by"] == "node-x"


def test_release_deferred_unknown_id(db):
    assert core_db.release_deferred(db, 999999) == "unknown"
