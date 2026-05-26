"""The hone-node claim loop — claim a task, do it, submit, repeat.
See ../docs/ARCHITECTURE.md (hone-node) and
../docs/ARCHITECTURE-WORK-LIFECYCLE.md (Node resilience).

The loop, its idle pacing, and the transient-failure backoff are real;
bootstrap (the reference repo / methodology) and task execution are stubs.
"""
import logging
import random
import signal
import sys
import time

import anthropic
import httpx

from common.version import __version__ as VERSION
from node import tasks
from node.ai import CallClaudeAuthError
from node.client import EnrollmentError, HoneCoreClient
from node.config import Config

logging.basicConfig(
    level=logging.INFO,
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

    TODO: build / update the reference kernel repo (node.refrepo) under
    cfg.repo_dir; fetch the current methodology (client.get_methodology()).
    """
    _with_backoff(cfg, "enrollment", client.ensure_enrolled)
    log.info("bootstrap — reference repo + methodology not yet implemented "
             "(repo_dir=%s)", cfg.repo_dir)


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
    only path to a result."""
    claim = _with_backoff(cfg, "claim", client.claim)
    if claim is None:
        return False
    log.info("claimed %s (%s)", claim.get("claim_id"),
             claim.get("task_type"))
    record = _with_backoff(
        cfg, f"{claim.get('task_type')} task",
        lambda: tasks.dispatch(cfg, client, claim))
    _with_backoff(cfg, "submit result",
                  lambda: client.submit_result(claim["claim_id"], record))
    log.info("submitted result for %s", claim.get("claim_id"))
    return True


def _print_banner() -> None:
    """Stamp the running version to stdout at startup as `hone-node-<version>`,
       framed so it stands out in `docker logs`."""
    label = f"hone-node-{VERSION}"
    bar = "=" * (len(label) + 4)
    print(f"{bar}\n  {label}\n{bar}", flush=True)


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
    client = HoneCoreClient(cfg)
    try:
        bootstrap(cfg, client)
        # The claim loop. Transient failures (core unreachable, 5xx, 429) are
        # retried with backoff inside the calls above.
        # TODO: persist an in-flight result to cfg.scratch_dir so a completed
        # review survives a node restart instead of being re-claimed.
        while True:
            did_work = run_once(cfg, client)
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
