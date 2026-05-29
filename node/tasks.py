"""hone-node task handlers — the four task types a node claims
(../docs/ARCHITECTURE.md → hone-node).

Each handler takes a claim payload (the body of POST /v1/claims/), does its
work, and returns the completion record the runner submits via
POST /v1/claims/{id}/result. The records are validated against
../common/schema/completion-record.schema.yaml.

Today: `prepare` is wired end-to-end through Claude; `review`, `train`, and
`draft` still raise NotImplementedError pending their AI integration.
"""
import email
import email.utils
import json
import logging

from node import ai, cgit, tier0
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

# Cap on the per-call assistant/tool trace (node.ai builds it from the
# streamed Claude turn) attached to a record's meta.trace. It's telemetry
# for the web UI, not authoritative data, so keep it bounded: a step
# ceiling plus a per-text truncation, preserving each assistant_text's
# original length as `chars` so the UI can still show "(N chars)".
_TRACE_MAX_STEPS = 50
_TRACE_TEXT_CAP = 2000


def _cap_trace(trace):
    """Bound a call's trace for storage in the completion record's meta —
       cap the step count, and truncate each assistant_text to
       _TRACE_TEXT_CAP (recording the pre-truncation length in `chars`).
       tool_use / tool_result steps are already small. Returns a list
       (empty when the backend produced no trace)."""
    capped = []
    for step in (trace or [])[:_TRACE_MAX_STEPS]:
        s = dict(step)
        if s.get("step") == "assistant_text":
            text = s.get("text") or ""
            s["chars"] = len(text)
            if len(text) > _TRACE_TEXT_CAP:
                s["text"] = text[:_TRACE_TEXT_CAP]
        capped.append(s)
    return capped


def _worker_id(cfg: Config) -> str:
    """The worker_id every completion record carries. The node-name from
       Config doubles as the worker label — set by the operator at deploy,
       defaults to socket.gethostname()."""
    return cfg.node_name


# --- prepare: Tier-0 deterministic phase -----------------------------------

def _patch_text(claim: dict) -> str:
    """The combined patchset text the deterministic resolver works on —
       cover letter + every patch body. Carries the base-commit trailer,
       all touched file paths (for get_maintainer), and the diff lines
       (for patch_size)."""
    parts = []
    if claim.get("cover_letter_body"):
        parts.append(claim["cover_letter_body"])
    for p in (claim.get("patches") or []):
        if p.get("body"):
            parts.append(p["body"])
    return "\n".join(parts)


def _recipients(claim: dict):
    """The To:/Cc: address set (lower-cased) parsed from the cover letter
       — else the first patch — for the maintainer coverage ratios. None
       when no headers are parseable, so coverage stays null rather than
       falsely reporting nobody was Cc'd."""
    raw = claim.get("cover_letter_body")
    if not raw:
        patches = claim.get("patches") or []
        raw = patches[0].get("body") if patches else None
    if not raw:
        return None
    try:
        msg = email.message_from_string(raw)
        pairs = email.utils.getaddresses(
            msg.get_all("To", []) + msg.get_all("Cc", []))
        addrs = {addr.lower() for _name, addr in pairs if addr}
    except Exception:                                   # malformed headers
        return None
    return addrs or None


def _run_deterministic(cfg: Config, claim: dict) -> dict:
    """Tier-0 code phase: resolve base + maintainers + patch_size with no
       LLM. Builds the cgit tree set from cfg and hands it to the
       resolver, which degrades to heuristic (never raises) on any cgit /
       get_maintainer failure."""
    trees = cgit.KernelTrees.from_registry(cfg.cgit_trees)
    ps = claim.get("patchset") or {}
    try:
        return tier0.resolve_deterministic(
            trees, _patch_text(claim),
            recipients=_recipients(claim),
            series_length=len(claim.get("patches") or []) or None,
            subject=ps.get("subject"), sent=ps.get("sent"))
    finally:
        trees.close()


def _merge_deterministic(body: dict, det: dict) -> dict:
    """Overlay the Tier-0 deterministic fields onto the LLM body — code
       wins for the fields it owns, the LLM keeps the judgment fields
       (patch_type, review_intensity, preparation_notes,
       self_review_record).

       Granularity:
         - tree_state: overlay the base_* fields (the LLM keeps
           applies_cleanly / kernel_version_at_base / etc., which are
           tree-only and computed at review).
         - patch_size: always code-counted (exact beats the LLM's
           estimate); churn_ratio stays null until review.
         - subsystem / maintainer: the authoritative (source "tree")
           code result replaces the LLM block; in heuristic mode the
           LLM's block is kept (it did the heuristic work).

       NOTE: the LLM is still *asked* for these fields today and we
       discard its authoritative-field answers here. Stripping them from
       the prompt is a later methodology change — it saves tokens but
       isn't correctness-critical, since code already wins."""
    merged = dict(body)
    ts = dict(merged.get("tree_state") or {})
    for f in ("base_in_tree", "base_resolution", "base_tree", "base_fallback",
              "base_commit_declared", "base_commit_source"):
        ts[f] = det[f]
    merged["tree_state"] = ts
    merged["patch_size"] = det["patch_size"]
    if det["subsystem"]["source"] == "tree":
        merged["subsystem"] = det["subsystem"]
    if det["maintainer"]["source"] == "tree":
        merged["maintainer"] = det["maintainer"]
    return merged


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

    The deterministic Tier-0 fields (base resolution, subsystem +
    maintainer sets via get_maintainer.pl, patch_size counts) are
    computed by code — no LLM, no kernel clone — and overlaid onto
    Claude's response, with code winning for the fields it owns. The
    LLM produces only the judgment fields (patch_type,
    review_intensity, preparation_notes, self_review_record). See
    docs/ARCHITECTURE-PREPARE.md → Tier 0 / Tier 1."""
    det = _run_deterministic(cfg, claim)
    system = _build_prepare_system(claim)
    user_text = _build_prepare_user_text(claim)
    # prepare is a tree-free text→JSON characterisation: Tier-0 (above) owns
    # every tree-dependent field, so the model needs no tools. tools=[] bars
    # the CLI from running Bash/git — stops it probing for a kernel tree
    # (which the legacy prompt still nudges it to do) regardless of prompt.
    try:
        response = ai.call_claude(cfg, system, user_text, tools=[])
    except ai.CallClaudeError as exc:
        # The Claude call ran but yielded no usable answer (CLI non-auth
        # exit / timeout / stream with no success result). Rather than let
        # it crash the claim loop, submit an uncharacterisable record that
        # carries the partial agent trace + the CLI's failure context — the
        # attempt lands in the corpus (and the Agent-messages UI) as
        # debuggable data instead of a restart loop. Auth failures take a
        # different, configuration-fatal path (ai.CallClaudeAuthError).
        log.warning("prepare: Claude call failed (%s) — submitting "
                    "uncharacterisable: %s", exc.category, exc)
        return {"task_type": "prepare",
                "worker_id": _worker_id(cfg),
                "model":     exc.model or cfg.anthropic_model or "",
                "usage":     {"input_tokens":  0, "output_tokens": 0,
                              "duration_ms":   exc.duration_ms},
                "outcome":   "uncharacterisable",
                "reason":    str(exc),
                "meta":      {"deterministic_resolver_version":
                                  det["resolver_version"],
                              "trace":        _cap_trace(exc.trace),
                              "claude_error": {
                                  "category":   exc.category,
                                  "returncode": exc.returncode,
                                  "stderr":     (exc.stderr or "")
                                                    .strip()[:_RAW_RESPONSE_CAP]}}}
    header = {"task_type": "prepare",
              "worker_id": _worker_id(cfg),
              "model":     response["model"],
              "usage":     response["usage"]}
    # resolver_meta rides on both the prepared and the uncharacterisable
    # record (the trace is just as useful — more so — when the JSON didn't
    # parse, since it shows what Claude actually did).
    resolver_meta = {"deterministic_resolver_version": det["resolver_version"],
                     "trace": _cap_trace(response.get("trace"))}
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
                "meta":    {**resolver_meta,
                            "raw_response":        raw[:_RAW_RESPONSE_CAP],
                            "raw_response_length": len(raw),
                            "raw_response_truncated":
                                len(raw) > _RAW_RESPONSE_CAP}}
    merged = _merge_deterministic(body, det)
    return {**header,
             "outcome": "prepared",
             **{f: merged.get(f) for f in _PREPARE_FIELDS},
             "self_review_record": merged.get("self_review_record"),
             "meta": resolver_meta}


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
