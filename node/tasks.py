"""hone-node task handlers — the four task types a node claims
(../docs/ARCHITECTURE.md → hone-node).

Each handler takes a claim payload (the body of POST /v1/claims/), does its
work, and returns the completion record the runner submits via
POST /v1/claims/{id}/result. The records are validated against
../common/schema/completion-record.schema.yaml.

Today: `prepare` is wired end-to-end through Claude; `review`, `train`, and
`draft` still raise NotImplementedError pending their AI integration.
"""
import json
import logging

from node import ai
from node.client import HoneCoreClient
from node.config import Config

log = logging.getLogger("hone.node.tasks")


# The structured-metadata fields the prepare schema requires on a
# `prepared` record (per common/schema/completion-record.schema.yaml).
# The handler lifts these from Claude's response into the top-level
# completion record alongside `self_review_record`.
_PREPARE_FIELDS = ("patchset_id", "tree_state", "subsystem", "patch_size",
                   "maintainer", "patch_type", "review_intensity",
                   "preparation_notes")

# Max chars of Claude's raw response we attach to an uncharacterisable
# record's `meta.raw_response`. Big enough to cover a typical fenced
# prepare reply (~6 KB) plus headroom; small enough that a runaway
# response can't bloat work_items.record beyond reason. The original
# length is also recorded so the truncation point is obvious.
_RAW_RESPONSE_CAP = 20000


def _worker_id(cfg: Config) -> str:
    """The worker_id every completion record carries. The node-name from
       Config doubles as the worker label — set by the operator at deploy,
       defaults to socket.gethostname()."""
    return cfg.node_name


def _build_prepare_user_text(claim: dict) -> str:
    """The user-message payload for a prepare claim. Hands Claude the
       patchset (root + patches + cover letter) as JSON plus the
       methodology's prepare return-contract — so the model has the
       exact output shape spelled out alongside the payload.

       `thread_messages` is deliberately NOT forwarded today. The
       methodology's review_intensity is therefore computed against
       an empty thread (bucket_overall=none, per_reply=[]); this keeps
       prepare prompts compact and avoids burning thousands of tokens
       on review history that the current node revision doesn't yet
       use authoritatively. The hone-core side still ships
       thread_messages in the claim payload — re-add it here when
       prepare's review-intensity classification is wired up against
       real thread data."""
    payload = {
        "patchset":         claim.get("patchset"),
        "patches":          claim.get("patches"),
        "cover_letter_body": claim.get("cover_letter_body"),
    }
    return_contract = (claim.get("methodology", {})
                       .get("operations", {})
                       .get("prepare", {})
                       .get("return", ""))
    return (
        "Below is the patchset to characterise, followed by the return "
        "contract you must satisfy. Produce only the JSON object the "
        "contract describes.\n\n"
        "=== PATCHSET (JSON) ===\n"
        f"{json.dumps(payload, indent=2)}\n\n"
        "=== RETURN CONTRACT ===\n"
        f"{return_contract}")


def _build_prepare_system(claim: dict) -> str:
    """The system prompt for a prepare claim: the cross-operation
       principles followed by the prepare operation's guidance. Both
       come from the compiled methodology slice the claim payload
       carries (the `core` block is narrowed to `principles` for
       prepare — see core/api.py:_compile_methodology)."""
    methodology = claim.get("methodology", {}) or {}
    principles = (methodology.get("core") or {}).get("principles") or []
    guidance = ((methodology.get("operations") or {})
                 .get("prepare") or {}).get("guidance", "")
    blocks = []
    if principles:
        blocks.append("=== GOVERNING PRINCIPLES ===")
        for p in principles:
            blocks.append(f"\n## {p.get('title', p.get('id', ''))}\n"
                           f"{p.get('body', '')}")
    blocks.append("\n\n=== PREPARE OPERATION GUIDANCE ===\n")
    blocks.append(guidance)
    return "".join(blocks)


def handle_prepare_task(cfg: Config, client: HoneCoreClient,
                        claim: dict) -> dict:
    """`prepare` task: characterise one patchset for the corpus.

    Composes the system prompt (principles + the prepare operation
    guidance) and the user payload (the patchset JSON + the
    operation's return contract), calls Claude, and shapes the
    response into a prepare completion record. On a JSON parse
    failure returns an `uncharacterisable` record carrying the
    reason — surfacing the failure to hone-core's corpus rather than
    crashing the node.

    Tree access (resolving the declared base-commit against a local
    kernel tree, deciding authoritative vs. heuristic mode) is
    deferred: today the node operates in heuristic mode regardless,
    and Claude infers the per-field metadata from the patches +
    thread alone. See node/refrepo.py for the tree manager; wiring
    it up here is a follow-up."""
    system = _build_prepare_system(claim)
    user_text = _build_prepare_user_text(claim)
    response = ai.call_claude(cfg, system, user_text)
    header = {"task_type": "prepare",
              "worker_id": _worker_id(cfg),
              "model":     response["model"],
              "usage":     response["usage"]}
    try:
        body = ai.parse_json_response(response["text"])
    except ValueError as exc:
        log.warning("prepare: Claude returned malformed JSON — %s", exc)
        # Stash Claude's raw response on the record's `meta` field so a
        # future debugging pass can see WHAT the model produced rather
        # than just the parser's reason. Truncated to keep work_items.
        # record from ballooning; the original length is recorded
        # separately so the truncation is obvious.
        raw = response.get("text") or ""
        return {**header,
                "outcome": "uncharacterisable",
                "reason":  str(exc),
                "meta":    {"raw_response":        raw[:_RAW_RESPONSE_CAP],
                            "raw_response_length": len(raw),
                            "raw_response_truncated":
                                len(raw) > _RAW_RESPONSE_CAP}}
    return {**header,
             "outcome": "prepared",
             **{f: body.get(f) for f in _PREPARE_FIELDS},
             "self_review_record": body.get("self_review_record")}


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
