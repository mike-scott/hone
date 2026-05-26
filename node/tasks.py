"""hone-node task handlers — the four task types a node claims
(../docs/ARCHITECTURE.md → hone-node).

Each handler takes a claim payload (the body of POST /v1/claims/), does its
work, and returns the completion record the runner submits via
POST /v1/claims/{id}/result. The records are validated against
../common/schema/completion-record.schema.yaml.

Skeletons: the dispatch + shape are complete; the actual AI calls
(Claude API) raise NotImplementedError pending the AI integration.
"""
import logging

from node.client import HoneCoreClient
from node.config import Config

log = logging.getLogger("hone.node.tasks")


def handle_prepare_task(cfg: Config, client: HoneCoreClient,
                        claim: dict) -> dict:
    """`prepare` task: characterise one patchset for the corpus.

    Claim payload carries:
      - methodology_version, methodology (compiled doc with core.principles
        + operations.prepare.{guidance, return})
      - patchset (root_message_id, subject, declared_base_commit,
        submitter_email, n_patches)
      - patches: [{message_id, part_index, body}, …]
      - cover_letter_body (the [PATCH 0/N] body, or null)
      - thread_messages: [{message_id, author_*, in_reply_to, body}, …]
        — the comment/reply messages prepare reads for review-intensity
        classification (bot/self-filter, in_scope check)

    The handler:
      1. Discovers the base commit, decides mode (authoritative /
         heuristic / mixed) — owns all tree access.
      2. Calls the Claude API with the prepare prompt + the payload,
         requesting the structured per-field metadata return (subsystem,
         patch_size, maintainer, patch_type, review_intensity (incl.
         per_reply), tree_state, preparation_notes).
      3. Validates the response.
      4. Returns the prepare completion record.

    TODO: AI integration. The dispatch + shape are wired."""
    raise NotImplementedError("prepare: AI integration not yet wired")


def handle_review_task(cfg: Config, client: HoneCoreClient,
                       claim: dict) -> dict:
    """`review` task: an AI patchset review.

    Claim payload carries:
      - methodology_version, methodology (compiled doc with core +
        operations.review.{guidance, return})
      - patchset (root_message_id, subject, base_commit, n_patches, …)
      - patchset_metadata (the prepare-task output: subsystem,
        patch_size, maintainer, patch_type, review_intensity, tree_state)
      - patches: [{message_id, part_index, body}, …]  — the patch messages
        only, with no review comments interleaved

    The handler:
      1. Stages the base tree (refrepo.prepare) and confirms the patches
         apply (`git apply --check`); on failure → outcome=unappliable.
      2. Calls the Claude API with the methodology guidance + the patches,
         requesting the structured `concerns` return (each concern carries
         concern_id, stage_id, candidate_or_check_id, severity,
         is_preexisting, patch_scope, locations).
      3. Validates the response against the methodology's review return spec.
      4. Returns the review completion record.

    TODO: stages 1–3. For now the dispatch + shape are wired; the AI call
    raises so the integration is explicitly missing rather than silently
    returning empty."""
    raise NotImplementedError("review: AI integration not yet wired")


def handle_train_task(cfg: Config, client: HoneCoreClient,
                      claim: dict) -> dict:
    """`train` task: a per-(patch, comment) deep-dive comparison of
    hone-node's earlier review against one maintainer reply.

    Claim payload carries:
      - methodology_version, methodology (compiled doc with operations.train.
        {guidance, return})
      - training_session_id, session_role (`pool` or `holdout`),
        stratum_label — every train belongs to a session
      - patchset, patchset_metadata
      - patch (the patch the comment replies to)
      - comment (the one selected maintainer comment)
      - ai_review.concerns (hone-node's prior review of this patchset)

    The handler:
      1. Calls the Claude API with the methodology guidance + the prior
         concerns + the comment, requesting the structured comparison
         (concerns_considered, comment_points, point_matches,
         candidate_outcomes, check_outcomes, summary).
      2. In `pool` role, may add new_candidate / revise_existing entries
         to proposals[]. In `holdout` role, proposals[] MUST be empty.
      3. Validates the response.
      4. Returns the train completion record (echoing
         training_session_id, session_role, stratum_label).

    TODO: AI integration. The dispatch + shape are wired."""
    raise NotImplementedError("train: AI integration not yet wired")


def handle_draft_task(cfg: Config, client: HoneCoreClient,
                      claim: dict) -> dict:
    """`draft` task: author methodology change proposals for the merge gate
    (../docs/ARCHITECTURE-MERGE-GATE.md).

    Claim payload carries:
      - methodology_version, methodology (operations.draft.{guidance, return})
      - eligibility_flags (the snapshot of currently-actionable flags)
      - candidate_pool_stats, check_pool_stats, review_evaluations_summary,
        rejected_proposal_log, recent_session_evidence
      - redraft_context (set on redraft tasks)

    The handler dispositions every flag (propose / decline / defer) and
    drafts a per-recommendation payload for each `propose`. Returns the
    draft completion record (eligibility_dispositions[], proposals[],
    cross_proposal_dependencies[], node_notes).

    TODO: AI integration. The dispatch + shape are wired."""
    raise NotImplementedError("draft: AI integration not yet wired")


# Public dispatch table — the runner picks the handler from claim["task_type"].
HANDLERS = {
    "prepare": handle_prepare_task,
    "review":  handle_review_task,
    "train":   handle_train_task,
    "draft":   handle_draft_task,
}


def dispatch(cfg: Config, client: HoneCoreClient, claim: dict) -> dict:
    """Route a claim to its handler by `task_type`. Raises ValueError on an
       unknown type so a protocol drift is surfaced at the source rather than
       producing a silent or malformed result."""
    task_type = claim.get("task_type")
    handler = HANDLERS.get(task_type)
    if handler is None:
        raise ValueError(f"unknown task_type: {task_type!r}")
    return handler(cfg, client, claim)
