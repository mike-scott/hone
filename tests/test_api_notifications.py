"""Tests for the node-submit notification trigger — api._notify_submit_outcome
fans a terminal review/prepare outcome out to a patchset's tracking users."""
import pytest

from core import api, core_db


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _user(db, email):
    uid = core_db.create_user(db, email, email, "local")
    core_db.set_user_state(db, uid, "approved")
    return uid


def _uploaded(db, root, uid, *, subject="net: fix it"):
    core_db.upsert_patchset(db, root, subject=subject, n_patches=1,
                            origin=core_db.PATCHSET_ORIGIN_UPLOADED,
                            uploaded_by_user_id=uid)


def test_review_reviewed_notifies_owner_not_node_owner(db):
    up = _user(db, "up@x"); node_owner = _user(db, "no@x")
    _uploaded(db, "<r@x>", up)
    core_db.upsert_ai_review(db, "<r@x>", concerns=[])
    api._notify_submit_outcome(db, "review", "reviewed",
                               core_db.norm_msgid("<r@x>"), "claim-1", node_owner)
    notes = core_db.list_notifications(db, up)
    assert notes and notes[0]["type"] == core_db.NOTIF_TYPE_REVIEW_READY
    assert "net: fix it" in notes[0]["title"]
    assert core_db.unread_notification_count(db, node_owner) == 0   # actor excluded


def test_review_unappliable_and_prepare_uncharacterisable(db):
    up = _user(db, "up@x")
    _uploaded(db, "<r@x>", up)
    api._notify_submit_outcome(db, "review", "unappliable",
                               core_db.norm_msgid("<r@x>"), "claim-1", None)
    api._notify_submit_outcome(db, "prepare", "uncharacterisable",
                               core_db.norm_msgid("<r@x>"), "claim-2", None)
    types = {n["type"] for n in core_db.list_notifications(db, up)}
    assert core_db.NOTIF_TYPE_REVIEW_FAILED in types
    assert core_db.NOTIF_TYPE_PREPARE_FAILED in types


def test_transient_deferred_outcome_does_not_notify(db):
    up = _user(db, "up@x")
    _uploaded(db, "<r@x>", up)
    api._notify_submit_outcome(db, "review", "deferred",
                               core_db.norm_msgid("<r@x>"), "claim-1", None)
    api._notify_submit_outcome(db, "prepare", "deferred",
                               core_db.norm_msgid("<r@x>"), "claim-2", None)
    assert core_db.unread_notification_count(db, up) == 0
