"""Tests for the node's daily/weekly token budget (node/budget.py) and
   its integration points (config defaults, the call_claude accrual
   chokepoint, the run_once claim gate)."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from node import ai, budget, runner
from node.config import Config


def _cfg(tmp_path, daily=0, weekly=0):
    return SimpleNamespace(data_dir=str(tmp_path),
                           token_limit_daily=daily,
                           token_limit_weekly=weekly)


def _freeze(monkeypatch, iso):
    monkeypatch.setattr(budget, "_now", lambda: datetime.fromisoformat(iso)
                        .replace(tzinfo=timezone.utc))


# 2026-06-10 is a Wednesday; with the default Friday reset its weekly
# window is anchored at Friday 2026-06-05.
WED = "2026-06-10T12:00:00"


def test_record_accrues_into_both_windows_and_persists(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    _freeze(monkeypatch, WED)
    budget.record(cfg, {"input_tokens": 1000, "output_tokens": 200,
                        "duration_ms": 5})
    budget.record(cfg, {"input_tokens": 50, "output_tokens": 50})
    st = budget.status(cfg)
    assert st["day_tokens"] == 1300 and st["week_tokens"] == 1300
    # The ledger lives on the data volume — a restart (fresh read) sees
    # the same totals.
    stored = json.loads((tmp_path / budget.LEDGER_NAME).read_text())
    assert stored == {"day": "2026-06-10", "day_tokens": 1300,
                      "week": "2026-06-05", "week_tokens": 1300}


def test_record_is_thread_safe_under_concurrent_writes(monkeypatch, tmp_path):
    """record() is a read-modify-write of the ledger; without the lock,
       concurrent callers read the same base totals and lose all but the
       last write. With it, every token is counted. (The claim loop is
       serial today, so this guards against any future concurrent caller.)"""
    import threading
    cfg = _cfg(tmp_path)
    _freeze(monkeypatch, WED)
    n, per = 50, 100
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()                       # maximise overlap on the ledger
        budget.record(cfg, {"input_tokens": per, "output_tokens": 0})

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert budget.status(cfg)["day_tokens"] == n * per     # nothing lost


def test_day_rolls_at_utc_midnight_week_carries(monkeypatch, tmp_path):
    """At UTC midnight the daily window resets but the weekly total
       keeps accruing — they share a ledger, not a lifetime."""
    cfg = _cfg(tmp_path)
    _freeze(monkeypatch, "2026-06-10T23:59:59")
    budget.record(cfg, {"input_tokens": 700, "output_tokens": 0})
    _freeze(monkeypatch, "2026-06-11T00:00:01")     # Thursday, same ISO week
    st = budget.status(cfg)
    assert st["day_tokens"] == 0 and st["week_tokens"] == 700
    budget.record(cfg, {"input_tokens": 0, "output_tokens": 300})
    st = budget.status(cfg)
    assert st["day_tokens"] == 300 and st["week_tokens"] == 1000


def test_week_rolls_friday_utc_midnight_by_default(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    _freeze(monkeypatch, "2026-06-11T23:00:00")     # Thursday
    budget.record(cfg, {"input_tokens": 500, "output_tokens": 0})
    _freeze(monkeypatch, "2026-06-12T00:00:01")     # Friday 00:00 UTC
    st = budget.status(cfg)
    assert st["day_tokens"] == 0 and st["week_tokens"] == 0


def test_week_reset_day_is_configurable(monkeypatch, tmp_path):
    """token_week_reset_day moves the weekly boundary — here Sunday, so
       Saturday's usage vanishes at Sunday 00:00 UTC while a Friday
       boundary (the default) would have kept it."""
    cfg = _cfg(tmp_path)
    cfg.token_week_reset_day = budget.parse_week_reset_day("sunday")
    _freeze(monkeypatch, "2026-06-13T12:00:00")     # Saturday
    budget.record(cfg, {"input_tokens": 500, "output_tokens": 0})
    assert budget.status(cfg)["week_tokens"] == 500
    _freeze(monkeypatch, "2026-06-14T00:00:01")     # Sunday 00:00 UTC
    assert budget.status(cfg)["week_tokens"] == 0


def test_parse_week_reset_day_names_and_abbreviations():
    assert budget.parse_week_reset_day("friday") == 4
    assert budget.parse_week_reset_day("Friday") == 4
    assert budget.parse_week_reset_day("MON") == 0
    assert budget.parse_week_reset_day("sun") == 6
    with pytest.raises(RuntimeError, match="HONE_TOKEN_WEEK_RESET_DAY"):
        budget.parse_week_reset_day("payday")


def test_exhausted_reports_the_spent_window(monkeypatch, tmp_path):
    """A Friday-to-Tuesday walk, all inside one default weekly window
       (Friday reset): the daily cap trips and clears day by day until
       the accumulated weekly cap takes over."""
    cfg = _cfg(tmp_path, daily=1000, weekly=4500)
    _freeze(monkeypatch, "2026-06-12T12:00:00")      # Friday — fresh week
    assert budget.exhausted(cfg) is None
    budget.record(cfg, {"input_tokens": 999, "output_tokens": 0})
    assert budget.exhausted(cfg) is None             # under both caps
    budget.record(cfg, {"input_tokens": 1, "output_tokens": 0})
    assert budget.exhausted(cfg) == "daily"          # at the cap = spent
    assert budget.status(cfg)["exhausted"] == "daily"
    # Next day: the daily window is fresh, the weekly total survives —
    # keep going until the weekly cap trips with the daily one clear.
    _freeze(monkeypatch, "2026-06-13T01:00:00")      # Saturday
    assert budget.exhausted(cfg) is None
    budget.record(cfg, {"input_tokens": 999, "output_tokens": 0})
    _freeze(monkeypatch, "2026-06-14T01:00:00")      # Sunday
    budget.record(cfg, {"input_tokens": 999, "output_tokens": 0})
    _freeze(monkeypatch, "2026-06-15T01:00:00")      # Monday
    budget.record(cfg, {"input_tokens": 999, "output_tokens": 0})
    _freeze(monkeypatch, "2026-06-16T01:00:00")      # Tuesday
    budget.record(cfg, {"input_tokens": 600, "output_tokens": 5})
    # 1000 + 999*3 + 605 = 4602 ≥ the 4500 weekly cap, while Tuesday's
    # own 605 stays under the daily cap — only the weekly window trips.
    assert budget.exhausted(cfg) == "weekly"


def test_zero_limit_disables_that_window(monkeypatch, tmp_path):
    _freeze(monkeypatch, WED)
    cfg = _cfg(tmp_path, daily=0, weekly=0)
    budget.record(cfg, {"input_tokens": 10**9, "output_tokens": 0})
    assert budget.exhausted(cfg) is None
    # Daily off, weekly on: only the weekly cap can trip.
    cfg = _cfg(tmp_path, daily=0, weekly=100)
    assert budget.exhausted(cfg) == "weekly"


def test_corrupt_or_missing_ledger_reads_as_empty(monkeypatch, tmp_path):
    """A truncated/garbage ledger must not wedge the node — it reads as
       a fresh window (the atomic-rename save makes this an edge case,
       not a data-loss path)."""
    cfg = _cfg(tmp_path, daily=100)
    _freeze(monkeypatch, WED)
    (tmp_path / budget.LEDGER_NAME).write_text("{not json")
    assert budget.exhausted(cfg) is None
    budget.record(cfg, {"input_tokens": 60, "output_tokens": 60})
    assert budget.exhausted(cfg) == "daily"


def test_record_tolerates_empty_or_malformed_usage(monkeypatch, tmp_path):
    """record never raises — a ledger problem or odd usage dict must not
       turn an already-paid-for Claude turn into a task failure."""
    cfg = _cfg(tmp_path)
    _freeze(monkeypatch, WED)
    budget.record(cfg, None)
    budget.record(cfg, {})
    budget.record(cfg, {"input_tokens": "what", "output_tokens": None})
    assert budget.status(cfg)["day_tokens"] == 0


# --- config ----------------------------------------------------------------

def test_config_unset_means_no_budget_enforced(monkeypatch):
    """The .env settings are opt-in: unconfigured, both caps read 0 and
       budget.exhausted never trips (the 50M/300M figures live only in
       .env.example as suggested starting points). The weekly reset day
       still carries its Friday default for when a cap IS set."""
    for k in ("HONE_TOKEN_LIMIT_DAILY", "HONE_TOKEN_LIMIT_WEEKLY",
              "HONE_TOKEN_WEEK_RESET_DAY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HONE_CORE_URL", "https://core:8443")
    monkeypatch.setenv("HONE_FLEET_SECRET", "s")
    monkeypatch.setenv("HONE_CLAUDE_BACKEND", "cli")
    cfg = Config.from_env()
    assert cfg.token_limit_daily == 0
    assert cfg.token_limit_weekly == 0
    assert budget.exhausted(cfg) is None
    assert cfg.token_week_reset_day == 4             # Friday


def test_config_env_overrides_the_caps_and_reset_day(monkeypatch):
    monkeypatch.setenv("HONE_CORE_URL", "https://core:8443")
    monkeypatch.setenv("HONE_FLEET_SECRET", "s")
    monkeypatch.setenv("HONE_CLAUDE_BACKEND", "cli")
    monkeypatch.setenv("HONE_TOKEN_LIMIT_DAILY", "1000000")
    monkeypatch.setenv("HONE_TOKEN_LIMIT_WEEKLY", "0")
    monkeypatch.setenv("HONE_TOKEN_WEEK_RESET_DAY", "Sunday")
    cfg = Config.from_env()
    assert cfg.token_limit_daily == 1_000_000
    assert cfg.token_limit_weekly == 0
    assert cfg.token_week_reset_day == 6


def test_config_rejects_a_bogus_reset_day(monkeypatch):
    """A typo'd weekday fails the node at startup, like the other
       config validations — not a silent fall-back to Friday."""
    monkeypatch.setenv("HONE_CORE_URL", "https://core:8443")
    monkeypatch.setenv("HONE_FLEET_SECRET", "s")
    monkeypatch.setenv("HONE_CLAUDE_BACKEND", "cli")
    monkeypatch.setenv("HONE_TOKEN_WEEK_RESET_DAY", "fridya")
    with pytest.raises(RuntimeError, match="HONE_TOKEN_WEEK_RESET_DAY"):
        Config.from_env()


# --- integration: call_claude accrual chokepoint ----------------------------

def test_call_claude_accrues_usage_whichever_backend(monkeypatch, tmp_path):
    _freeze(monkeypatch, WED)
    cfg = SimpleNamespace(data_dir=str(tmp_path), claude_backend="cli",
                          anthropic_model="", token_limit_daily=0,
                          token_limit_weekly=0)
    out = {"text": "ok", "model": "m",
           "usage": {"input_tokens": 11, "output_tokens": 7,
                     "duration_ms": 3}}
    monkeypatch.setattr(ai, "_call_claude_cli", lambda *a, **kw: out)
    ai.call_claude(cfg, "sys", "hi")
    assert budget.status(cfg)["day_tokens"] == 18

    cfg = SimpleNamespace(data_dir=str(tmp_path / "sdk"),
                          claude_backend="sdk", anthropic_model="",
                          token_limit_daily=0, token_limit_weekly=0)
    monkeypatch.setattr(ai, "_call_claude_sdk", lambda *a, **kw: out)
    ai.call_claude(cfg, "sys", "hi")
    assert budget.status(cfg)["day_tokens"] == 18


# --- integration: the run_once claim gate -----------------------------------

class _NeverClaim:
    def claim(self):
        raise AssertionError("a budget-paused node must not claim")


def test_run_once_pauses_claiming_when_the_budget_is_spent(monkeypatch,
                                                           tmp_path):
    """Mirrors the low-disk guard: an exhausted budget idles the loop
       (return False) without touching hone-core, and the pause lifts
       by itself at rollover because exhausted() re-keys the windows."""
    _freeze(monkeypatch, WED)
    cfg = SimpleNamespace(data_dir=str(tmp_path), min_free_disk_mb=0,
                          token_limit_daily=100, token_limit_weekly=0,
                          backoff_initial=0.1, backoff_max=0.1)
    budget.record(cfg, {"input_tokens": 100, "output_tokens": 0})
    assert runner.run_once(cfg, _NeverClaim()) is False
    # Midnight rolls the window; the same call now reaches claim().
    _freeze(monkeypatch, "2026-06-11T00:00:01")
    with pytest.raises(AssertionError, match="must not claim"):
        runner.run_once(cfg, _NeverClaim())
