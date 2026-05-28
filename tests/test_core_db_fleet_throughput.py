"""Tests for core_db.fleet_throughput — the per-minute count of
terminal-state work items the operator-UI sparkline graphs."""
import time

import pytest

from core import core_db


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _patchset(db, root="<r1@x>"):
    db.execute(
        "INSERT INTO patchsets (root_message_id,subject,submitter_email,"
        "sent,n_patches,gathered_at) VALUES (?,?,?,?,?,?)",
        (root, "s", "a@b", int(time.time()), 1, int(time.time())))
    db.commit()


def _completion(db, completed_at, state=core_db.WORK_ITEM_STATE_COMPLETED,
                 root="<r1@x>"):
    db.execute(
        "INSERT INTO work_items (type,root_message_id,state,enqueued_at,"
        "completed_at) VALUES (?,?,?,?,?)",
        (core_db.WORK_ITEM_TYPE_PREPARE, root, state,
         completed_at - 30, completed_at))
    db.commit()


def test_empty_db_returns_60_zero_bins(db):
    """With no work history the sparkline still has a defined shape
       — 60 zero-bins — so the UI renders a stable baseline rather
       than mis-sizing the SVG."""
    bins = core_db.fleet_throughput(db)
    assert len(bins) == 60 and all(v == 0 for v in bins)


def test_most_recent_bin_holds_a_just_completed_item(db):
    """A claim completed in the current minute lands in the
       rightmost bin — the sparkline's most-recent point."""
    _patchset(db)
    _completion(db, completed_at=int(time.time()))
    bins = core_db.fleet_throughput(db)
    assert bins[-1] == 1
    assert sum(bins[:-1]) == 0


def test_completions_distribute_across_bins_by_age(db):
    """Bins are anchored to `now - k minutes`. A claim completed
       17 minutes ago lands in bin -18 (counting from the right);
       a claim completed 0 minutes ago lands in bin -1."""
    _patchset(db)
    now = int(time.time())
    _completion(db, completed_at=now)
    _completion(db, completed_at=now - 17 * 60)
    _completion(db, completed_at=now - 59 * 60)
    bins = core_db.fleet_throughput(db)
    assert bins[-1] == 1                             # current minute
    assert bins[-18] == 1                            # 17 min ago
    assert bins[-60] == 1                            # 59 min ago
    assert sum(bins) == 3


def test_completions_outside_the_window_are_excluded(db):
    """Older than the window? Excluded from the bins entirely —
       the sparkline is a rolling-hour view, not cumulative."""
    _patchset(db)
    now = int(time.time())
    _completion(db, completed_at=now - 3 * 3600)     # 3h ago
    bins = core_db.fleet_throughput(db)
    assert sum(bins) == 0


def test_all_terminal_states_count(db):
    """COMPLETED, UNAPPLIABLE, and DEFERRED all represent claim
       turnover — the operator's "is anything moving?" signal."""
    _patchset(db)
    now = int(time.time())
    for state in (core_db.WORK_ITEM_STATE_COMPLETED,
                   core_db.WORK_ITEM_STATE_UNAPPLIABLE,
                   core_db.WORK_ITEM_STATE_DEFERRED):
        _completion(db, completed_at=now, state=state)
    # CLAIMED and CLAIMABLE rows don't count.
    db.execute("INSERT INTO work_items (type,root_message_id,state,"
                "enqueued_at,completed_at) VALUES (?,?,?,?,?)",
                (core_db.WORK_ITEM_TYPE_PREPARE, "<r1@x>",
                 core_db.WORK_ITEM_STATE_CLAIMED, now - 30, now))
    db.commit()
    bins = core_db.fleet_throughput(db)
    assert bins[-1] == 3                             # CLAIMED excluded


def test_custom_window_and_bin_size(db):
    """A caller wanting a finer-grained sparkline (e.g. 60 seconds
       in 10×6s bins for a load-test view) gets 6 bins back."""
    _patchset(db)
    now = int(time.time())
    _completion(db, completed_at=now)
    bins = core_db.fleet_throughput(db, window_seconds=60, bin_seconds=6)
    assert len(bins) == 10 and bins[-1] == 1
