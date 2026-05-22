"""The hone-node claim loop — claim a task, do it, submit, repeat.
See ../ARCHITECTURE.md (AI node, Node resilience).

The loop, its idle pacing, and the transient-failure backoff are real;
bootstrap (the reference repo / methodology) and task execution are stubs.
"""
import logging
import random
import signal
import time

import httpx

from common.version import __version__ as VERSION
from node import tasks
from node.client import EnrollmentError, HoneCoreClient
from node.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("hone.node")


# --- transient-failure backoff (ARCHITECTURE.md → Node resilience) ---------

def _is_transient(exc: Exception) -> bool:
    """Whether a failure should be retried with backoff. Network errors and
       timeouts always are; an HTTP status only for 429 and 5xx. Everything
       else — a 4xx, an EnrollmentError — is not transient and must surface."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _retry_after(exc: Exception) -> float | None:
    """The delay a 429 asks for via Retry-After (integer-seconds form), or
       None — an HTTP-date Retry-After falls back to the computed backoff."""
    if isinstance(exc, httpx.HTTPStatusError):
        hdr = exc.response.headers.get("Retry-After", "")
        if hdr.isdigit():
            return float(hdr)
    return None


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"hone-core returned HTTP {exc.response.status_code}"
    return f"hone-core unreachable ({exc.__class__.__name__})"


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
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
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
    """Claim and handle one task. Return True if work was done, False if the
    queue was empty. The hone-core calls — the claim and the result submit —
    are retried through transient failures; `submit_result` is idempotent on
    the claim id, so retrying it never double-counts the work."""
    claim = _with_backoff(cfg, "claim", client.claim)
    if claim is None:
        return False
    task_type = claim.get("task_type")
    log.info("claimed %s (%s)", claim.get("claim_id"), task_type)
    if task_type == "review":
        record = tasks.handle_review_task(cfg, client, claim)
    elif task_type == "maintenance":
        record = tasks.handle_maintenance_task(cfg, client, claim)
    else:
        raise ValueError(f"unknown task_type: {task_type!r}")
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


def main() -> None:
    _print_banner()
    cfg = Config.from_env()
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
    except KeyboardInterrupt:
        log.info("hone-node stopping (SIGTERM/SIGINT)")
    finally:
        client.close()
