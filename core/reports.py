"""reports.py — the operator Reports section's data layer.

Daily activity rollups, materialized once per closed UTC day into the
`daily_stats` table (schema v7) by a lazy materializer the /reports view
calls — NOT recomputed per page view, and deliberately frozen: several
flows delete work_items rows (cancel, superseded-iteration retirement,
delete_review, delete_patchset), so a live-table recomputation would
quietly rewrite history. The only per-view computation is "today so
far", a single day's index-backed range queries.

Weekly figures are aggregated from the daily rows in Python (ISO Monday
weeks); there is no weekly table. Charts render client-side with the
vendored Chart.js — this module builds the complete config dicts, so
chart content stays unit-testable without a browser.

Token counts come from work_items.record ($.usage) ONLY: that covers
prepare/review/train uniformly, and ai_reviews' token columns are the
review task's usage re-recorded — summing both would double-count.
"""
import datetime
import math
import time

from core import core_db

# First-run backfill bound: best-effort history reconstruction (deleted
# work items are gone), capped so a years-old corpus doesn't stall the
# first /reports view.
BACKFILL_MAX_DAYS = 366

_DAY_SECONDS = 86400

# daily_stats metric columns, in table order — drives the INSERT, the
# zero-fill template, and the weekly summing.
_COLUMNS = ("ops_prepare", "ops_review", "ops_train",
            "ops_completed", "ops_unappliable", "ops_deferred",
            "ops_user_origin", "ops_system_origin", "ops_enqueued",
            "patchsets_gathered", "patchsets_uploaded",
            "active_users", "nodes_active",
            "input_tokens", "output_tokens",
            "duration_ms_sum", "duration_n")

# Per-day distinct counts can't be summed across days — the weekly
# rollup reports their peak day instead.
_PEAK_COLUMNS = ("active_users", "nodes_active")

_TERMINAL = tuple(sorted(core_db._WORK_ITEM_STATE_TERMINAL))


def today_utc(now=None):
    """Today's UTC date as 'YYYY-MM-DD'. `now` (epoch seconds) is the
       test seam."""
    ts = time.time() if now is None else now
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def _shift_day(day, delta):
    return (datetime.date.fromisoformat(day)
            + datetime.timedelta(days=delta)).isoformat()


def _day_bounds(day):
    """('YYYY-MM-DD') → [start, end) epoch seconds of that UTC day."""
    start = datetime.datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc)
    return int(start.timestamp()), int(start.timestamp()) + _DAY_SECONDS


def compute_day_stats(db, day):
    """One UTC day's numbers, straight from the live tables — the single
       source of truth, used by the materializer for closed days and by
       the view for the live "today so far" bar. All predicates are
       epoch ranges (index-backed); strftime in a WHERE would force a
       full scan."""
    lo, hi = _day_bounds(day)
    out = {"day": day, **{c: 0 for c in _COLUMNS}}

    # Terminal work-items: type, state, origin, tokens and duration in
    # one pass. record is NULL until submit and $.usage may be absent on
    # legacy rows — COALESCE everything; duration_n keeps missing
    # durations from dragging averages.
    marks = ",".join("?" * len(_TERMINAL))
    rows = db.execute(
        f"SELECT type, state, COUNT(*) n, "
        f"SUM(CASE WHEN requested_by_user_id IS NOT NULL "
        f"    THEN 1 ELSE 0 END) user_n, "
        f"SUM(COALESCE(json_extract(record,'$.usage.input_tokens'),0)) "
        f"    in_tok, "
        f"SUM(COALESCE(json_extract(record,'$.usage.output_tokens'),0)) "
        f"    out_tok, "
        f"SUM(COALESCE(json_extract(record,'$.usage.duration_ms'),0)) "
        f"    dur_sum, "
        f"SUM(CASE WHEN json_extract(record,'$.usage.duration_ms') "
        f"    IS NOT NULL THEN 1 ELSE 0 END) dur_n "
        f"FROM work_items "
        f"WHERE state IN ({marks}) AND completed_at >= ? AND completed_at < ? "
        f"GROUP BY type, state",
        (*_TERMINAL, lo, hi)).fetchall()
    type_key = {core_db.WORK_ITEM_TYPE_PREPARE: "ops_prepare",
                core_db.WORK_ITEM_TYPE_REVIEW:  "ops_review",
                core_db.WORK_ITEM_TYPE_TRAIN:   "ops_train"}
    state_key = {core_db.WORK_ITEM_STATE_COMPLETED:   "ops_completed",
                 core_db.WORK_ITEM_STATE_UNAPPLIABLE: "ops_unappliable",
                 core_db.WORK_ITEM_STATE_DEFERRED:    "ops_deferred"}
    for r in rows:
        if r["type"] in type_key:
            out[type_key[r["type"]]] += r["n"]
        if r["state"] in state_key:
            out[state_key[r["state"]]] += r["n"]
        out["ops_user_origin"]   += r["user_n"]
        out["ops_system_origin"] += r["n"] - r["user_n"]
        out["input_tokens"]      += r["in_tok"]
        out["output_tokens"]     += r["out_tok"]
        out["duration_ms_sum"]   += r["dur_sum"]
        out["duration_n"]        += r["dur_n"]

    out["nodes_active"] = db.execute(
        f"SELECT COUNT(DISTINCT claimed_by) FROM work_items "
        f"WHERE state IN ({marks}) AND claimed_by IS NOT NULL "
        f"AND completed_at >= ? AND completed_at < ?",
        (*_TERMINAL, lo, hi)).fetchone()[0]

    out["ops_enqueued"] = db.execute(
        "SELECT COUNT(*) FROM work_items "
        "WHERE enqueued_at >= ? AND enqueued_at < ?", (lo, hi)).fetchone()[0]

    for origin, n in db.execute(
            "SELECT origin, COUNT(*) FROM patchsets "
            "WHERE gathered_at >= ? AND gathered_at < ? GROUP BY origin",
            (lo, hi)).fetchall():
        if origin == core_db.PATCHSET_ORIGIN_UPLOADED:
            out["patchsets_uploaded"] = n
        else:
            out["patchsets_gathered"] = n

    # Distinct users seen that day, across the three activity signals we
    # actually record. last_login_at is a singleton (latest login only),
    # so it under-witnesses logins — folded into the distinct union,
    # where being incomplete is harmless, rather than reported as a
    # misleading "logins" count.
    out["active_users"] = db.execute(
        "SELECT COUNT(*) FROM ("
        " SELECT uploaded_by_user_id uid FROM patchsets"
        "  WHERE uploaded_by_user_id IS NOT NULL"
        "  AND gathered_at >= ? AND gathered_at < ?"
        " UNION "
        " SELECT requested_by_user_id FROM work_items"
        "  WHERE requested_by_user_id IS NOT NULL"
        "  AND enqueued_at >= ? AND enqueued_at < ?"
        " UNION "
        " SELECT id FROM users WHERE last_login_at >= ? AND last_login_at < ?"
        ")", (lo, hi, lo, hi, lo, hi)).fetchone()[0]
    return out


def ensure_daily_stats(db, *, now=None):
    """The lazy materializer — called on every /reports view; cheap when
       up to date (one MAX() probe on the day PK). Writes every missing
       closed day up to yesterday (zero rows for idle days keep the
       MAX check gap-free) and returns how many days were written.

       First run backfills from the earliest recorded signal, capped at
       BACKFILL_MAX_DAYS. A concurrent midnight race (two requests both
       seeing yesterday missing) is benign: the values are deterministic
       and INSERT OR REPLACE is idempotent."""
    today = today_utc(now)
    yesterday = _shift_day(today, -1)
    last = db.execute("SELECT MAX(day) FROM daily_stats").fetchone()[0]
    if last is not None and last >= yesterday:
        return 0
    if last is None:
        first = db.execute(
            "SELECT MIN(t) FROM ("
            " SELECT MIN(enqueued_at) t FROM work_items"
            " UNION ALL SELECT MIN(completed_at) FROM work_items"
            " UNION ALL SELECT MIN(gathered_at) FROM patchsets)"
        ).fetchone()[0]
        if first is None:
            return 0                      # empty corpus — nothing to report
        start = max(today_utc(first), _shift_day(today, -BACKFILL_MAX_DAYS))
    else:
        start = _shift_day(last, 1)

    cols = ", ".join(("day",) + _COLUMNS + ("computed_at",))
    marks = ", ".join("?" * (len(_COLUMNS) + 2))
    stamp = int(time.time() if now is None else now)
    n, day = 0, start
    while day <= yesterday:
        s = compute_day_stats(db, day)
        db.execute(
            f"INSERT OR REPLACE INTO daily_stats ({cols}) VALUES ({marks})",
            (day, *[s[c] for c in _COLUMNS], stamp))
        n += 1
        day = _shift_day(day, 1)
    if n:
        db.commit()
    return n


def load_daily_stats(db, *, days, now=None):
    """The last `days` CLOSED days (ending yesterday), oldest first,
       zero-filled so charts get a continuous axis. Reads only the
       materialized table — call ensure_daily_stats first."""
    today = today_utc(now)
    start = _shift_day(today, -days)
    by_day = {r["day"]: dict(r) for r in db.execute(
        "SELECT * FROM daily_stats WHERE day >= ? AND day < ? ORDER BY day",
        (start, today))}
    out = []
    day = start
    while day < today:
        out.append(by_day.get(day, {"day": day, **{c: 0 for c in _COLUMNS}}))
        day = _shift_day(day, 1)
    return out


def weekly_rollup(daily_rows, *, weeks, today=None):
    """Aggregate daily rows into ISO (Monday) weeks, oldest first,
       keeping the last `weeks`. Count columns sum; the per-day distinct
       columns (active_users, nodes_active) report their PEAK day —
       distincts don't sum across days. `today`'s live stats, when
       given, fold into the current week, which is marked partial."""
    rows = list(daily_rows) + ([today] if today else [])
    by_week = {}
    order = []
    for r in rows:
        iso = datetime.date.fromisoformat(r["day"]).isocalendar()
        label = f"{iso[0]}-W{iso[1]:02d}"
        if label not in by_week:
            by_week[label] = {"label": label, "partial": False,
                              **{c: 0 for c in _COLUMNS}}
            order.append(label)
        w = by_week[label]
        for c in _COLUMNS:
            if c in _PEAK_COLUMNS:
                w[c] = max(w[c], r[c])
            else:
                w[c] += r[c]
    if today:
        iso = datetime.date.fromisoformat(today["day"]).isocalendar()
        by_week[f"{iso[0]}-W{iso[1]:02d}"]["partial"] = True
    return [by_week[lbl] for lbl in order[-weeks:]]


def chart_rows_daily(daily_rows, today=None):
    """Daily rows → chart rows: short MM-DD labels, today appended as a
       partial bar."""
    rows = [{"label": r["day"][5:], "partial": False, **r}
            for r in daily_rows]
    if today:
        rows.append({"label": "today", "partial": True, **today})
    return rows


def stacked_chart_config(rows, segments, *, stacked=True):
    """A complete Chart.js config dict for a (stacked) bar chart.
       `rows` carry `label`, optional `partial`, and metric keys;
       `segments` is [(metric_key, display_label, hex_color), ...].
       Partial rows (today / the running week) render alpha-faded via
       Chart.js' per-point color arrays. Fixed hex palette — CSS theme
       variables can't reach a canvas. Pure data, unit-testable."""
    labels = [r["label"] for r in rows]
    datasets = []
    for key, label, color in segments:
        datasets.append({
            "label": label,
            "data": [r.get(key, 0) for r in rows],
            "backgroundColor": [color + ("80" if r.get("partial") else "")
                                for r in rows],
        })
    return {
        "type": "bar",
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "responsive": True,
            "maintainAspectRatio": False,
            "animation": False,
            "scales": {"x": {"stacked": stacked},
                       "y": {"stacked": stacked, "beginAtZero": True,
                              "ticks": {"precision": 0}}},
            "plugins": {"legend": {"position": "bottom"}},
        },
    }


def summary_totals(daily_rows, today=None):
    """The 30-day summary cards: totals over the closed window plus
       today's live numbers; peak-day values for the distinct columns."""
    rows = list(daily_rows) + ([today] if today else [])
    tot = {c: 0 for c in _COLUMNS}
    for r in rows:
        for c in _COLUMNS:
            if c in _PEAK_COLUMNS:
                tot[c] = max(tot[c], r[c])
            else:
                tot[c] += r[c]
    ops = tot["ops_completed"] + tot["ops_unappliable"] + tot["ops_deferred"]
    avg_ms = (tot["duration_ms_sum"] // tot["duration_n"]
              if tot["duration_n"] else None)
    return {"ops_total": ops, **tot, "avg_duration_ms": avg_ms}


# ---------------------------------------------------------------------------
# Check-usage analytics — which methodology review checks fire, over the
# per-review coverage (ai_reviews.check_coverage, see core/check_gates.py).
# ---------------------------------------------------------------------------

# Below this many reviews in the cohort, rates are noise: the page flags them
# "directional only" and leans on the (wide) Wilson intervals rather than the
# point estimates. ~30 applicable observations is the rule-of-thumb floor for
# distinguishing a 10%- from a 30%-rate check.
_CHECK_USAGE_SMALL_N = 30


def _wilson_ci(k, n, z=1.96):
    """Wilson score interval for a binomial proportion k/n, as (lo, hi) in
       0..1. Honest at small n (unlike normal-approx): a 0/12 reads as a wide
       band near zero, not a false 0%. Returns (0, 0) for n == 0."""
    if n <= 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def check_usage_stats(db, *, methodology_version=None):
    """Per-check usage over ai_reviews.check_coverage: for each methodology
       check, how many reviews it was applicable in vs fired in, the fire rate
       with a Wilson 95% CI, and the two data-quality flags (default-gate,
       fired-but-not-applicable). Optionally scoped to one methodology_version
       (cohort) — rates must not be pooled across versions whose check set
       differs.

       Denominator convention (see core/check_gates): a fired check is counted
       applicable regardless of its gate — `effective applicable = applicable
       OR fired` — so a rate is never > 100%.

       Returns a view-model dict: rows (sorted by fired desc), n_reviews,
       versions present, selected_version, has_data, small_n, and a Chart.js
       config (check_usage_chart_config)."""
    mv = methodology_version
    agg = db.execute(
        "SELECT json_extract(je.value,'$.id') AS check_id, "
        "COUNT(*) AS reviews, "
        "SUM(json_extract(je.value,'$.applicable')=1 "
        "    OR json_extract(je.value,'$.fired')=1) AS applicable, "
        "SUM(json_extract(je.value,'$.fired')=1) AS fired, "
        "SUM(json_extract(je.value,'$.gate')='default') AS default_gate, "
        "SUM(json_extract(je.value,'$.fired')=1 "
        "    AND json_extract(je.value,'$.applicable')=0) AS mismatch, "
        "SUM(COALESCE(json_extract(je.value,'$.n_concerns'),0)) AS concerns "
        "FROM ai_reviews ar, json_each(ar.check_coverage) je "
        "WHERE ar.check_coverage IS NOT NULL "
        "  AND (? IS NULL OR ar.methodology_version = ?) "
        "GROUP BY check_id "
        "ORDER BY fired DESC, applicable DESC, check_id",
        (mv, mv)).fetchall()

    rows = []
    for r in agg:
        applicable, fired = r["applicable"], r["fired"]
        lo, hi = _wilson_ci(fired, applicable)
        rate = fired / applicable if applicable else None
        rows.append({
            "id":          r["check_id"],
            "reviews":     r["reviews"],
            "applicable":  applicable,
            "fired":       fired,
            "concerns":    r["concerns"],
            "default_gate": r["default_gate"],
            "mismatch":    r["mismatch"],
            "rate":        rate,
            "rate_pct":    round(rate * 100) if rate is not None else None,
            "ci_lo_pct":   round(lo * 100, 1),
            "ci_hi_pct":   round(hi * 100, 1),
        })

    n_reviews = db.execute(
        "SELECT COUNT(*) FROM ai_reviews "
        "WHERE check_coverage IS NOT NULL "
        "  AND (? IS NULL OR methodology_version = ?)", (mv, mv)).fetchone()[0]
    versions = [v[0] for v in db.execute(
        "SELECT DISTINCT methodology_version FROM ai_reviews "
        "WHERE check_coverage IS NOT NULL AND methodology_version IS NOT NULL "
        "ORDER BY methodology_version DESC")]
    return {
        "rows":             rows,
        "n_reviews":        n_reviews,
        "versions":         versions,
        "selected_version": mv,
        "has_data":         bool(rows),
        "small_n":          n_reviews < _CHECK_USAGE_SMALL_N,
        "small_n_floor":    _CHECK_USAGE_SMALL_N,
        "chart":            check_usage_chart_config(rows),
    }


def check_usage_chart_config(rows):
    """A Chart.js horizontal floating-bar config: each check's bar spans its
       fire-rate Wilson 95% CI (lo→hi %), so the bar LENGTH is the uncertainty
       — a wide bar means "not enough data", which is the honest read at low
       review counts. Pure data, unit-testable."""
    return {
        "type": "bar",
        "data": {
            "labels": [r["id"] for r in rows],
            "datasets": [{
                "label": "fire rate 95% CI (%)",
                "data": [[r["ci_lo_pct"], r["ci_hi_pct"]] for r in rows],
                "backgroundColor": "#4e79a7",
            }],
        },
        "options": {
            "indexAxis": "y",
            "responsive": True,
            "maintainAspectRatio": False,
            "animation": False,
            "scales": {"x": {"beginAtZero": True, "max": 100,
                             "title": {"display": True,
                                       "text": "fire rate % (Wilson 95% CI)"}}},
            "plugins": {"legend": {"display": False}},
        },
    }
