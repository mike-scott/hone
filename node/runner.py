"""The hone-node claim loop — claim a task, do it, submit, repeat.
See ../docs/ARCHITECTURE.md (hone-node) and
../docs/ARCHITECTURE-WORK-LIFECYCLE.md (Node resilience).

The loop, its idle pacing, and the transient-failure backoff are real;
bootstrap (the reference repo / methodology) and task execution are stubs.
"""
import contextlib
import logging
import os
import random
import signal
import sys
import threading
import time

import anthropic
import httpx

from common.version import __version__ as VERSION
from node import ai, budget, health, refrepo, tasks
from node.ai import CallClaudeAuthError
from node.client import EnrollmentError, HoneCoreClient, SchemaRejectedError
from node.config import Config


# The failure-path outcome each task_type's record uses when we need
# to submit a fallback after hone-core rejected the original record's
# shape (HTTP 422). Picked from each branch's schema enum:
# prepare → uncharacterisable, review/train → unappliable (deferred
# also valid but unappliable matches "we tried, this won't work"
# semantics better), draft → failed.
_FALLBACK_OUTCOME = {
    "prepare": "uncharacterisable",
    "review":  "unappliable",
    "train":   "unappliable",
    "draft":   "failed",
}

# Log level is env-driven so an operator can flip to DEBUG without a
# rebuild. HONE_LOG_LEVEL accepts the standard level names
# (DEBUG / INFO / WARNING / ERROR); unknown values fall back to INFO.
# Set in node/.env to e.g. `HONE_LOG_LEVEL=DEBUG` and recreate the
# container.
_LOG_LEVEL = os.environ.get("HONE_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("hone.node")


# --- transient-failure backoff (ARCHITECTURE.md → Node resilience) ---------

# The exception classes the backoff loop catches and inspects. Each call to
# fn() may raise either a hone-core HTTP error (httpx) or a Claude API
# error (anthropic). The list is conservative — only classes that
# `_is_transient` may decide are retryable. Everything else propagates
# straight up and gets the operator's attention.
_BACKOFF_CATCHES = (httpx.TransportError, httpx.HTTPStatusError,
                    anthropic.APIConnectionError, anthropic.APIStatusError)


def _is_transient(exc: Exception) -> bool:
    """Whether a failure should be retried with backoff.

       Network errors and timeouts are always transient (both httpx-side
       talking to hone-core, and anthropic-side talking to Claude). An
       HTTP status is transient only when it's 429 (rate limit) or 5xx
       (upstream momentarily unhealthy). Everything else — a 4xx,
       an EnrollmentError, an `anthropic.AuthenticationError` (which is
       already translated to `CallClaudeAuthError` and never reaches
       this classifier) — is configuration- or contract-level and
       must surface to the operator rather than loop silently."""
    # Network-level transients on either upstream.
    if isinstance(exc, (httpx.TransportError,
                         anthropic.APIConnectionError)):
        return True
    # HTTP-status transients on hone-core.
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    # HTTP-status transients on Claude. The SDK's status-error hierarchy
    # has dedicated subclasses (RateLimitError, InternalServerError);
    # checking status_code is equivalent and stays robust to future
    # subclasses the SDK might add.
    if isinstance(exc, anthropic.APIStatusError):
        code = getattr(exc, "status_code", None) or 0
        return code == 429 or code >= 500
    return False


def _retry_after(exc: Exception) -> float | None:
    """The delay an upstream asks for via `Retry-After` (integer-seconds
       form), or None — an HTTP-date Retry-After falls back to the
       computed backoff. Both httpx.HTTPStatusError and
       anthropic.APIStatusError expose `.response.headers`, so one
       getattr-driven path serves both."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        hdr = resp.headers.get("Retry-After", "")
        if hdr.isdigit():
            return float(hdr)
    return None


def _describe(exc: Exception) -> str:
    """A one-phrase log-line label for a transient error. Per-class so
       the operator can tell at a glance whether the upstream is
       hone-core or Claude."""
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude returned HTTP {exc.status_code}"
    if isinstance(exc, anthropic.APIConnectionError):
        return f"Claude unreachable ({exc.__class__.__name__})"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"hone-core returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TransportError):
        return f"hone-core unreachable ({exc.__class__.__name__})"
    return f"unexpected transient error ({exc.__class__.__name__})"


def _with_backoff(cfg: Config, label: str, fn):
    """Call fn(); on a transient failure wait — exponential backoff with full
       jitter, a 429's Retry-After honoured — and retry, indefinitely (a node
       has nothing to do but reconnect). A non-transient exception propagates.
       Returns fn()'s result once it succeeds."""
    delay = cfg.backoff_initial
    failures = 0
    while True:
        try:
            result = fn()
            if failures:
                log.info("%s — recovered after %d transient failure(s)",
                         label, failures)
            return result
        except _BACKOFF_CATCHES as exc:
            if not _is_transient(exc):
                raise
            failures += 1
            wait = _retry_after(exc)
            if wait is None:
                wait = random.uniform(0, delay)          # exponential + jitter
                delay = min(delay * 2, cfg.backoff_max)
            log.warning("%s — %s (attempt %d); retrying in %.1fs",
                        label, _describe(exc), failures, wait)
            time.sleep(wait)


# --- the claim loop --------------------------------------------------------

def bootstrap(cfg: Config, client: HoneCoreClient) -> None:
    """Prepare everything a from-scratch node needs before its first claim.

    Enrolls the node into the fleet via the device-authorization grant if it
    is not already — this blocks until an operator approves it, and retries
    with backoff if hone-core is unreachable.

    Then initializes the reference kernel repo (node.refrepo) — an empty
    git repo with the named-trees remotes registered; base commits fetch on
    demand at review time. Tree-bound work (`review`) needs it; a prepare-only
    node (Tier-0/1 are tree-free) would not, but this node supports review so
    it is initialized unconditionally. Pure local init (no network), so it
    runs outside the backoff wrapper; idempotent, so a restart reuses the
    volume.

    TODO: fetch the current methodology (client.get_methodology()). Not yet
    blocking — the claim payload already carries the compiled methodology.
    """
    _with_backoff(cfg, "enrollment", client.ensure_enrolled)
    if "review" in tasks.SUPPORTED_TASK_TYPES:
        refrepo.ensure_repo()
        # A restart after a SIGKILL mid-review strands that review's ~1.5 GB
        # worktree; reclaim any such leftovers before claiming new work.
        swept = refrepo.sweep_worktrees(cfg.scratch_dir)
        if swept:
            log.info("bootstrap — reclaimed %d leaked review worktree(s)",
                     swept)
        log.info("bootstrap — reference repo ready (repo_dir=%s)", cfg.repo_dir)
    else:
        log.info("bootstrap — no tree-bound task types; skipping reference repo")


@contextlib.contextmanager
def _keepalive(cfg: Config, client: HoneCoreClient, claim_id: str):
    """Keep this node fresh with hone-core while a long task runs.

       A task handler — a review especially — can block for many minutes
       inside a single Claude call, during which the claim loop sends
       nothing to hone-core. Two things then drift: the work-item lease
       creeps toward expiry (the work gets reclaimed out from under us),
       and the operator UI marks the node stale (its `last_seen` ages past
       `stale_after`). This daemon thread fills that silence — every
       `cfg.heartbeat_interval` seconds it extends the claim's lease and
       posts a health snapshot, so a node mid-review stays live in the UI
       and holds its claim.

       Best-effort and self-contained: each upstream call is wrapped so a
       blip talking to hone-core never propagates into the task. The
       handler doesn't touch `client`, so this thread has the HTTP client
       to itself for the task's duration — no concurrent use to guard. A
       daemon thread, so it can't keep the process alive; the stop is a
       threading.Event set on block exit (mirrors node.ai._heartbeat)."""
    done = threading.Event()

    def beat():
        while not done.wait(cfg.heartbeat_interval):
            try:
                client.heartbeat(claim_id)
            except Exception:
                log.warning("keep-alive heartbeat failed for %s "
                            "(will retry next interval)", claim_id,
                            exc_info=True)
            _report_health_safely(cfg, client)

    t = threading.Thread(target=beat, name="hone-keepalive", daemon=True)
    t.start()
    try:
        yield
    finally:
        done.set()
        t.join(timeout=5)


def run_once(cfg: Config, client: HoneCoreClient) -> bool:
    """Claim and handle one task. Return True if work was done, False if
    the queue was empty.

    All three upstream calls — claim, task handler, submit — are wrapped
    in `_with_backoff`, so a transient failure on either hone-core
    (network blip, 429, 5xx) or Claude (rate limit, momentary
    APIConnectionError) loops with exponential backoff instead of
    crashing the node. Configuration-fatal errors (a wrong
    ANTHROPIC_API_KEY, an EnrollmentError) propagate up to `main` for
    a clean one-line exit.

    `submit_result` is idempotent on the claim id, so a retry after a
    network blip never double-counts the work. The task handler is
    NOT idempotent — a retry re-runs the Claude call from scratch —
    which is the right behaviour: a 429 or 5xx on the prior attempt
    means the model never observed our prompt, so re-issuing is the
    only path to a result.

    A disk-floor guard runs first: when free space on the data volume is
    below cfg.min_free_disk_mb the node idles instead of claiming, so a
    base fetch or ~1.5 GB review worktree can't fail mid-task with ENOSPC.
    The idle tick still runs _maybe_maintain (sweep + gc), which can
    reclaim space and let the next tick resume.

    A token-budget guard runs second: once the day's or week's accrued
    Claude usage crosses its cap (cfg.token_limit_daily / _weekly), the
    node idles instead of claiming and resumes by itself when the window
    rolls over at UTC midnight (node/budget). Enforced between tasks
    only — an in-flight task always runs to completion."""
    if _disk_too_low(cfg):
        log.warning("disk: free space on %s below the %d MB floor — pausing "
                    "claims until it recovers (sweep/gc run between ticks)",
                    cfg.data_dir, cfg.min_free_disk_mb)
        return False
    over = budget.exhausted(cfg)
    if over:
        log.warning("token budget: the %s limit is spent — pausing claims "
                    "until the window rolls over at UTC midnight", over)
        return False
    claim = _with_backoff(cfg, "claim", client.claim)
    if claim is None:
        return False
    log.info("claimed %s (%s)", claim.get("claim_id"),
             claim.get("task_type"))
    # Safety net: the node declares its capabilities on every claim, so
    # hone-core shouldn't hand back an unsupported type — but if it does
    # (an old hone-core that ignores the declaration, or a deploy-order
    # window), release the claim and idle rather than crash on the handler's
    # NotImplementedError. Returning False idles the loop so this degrades
    # to a slow poll, not a hot reclaim spin.
    task_type = claim.get("task_type")
    if task_type not in tasks.SUPPORTED_TASK_TYPES:
        log.warning("claimed unsupported task_type %r (this node supports "
                    "%s) — releasing; check hone-core honours the per-claim "
                    "capability declaration", task_type,
                    ", ".join(tasks.SUPPORTED_TASK_TYPES))
        try:
            _with_backoff(cfg, "release unsupported claim",
                          lambda: client.release_claim(
                              claim["claim_id"],
                              reason=f"node does not support {task_type}"))
        except Exception:
            log.exception("release of unsupported claim failed — it will "
                          "lapse on its lease instead")
        return False
    try:
        # A long task (a review can block minutes in one Claude call)
        # otherwise sends nothing to hone-core: the lease drifts toward
        # expiry and the node shows stale. The keep-alive thread heartbeats
        # the claim and posts health on cfg.heartbeat_interval until the
        # task returns. It exits before submit_result below, so the main
        # thread never shares the HTTP client with it.
        with _keepalive(cfg, client, claim["claim_id"]):
            record = _with_backoff(
                cfg, f"{claim.get('task_type')} task",
                lambda: tasks.dispatch(cfg, client, claim))
    except Exception as exc:
        # Non-transient task failure — _with_backoff has already
        # exhausted retries for the transient classes. Release the
        # claim before the exception propagates so a correctly-
        # configured peer can pick the work-item up immediately
        # instead of waiting (default 30 min) for the lease to lapse.
        # Best-effort: a failed release falls back to lease expiry,
        # the original exception still propagates to main().
        # KeyboardInterrupt is BaseException, not Exception — the
        # SIGTERM/SIGINT shutdown path is unaffected.
        try:
            _with_backoff(cfg, "release claim",
                          lambda: client.release_claim(
                              claim["claim_id"],
                              reason=f"{type(exc).__name__}: {exc}"))
        except Exception:
            log.exception("release claim failed — the claim will "
                          "lapse on its lease instead")
        # Surface the failure category to hone-core before re-raising,
        # so the operator's /nodes page reflects the latest snapshot
        # (e.g. last_anthropic_error="auth") even though this node is
        # about to exit.
        _report_health_safely(cfg, client)
        raise
    _stamp_provenance(cfg, record)
    try:
        _with_backoff(cfg, "submit result",
                      lambda: client.submit_result(claim["claim_id"], record))
    except SchemaRejectedError as exc:
        # hone-core's schema validator returned 422. Build a fallback
        # failure-outcome record carrying the rejected payload + the
        # validator's reason in `meta` and submit THAT, so the failure
        # lands in the corpus as debuggable data instead of the node
        # crashing on an unhandled httpx exception. If the fallback
        # also fails, the original SchemaRejectedError surfaces (and
        # the outer abort path releases the claim cleanly).
        log.warning("submit result: hone-core 422 — submitting "
                     "fallback %s record: %s",
                     _FALLBACK_OUTCOME.get(claim.get("task_type"),
                                            "uncharacterisable"),
                     exc.detail[:200])
        fallback = _build_schema_rejected_fallback(claim, record, exc)
        _stamp_provenance(cfg, fallback)
        _with_backoff(cfg, "submit fallback",
                      lambda: client.submit_result(
                          claim["claim_id"], fallback))
    log.info("submitted result for %s", claim.get("claim_id"))
    return True


def _stamp_provenance(cfg, record):
    """Stamp run provenance into the record's open `meta` before submit:
       `claude_cli_version` — the CLI build that produced this result.
       Permanently attributes every operation to the exact build (builds
       drift across the fleet with per-prompt auto-update), so a
       review-quality regression can be correlated with a CLI release
       after the fact. Centralised at the submit chokepoint so every
       task type and outcome — including future handlers and the
       schema-rejected fallback — carries it. CLI backend only: an
       SDK-produced record must not claim CLI provenance. The record
       schema's `meta` is deliberately open, so no schema bump."""
    if getattr(cfg, "claude_backend", None) != "cli":
        return
    version = ai.get_cli_version()
    if not version or not isinstance(record, dict):
        return
    record.setdefault("meta", {})["claude_cli_version"] = version


def _build_schema_rejected_fallback(claim: dict, record: dict,
                                     exc: SchemaRejectedError) -> dict:
    """Compose a failure-outcome record after hone-core rejected the
       original record's shape (HTTP 422). The original payload + the
       validator's reason go into `meta` so the failure remains
       inspectable from work_items.record — not lost to a node
       crash."""
    task_type = claim.get("task_type", "prepare")
    outcome = _FALLBACK_OUTCOME.get(task_type, "uncharacterisable")
    return {
        "task_type": task_type,
        "worker_id": record.get("worker_id", ""),
        "outcome":   outcome,
        "model":     record.get("model", ""),
        "usage":     record.get("usage")
                      or {"input_tokens":  0, "output_tokens": 0,
                          "duration_ms":   0},
        "reason":    f"hone-core 422: {exc.detail[:300]}",
        "meta":      {"rejected_outcome": record.get("outcome"),
                      "rejected_record":  record,
                      "schema_error":     exc.detail},
    }


def _print_banner() -> None:
    """Stamp the running version to stdout at startup as `hone-node-<version>`,
       framed so it stands out in `docker logs`."""
    label = f"hone-node-{VERSION}"
    bar = "=" * (len(label) + 4)
    print(f"{bar}\n  {label}\n{bar}", flush=True)


def _report_health_safely(cfg: Config, client: HoneCoreClient) -> None:
    """Send a health snapshot to hone-core, swallowing any failure —
       a flaky health-report endpoint must never disrupt the claim
       loop. Called once per loop tick (idle and work-done) plus
       inside the task-abort path so the latest signal reaches the
       operator UI even on the exit path.

       Failures log at WARNING so the operator notices when a health
       report can't get through (the operator UI's /nodes page would
       otherwise sit stale and the cause would be invisible). The
       failure itself is not fatal — the next tick retries."""
    try:
        snap = health.collect(cfg)
        client.report_health(snap)
        log.debug("health report sent: %s", snap)
    except Exception:
        log.warning("health report failed (will retry next tick)",
                    exc_info=True)


# Idle disk-maintenance cadence. The worktree sweep is cheap and runs every
# maintenance pass; the gc size-check (a `du` over the repo) and gc itself
# are throttled to this interval so a busy node doesn't `du` a large repo on
# every loop. See refrepo.gc / refrepo.sweep_worktrees.
_GC_CHECK_INTERVAL_SECONDS = 600


def _disk_too_low(cfg: Config) -> bool:
    """True when free space on the data volume is below cfg.min_free_disk_mb —
       run_once then idles instead of claiming work whose base fetch or
       ~1.5 GB review worktree could fail mid-task with ENOSPC (leaving a
       partial checkout). HONE_MIN_FREE_DISK_MB=0 disables the guard. A None
       reading (volume not mounted / stat error) does NOT pause — a
       measurement gap shouldn't wedge the node. The same condition surfaces
       to the operator as health.disk_low."""
    floor = getattr(cfg, "min_free_disk_mb", 0)
    if not floor:
        return False
    free = health._free_disk_mb(cfg.data_dir)
    return free is not None and free < floor


def _maybe_maintain(cfg: Config, state: dict) -> None:
    """Between-task disk maintenance — the caller invokes it only after
       run_once returns, when this node holds no worktree, so it never
       contends with an in-flight review. Two jobs:

         1. Reclaim leaked review worktrees — a SIGKILL mid-review strands a
            ~1.5 GB checkout (graceful paths clean up via refrepo.cleanup).
            Cheap, so every pass.
         2. gc the reference repo once it crosses cfg.repo_gc_threshold_mb, or
            cfg.repo_gc_every tasks have run since the last gc — discarding the
            unreachable churn arbitrary base fetches accrete (left unchecked
            the repo reached 116 GB). The du + gc is throttled to
            _GC_CHECK_INTERVAL_SECONDS.

       `state` carries the cross-tick counters {tasks_since_gc, last_gc_check}.
       Best-effort: any failure is logged, never fatal to the claim loop."""
    try:
        swept = refrepo.sweep_worktrees(cfg.scratch_dir)
        if swept:
            log.info("disk: reclaimed %d leaked review worktree(s) from %s",
                     swept, cfg.scratch_dir)
        now = time.monotonic()
        if now - state["last_gc_check"] < _GC_CHECK_INTERVAL_SECONDS:
            return
        state["last_gc_check"] = now
        due_by_count = (cfg.repo_gc_every > 0
                        and state["tasks_since_gc"] >= cfg.repo_gc_every)
        size_mb = refrepo.size_mb()
        due_by_size = (cfg.repo_gc_threshold_mb > 0
                       and size_mb >= cfg.repo_gc_threshold_mb)
        if not (due_by_count or due_by_size):
            return
        log.info("disk: gc reference repo — %.1f GB, %d task(s) since gc "
                 "(trigger=%s)", size_mb / 1024, state["tasks_since_gc"],
                 "size" if due_by_size else "count")
        if refrepo.gc():
            log.info("disk: gc complete — %.1f GB -> %.1f GB",
                     size_mb / 1024, refrepo.size_mb() / 1024)
        else:
            log.warning("disk: gc failed (will retry next cycle)")
        state["tasks_since_gc"] = 0
    except Exception:
        log.warning("disk maintenance failed (non-fatal)", exc_info=True)


def _fatal_config_error(message: str) -> None:
    """Log a known-fatal configuration error as a single ERROR line and
       exit non-zero. The traceback would not help an operator — the
       cause is environmental — so we keep the container log readable
       by NOT raising further. Docker's restart policy reinvokes the
       node; each restart prints the same one-line message until the
       operator fixes the config."""
    log.error("hone-node CONFIG ERROR — %s", message)
    log.error("Container will exit; restart-loop will continue until "
              "the configuration is fixed.")
    sys.exit(1)


def main() -> None:
    _print_banner()
    try:
        cfg = Config.from_env()
    except RuntimeError as exc:
        # `Config.from_env` raises this when a required env var is missing
        # ("missing required environment: HONE_CORE_URL, …"). The default
        # unhandled-exception path would bury this in a Python traceback;
        # surface it as a one-line operator error instead.
        _fatal_config_error(str(exc))
        return        # _fatal_config_error sys.exits; this satisfies linters
    # `docker stop` sends SIGTERM. The node is PID 1, and the kernel does not
    # apply a signal's default action to PID 1 — a SIGTERM left at its OS
    # default is silently dropped, so docker SIGKILLs the node (exit 137).
    # Route SIGTERM to the SIGINT handler: it raises KeyboardInterrupt, which
    # unwinds the loop cleanly so `finally` runs.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    log.info("hone-node starting — core=%s", cfg.core_url)
    client = HoneCoreClient(cfg, task_types=tasks.SUPPORTED_TASK_TYPES)
    try:
        bootstrap(cfg, client)
        # The claim loop. Transient failures (core unreachable, 5xx, 429) are
        # retried with backoff inside the calls above.
        # TODO: persist an in-flight result to cfg.scratch_dir so a completed
        # review survives a node restart instead of being re-claimed.
        # Cross-tick disk-maintenance counters (see _maybe_maintain).
        maint = {"tasks_since_gc": 0, "last_gc_check": 0.0}
        while True:
            did_work = run_once(cfg, client)
            if did_work:
                maint["tasks_since_gc"] += 1
            # Per-tick health report: fires after a successful task
            # submit (did_work=True) and after an empty-queue 204
            # (did_work=False). The abort path inside run_once posts
            # its own snapshot before re-raising, so the operator UI
            # always reflects the most recent signal regardless of
            # exit path.
            _report_health_safely(cfg, client)
            # Disk maintenance at the safe between-task point: run_once has
            # returned, so no worktree is in flight on this node. Throttled
            # internally; sweeps leaked worktrees and gc's the reference repo
            # when it has grown past the threshold.
            _maybe_maintain(cfg, maint)
            if not did_work:
                time.sleep(cfg.poll_interval)
    except EnrollmentError as exc:
        log.error("node stopping — %s", exc)
        sys.exit(1)
    except CallClaudeAuthError as exc:
        # Configuration-fatal: a wrong / revoked / placeholder
        # ANTHROPIC_API_KEY. A 30-line SDK traceback is what was making
        # this hard to diagnose in `docker logs`; collapse it to a
        # single readable line.
        _fatal_config_error(str(exc))
    except KeyboardInterrupt:
        log.info("hone-node stopping (SIGTERM/SIGINT)")
    finally:
        client.close()
