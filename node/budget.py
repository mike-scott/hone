"""Daily / weekly token budget for a node.

Every Claude call's reported usage (input + output tokens) accrues into
a small JSON ledger on the data volume. Once a window's total crosses
its cap, the runner pauses claiming — the same idle-not-crash behaviour
as the low-disk guard — and resumes when the window rolls over at UTC
midnight. The daily window is the UTC calendar day; the weekly window
runs from UTC midnight on the configured reset day (default Friday) to
the next.

Optional configuration (node/.env) — unset (or 0), no budget is
enforced; usage still accrues to the ledger so a later-set cap starts
from real numbers:

  HONE_TOKEN_LIMIT_DAILY        tokens/day; .env.example suggests 50M
  HONE_TOKEN_LIMIT_WEEKLY       tokens/week; .env.example suggests 300M
  HONE_TOKEN_WEEK_RESET_DAY     default friday — the weekday whose UTC
                                midnight starts a fresh weekly window

Ledger: $HONE_DATA/token-ledger.json — on the persistent volume so a
container restart doesn't reset the count. The node is single-process
and the claim loop is serial, so a plain read-modify-write with an
atomic rename is sufficient; an unreadable or corrupt ledger is treated
as empty rather than wedging the node.

The cap is enforced between tasks, not mid-task: a claim made just
under the limit runs to completion, so a window can overshoot by up to
one task's usage. That's deliberate — killing an in-flight Claude turn
would waste the tokens it already spent.
"""
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

log = logging.getLogger("hone.node.budget")

LEDGER_NAME = "token-ledger.json"

# datetime.weekday() numbering (Monday = 0). Full names and 3-letter
# abbreviations both accepted in HONE_TOKEN_WEEK_RESET_DAY.
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}
_WEEKDAYS.update({name[:3]: n for name, n in list(_WEEKDAYS.items())})

DEFAULT_WEEK_RESET_DAY = _WEEKDAYS["friday"]


def parse_week_reset_day(name):
    """HONE_TOKEN_WEEK_RESET_DAY → a datetime.weekday() number. A typo
       here must fail the node at startup (like the other config
       validations), not silently budget against the wrong week."""
    try:
        return _WEEKDAYS[str(name).strip().lower()]
    except KeyError:
        raise RuntimeError(
            f"HONE_TOKEN_WEEK_RESET_DAY={name!r} unsupported; expected a "
            "weekday name (monday..sunday, or mon..sun)")


def _week_reset_day(cfg):
    return getattr(cfg, "token_week_reset_day", DEFAULT_WEEK_RESET_DAY)


def _now():
    """UTC now — a seam for the rollover tests."""
    return datetime.now(timezone.utc)


def _window_keys(dt, week_reset_day=DEFAULT_WEEK_RESET_DAY):
    """(day_key, week_key) for a UTC datetime: the ISO date, and the
       ISO date of the most recent reset day at or before it. Both
       windows roll at UTC midnight — the day every midnight, the week
       on the reset day's. Changing the reset day mid-week re-keys the
       window, which simply starts a fresh weekly count."""
    day = dt.date()
    start = day - timedelta(days=(day.weekday() - week_reset_day) % 7)
    return day.isoformat(), start.isoformat()


def _ledger_path(cfg):
    """The ledger file on the data volume, or None when cfg carries no
       data_dir (minimal test stubs) — no ledger means nothing accrues
       and nothing is enforced."""
    data_dir = getattr(cfg, "data_dir", None)
    return os.path.join(data_dir, LEDGER_NAME) if data_dir else None


def _load(cfg):
    """The ledger with expired windows already rolled to zero. Corrupt
       or missing files read as an empty current-window ledger."""
    day_key, week_key = _window_keys(_now(), _week_reset_day(cfg))
    ledger = {"day": day_key, "day_tokens": 0,
              "week": week_key, "week_tokens": 0}
    path = _ledger_path(cfg)
    if path is None:
        return ledger
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return ledger
    if not isinstance(stored, dict):
        return ledger
    if stored.get("day") == day_key:
        ledger["day_tokens"] = int(stored.get("day_tokens") or 0)
    if stored.get("week") == week_key:
        ledger["week_tokens"] = int(stored.get("week_tokens") or 0)
    return ledger


def _save(cfg, ledger):
    """Atomic write (tmp + rename) so a crash mid-write can't leave a
       truncated ledger that _load would then zero."""
    path = _ledger_path(cfg)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix=".token-ledger-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ledger, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record(cfg, usage):
    """Accrue one Claude call's usage (input + output tokens, as the
       backend reported them — the CLI path already folds cache tokens
       into input_tokens) into both windows. Never raises: a ledger
       I/O failure must not turn a successful Claude turn into a task
       failure — the tokens are already spent either way."""
    try:
        tokens = (int((usage or {}).get("input_tokens") or 0)
                  + int((usage or {}).get("output_tokens") or 0))
    except (TypeError, ValueError):
        tokens = 0
    if tokens <= 0 or _ledger_path(cfg) is None:
        return
    try:
        ledger = _load(cfg)
        ledger["day_tokens"] += tokens
        ledger["week_tokens"] += tokens
        _save(cfg, ledger)
    except OSError as exc:
        log.error("token ledger update failed (%s) — %d token(s) not "
                  "counted toward the budget", exc, tokens)


def exhausted(cfg):
    """Which budget window is spent: "daily", "weekly", or None. A cap
       of 0 disables that window; rolled-over windows read as zero, so
       a paused node resumes by itself at UTC midnight."""
    daily = getattr(cfg, "token_limit_daily", 0) or 0
    weekly = getattr(cfg, "token_limit_weekly", 0) or 0
    if not daily and not weekly:
        return None
    ledger = _load(cfg)
    if daily and ledger["day_tokens"] >= daily:
        return "daily"
    if weekly and ledger["week_tokens"] >= weekly:
        return "weekly"
    return None


def status(cfg):
    """The compact dict the health snapshot carries: current-window
       totals, the configured caps (0 = disabled), and which window —
       if any — has the node paused."""
    ledger = _load(cfg)
    return {"day_tokens":  ledger["day_tokens"],
            "day_limit":   getattr(cfg, "token_limit_daily", 0) or 0,
            "week_tokens": ledger["week_tokens"],
            "week_limit":  getattr(cfg, "token_limit_weekly", 0) or 0,
            "exhausted":   exhausted(cfg)}
