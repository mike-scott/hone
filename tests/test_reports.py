"""Tests for core/reports.py — the daily_stats materializer, per-day
computation, weekly rollup and Chart.js config builder behind the
operator /reports page."""
import json

import pytest

from core import core_db, reports

# A fixed "now" so day boundaries are deterministic:
# 2026-06-10 12:00:00 UTC.
NOW = 1781092800
TODAY = "2026-06-10"
YESTERDAY = "2026-06-09"


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _epoch(day, hour=12):
    lo, _ = reports._day_bounds(day)
    return lo + hour * 3600


def _patchset(db, root, *, gathered_at, origin=None, uploader=None):
    core_db.upsert_patchset(db, root, subject="s", n_patches=1,
                            gathered_at=gathered_at, origin=origin,
                            uploaded_by_user_id=uploader)


def _work(db, root, *, type, state, completed_at=None, enqueued_at=None,
          claimed_by=None, requested_by=None, usage=None):
    record = json.dumps({"usage": usage}) if usage is not None else None
    db.execute(
        "INSERT INTO work_items (type,root_message_id,state,enqueued_at,"
        "completed_at,claimed_by,requested_by_user_id,record) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (type, core_db.norm_msgid(root), state,
         enqueued_at if enqueued_at is not None else
         (completed_at or NOW) - 60,
         completed_at, claimed_by, requested_by, record))
    db.commit()


# --- day bounds --------------------------------------------------------------

def test_day_bounds_midnight_fences():
    lo, hi = reports._day_bounds(YESTERDAY)
    assert hi - lo == 86400
    assert reports.today_utc(lo) == YESTERDAY        # midnight → new day
    assert reports.today_utc(hi - 1) == YESTERDAY    # last second
    assert reports.today_utc(hi) == TODAY            # next midnight


# --- compute_day_stats --------------------------------------------------------

def test_compute_day_stats_splits_type_state_origin_and_tokens(db):
    uid = core_db.create_user(db, "u@x", "u", "local")
    _patchset(db, "<r1@x>", gathered_at=_epoch(YESTERDAY))
    _patchset(db, "<up@x>", gathered_at=_epoch(YESTERDAY),
              origin=core_db.PATCHSET_ORIGIN_UPLOADED, uploader=None)
    ts = _epoch(YESTERDAY)
    _work(db, "<r1@x>", type=core_db.WORK_ITEM_TYPE_PREPARE,
          state=core_db.WORK_ITEM_STATE_COMPLETED, completed_at=ts,
          claimed_by="node-a",
          usage={"input_tokens": 100, "output_tokens": 10,
                 "duration_ms": 5000})
    _work(db, "<r1@x>", type=core_db.WORK_ITEM_TYPE_REVIEW,
          state=core_db.WORK_ITEM_STATE_COMPLETED, completed_at=ts + 60,
          claimed_by="node-b", requested_by=uid,
          usage={"input_tokens": 200, "output_tokens": 20,
                 "duration_ms": 7000})
    # A deferred review with NO record (deferral carries no usage;
    # train rows need a full training-session row — the type mapping is
    # the same dict lookup, so review covers the code path).
    _work(db, "<r1@x>", type=core_db.WORK_ITEM_TYPE_REVIEW,
          state=core_db.WORK_ITEM_STATE_DEFERRED, completed_at=ts + 120,
          claimed_by="node-a")
    # An unappliable review on the SAME node — distinct nodes stays 2.
    _work(db, "<r1@x>", type=core_db.WORK_ITEM_TYPE_REVIEW,
          state=core_db.WORK_ITEM_STATE_UNAPPLIABLE, completed_at=ts + 180,
          claimed_by="node-a")
    # Outside the day — must not count.
    _work(db, "<r1@x>", type=core_db.WORK_ITEM_TYPE_PREPARE,
          state=core_db.WORK_ITEM_STATE_COMPLETED, completed_at=_epoch(TODAY),
          claimed_by="node-z", usage={"input_tokens": 999,
                                      "output_tokens": 999})

    s = reports.compute_day_stats(db, YESTERDAY)
    assert s["ops_prepare"] == 1 and s["ops_review"] == 3
    assert s["ops_train"] == 0
    assert s["ops_completed"] == 2 and s["ops_deferred"] == 1
    assert s["ops_unappliable"] == 1
    assert s["ops_user_origin"] == 1 and s["ops_system_origin"] == 3
    assert s["input_tokens"] == 300 and s["output_tokens"] == 30
    assert s["duration_ms_sum"] == 12000 and s["duration_n"] == 2
    assert s["nodes_active"] == 2
    assert s["patchsets_gathered"] == 1 and s["patchsets_uploaded"] == 1
    # 5 rows enqueued ~60s before their completion stamps — 4 of those
    # enqueue instants fall inside yesterday, the last one today.
    assert s["ops_enqueued"] == 4


def test_compute_day_stats_active_users_dedups_across_signals(db):
    uid_a = core_db.create_user(db, "a@x", "a", "local")
    uid_b = core_db.create_user(db, "b@x", "b", "local")
    ts = _epoch(YESTERDAY)
    # User A uploads AND requests work — one distinct user.
    _patchset(db, "<u1@x>", gathered_at=ts,
              origin=core_db.PATCHSET_ORIGIN_UPLOADED, uploader=uid_a)
    _work(db, "<u1@x>", type=core_db.WORK_ITEM_TYPE_PREPARE,
          state=core_db.WORK_ITEM_STATE_CLAIMABLE, enqueued_at=ts,
          requested_by=uid_a)
    # User B only logged in.
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (ts, uid_b))
    db.commit()
    assert reports.compute_day_stats(db, YESTERDAY)["active_users"] == 2


# --- ensure_daily_stats --------------------------------------------------------

def test_first_run_backfills_from_earliest_signal(db):
    _patchset(db, "<r1@x>", gathered_at=_epoch("2026-06-05"))
    _work(db, "<r1@x>", type=core_db.WORK_ITEM_TYPE_PREPARE,
          state=core_db.WORK_ITEM_STATE_COMPLETED,
          completed_at=_epoch("2026-06-07"), claimed_by="n1",
          usage={"input_tokens": 5, "output_tokens": 5})
    n = reports.ensure_daily_stats(db, now=NOW)
    # 2026-06-05 (earliest enqueue ~06-07 minus 60s... earliest signal is
    # gathered_at on 06-05) through yesterday 06-09 inclusive = 5 days.
    assert n == 5
    days = [r["day"] for r in db.execute(
        "SELECT day FROM daily_stats ORDER BY day")]
    assert days == ["2026-06-05", "2026-06-06", "2026-06-07",
                    "2026-06-08", "2026-06-09"]
    row = db.execute("SELECT * FROM daily_stats WHERE day='2026-06-07'"
                     ).fetchone()
    assert row["ops_prepare"] == 1 and row["input_tokens"] == 5
    # Idle day materialized as zeros (keeps the MAX(day) probe gap-free).
    assert db.execute("SELECT ops_prepare FROM daily_stats "
                      "WHERE day='2026-06-06'").fetchone()[0] == 0


def test_second_call_is_a_noop_and_catchup_resumes(db):
    _patchset(db, "<r1@x>", gathered_at=_epoch("2026-06-08"))
    assert reports.ensure_daily_stats(db, now=NOW) == 2   # 06-08, 06-09
    assert reports.ensure_daily_stats(db, now=NOW) == 0   # up to date
    # Two days later: only the newly closed days are computed.
    assert reports.ensure_daily_stats(db, now=NOW + 2 * 86400) == 2
    assert db.execute("SELECT MAX(day) FROM daily_stats").fetchone()[0] \
        == "2026-06-11"


def test_materialized_days_freeze_against_deletion(db):
    """THE point of materializing: cancel/supersede/delete flows remove
       work_items rows, and the closed day's numbers must survive."""
    _patchset(db, "<r1@x>", gathered_at=_epoch(YESTERDAY))
    _work(db, "<r1@x>", type=core_db.WORK_ITEM_TYPE_REVIEW,
          state=core_db.WORK_ITEM_STATE_COMPLETED,
          completed_at=_epoch(YESTERDAY), claimed_by="n1",
          usage={"input_tokens": 42, "output_tokens": 7})
    reports.ensure_daily_stats(db, now=NOW)
    db.execute("DELETE FROM work_items")
    db.commit()
    reports.ensure_daily_stats(db, now=NOW)               # no-op
    row = db.execute("SELECT * FROM daily_stats WHERE day=?",
                     (YESTERDAY,)).fetchone()
    assert row["ops_review"] == 1 and row["input_tokens"] == 42


def test_empty_db_materializes_nothing(db):
    assert reports.ensure_daily_stats(db, now=NOW) == 0
    assert db.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0] == 0


def test_backfill_is_capped(db):
    _patchset(db, "<old@x>",
              gathered_at=_epoch(YESTERDAY) - 500 * 86400)
    n = reports.ensure_daily_stats(db, now=NOW)
    # start capped at today-366; through yesterday inclusive = 366 days
    assert n == reports.BACKFILL_MAX_DAYS


# --- load + weekly rollup ------------------------------------------------------

def test_load_daily_stats_zero_fills_missing_days(db):
    _patchset(db, "<r1@x>", gathered_at=_epoch(YESTERDAY))
    reports.ensure_daily_stats(db, now=NOW)               # only 06-09
    rows = reports.load_daily_stats(db, days=5, now=NOW)
    assert [r["day"] for r in rows] == [
        "2026-06-05", "2026-06-06", "2026-06-07", "2026-06-08",
        "2026-06-09"]
    assert rows[-1]["patchsets_gathered"] == 1
    assert all(r["patchsets_gathered"] == 0 for r in rows[:-1])


def test_weekly_rollup_iso_weeks_sum_counts_peak_distincts():
    daily = [
        # 2026-06-05 (Fri) and 06-07 (Sun) → ISO week 2026-W23;
        # 06-08 (Mon) → 2026-W24.
        {"day": "2026-06-05", **{c: 0 for c in reports._COLUMNS},
         "ops_review": 2, "active_users": 3, "duration_ms_sum": 1000,
         "duration_n": 2},
        {"day": "2026-06-07", **{c: 0 for c in reports._COLUMNS},
         "ops_review": 1, "active_users": 5, "duration_ms_sum": 5000,
         "duration_n": 1},
        {"day": "2026-06-08", **{c: 0 for c in reports._COLUMNS},
         "ops_review": 4, "active_users": 1},
    ]
    weeks = reports.weekly_rollup(daily, weeks=12)
    assert [w["label"] for w in weeks] == ["2026-W23", "2026-W24"]
    w23, w24 = weeks
    assert w23["ops_review"] == 3 and w24["ops_review"] == 4
    assert w23["active_users"] == 5            # peak day, not 8
    assert w23["duration_ms_sum"] == 6000 and w23["duration_n"] == 3
    assert not w23["partial"] and not w24["partial"]


def test_weekly_rollup_folds_today_and_marks_partial():
    daily = [{"day": "2026-06-08", **{c: 0 for c in reports._COLUMNS},
              "ops_review": 1}]
    today = {"day": "2026-06-10", **{c: 0 for c in reports._COLUMNS},
             "ops_review": 2}
    weeks = reports.weekly_rollup(daily, weeks=12, today=today)
    assert len(weeks) == 1
    assert weeks[0]["label"] == "2026-W24"
    assert weeks[0]["ops_review"] == 3
    assert weeks[0]["partial"] is True


# --- chart config ----------------------------------------------------------

def test_stacked_chart_config_shape_and_partial_fading():
    rows = [
        {"label": "06-08", "partial": False, "ops_prepare": 2,
         "ops_review": 5},
        {"label": "today", "partial": True, "ops_prepare": 1,
         "ops_review": 0},
    ]
    cfg = reports.stacked_chart_config(
        rows, (("ops_prepare", "Prepare", "#0d6efd"),
               ("ops_review", "Review", "#198754")))
    assert cfg["type"] == "bar"
    assert cfg["data"]["labels"] == ["06-08", "today"]
    prep, rev = cfg["data"]["datasets"]
    assert prep["label"] == "Prepare" and prep["data"] == [2, 1]
    assert rev["data"] == [5, 0]
    # Closed bar solid, today's bar alpha-faded.
    assert prep["backgroundColor"] == ["#0d6efd", "#0d6efd80"]
    assert cfg["options"]["scales"]["y"]["stacked"] is True
    grouped = reports.stacked_chart_config(
        rows, (("ops_prepare", "Prepare", "#0d6efd"),), stacked=False)
    assert grouped["options"]["scales"]["y"]["stacked"] is False
    # The config must be JSON-serializable as-is (it's embedded verbatim).
    json.dumps(cfg)


def test_summary_totals_weighted_average_and_peaks():
    daily = [{"day": "2026-06-08", **{c: 0 for c in reports._COLUMNS},
              "ops_completed": 2, "duration_ms_sum": 4000, "duration_n": 2,
              "active_users": 4},
             {"day": "2026-06-09", **{c: 0 for c in reports._COLUMNS},
              "ops_deferred": 1, "active_users": 1}]
    t = reports.summary_totals(daily)
    assert t["ops_total"] == 3
    assert t["avg_duration_ms"] == 2000
    assert t["active_users"] == 4              # peak day
    assert reports.summary_totals([])["avg_duration_ms"] is None
