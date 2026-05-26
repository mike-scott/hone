"""Unit tests for the training-session helpers in core_db: session lifecycle,
patchset linkage, per-patchset progress tracking, and the strict-virginity
primitive the session-draft solver uses."""
import json

import pytest

from core import core_db


@pytest.fixture
def db(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    # Many session tests need a patchset row to satisfy the FK in
    # training_session_patchsets; plant a minimal one.
    core_db.upsert_patchset(db, "<r1@x>", n_patches=1)
    core_db.upsert_patchset(db, "<r2@x>", n_patches=1)
    return db


# --- session lifecycle ----------------------------------------------------

def test_create_session_draft_returns_id_in_draft_state(db):
    sid = core_db.create_session_draft(
        db, "standard", target_pool_size=300, target_holdout_size=60,
        stratification_spec={"strata": ["net:light"]},
        methodology_version=None)
    row = db.execute("SELECT state, profile, target_pool_size, "
                     "stratification_spec FROM training_sessions WHERE id=?",
                     (sid,)).fetchone()
    assert row["state"] == core_db.SESSION_STATE_DRAFT
    assert row["profile"] == "standard"
    assert row["target_pool_size"] == 300
    assert json.loads(row["stratification_spec"])["strata"] == ["net:light"]


def test_transition_session_to_complete_stamps_completed_at(db):
    sid = core_db.create_session_draft(db, "standard")
    core_db.transition_session(db, sid, core_db.SESSION_STATE_COMPLETE)
    row = db.execute(
        "SELECT state, completed_at FROM training_sessions WHERE id=?",
        (sid,)).fetchone()
    assert row["state"] == core_db.SESSION_STATE_COMPLETE
    assert row["completed_at"] is not None


def test_transition_session_to_analyzed_stores_stats(db):
    sid = core_db.create_session_draft(db, "standard")
    core_db.transition_session(
        db, sid, core_db.SESSION_STATE_ANALYZED,
        stats={"per_stratum": {"net:light": {"catch_rate": 0.42}}})
    row = db.execute("SELECT state, stats FROM training_sessions WHERE id=?",
                     (sid,)).fetchone()
    assert row["state"] == core_db.SESSION_STATE_ANALYZED
    assert json.loads(row["stats"])["per_stratum"]["net:light"][
        "catch_rate"] == 0.42


def test_transition_session_rejects_unknown_state(db):
    sid = core_db.create_session_draft(db, "standard")
    with pytest.raises(ValueError):
        core_db.transition_session(db, sid, 99)


def test_transition_session_raises_on_unknown_session(db):
    with pytest.raises(KeyError):
        core_db.transition_session(db, 9999, core_db.SESSION_STATE_READY)


# --- patchset linkage -----------------------------------------------------

def test_add_session_patchset_writes_to_both_membership_and_history(db):
    sid = core_db.create_session_draft(db, "standard")
    core_db.add_session_patchset(
        db, sid, "<r1@x>",
        role=core_db.SESSION_ROLE_POOL, stratum_label="net:light")
    member = db.execute(
        "SELECT role, stratum_label FROM training_session_patchsets "
        "WHERE session_id=? AND root_message_id=?",
        (sid, "r1@x")).fetchone()
    hist = db.execute(
        "SELECT role FROM patchset_session_history "
        "WHERE session_id=? AND root_message_id=?",
        (sid, "r1@x")).fetchone()
    assert member["role"] == core_db.SESSION_ROLE_POOL
    assert member["stratum_label"] == "net:light"
    assert hist["role"] == core_db.SESSION_ROLE_POOL


def test_add_session_patchset_is_idempotent(db):
    sid = core_db.create_session_draft(db, "standard")
    for _ in range(3):
        core_db.add_session_patchset(
            db, sid, "<r1@x>",
            role=core_db.SESSION_ROLE_POOL, stratum_label="net:light")
    count = db.execute(
        "SELECT COUNT(*) FROM training_session_patchsets "
        "WHERE session_id=?", (sid,)).fetchone()[0]
    assert count == 1


def test_add_session_patchset_rejects_bad_role(db):
    sid = core_db.create_session_draft(db, "standard")
    with pytest.raises(ValueError):
        core_db.add_session_patchset(db, sid, "<r1@x>",
                                      role=99, stratum_label="x")


# --- per-patchset progress recomputation ---------------------------------

def test_bump_session_patchset_progress_drives_completion_state(db):
    sid = core_db.create_session_draft(db, "standard")
    core_db.add_session_patchset(
        db, sid, "<r1@x>",
        role=core_db.SESSION_ROLE_POOL, stratum_label="net:light")
    # 0/0 → PENDING (no trains yet)
    state = lambda: db.execute(
        "SELECT completion_state, train_work_items_total, "
        "train_work_items_done FROM training_session_patchsets "
        "WHERE session_id=? AND root_message_id=?",
        (sid, "r1@x")).fetchone()
    row = state()
    assert row["completion_state"] == core_db.SESSION_PATCHSET_COMPLETION_PENDING
    # add three trains (total=3, done=0) → PARTIAL
    core_db.bump_session_patchset_progress(
        db, sid, "<r1@x>", total_delta=3)
    row = state()
    assert row["train_work_items_total"] == 3
    assert row["completion_state"] == core_db.SESSION_PATCHSET_COMPLETION_PARTIAL
    # mark two done → still PARTIAL
    core_db.bump_session_patchset_progress(
        db, sid, "<r1@x>", done_delta=2)
    assert state()["completion_state"] == \
        core_db.SESSION_PATCHSET_COMPLETION_PARTIAL
    # the last one → COMPLETE
    core_db.bump_session_patchset_progress(
        db, sid, "<r1@x>", done_delta=1)
    row = state()
    assert row["train_work_items_done"] == 3
    assert row["completion_state"] == \
        core_db.SESSION_PATCHSET_COMPLETION_COMPLETE


def test_bump_session_patchset_progress_raises_on_unknown_pair(db):
    sid = core_db.create_session_draft(db, "standard")
    with pytest.raises(KeyError):
        core_db.bump_session_patchset_progress(
            db, sid, "<r-missing@x>", done_delta=1)


# --- listings -------------------------------------------------------------

def test_list_sessions_orders_newest_first_and_filters_by_state(db):
    s1 = core_db.create_session_draft(db, "standard")
    s2 = core_db.create_session_draft(db, "targeted_graduation")
    s3 = core_db.create_session_draft(db, "exploratory")
    core_db.transition_session(db, s2, core_db.SESSION_STATE_READY)
    all_ = core_db.list_sessions(db)
    assert [s["id"] for s in all_] == [s3, s2, s1]                # newest first
    drafts = core_db.list_sessions(db, state=core_db.SESSION_STATE_DRAFT)
    assert {s["id"] for s in drafts} == {s1, s3}


def test_list_sessions_decodes_json_fields(db):
    sid = core_db.create_session_draft(
        db, "standard",
        stratification_spec={"strata": ["net:light"]})
    core_db.transition_session(db, sid, core_db.SESSION_STATE_ANALYZED,
                                stats={"summary": "ok"})
    row = core_db.list_sessions(db)[0]
    assert row["stratification_spec"] == {"strata": ["net:light"]}
    assert row["stats"] == {"summary": "ok"}


def test_session_patchsets_filters_by_role(db):
    sid = core_db.create_session_draft(db, "standard")
    core_db.add_session_patchset(
        db, sid, "<r1@x>",
        role=core_db.SESSION_ROLE_POOL, stratum_label="net:light")
    core_db.add_session_patchset(
        db, sid, "<r2@x>",
        role=core_db.SESSION_ROLE_HOLDOUT, stratum_label="net:light")
    pool = core_db.session_patchsets(db, sid, role=core_db.SESSION_ROLE_POOL)
    held = core_db.session_patchsets(
        db, sid, role=core_db.SESSION_ROLE_HOLDOUT)
    assert {p["root_message_id"] for p in pool} == {"r1@x"}
    assert {p["root_message_id"] for p in held} == {"r2@x"}


# --- strict virginity (the session-draft solver's hot-path primitive) -----

def test_patchset_appears_in_session_role_returns_true_after_assignment(db):
    sid = core_db.create_session_draft(db, "standard")
    core_db.add_session_patchset(
        db, sid, "<r1@x>",
        role=core_db.SESSION_ROLE_HOLDOUT, stratum_label="net:light")
    assert core_db.patchset_appears_in_session_role(
        db, "<r1@x>", core_db.SESSION_ROLE_HOLDOUT)
    # The same patchset is not in pool role — strict per-role check.
    assert not core_db.patchset_appears_in_session_role(
        db, "<r1@x>", core_db.SESSION_ROLE_POOL)


def test_patchset_appears_in_session_role_false_for_unused(db):
    assert not core_db.patchset_appears_in_session_role(
        db, "<r1@x>", core_db.SESSION_ROLE_HOLDOUT)
