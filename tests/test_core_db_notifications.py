"""Tests for the user-notifications data layer (core/core_db.py migration v13):
fan-out + dedup + preferences, read/mark/prune, and the trigger helpers
(mark_skipped fan-out, edge-triggered node health, submit-outcome fan-out)."""
import pytest

from core import core_db


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _user(db, email, *, admin=False, state="approved"):
    uid = core_db.create_user(db, email, email, "local")
    core_db.set_user_state(db, uid, state)
    if admin:
        core_db.set_user_admin(db, uid, True)
    return uid


def _uploaded(db, root, uid, *, subject="s"):
    core_db.upsert_patchset(db, root, subject=subject, n_patches=1,
                            origin=core_db.PATCHSET_ORIGIN_UPLOADED,
                            uploaded_by_user_id=uid)


def _seed_node(db, *, name, owner_user_id=None):
    cur = db.execute(
        "INSERT INTO nodes (name, task_types, state, enrolled_at, "
        "owner_user_id, handles_system) VALUES (?,?,?,?,?,?)",
        (name, '["prepare"]', core_db.NODE_STATE_ACTIVE, 1, owner_user_id,
         1 if owner_user_id is None else 0))
    db.commit()
    return cur.lastrowid


# --- migration -------------------------------------------------------------

def test_migration_v13_table_and_prefs_column(db):
    assert db.execute("PRAGMA user_version").fetchone()[0] >= 13
    cols = [r[1] for r in db.execute("PRAGMA table_info(notifications)")]
    assert {"user_id", "type", "title", "link", "dedup_key", "read_at",
            "emailed_at"} <= set(cols)
    ucols = [r[1] for r in db.execute("PRAGMA table_info(users)")]
    assert "notification_prefs" in ucols


# --- fan-out + dedup -------------------------------------------------------

def test_patchset_fanout_to_owner_and_claimants(db):
    up = _user(db, "up@x"); cl = _user(db, "cl@x"); other = _user(db, "o@x")
    _uploaded(db, "<r@x>", up)
    db.execute("INSERT INTO patchset_claims(root_message_id,user_id,claimed_at)"
               " VALUES(?,?,?)", (core_db.norm_msgid("<r@x>"), cl, 1))
    db.commit()
    n = core_db.notify_patchset_users(
        db, "<r@x>", type=core_db.NOTIF_TYPE_REVIEW_READY,
        dedup_key="rr:1", title="Review ready")
    assert n == 2
    assert core_db.unread_notification_count(db, up) == 1
    assert core_db.unread_notification_count(db, cl) == 1
    assert core_db.unread_notification_count(db, other) == 0
    # the default link points at the (quoted) patchset detail, anchored at
    # the per-type element the click-through scrolls to and flashes
    assert (core_db.list_notifications(db, up)[0]["link"]
            == "/patchsets/r%40x#ai-review")


def test_fanout_is_idempotent_on_dedup_key(db):
    up = _user(db, "up@x"); _uploaded(db, "<r@x>", up)
    k = dict(type=core_db.NOTIF_TYPE_REVIEW_READY, dedup_key="rr:1",
             title="Review ready")
    assert core_db.notify_patchset_users(db, "<r@x>", **k) == 1
    assert core_db.notify_patchset_users(db, "<r@x>", **k) == 0   # re-run no-op
    assert core_db.unread_notification_count(db, up) == 1


def test_fanout_excludes_actor_and_unowned_is_cheap_noop(db):
    up = _user(db, "up@x"); _uploaded(db, "<r@x>", up)
    assert core_db.notify_patchset_users(
        db, "<r@x>", type=core_db.NOTIF_TYPE_REVIEW_READY, dedup_key="rr:1",
        title="x", exclude_user_id=up) == 0                      # only owner == actor
    # an unowned patchset fans out to nobody
    core_db.upsert_patchset(db, "<u@x>", subject="s", n_patches=1)
    assert core_db.notify_patchset_users(
        db, "<u@x>", type=core_db.NOTIF_TYPE_NEW_COMMENT,
        dedup_key="c:1", title="x") == 0


def test_prefs_opt_out_suppresses(db):
    up = _user(db, "up@x"); _uploaded(db, "<r@x>", up)
    core_db.set_notification_prefs(db, up, {"review_ready": False})
    assert core_db.notify_patchset_users(
        db, "<r@x>", type=core_db.NOTIF_TYPE_REVIEW_READY, dedup_key="rr:1",
        title="x") == 0
    # a different type the user didn't opt out of still lands
    assert core_db.notify_patchset_users(
        db, "<r@x>", type=core_db.NOTIF_TYPE_NEW_COMMENT, dedup_key="c:1",
        title="x") == 1


def test_set_prefs_drops_unknown_slugs(db):
    up = _user(db, "up@x")
    core_db.set_notification_prefs(db, up, {"review_ready": False, "bogus": True})
    assert core_db.get_notification_prefs(db, up) == {"review_ready": False}


def test_notify_admins_targets_only_approved_admins(db):
    a1 = _user(db, "a1@x", admin=True)
    _user(db, "a2@x", admin=True, state="pending")     # not approved
    _user(db, "dev@x")                                  # not admin
    assert core_db.notify_admins(
        db, type=core_db.NOTIF_TYPE_USER_ACCESS, dedup_key="ua:1",
        title="req", link="/users") == 1
    assert core_db.unread_notification_count(db, a1) == 1


# --- read / mark / prune ---------------------------------------------------

def test_mark_read_is_user_scoped(db):
    a = _user(db, "a@x"); b = _user(db, "b@x"); _uploaded(db, "<r@x>", a)
    core_db.notify_patchset_users(db, "<r@x>", type=core_db.NOTIF_TYPE_REVIEW_READY,
                                  dedup_key="rr:1", title="x")
    nid = core_db.list_notifications(db, a)[0]["id"]
    assert core_db.mark_notification_read(db, b, nid) is False   # not b's
    assert core_db.unread_notification_count(db, a) == 1
    assert core_db.mark_notification_read(db, a, nid) is True
    assert core_db.unread_notification_count(db, a) == 0


def test_mark_all_and_prune_keeps_unread(db):
    a = _user(db, "a@x")
    for i in range(5):
        core_db.insert_notification(db, a, type=core_db.NOTIF_TYPE_NEW_COMMENT,
                                    dedup_key=f"c:{i}", title=f"c{i}")
    assert core_db.mark_all_notifications_read(db, a) == 5
    # add an unread one, then prune to keep newest 2 read — unread survives
    core_db.insert_notification(db, a, type=core_db.NOTIF_TYPE_NEW_COMMENT,
                                dedup_key="c:new", title="fresh")
    core_db.prune_read_notifications(db, a, keep=2)
    rows = core_db.list_notifications(db, a)
    assert sum(1 for r in rows if r["read_at"] is None) == 1     # unread kept
    assert sum(1 for r in rows if r["read_at"] is not None) == 2  # pruned to 2


def test_config_admin_none_is_a_noop_everywhere(db):
    assert core_db.unread_notification_count(db, None) == 0
    assert core_db.list_notifications(db, None) == []
    assert core_db.insert_notification(db, None, type=core_db.NOTIF_TYPE_NEW_COMMENT,
                                       dedup_key="c:1", title="x") == 0


# --- trigger helpers -------------------------------------------------------

def test_mark_skipped_notifies_trackers(db):
    up = _user(db, "up@x"); _uploaded(db, "<r@x>", up)
    core_db.mark_skipped(db, "<r@x>", "tag not enabled")
    notes = core_db.list_notifications(db, up)
    assert notes and notes[0]["type"] == core_db.NOTIF_TYPE_PATCHSET_SKIPPED
    assert "tag not enabled" in notes[0]["title"]


def test_node_health_edge_triggers_owner_once(db):
    owner = _user(db, "own@x")
    nid = _seed_node(db, name="n1", owner_user_id=owner)
    core_db.update_node_health(db, nid, {"disk_low": False})       # healthy
    assert core_db.unread_notification_count(db, owner) == 0
    core_db.update_node_health(db, nid, {"disk_low": True})        # → alert
    assert core_db.unread_notification_count(db, owner) == 1
    core_db.update_node_health(db, nid, {"disk_low": True})        # steady-state
    assert core_db.unread_notification_count(db, owner) == 1      # no re-spam


def test_node_health_skips_unowned_node(db):
    nid = _seed_node(db, name="sys")                      # owner None
    core_db.update_node_health(db, nid, {"disk_low": True})        # no error
    assert db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
