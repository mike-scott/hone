"""hone-node task handlers — the two task types a node claims
(../ARCHITECTURE.md → AI node).

Skeleton: both handlers are stubs. Each takes a claim and returns the
completion record the runner submits via POST /v1/claims/{id}/result.
"""
import logging

from node.client import HoneCoreClient
from node.config import Config

log = logging.getLogger("hone.node.tasks")


def handle_review_task(cfg: Config, client: HoneCoreClient,
                       claim: dict) -> dict:
    """Review one patchset for one client; return the review record
    (../API.md → review completion record).

    TODO: fetch the patch archive (client.get_blob), prepare a worktree at
    the stated base commit (node.refrepo.prepare), confirm it applies
    (`git apply --check`), review it as pure analysis against the current
    methodology using the Claude API (the anthropic SDK), and shape the
    findings into the review record.
    """
    raise NotImplementedError("review task")


def handle_maintenance_task(cfg: Config, client: HoneCoreClient,
                            claim: dict) -> dict:
    """Evaluate the candidate practices against the methodology; return a
    maintenance record proposing changes (../ARCHITECTURE.md → merge gate).

    TODO: holistic evaluation / redraft as the claim specifies, using the
    Claude API.
    """
    raise NotImplementedError("maintenance task")
