"""Unit tests for the eligibility-flag and draft-task helpers in core_db —
the deterministic-gate state that drives the merge-gate draft pipeline.

Covers: set/clear/mark_suppressed/mark_defer_watermark and the actionable
filter; enqueue_draft_task → claim → complete with the lease + debounce
behavior that bounds the gate to one outstanding draft at a time."""
import json
import time

import pytest

from core import core_db


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


# --- eligibility flags ----------------------------------------------------

def test_set_eligibility_flag_inserts_one_row(db):
    fid = core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE,
        {"bootstrap_ci_lower": 0.31, "icc": 0.72})
    row = db.execute(
        "SELECT subject_kind, subject_id, kind, evidence_snapshot, "
        "suppressed_at, defer_watermark_at FROM eligibility_flags "
        "WHERE id=?", (fid,)).fetchone()
    assert row["subject_kind"] == core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE
    assert row["subject_id"] == "c-1"
    assert row["kind"] == core_db.ELIGIBILITY_KIND_GRADUATE
    assert json.loads(row["evidence_snapshot"])["icc"] == 0.72
    assert row["suppressed_at"] is None
    assert row["defer_watermark_at"] is None


def test_set_eligibility_flag_is_idempotent_on_tuple(db):
    """Re-setting the same (subject_kind, subject_id, kind) refreshes the
       evidence snapshot but doesn't duplicate the row."""
    f1 = core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE,
        {"icc": 0.60})
    f2 = core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE,
        {"icc": 0.80})
    assert f1 == f2
    row = db.execute(
        "SELECT evidence_snapshot FROM eligibility_flags WHERE id=?",
        (f1,)).fetchone()
    assert json.loads(row["evidence_snapshot"])["icc"] == 0.80


def test_set_eligibility_flag_clears_prior_suppression(db):
    """If a flag is re-set after a Reject (suppressed) or Defer
       (watermark), the fresh evidence clears those marks — the gate is
       now firing again on counter-fresh evidence."""
    f = core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE, {})
    core_db.mark_flag_suppressed(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE)
    core_db.mark_flag_defer_watermark(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE)
    # re-set should clear the marks
    core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE, {})
    row = db.execute(
        "SELECT suppressed_at, defer_watermark_at FROM eligibility_flags "
        "WHERE id=?", (f,)).fetchone()
    assert row["suppressed_at"] is None
    assert row["defer_watermark_at"] is None


def test_set_eligibility_flag_rejects_bad_subject_kind(db):
    with pytest.raises(ValueError):
        core_db.set_eligibility_flag(
            db, 99, "c-1", core_db.ELIGIBILITY_KIND_GRADUATE, {})


def test_set_eligibility_flag_rejects_bad_kind(db):
    with pytest.raises(ValueError):
        core_db.set_eligibility_flag(
            db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1", 99, {})


def test_clear_eligibility_flag_removes_the_row(db):
    core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE, {})
    core_db.clear_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE)
    assert db.execute(
        "SELECT COUNT(*) FROM eligibility_flags").fetchone()[0] == 0


def test_list_actionable_eligibility_flags_filters_suppressed_and_deferred(db):
    """Actionable = unsuppressed AND not currently defer-watermarked."""
    # Two flags; suppress one, defer one — both must drop out.
    core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE, {})
    core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-2",
        core_db.ELIGIBILITY_KIND_GRADUATE, {})
    # c-3: actionable
    core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-3",
        core_db.ELIGIBILITY_KIND_GRADUATE, {})
    core_db.mark_flag_suppressed(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE)
    core_db.mark_flag_defer_watermark(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-2",
        core_db.ELIGIBILITY_KIND_GRADUATE)
    actionable = core_db.list_actionable_eligibility_flags(db)
    assert {f["subject_id"] for f in actionable} == {"c-3"}


def test_list_actionable_eligibility_flags_filters_by_kind(db):
    """The optional `kind` arg restricts to one recommendation type."""
    core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE, {})
    core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CHECK, "chk-1",
        core_db.ELIGIBILITY_KIND_PRUNE_INEFFECTIVE, {})
    only_prune = core_db.list_actionable_eligibility_flags(
        db, kind=core_db.ELIGIBILITY_KIND_PRUNE_INEFFECTIVE)
    assert len(only_prune) == 1
    assert only_prune[0]["subject_id"] == "chk-1"


def test_list_actionable_eligibility_flags_decodes_evidence_snapshot(db):
    core_db.set_eligibility_flag(
        db, core_db.ELIGIBILITY_SUBJECT_KIND_CANDIDATE, "c-1",
        core_db.ELIGIBILITY_KIND_GRADUATE,
        {"bootstrap_ci_lower": 0.31, "supporting_sessions": [1, 2]})
    rows = core_db.list_actionable_eligibility_flags(db)
    assert rows[0]["evidence_snapshot"]["bootstrap_ci_lower"] == 0.31
    assert rows[0]["evidence_snapshot"]["supporting_sessions"] == [1, 2]


# --- draft tasks ----------------------------------------------------------

def test_enqueue_draft_task_round_trips_the_snapshot_and_parent_id(db):
    """Plant a methodology version + parent proposal so the FKs resolve."""
    v = core_db.add_methodology_version(db, {"version": 1, "name": "t"})
    parent = core_db.add_proposal(
        db, core_db.METHODOLOGY_PROPOSAL_TYPE_GRADUATE, {})
    snapshot = [{"flag_id": 1, "kind": "graduate"}]
    tid = core_db.enqueue_draft_task(
        db, snapshot, methodology_version=v, parent_proposal_id=parent)
    row = db.execute(
        "SELECT eligibility_flag_snapshot, parent_proposal_id, "
        "methodology_version, state FROM draft_tasks WHERE id=?",
        (tid,)).fetchone()
    assert json.loads(row["eligibility_flag_snapshot"]) == snapshot
    assert row["parent_proposal_id"] == parent
    assert row["methodology_version"] == v
    assert row["state"] == core_db.DRAFT_TASK_STATE_CLAIMABLE


def test_claim_draft_task_returns_none_when_queue_empty(db):
    assert core_db.claim_draft_task(db, "worker-1") is None


def test_claim_draft_task_marks_claimed_and_stamps_lease(db):
    core_db.enqueue_draft_task(db, [{"flag_id": 1}])
    claim = core_db.claim_draft_task(db, "worker-1", lease_seconds=600)
    assert claim is not None
    assert claim["claim_id"]
    row = db.execute(
        "SELECT state, claimed_by, lease_expires FROM draft_tasks "
        "WHERE claim_id=?", (claim["claim_id"],)).fetchone()
    assert row["state"] == core_db.DRAFT_TASK_STATE_CLAIMED
    assert row["claimed_by"] == "worker-1"
    assert row["lease_expires"] is not None


def test_claim_draft_task_decodes_the_eligibility_snapshot(db):
    """Claims hand the node the snapshot as decoded JSON, not a raw blob."""
    snapshot = [{"flag_id": 1, "kind": "graduate", "subject_id": "c-1"}]
    core_db.enqueue_draft_task(db, snapshot)
    claim = core_db.claim_draft_task(db, "worker-1")
    assert claim["eligibility_flag_snapshot"] == snapshot


def test_claim_draft_task_re_offers_an_expired_claim(db):
    """A claim whose lease has elapsed (worker dead) is re-offered to the
       next caller — the atomic UPDATE-or-claim selects expired rows too."""
    core_db.enqueue_draft_task(db, [{"flag_id": 1}])
    first = core_db.claim_draft_task(db, "worker-1", lease_seconds=0)
    # let the lease expire (lease_seconds=0 expires immediately)
    time.sleep(0.01)
    second = core_db.claim_draft_task(db, "worker-2")
    assert second is not None
    assert second["id"] == first["id"]              # same draft task
    assert second["claim_id"] != first["claim_id"]  # fresh claim id


def test_complete_draft_task_records_the_record(db):
    core_db.enqueue_draft_task(db, [{"flag_id": 1}])
    claim = core_db.claim_draft_task(db, "worker-1")
    record = {"outcome": "drafted", "proposals": []}
    assert core_db.complete_draft_task(db, claim["claim_id"], record) == "ok"
    row = db.execute(
        "SELECT state, record, completed_at FROM draft_tasks WHERE id=?",
        (claim["id"],)).fetchone()
    assert row["state"] == core_db.DRAFT_TASK_STATE_COMPLETED
    assert json.loads(row["record"]) == record
    assert row["completed_at"] is not None


def test_complete_draft_task_returns_lapsed_for_unknown_claim(db):
    assert core_db.complete_draft_task(db, "nope-cid", {}) == "lapsed"


def test_complete_draft_task_double_submit_is_a_safe_noop(db):
    core_db.enqueue_draft_task(db, [{"flag_id": 1}])
    claim = core_db.claim_draft_task(db, "worker-1")
    cid = claim["claim_id"]
    assert core_db.complete_draft_task(db, cid, {"first": True}) == "ok"
    # second submit is still "ok" but doesn't overwrite
    assert core_db.complete_draft_task(db, cid, {"second": True}) == "ok"
    record = json.loads(db.execute(
        "SELECT record FROM draft_tasks WHERE claim_id=?", (cid,)
    ).fetchone()["record"])
    assert record == {"first": True}


def test_has_outstanding_draft_task_reflects_queue_state(db):
    assert core_db.has_outstanding_draft_task(db) is False
    core_db.enqueue_draft_task(db, [{"flag_id": 1}])
    assert core_db.has_outstanding_draft_task(db) is True       # claimable
    claim = core_db.claim_draft_task(db, "worker-1")
    assert core_db.has_outstanding_draft_task(db) is True       # claimed
    core_db.complete_draft_task(db, claim["claim_id"], {})
    assert core_db.has_outstanding_draft_task(db) is False      # terminal
