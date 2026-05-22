"""The hone-node claim loop — claim a task, do it, submit, repeat.
See ../ARCHITECTURE.md (AI node, Node resilience).

Skeleton: the loop and its idle pacing are real; bootstrap, task execution
and the failure backoff are stubs / TODOs.
"""
import logging
import time

from node import tasks
from node.client import EnrollmentError, HoneCoreClient
from node.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("hone.node")


def bootstrap(cfg: Config, client: HoneCoreClient) -> None:
    """Prepare everything a from-scratch node needs before its first claim.

    Enrolls the node into the fleet via the device-authorization grant if it
    is not already — this blocks until an operator approves it.

    TODO: build / update the reference kernel repo (node.refrepo) under
    cfg.repo_dir; fetch the current methodology (client.get_methodology()).
    """
    client.ensure_enrolled()
    log.info("bootstrap — reference repo + methodology not yet implemented "
             "(repo_dir=%s)", cfg.repo_dir)


def run_once(cfg: Config, client: HoneCoreClient) -> bool:
    """Claim and handle one task. Return True if work was done, False if the
    queue was empty."""
    claim = client.claim()
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
    client.submit_result(claim["claim_id"], record)
    log.info("submitted result for %s", claim.get("claim_id"))
    return True


def main() -> None:
    cfg = Config.from_env()
    log.info("hone-node starting — core=%s", cfg.core_url)
    client = HoneCoreClient(cfg)
    try:
        bootstrap(cfg, client)
        # The claim loop. TODO: wrap transient failures in exponential
        # backoff + jitter (ARCHITECTURE.md → Node resilience); persist an
        # in-flight result to cfg.scratch_dir so it survives an outage.
        while True:
            did_work = run_once(cfg, client)
            if not did_work:
                time.sleep(cfg.poll_interval)
    except EnrollmentError as exc:
        log.error("node stopping — %s", exc)
    finally:
        client.close()
