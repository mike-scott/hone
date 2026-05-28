"""Tests for core_db.fleet_status — the rollup view the operator-UI
fleet-pulse chip consumes. Verifies the count buckets are exclusive
(errored wins over stale), revoked nodes are excluded, in-flight is
counted off work_items.state=CLAIMED, and the function stays O(small)
for a 30+-node fleet."""
import json
import time

import pytest

from core import core_db


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _node(db, *, name, last_seen, state=None, health=None):
    """Create an enrolled node with a controlled `last_seen` and health
       snapshot. Bypasses the enrollment dance — these tests care only
       about the rollup query, not the OAuth flow."""
    state = state if state is not None else core_db.NODE_STATE_ACTIVE
    health_json = json.dumps(health) if health is not None else None
    cur = db.execute(
        "INSERT INTO nodes (name,task_types,state,enrolled_at,last_seen,"
        "health,health_at) VALUES (?,?,?,?,?,?,?)",
        (name, "[]", state, int(time.time()), last_seen,
         health_json, int(time.time())))
    db.commit()
    return cur.lastrowid


def test_empty_fleet_reports_zero_everywhere(db):
    s = core_db.fleet_status(db, stale_after_seconds=900)
    assert s == {"total": 0, "healthy": 0, "errored": 0, "stale": 0,
                  "in_flight": 0, "last_activity_at": None}


def test_healthy_node_counted_as_healthy(db):
    now = int(time.time())
    _node(db, name="n1", last_seen=now, health={"last_anthropic_error": None})
    s = core_db.fleet_status(db, stale_after_seconds=900)
    assert s["total"] == 1 and s["healthy"] == 1
    assert s["errored"] == 0 and s["stale"] == 0


def test_stale_node_counted_as_stale(db):
    now = int(time.time())
    _node(db, name="n1", last_seen=now - 10_000,    # well past 900s cutoff
          health={"last_anthropic_error": None})
    s = core_db.fleet_status(db, stale_after_seconds=900)
    assert s["total"] == 1 and s["stale"] == 1 and s["healthy"] == 0


def test_errored_node_wins_over_stale(db):
    """A node that's BOTH stale and carrying an anthropic error is
       reported as errored — the louder signal. Rollup buckets are
       exclusive so totals stay consistent."""
    now = int(time.time())
    _node(db, name="n1", last_seen=now - 10_000,
          health={"last_anthropic_error": "auth"})
    s = core_db.fleet_status(db, stale_after_seconds=900)
    assert s["errored"] == 1
    assert s["stale"] == 0          # NOT also counted as stale
    assert s["healthy"] == 0
    assert s["total"] == 1


def test_revoked_nodes_are_excluded(db):
    now = int(time.time())
    _node(db, name="n1", last_seen=now)
    _node(db, name="n2", last_seen=now, state=core_db.NODE_STATE_REVOKED)
    s = core_db.fleet_status(db, stale_after_seconds=900)
    assert s["total"] == 1 and s["healthy"] == 1


def test_health_snapshot_without_last_anthropic_error_is_healthy(db):
    """An old node health record predating the anthropic-error
       field — or one where the key is absent — must NOT be
       mistaken for errored. Defensive against schema drift."""
    now = int(time.time())
    _node(db, name="n1", last_seen=now,
          health={"free_disk_mb": 1024})    # no last_anthropic_error key
    s = core_db.fleet_status(db, stale_after_seconds=900)
    assert s["errored"] == 0 and s["healthy"] == 1


def test_malformed_health_json_does_not_crash(db):
    """If somehow the `health` column carries bad JSON, fleet_status
       must still return a coherent rollup — operator UI shouldn't
       go down because one node row is corrupt."""
    now = int(time.time())
    cur = db.execute(
        "INSERT INTO nodes (name,task_types,state,enrolled_at,last_seen,"
        "health) VALUES (?,?,?,?,?,?)",
        ("n1", "[]", core_db.NODE_STATE_ACTIVE, now, now, "{not json"))
    db.commit()
    s = core_db.fleet_status(db, stale_after_seconds=900)
    assert s["total"] == 1 and s["healthy"] == 1     # treated as no error


def test_in_flight_counts_claimed_work_items_only(db):
    """`in_flight` is the COUNT of work_items.state = CLAIMED. Not
       affected by methodology rows or anything else."""
    now = int(time.time())
    _node(db, name="n1", last_seen=now)
    # Need a patchset to enqueue work items.
    db.execute(
        "INSERT INTO patchsets (root_message_id,subject,submitter_email,sent,"
        "n_patches,gathered_at) VALUES (?,?,?,?,?,?)",
        ("<r1@x>", "subj", "a@b", now, 1, now))
    for state in (core_db.WORK_ITEM_STATE_CLAIMED,
                   core_db.WORK_ITEM_STATE_CLAIMED,
                   core_db.WORK_ITEM_STATE_CLAIMABLE,
                   core_db.WORK_ITEM_STATE_COMPLETED):
        db.execute("INSERT INTO work_items (type,root_message_id,state,"
                    "enqueued_at) VALUES (?,?,?,?)",
                    (core_db.WORK_ITEM_TYPE_PREPARE, "<r1@x>", state, now))
    db.commit()
    s = core_db.fleet_status(db, stale_after_seconds=900)
    assert s["in_flight"] == 2


def test_last_activity_at_is_max_of_last_seen(db):
    """`last_activity_at` should reflect the most-recently-seen
       node's timestamp — that's the operator's "is anything alive
       right now?" signal."""
    now = int(time.time())
    _node(db, name="n1", last_seen=now - 100)
    _node(db, name="n2", last_seen=now - 5)
    _node(db, name="n3", last_seen=now - 50)
    s = core_db.fleet_status(db, stale_after_seconds=900)
    assert s["last_activity_at"] == now - 5


def test_fleet_with_30_plus_nodes(db):
    """A 30+-node fleet returns a small dict in O(N) time — sanity
       check that the rollup stays compact regardless of size and
       that all three buckets get populated correctly."""
    now = int(time.time())
    for i in range(20):
        _node(db, name=f"healthy-{i}", last_seen=now,
              health={"last_anthropic_error": None})
    for i in range(7):
        _node(db, name=f"stale-{i}", last_seen=now - 10_000,
              health={"last_anthropic_error": None})
    for i in range(3):
        _node(db, name=f"err-{i}", last_seen=now,
              health={"last_anthropic_error": "rate_limit"})
    s = core_db.fleet_status(db, stale_after_seconds=900)
    assert s["total"] == 30
    assert s["healthy"] == 20
    assert s["stale"] == 7
    assert s["errored"] == 3
