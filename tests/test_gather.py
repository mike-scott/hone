"""Tests for the GATHER framework (core/gather.py) — the per-source ingest
stream + watermark, the source-selection helper, and the supervisor's
per-source task scheduling. The real gather modules (lore today) are
exercised in their own test files."""
import asyncio
from types import SimpleNamespace

import pytest

from core import core_db, gather

PatchsetRef = gather.gather_api.PatchsetRef
MessageRef  = gather.gather_api.MessageRef
GatherState = gather.gather_api.GatherState


class _FakeModule:
    """A minimal in-memory GatherModule — no network. `list(state)` yields
       the configured refs, optionally filtered by an int-encoded resume
       cursor (yields refs whose cursor > state.cursor)."""

    name = "fake-source"
    since_date = ""

    def __init__(self, refs):
        self._refs = list(refs)

    def list(self, state=None, db=None):
        refs = self._refs
        if state and state.cursor:
            cutoff = int(state.cursor)
            refs = [r for r in refs if r.cursor and int(r.cursor) > cutoff]
        return iter(refs)


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _patch_ref(root, *, cursor, **kw):
    kw.setdefault("subject", "ps")
    kw.setdefault("sent", 100)
    kw.setdefault("n_patches", 1)
    return PatchsetRef(root_message_id=root, cursor=cursor, **kw)


def _msg_ref(mid, *, root, cursor, type=None, parent=None,
             body="--- a/x\n+++ b/x\n"):
    return MessageRef(message_id=mid, root_message_id=root,
                      type=type if type is not None else core_db.MSG_TYPE_PATCH,
                      parent_message_id=parent,
                      body=body, cursor=cursor)


# --- ingest stream ---------------------------------------------------------

def test_gather_ingests_a_patchset_and_messages(db):
    refs = [
        _patch_ref("<p1@x>", cursor="1"),
        _msg_ref("<m1@x>", root="<p1@x>", cursor="2"),
        _msg_ref("<c1@x>", root="<p1@x>", type=core_db.MSG_TYPE_COMMENT,
                 parent="<m1@x>", body="LGTM", cursor="3"),
    ]
    stats = gather._gather_source(db, _FakeModule(refs))
    assert stats == {"patchsets": 1, "messages": 2,
                     "skipped": 0, "failed": 0}
    assert core_db.get_patchset(db, "<p1@x>") is not None
    assert len(core_db.messages_for_patchset(db, "<p1@x>")) == 2
    assert len(core_db.comments_for_patch(db, "<m1@x>")) == 1


def test_gather_does_not_auto_enqueue_a_review(db):
    """Review is operator-triggered, not auto-enqueued at gather time —
       even with a patchset_metadata row present (the old enqueue gate),
       a gather pass enqueues only prepare, never review."""
    # First pass creates the patchset row + its patch message.
    gather._gather_source(db, _FakeModule([
        _patch_ref("<p1@x>", cursor="1"),
        _msg_ref("<m1@x>", root="<p1@x>", cursor="2"),
    ]))
    # Simulate prepare having completed — this is the metadata row that
    # used to arm the review auto-enqueue gate (FK requires the patchset
    # to exist first, hence after the gather above).
    core_db.upsert_patchset_metadata(
        db, "<p1@x>", mode="heuristic",
        tree_state={}, subsystem={}, patch_size={}, maintainer={},
        patch_type={}, review_intensity={"bucket_overall": "light"},
        preparation_notes={"mode": "heuristic"})
    # A further gather pass (another patch message landing) must still
    # enqueue no review, even though the metadata gate is now satisfied.
    gather._gather_source(db, _FakeModule([
        _msg_ref("<m2@x>", root="<p1@x>", cursor="3"),
    ]))
    n_reviews = db.execute(
        "SELECT COUNT(*) FROM work_items WHERE type=?",
        (core_db.WORK_ITEM_TYPE_REVIEW,)).fetchone()[0]
    assert n_reviews == 0


def test_comment_landing_does_not_auto_enqueue_a_train(db):
    """Training is exclusively session-driven; a comment landing at
       gather time upserts the message but generates no work-item."""
    refs = [
        _patch_ref("<p1@x>", cursor="1"),
        _msg_ref("<m1@x>", root="<p1@x>", cursor="2"),
        _msg_ref("<c1@x>", root="<p1@x>", type=core_db.MSG_TYPE_COMMENT,
                 parent="<m1@x>", body="nack!", cursor="3"),
    ]
    gather._gather_source(db, _FakeModule(refs))
    n_trains = db.execute(
        "SELECT COUNT(*) FROM work_items WHERE type=?",
        (core_db.WORK_ITEM_TYPE_TRAIN,)).fetchone()[0]
    assert n_trains == 0


def test_gather_marks_skipped_when_patchset_has_skip_reason(db):
    ref = PatchsetRef(root_message_id="<p1@x>", subject="bad",
                      skip_reason="unresolved-date", cursor="1")
    stats = gather._gather_source(db, _FakeModule([ref]))
    assert stats["skipped"] == 1 and stats["patchsets"] == 0
    ps = core_db.get_patchset(db, "<p1@x>")
    assert ps["state"] == core_db.PATCHSET_STATE_SKIPPED
    assert ps["skip_reason"] == "unresolved-date"


def test_gather_applies_list_tags_to_patchset(db):
    refs = [PatchsetRef(root_message_id="<p1@x>",
                        list_tags=["a.kernel.org", "b.kernel.org"],
                        cursor="1")]
    gather._gather_source(db, _FakeModule(refs))
    assert sorted(core_db.tags_for_patchset(db, "<p1@x>")) == [
        "a.kernel.org", "b.kernel.org"]


def test_gather_handles_a_standalone_patch(db):
    # a single [PATCH] is a 1-message patchset whose root_message_id IS the
    # patch's own message_id (no cover letter, part_index NULL).
    refs = [
        _patch_ref("<p1@x>", cursor="1", n_patches=1),
        _msg_ref("<p1@x>", root="<p1@x>", cursor="2"),
    ]
    gather._gather_source(db, _FakeModule(refs))
    patch = core_db.patch_message(db, "<p1@x>", part_index=None)
    assert patch is not None
    assert patch["message_id"] == "p1@x"
    assert patch["part_index"] is None


# --- watermark -------------------------------------------------------------

def test_watermark_advances_per_ref(db):
    refs = [
        _patch_ref("<p1@x>", cursor="10"),
        _msg_ref("<m1@x>", root="<p1@x>", cursor="20"),
        _msg_ref("<m2@x>", root="<p1@x>", cursor="30"),
    ]
    gather._gather_source(db, _FakeModule(refs))
    assert core_db.get_gather_state(db, "fake-source") == "30"


def test_watermark_freezes_at_the_first_failure(db, monkeypatch):
    # m2 fails to ingest; watermark holds at m1's cursor even though m3
    # succeeds — the next cycle retries from after m1, re-attempts m2, and
    # finds m3 already in the corpus.
    refs = [
        _patch_ref("<p1@x>", cursor="10"),
        _msg_ref("<m1@x>", root="<p1@x>", cursor="20"),
        _msg_ref("<m2@x>", root="<p1@x>", cursor="30"),
        _msg_ref("<m3@x>", root="<p1@x>", cursor="40"),
    ]
    real_upsert_message = core_db.upsert_message

    def boom(db_, message_id, **kw):
        if message_id == "<m2@x>":
            raise RuntimeError("simulated ingest failure")
        return real_upsert_message(db_, message_id, **kw)

    monkeypatch.setattr(core_db, "upsert_message", boom)
    stats = gather._gather_source(db, _FakeModule(refs))
    assert stats["failed"] == 1
    assert stats["messages"] == 2                        # m1 and m3 ingested
    assert core_db.get_gather_state(db, "fake-source") == "20"


def test_watermark_survives_an_abrupt_stop(db):
    # the source ingests two refs, then dies mid-stream
    def dying_list(state=None, db=None):
        yield _patch_ref("<p1@x>", cursor="1")
        yield _msg_ref("<m1@x>", root="<p1@x>", cursor="2")
        raise RuntimeError("rate limited mid-cycle")
    mod = _FakeModule([])
    mod.list = dying_list
    with pytest.raises(RuntimeError):
        gather._gather_source(db, mod)
    assert core_db.get_gather_state(db, "fake-source") == "2"


def test_resume_skips_what_is_below_the_watermark(db):
    refs1 = [
        _patch_ref("<p1@x>", cursor="10"),
        _msg_ref("<m1@x>", root="<p1@x>", cursor="20"),
    ]
    gather._gather_source(db, _FakeModule(refs1))
    # a later pass: the fake's resume filter drops refs with cursor <= 20
    refs2 = refs1 + [_msg_ref("<m2@x>", root="<p1@x>", cursor="30")]
    stats = gather._gather_source(db, _FakeModule(refs2))
    assert stats["messages"] == 1 and stats["patchsets"] == 0
    assert core_db.get_gather_state(db, "fake-source") == "30"


def test_gather_state_round_trips(db):
    assert core_db.get_gather_state(db, "src") == ""
    core_db.set_gather_state(db, "src", "cursor-A")
    assert core_db.get_gather_state(db, "src") == "cursor-A"
    core_db.set_gather_state(db, "src", "cursor-B")        # upsert
    assert core_db.get_gather_state(db, "src") == "cursor-B"


# --- source selection ------------------------------------------------------

_INSTALLED = ["lore", "other"]


def test_select_sources_empty_selection_gathers_nothing():
    assert gather._select_sources((), _INSTALLED) == []


def test_select_sources_restricts_to_the_configured_set():
    assert gather._select_sources(("lore",), _INSTALLED) == ["lore"]


def test_select_sources_keeps_the_operators_order():
    assert gather._select_sources(("other", "lore"),
                                  _INSTALLED) == ["other", "lore"]


def test_select_sources_drops_unknown_names():
    assert gather._select_sources(("lore", "nope"),
                                  _INSTALLED) == ["lore"]


# --- the GATHER supervisor scheduling --------------------------------------

class _FakeTask:
    """Stands in for an asyncio.Task in the supervisor scheduling tests."""

    def __init__(self, *, done=False, cancelled=False, exc=None):
        self._done, self._cancelled, self._exc = done, cancelled, exc

    def done(self): return self._done
    def cancelled(self): return self._cancelled
    def exception(self): return self._exc


def test_supervisor_spawns_every_enabled_source():
    reg = gather._Registry()
    to_spawn, to_cancel = gather._plan_tick(
        reg, now=1000.0, enabled=["a", "b"], interval=600, stall_after=900)
    assert sorted(to_spawn) == ["a", "b"] and to_cancel == []


def test_supervisor_does_not_spawn_a_source_with_a_running_task():
    reg = gather._Registry()
    reg.tasks["a"] = _FakeTask(done=False)
    reg.beats["a"] = 1000.0
    reg.last_spawn["a"] = 0.0                          # long overdue
    to_spawn, to_cancel = gather._plan_tick(
        reg, now=1000.0, enabled=["a"], interval=600, stall_after=900)
    assert to_spawn == []                              # overran — keep the slot
    assert to_cancel == []


def test_supervisor_reaps_a_finished_task_and_respawns():
    reg = gather._Registry()
    reg.tasks["a"] = _FakeTask(done=True)
    reg.beats["a"] = 1000.0
    reg.last_spawn["a"] = 0.0
    to_spawn, _ = gather._plan_tick(
        reg, now=1000.0, enabled=["a"], interval=600, stall_after=900)
    assert "a" not in reg.tasks                        # reaped
    assert to_spawn == ["a"]                           # slot free -> respawn


def test_run_source_logs_start_and_done(monkeypatch, caplog):
    """`_run_source` brackets the cycle with INFO log lines — `cycle
       started` at the top, `cycle done in Ns: <tally>` after the worker
       thread returns. The pair lets an operator see when each source
       woke up and how long its cycle took."""
    monkeypatch.setattr(gather, "_gather_source_cycle",
                        lambda db_path, name, beat: {
                            "patchsets": 0, "messages": 0,
                            "skipped": 0, "failed": 0})
    app = SimpleNamespace(
        state=SimpleNamespace(config=SimpleNamespace(db_path=":memory:")))
    beats = {}
    with caplog.at_level("INFO", logger="hone.gather"):
        asyncio.run(gather._run_source(app, "lore", beats))
    msgs = [r.message for r in caplog.records]
    assert any("lore — cycle started" in m for m in msgs)
    assert any("lore — cycle done in" in m for m in msgs)


def test_supervisor_holds_a_source_until_its_period_elapses():
    reg = gather._Registry()
    reg.last_spawn["a"] = 950.0                        # spawned 50s ago
    to_spawn, _ = gather._plan_tick(
        reg, now=1000.0, enabled=["a"], interval=600, stall_after=900)
    assert to_spawn == []                              # 50s < 600s period


def test_trigger_now_bypasses_the_cadence_for_idle_sources():
    """The operator's "Gather now" button fires every idle enabled source
       regardless of when each last spawned."""
    reg = gather._Registry()
    reg.last_spawn["a"] = 950.0                        # spawned 50s ago
    reg.last_spawn["b"] = 998.0                        # spawned 2s ago
    to_spawn, _ = gather._plan_tick(
        reg, now=1000.0, enabled=["a", "b"], interval=600,
        stall_after=900, trigger_now=True)
    assert sorted(to_spawn) == ["a", "b"]


def test_trigger_now_does_not_spawn_over_a_running_source():
    """The trigger respects the "one task per source at a time" rule —
       sources mid-cycle keep their slot and aren't double-spawned."""
    reg = gather._Registry()
    reg.tasks["a"] = _FakeTask(done=False)             # mid-cycle
    reg.beats["a"] = 1000.0
    reg.last_spawn["a"] = 100.0
    to_spawn, _ = gather._plan_tick(
        reg, now=1000.0, enabled=["a"], interval=600,
        stall_after=900, trigger_now=True)
    assert to_spawn == []


def test_supervisor_cancels_a_stalled_task():
    reg = gather._Registry()
    reg.tasks["a"] = _FakeTask(done=False)
    reg.beats["a"] = 50.0                              # heartbeat 950s stale
    reg.last_spawn["a"] = 50.0
    _, to_cancel = gather._plan_tick(
        reg, now=1000.0, enabled=["a"], interval=600, stall_after=900)
    assert to_cancel == ["a"]


# --- new-comment notifications --------------------------------------------

def test_gather_new_comment_notifies_trackers_once(db):
    """A new comment on a TRACKED patchset notifies its owner; re-running the
       same stream (idempotent gather) does not duplicate the notification;
       and patches/cover don't notify."""
    uid = core_db.create_user(db, "u@x", "U", "local")
    core_db.set_user_state(db, uid, "approved")
    # An uploaded series this user tracks.
    core_db.upsert_patchset(db, "<p1@x>", subject="s", n_patches=1,
                            origin=core_db.PATCHSET_ORIGIN_UPLOADED,
                            uploaded_by_user_id=uid)
    refs = [
        _msg_ref("<m1@x>", root="<p1@x>", cursor="1"),          # a patch — no notif
        _msg_ref("<c1@x>", root="<p1@x>", type=core_db.MSG_TYPE_COMMENT,
                 parent="<m1@x>", body="LGTM", cursor="2"),     # a comment — notif
    ]
    gather._gather_source(db, _FakeModule(refs))
    notes = core_db.list_notifications(db, uid)
    assert len(notes) == 1
    assert notes[0]["type"] == core_db.NOTIF_TYPE_NEW_COMMENT
    # The link anchors at the comment's own thread row (id="msg-<norm-id>"),
    # with the Message-Id URL-encoded so its @ survives the fragment.
    assert notes[0]["link"] == "/patchsets/p1%40x#msg-c1%40x"
    # Re-run the whole stream — re-upserts the comment, must NOT re-notify.
    gather._gather_source(db, _FakeModule(refs))
    assert len(core_db.list_notifications(db, uid)) == 1


def test_gather_comment_on_unowned_patchset_no_notification(db):
    core_db.upsert_patchset(db, "<p1@x>", subject="s", n_patches=1)   # unowned
    gather._gather_source(db, _FakeModule([
        _msg_ref("<c1@x>", root="<p1@x>", type=core_db.MSG_TYPE_COMMENT,
                 body="hi", cursor="1"),
    ]))
    assert db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
