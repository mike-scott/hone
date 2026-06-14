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
import os
import re
import subprocess

import jsonschema
import yaml

from node import ai, cgit, refrepo, tier0
from node.client import HoneCoreClient
from node.config import Config

log = logging.getLogger("hone.node.tasks")


# The task types whose AI integration is actually wired — the single source
# of truth for this node's capability (the matching entries in HANDLERS below
# do the work; the rest are NotImplementedError stubs). The runner injects it
# into the client (HoneCoreClient(cfg, task_types=...)), which declares it to
# hone-core at enrollment and on every claim, so the queue is filtered to work
# this node can do — without it the node would be handed review/train/draft
# work and crash. Add a type here as its handler lands.
SUPPORTED_TASK_TYPES = ("prepare", "review")


# The CLI tools the review agent is allowed — read-only code exploration of
# the prepared worktree, nothing else. Read (whole functions), Grep (the
# methodology's "search the whole driver for this field" — the core of the
# concurrency / object-lifetime checks), Glob (find files). Deliberately NO
# Bash and NO WebFetch/WebSearch: the review is blind (no external lookup of
# the patch's mailing-list discussion) and needs no shell — `git apply`/`am`
# is done by the handler before the model runs, and the diffs ride in the
# prompt. Enforced by the CLI's --allowedTools allowlist independent of the
# prompt. (Granting scoped git via Bash(git …) is a possible later
# enhancement; omitted now to keep the blind-review guarantee airtight.)
_REVIEW_TOOLS = ["Read", "Grep", "Glob"]

# Last-resort tip-at-submission tree when a base-less patchset carries no
# recorded base_fallback. linux-next merges the subsystem trees daily, so
# its tip at submission time most closely tracks whatever a submitter built
# against; it's also the registry's lead probe tree (node/cgit.py), so it's
# the one most likely already fetched.
_DEFAULT_FALLBACK_TREE = "linux-next"


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

# Backstop cap on the prepare user payload (the patchset JSON). _slim_patch_body
# already drops the unbounded part — the raw diff hunks — so a normal series is
# far under this; the cap only guards a pathological case (e.g. a huge series
# diffstat or commit message) from overflowing the model context, which the CLI
# rejects outright with "Prompt is too long" before any tokens are spent. ~150K
# tokens of headroom under a 200K-context backend; the return contract is
# appended after this cap so it always survives.
_PREPARE_PAYLOAD_CHAR_CAP = 600000


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


def _slim_patch_body(body):
    """Drop the raw diff hunks from a patch (or cover) body, keeping the
       email headers, the commit message and the diffstat. git format-patch
       puts the diffstat between the `---` line and the first `diff --git`,
       so cutting at the first `diff --git` preserves it while shedding the
       unbounded part.

       The hunks are why a large series overflows the model context — the
       CLI rejects it with "Prompt is too long" before any tokens are spent
       — and the prepare LLM doesn't need them: Tier-0 already mined the
       diffs (patch_size counts, maintainers, subsystem) deterministically,
       leaving the LLM only judgment fields it drives from the prose. A body
       with no diff (cover letter, message-only) passes through unchanged."""
    if not body:
        return body
    lines = body.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            kept = "".join(lines[:i]).rstrip()
            return f"{kept}\n[diff hunks omitted — diffstat above]\n"
    return body


def _build_prepare_user_text(claim: dict) -> str:
    """The user-message payload for a prepare claim. Hands Claude the
       patchset (root + patches + cover letter) as JSON plus the
       methodology's prepare return-contract — so the model has the
       exact output shape spelled out alongside the payload.

       Each patch body is slimmed to headers + commit message + diffstat
       (_slim_patch_body); the raw diff hunks are dropped because Tier-0
       already mined them and a full-diff series overflows the context. A
       backstop cap (_PREPARE_PAYLOAD_CHAR_CAP) truncates the assembled
       payload JSON for pathological cases — applied before the return
       contract is appended, so the contract always survives.

       `thread_messages` is deliberately NOT forwarded today. The
       methodology's review_intensity is therefore computed against
       an empty thread (bucket_overall=none, per_reply=[]); this keeps
       prepare prompts compact and avoids burning thousands of tokens
       on review history that the current node revision doesn't yet
       use authoritatively. The hone-core side still ships
       thread_messages in the claim payload — re-add it here when
       prepare's review-intensity classification is wired up against
       real thread data."""
    patches = []
    for p in (claim.get("patches") or []):
        slim = dict(p)
        slim["body"] = _slim_patch_body(p.get("body"))
        patches.append(slim)
    payload = {
        "patchset":         claim.get("patchset"),
        "patches":          patches,
        "cover_letter_body": _slim_patch_body(claim.get("cover_letter_body")),
    }
    payload_json = json.dumps(payload, indent=2)
    if len(payload_json) > _PREPARE_PAYLOAD_CHAR_CAP:
        payload_json = (payload_json[:_PREPARE_PAYLOAD_CHAR_CAP]
                        + "\n… [payload truncated to fit the model context] …")
    return_contract = (claim.get("methodology", {})
                       .get("operations", {})
                       .get("prepare", {})
                       .get("return", ""))
    return (
        "Below is the patchset to characterise, followed by the return "
        "contract you must satisfy. Produce only the JSON object the "
        "contract describes.\n\n"
        "=== PATCHSET (JSON) ===\n"
        f"{payload_json}\n\n"
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


# --- review: worktree staging + series apply -------------------------------

def _review_worktree_dir(cfg: Config, root: str) -> str:
    """A per-review scratch worktree path, derived from the root Message-ID.
       Sanitised (Message-IDs carry @, <>, etc.) and length-bounded so it's
       a safe directory name under the node's scratch volume."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", root or "anon")[:120]
    return os.path.join(cfg.scratch_dir, f"review-{safe}")


def _apply_series(wt: str, patches: list) -> tuple:
    """Apply the patch series into the worktree with `git am`, so the model
       reads the *post-apply* (series-tip) code. Returns (True, None) on
       success or (False, reason) when the series will not apply — the
       Stage-0 'applies cleanly' gate, surfaced as outcome=unappliable.

       Only diff-bearing patch messages are applied (the cover letter has
       no diff), ordered by part_index. The bodies are full RFC 5322
       emails (what lore stored), which is exactly what `git am` consumes;
       a throwaway committer identity is supplied via `-c` since `am`
       records commits on the detached worktree HEAD."""
    diffs = sorted((p for p in patches
                    if p.get("type") == "patch" and p.get("body")),
                   key=lambda p: (p.get("part_index") or 0))
    if not diffs:
        return (False, "review payload carried no patch messages to apply")
    # The bodies are raw RFC 5322 emails (no mbox "From " separators), so
    # joining them with a newline yields ONE message to `git am` — it
    # applies only the first patch and silently drops the rest. Frame each
    # body as an mbox entry with a "From " separator line so `git am`
    # splits the series into its N messages and applies all of them.
    mbox = "".join(f"From mboxrd@hone Thu Jan  1 00:00:00 1970\n"
                   f"{p['body'].rstrip(chr(10))}\n\n" for p in diffs)
    r = subprocess.run(
        ["git", "-c", "user.name=hone-node", "-c", "user.email=hone@invalid",
         "-C", wt, "am", "--keep-cr"],
        input=mbox, capture_output=True, text=True)
    if r.returncode != 0:
        # Leave no half-applied state behind for the (disposable) worktree.
        subprocess.run(["git", "-C", wt, "am", "--abort"],
                       capture_output=True, text=True)
        detail = (r.stderr or r.stdout or "").strip()
        return (False, f"git am failed: {detail[:500]}")
    return (True, None)


# --- completion-record validation + repair ----------------------------------
#
# The node validates every model-emitted record against the SAME schema
# hone-core enforces, BEFORE submitting. A long-context review (the
# contract sits megatokens behind the emission point) occasionally
# paraphrases the record shape — plausible field names, wrong contract.
# Submitting that costs the whole task: hone-core 422s and the runner's
# fallback buries an otherwise-sound review. Caught node-side, one cheap
# no-tools repair turn (the validator's errors + the binding schema +
# the model's own JSON) recovers it.

_RECORD_SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "common", "schema",
    "completion-record.schema.yaml")
with open(_RECORD_SCHEMA_PATH, encoding="utf-8") as _f:
    _RECORD_SCHEMA = yaml.safe_load(_f)

_BRANCH_VALIDATORS = {}

# Caps on what a validation failure attaches to prompts / meta: each
# jsonschema message can embed the whole offending instance.
_SCHEMA_ERR_MSG_CAP = 300
_SCHEMA_ERR_LIST_CAP = 30


def _record_schema_errors(record):
    """Validate `record` against its task_type's schema branch, NOT the
       root oneOf — the root's "not valid under any of the given
       schemas" hides the actual failures; the branch enumerates them.
       Returns jsonschema errors sorted by path (empty = valid)."""
    tt = record.get("task_type")
    v = _BRANCH_VALIDATORS.get(tt)
    if v is None:
        branch = {"$ref": f"#/$defs/{tt}_record",
                  "$defs": _RECORD_SCHEMA.get("$defs", {})}
        v = jsonschema.Draft202012Validator(branch)
        _BRANCH_VALIDATORS[tt] = v
    return sorted(v.iter_errors(record),
                  key=lambda e: [str(p) for p in e.absolute_path])


def _format_schema_errors(errors):
    """The capped, human/model-readable error list — prompt and meta
       payload for a repair turn / deferred record."""
    out = []
    for e in errors[:_SCHEMA_ERR_LIST_CAP]:
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        out.append(f"at {loc}: {e.message[:_SCHEMA_ERR_MSG_CAP]}")
    if len(errors) > _SCHEMA_ERR_LIST_CAP:
        out.append(f"... and {len(errors) - _SCHEMA_ERR_LIST_CAP} more")
    return out


def _resolve_schema_refs(node, defs, seen=()):
    """Inline `{"$ref": "#/$defs/X"}` nodes so a schema branch is
       self-contained for prompt injection (mirrors core/api.py)."""
    if isinstance(node, dict):
        ref = node.get("$ref", "")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.split("/")[-1]
            if name in seen or name not in defs:
                return node
            return _resolve_schema_refs(defs[name], defs, seen + (name,))
        return {k: _resolve_schema_refs(v, defs, seen)
                for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_schema_refs(v, defs, seen) for v in node]
    return node


_RESOLVED_BRANCHES = {}


def _resolved_branch(task_type):
    """The task type's record schema with $refs inlined — walkable
       without a resolver. Cached (the schema is static)."""
    b = _RESOLVED_BRANCHES.get(task_type)
    if b is None:
        defs = _RECORD_SCHEMA.get("$defs", {})
        b = _resolve_schema_refs(defs.get(f"{task_type}_record", {}), defs)
        _RESOLVED_BRANCHES[task_type] = b
    return b


def _allows_null(prop):
    """Whether a (ref-resolved) property schema admits JSON null."""
    if not isinstance(prop, dict):
        return True                      # unknown shape — keep the key
    t = prop.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t):
        return True
    if None in (prop.get("enum") or []) or prop.get("const", "") is None:
        return True
    return any(_allows_null(s)
               for k in ("oneOf", "anyOf", "allOf")
               for s in prop.get(k) or [])


def _strip_null_optionals(value, schema):
    """Delete None-valued OPTIONAL keys whose schema has no null branch
       — in place, recursively, guided by the (ref-resolved) schema. A
       model writes `"field": null` to mean "absent" (the 2026-06-13
       rejection: `contributing_check_ids: null` against an optional
       array); jsonschema reads it as a type violation and hone-core
       422s. Omitting the key states the same thing validly, without
       spending a repair turn. Keys required at this level — including
       by any oneOf/anyOf branch, since outcome discrimination lives
       there — are left for the validator: null there is a real
       contract breach the repair turn must see."""
    if not isinstance(schema, dict):
        return
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        for branch in ((schema.get("oneOf") or [])
                       + (schema.get("anyOf") or [])):
            if isinstance(branch, dict):
                required.update(branch.get("required") or [])
        for k in list(value):
            prop = props.get(k)
            if prop is None:
                continue
            if (value[k] is None and k not in required
                    and not _allows_null(prop)):
                del value[k]
            else:
                _strip_null_optionals(value[k], prop)
    elif isinstance(value, list):
        for v in value:
            _strip_null_optionals(v, schema.get("items"))


def _norm_msgid(m):
    """Mirrors core_db.norm_msgid — strip wrapping <>, whitespace, case —
       so node-side citation checks match how hone-core's UI anchors
       concerns to patches."""
    m = (m or "").strip()
    if m.startswith("<") and m.endswith(">"):
        m = m[1:-1]
    return m.strip().lower()


def _claim_patch_ids(claim):
    """The normalised Message-Ids of THIS claim's patches — the only
       values a concern's patch_scope.patches may cite."""
    return {_norm_msgid(p.get("message_id"))
            for p in (claim.get("patches") or [])
            if p.get("type") == "patch" and p.get("message_id")}


def _citation_errors(record, valid_ids):
    """Patch-citation errors the schema cannot police: every
       patch_scope.patches entry must be a Message-Id of a patch in this
       claim. The 2026-06-12 rejection: a reviewer that never saw the
       series' Message-Ids invented `<patch-...>` placeholders — schema-
       valid, but hone-core anchors concerns to patches by these ids, so
       every per-patch finding would render in the series-wide bucket —
       and emitted [] for series scope, which hone-core 422s."""
    out = []
    for i, c in enumerate(record.get("concerns") or []):
        cited = (c.get("patch_scope") or {}).get("patches") or []
        for j, mid in enumerate(cited):
            if _norm_msgid(mid) not in valid_ids:
                out.append(f"at concerns/{i}/patch_scope/patches/{j}: "
                           f"{str(mid)[:80]!r} is not the Message-Id of "
                           "any patch in this series")
    return out


def _series_patch_listing(claim):
    """One line per patch — part index, Message-Id, subject — the
       authoritative citation table for a repair turn."""
    patches = sorted((p for p in (claim.get("patches") or [])
                      if p.get("type") == "patch"),
                     key=lambda p: (p.get("part_index") or 0))
    return "\n".join(f"part {p.get('part_index')}: {p.get('message_id')}"
                     f" — {p.get('subject', '')}" for p in patches)


# The record keys a repair turn may rewrite — the model-emitted body.
# Errors anywhere else (header fields, meta) are node-built and a node
# bug; a model turn can't fix those.
_REVIEW_REPAIRABLE_KEYS = ("concerns", "self_review_record")

_REPAIR_SYSTEM = (
    "You repair the JSON body of a Linux-kernel patchset review so it "
    "conforms to its completion-record JSON Schema. You are given the "
    "schema, the current JSON, and the validator's error list. Fix "
    "EXACTLY what the errors require — renaming mis-named fields, "
    "supplying required fields, mapping off-enum values to the nearest "
    "enum value, replacing patch_scope.patches entries with the correct "
    "Message-Ids from the SERIES PATCHES table (match by the concern's "
    "files and the patch subjects; series scope cites the full set) — "
    "while preserving every finding and every piece of prose verbatim. "
    "Never drop, add, reorder or rewrite review content. Respond with "
    "ONE JSON object and nothing else: "
    '{"concerns": [...], "self_review_record": {...}}')


def _attempt_record_repair(cfg, record, formatted_errors, claim):
    """One no-tools repair turn for an off-contract review record:
       hand the model its own concerns[] + self_review_record, the
       validator's errors, the series' patch Message-Ids (the only
       valid patch_scope.patches values), and the binding schema; get
       back a corrected body. Returns (body_or_None, usage) — usage
       accrues to the record either way (the turn was spent)."""
    branch = _resolved_branch("review")
    user_text = (
        "=== VALIDATOR ERRORS ===\n"
        + "\n".join(formatted_errors)
        + "\n\n=== SERIES PATCHES (the ONLY valid "
          "patch_scope.patches values) ===\n"
        + _series_patch_listing(claim)
        + "\n\n=== CURRENT JSON (to repair) ===\n"
        + json.dumps({k: record.get(k) for k in _REVIEW_REPAIRABLE_KEYS},
                     indent=2)
        + "\n\n=== JSON SCHEMA (the binding contract; the keys above "
          "live at the record's top level) ===\n"
        + json.dumps(branch, indent=2))
    try:
        response = ai.call_claude(cfg, _REPAIR_SYSTEM, user_text, tools=[])
    except ai.CallClaudeError as exc:
        log.warning("record repair: Claude call failed (%s): %s",
                    exc.category, exc)
        return None, None
    usage = response.get("usage")
    try:
        body = ai.parse_json_response(response["text"])
    except ValueError as exc:
        log.warning("record repair: response not valid JSON: %s", exc)
        return None, usage
    return body, usage


def _sum_usage(a, b):
    """Combined cost accounting for a record whose emission took more
       than one turn (the review + its repair)."""
    if not b:
        return a
    return {k: (a.get(k) or 0) + (b.get(k) or 0)
            for k in ("input_tokens", "output_tokens", "duration_ms")}


# --- review: prompt assembly -----------------------------------------------

def _render_core_reference(core: dict) -> str:
    """Render the compiled `core` methodology block (stages, checks,
       candidates, documentation_review, report_finalization + the severity
       scale) into the review system prompt. The checks already carry the
       appended concern-return contract (core/api.py:_compile_methodology
       footers them for the review slice)."""
    out = []
    for s in (core.get("stages") or []):
        out.append(f"\n\n### Stage {s.get('id')}: {s.get('title', '')}")
        if s.get("applies"):
            out.append(f"\n_Applies: {s['applies']}_")
        out.append(f"\n{s.get('body', '')}")
    checks = core.get("checks") or []
    if checks:
        out.append("\n\n## Stage-2 checks (unordered set)")
        for c in checks:
            out.append(f"\n\n### {c.get('title', c.get('id', ''))} "
                       f"(id: {c.get('id')})\n{c.get('body', '')}")
    cands = core.get("candidates") or []
    if cands:
        out.append("\n\n## Candidate practices (experimental — apply like "
                   "checks)")
        for c in cands:
            out.append(f"\n\n### {c.get('title', c.get('id', ''))} "
                       f"(id: {c.get('id')})\n{c.get('body', '')}")
    doc = core.get("documentation_review")
    if doc:
        out.append(f"\n\n## {doc.get('title', 'Documentation review')}\n"
                   f"{doc.get('body', '')}")
    rf = core.get("report_finalization")
    if rf:
        out.append(f"\n\n## Report finalization\n{rf.get('body', '')}")
        for lv in ((rf.get("severity_scale") or {}).get("levels") or []):
            out.append(f"\n- **{lv.get('tag')}** "
                       f"(blocks_merge={lv.get('blocks_merge')}): "
                       f"{(lv.get('meaning') or '').strip()}")
    return "".join(out)


def _build_review_system(claim: dict) -> str:
    """The review system prompt: the governing principles (hard
       requirements), the review operation guidance (how to drive the
       methodology), then the substantive methodology itself (stages,
       checks, rubric) from the compiled `core` slice."""
    methodology = claim.get("methodology", {}) or {}
    core = methodology.get("core") or {}
    guidance = ((methodology.get("operations") or {})
                .get("review") or {}).get("guidance", "")
    blocks = []
    principles = core.get("principles") or []
    if principles:
        blocks.append("=== GOVERNING PRINCIPLES (hard requirements) ===")
        for p in principles:
            blocks.append(f"\n## {p.get('title', p.get('id', ''))}\n"
                          f"{p.get('body', '')}")
    blocks.append("\n\n=== REVIEW OPERATION GUIDANCE ===\n")
    blocks.append(guidance)
    blocks.append("\n\n=== METHODOLOGY ===")
    blocks.append(_render_core_reference(core))
    return "".join(blocks)


def _build_review_user_text(claim: dict) -> str:
    """The review user message: the patchset + its prepare metadata as
       context, the patch diffs (what the series changes), and the review
       return contract. The series is already applied in the model's cwd
       worktree, so the prompt tells it the files it reads there are the
       post-apply tip and the diffs below are the change."""
    patchset = claim.get("patchset") or {}
    meta_block = claim.get("patchset_metadata") or {}
    diffs = sorted((p for p in (claim.get("patches") or [])
                    if p.get("type") == "patch" and p.get("body")),
                   key=lambda p: (p.get("part_index") or 0))
    return_contract = ((claim.get("methodology") or {})
                       .get("operations") or {}).get("review", {}).get(
                           "return", "")
    parts = [
        "You are reviewing a Linux kernel patchset. The full series is "
        "already applied in your working directory (your cwd), on top of "
        "its base commit — so the files you Read/Grep there are the "
        "post-apply (series-tip) code. Build the call graph and read whole "
        "functions from that tree. The diffs below are exactly what the "
        f"series changed; the base commit is {patchset.get('base_commit')}."
        " Each diff header below carries the patch's Message-Id — cite "
        "patches in `patch_scope.patches` by those Message-Ids exactly."
        "\n\n=== PATCHSET (context) ===\n",
        json.dumps({"patchset": patchset,
                    "patchset_metadata": meta_block}, indent=2),
        "\n\n=== PATCH DIFFS (the change under review) ===\n"]
    for p in diffs:
        parts.append(f"\n--- patch {p.get('part_index')} "
                     f"(Message-Id: {p.get('message_id')}): "
                     f"{p.get('subject', '')} ---\n{p.get('body', '')}\n")
    parts.append("\n\n=== RETURN CONTRACT ===\n")
    parts.append(return_contract)
    return "".join(parts)


def _review_failure(cfg: Config, outcome: str, reason: str,
                    meta: dict = None) -> dict:
    """A reason-only review record for the non-success outcomes
       (`unappliable` / `deferred`) — no concerns, no self_review_record,
       per the review_record schema's oneOf."""
    rec = {"task_type": "review",
           "worker_id": _worker_id(cfg),
           # A failure before any Claude call (e.g. no base_commit) has no
           # model to report, but the schema requires a non-empty model
           # (minLength: 1) — fall back to the node's default model rather
           # than "" so the record validates instead of wedging the node on
           # its own failure report.
           "model":     getattr(cfg, "anthropic_model", "") or ai.DEFAULT_MODEL,
           "usage":     {"input_tokens": 0, "output_tokens": 0,
                         "duration_ms": 0},
           "outcome":   outcome,
           "reason":    reason}
    if meta:
        rec["meta"] = meta
    return rec


def handle_review_task(cfg: Config, client: HoneCoreClient,
                       claim: dict) -> dict:
    """`review` task: a blind, agentic AI patchset review.

    Claim payload carries:
      - methodology_version, methodology (compiled doc with core +
        operations.review.{guidance, return})
      - patchset (root_message_id, subject, base_commit, n_patches, …)
      - patchset_metadata (the prepare-task output: subsystem,
        patch_size, maintainer, patch_type, review_intensity, tree_state)
      - patches: [{message_id, part_index, body}, …]  — the patch messages
        only (raw lore emails), no review comments interleaved

    The handler:
      1. Stages a worktree at the base commit (refrepo.prepare, honouring
         the prepare phase's base_tree hint) and applies the series into it
         with `git am` — the Stage-0 apply gate. When the patchset declares
         no base, falls back to the prepare phase's recorded tip-at-
         submission hint (tree_state.base_fallback), resolving it to a
         concrete commit. Apply failure → outcome=unappliable; base tree
         unobtainable or no resolvable base → deferred.
      2. Calls Claude (CLI backend) with the methodology as the system
         prompt and the patch diffs as the user message, rooted (cwd) in
         the worktree with read-only tools (Read/Grep/Glob) so it reads the
         post-apply code to drive the stage/check methodology.
      3. Shapes the structured `concerns[]` + `self_review_record` into a
         `reviewed` record. Off-contract or unparseable output → deferred
         (re-arm) rather than emitting an invalid record.

    The worktree is always cleaned up (finally)."""
    # Agentic review needs the CLI backend's tool access (Read/Grep/Glob in
    # the worktree); the SDK path has no tools. A review-capable node must
    # run HONE_CLAUDE_BACKEND=cli — surface a misconfiguration loudly rather
    # than silently produce a tree-blind "review".
    if getattr(cfg, "claude_backend", None) != "cli":
        raise RuntimeError(
            "review requires HONE_CLAUDE_BACKEND=cli (agentic tree access); "
            f"node is configured for "
            f"{getattr(cfg, 'claude_backend', None)!r}")

    patchset = claim.get("patchset") or {}
    root = patchset.get("root_message_id")
    base = patchset.get("base_commit")
    tree_state = ((claim.get("patchset_metadata") or {})
                  .get("tree_state") or {})
    base_tree = tree_state.get("base_tree")
    if not base:
        # No declared base-commit: trailer. Resolve a tip-at-submission
        # base — the newest commit of a tree as of the series' submission
        # time. Prefer the prepare phase's recorded fallback (the tree its
        # subject prefix named); failing that, fall back to linux-next,
        # which merges the subsystem trees daily and so most closely tracks
        # what a submitter built against. Either way resolve_tip turns the
        # (tree, as_of) into a concrete SHA; if neither yields one there's
        # no tree to stage, so defer.
        fb = tree_state.get("base_fallback") or {}
        fb_tree, as_of = fb.get("tree"), fb.get("as_of")
        if not (fb_tree and as_of is not None):
            # No recorded fallback — try linux-next at submission time.
            fb_tree, as_of = _DEFAULT_FALLBACK_TREE, patchset.get("sent")
        base = (refrepo.resolve_tip(fb_tree, as_of)
                if fb_tree and as_of is not None else None)
        if not base:
            return _review_failure(
                cfg, "deferred",
                "no base_commit on the patchset and no resolvable "
                "tip-at-submission fallback — cannot stage a worktree to "
                "review against")
        # Stage against the fallback tree; resolve_tip already fetched it.
        base_tree = fb_tree
        log.info("review: no declared base — using %s tip-at-submission "
                 "%s for %s", fb_tree, base[:12], root)

    wt = _review_worktree_dir(cfg, root)
    try:
        refrepo.prepare(base, wt, base_tree=base_tree)
    except Exception as exc:                       # RuntimeError + git errors
        log.warning("review: base tree %s unobtainable — deferring: %s",
                    (base or "")[:12], exc)
        return _review_failure(
            cfg, "deferred",
            f"base tree {(base or '')[:12]} unobtainable: {exc}")

    try:
        applied, fail = _apply_series(wt, claim.get("patches") or [])
        if not applied:
            log.info("review: series does not apply — unappliable: %s", fail)
            return _review_failure(cfg, "unappliable", fail)

        system = _build_review_system(claim)
        user_text = _build_review_user_text(claim)
        try:
            response = ai.call_claude(cfg, system, user_text,
                                      tools=_REVIEW_TOOLS, cwd=wt)
        except ai.CallClaudeError as exc:
            # The call ran but produced no usable answer. Defer (re-arm) —
            # a transient (rate/connection) retry may succeed — carrying the
            # partial trace + failure context for the operator. Auth failures
            # take the configuration-fatal CallClaudeAuthError path instead.
            log.warning("review: Claude call failed (%s) — deferring: %s",
                        exc.category, exc)
            return _review_failure(
                cfg, "deferred",
                f"claude call failed ({exc.category}): {exc}",
                meta={"trace": _cap_trace(exc.trace),
                      "claude_error": {
                          "category":   exc.category,
                          "returncode": exc.returncode,
                          "stderr": (exc.stderr or "").strip()[
                              :_RAW_RESPONSE_CAP]}})

        header = {"task_type": "review",
                  "worker_id": _worker_id(cfg),
                  "model":     response["model"],
                  "usage":     response["usage"]}
        meta = {"trace": _cap_trace(response.get("trace"))}
        try:
            body = ai.parse_json_response(response["text"])
        except ValueError as exc:
            log.warning("review: malformed JSON — deferring: %s", exc)
            raw = response.get("text") or ""
            return {**header, "outcome": "deferred",
                    "reason": f"review response was not valid JSON: {exc}",
                    "meta": {**meta,
                             "raw_response":        raw[:_RAW_RESPONSE_CAP],
                             "raw_response_length": len(raw),
                             "raw_response_truncated":
                                 len(raw) > _RAW_RESPONSE_CAP}}
        # A `reviewed` record needs both concerns[] and self_review_record
        # (schema oneOf). If the model omitted either, the output is
        # off-contract — defer (re-arm) rather than emit an invalid record
        # that hone-core would 422 and the runner would mislabel.
        if "concerns" not in body or "self_review_record" not in body:
            log.warning("review: response missing concerns[] or "
                        "self_review_record — deferring")
            return {**header, "outcome": "deferred",
                    "reason": "review response missing concerns[] or "
                              "self_review_record",
                    "meta": {**meta,
                             "raw_response":
                                 (response.get("text") or "")[
                                     :_RAW_RESPONSE_CAP]}}
        record = {**header,
                  "outcome": "reviewed",
                  "concerns": body.get("concerns") or [],
                  "self_review_record": body.get("self_review_record"),
                  "meta": meta}
        # Deterministic normalisation first: null-valued optional fields
        # mean "absent" — drop the keys instead of spending a repair
        # turn on the type errors.
        _strip_null_optionals(record, _resolved_branch("review"))
        # Validate against the schema hone-core will enforce, BEFORE
        # submitting, plus the citation contract the schema can't express
        # (patch_scope.patches ⊆ this claim's patch Message-Ids).
        # Submitting off-contract loses the whole review (422 → the
        # runner's terminal fallback); a single no-tools repair turn
        # usually recovers it for a few thousand tokens.
        valid_ids = _claim_patch_ids(claim)
        errors = _record_schema_errors(record)
        formatted = (_format_schema_errors(errors)
                     + _citation_errors(record, valid_ids))
        if not formatted:
            return record
        log.warning("review: record fails contract (%d error(s)) — "
                    "first: %s", len(formatted), formatted[0])
        if any(e.absolute_path
               and e.absolute_path[0] not in _REVIEW_REPAIRABLE_KEYS
               for e in errors):
            # An error outside the model-emitted body is a node bug a
            # repair turn can't fix — defer loudly with the evidence.
            return {**header, "outcome": "deferred",
                    "reason": "review record failed schema validation "
                              "outside the model-emitted body (node bug): "
                              + formatted[0],
                    "meta": {**meta, "schema_errors": formatted}}
        log.info("review: attempting record repair turn")
        fixed, repair_usage = _attempt_record_repair(
            cfg, record, formatted, claim)
        if fixed is not None:
            repaired = {**header,
                        "usage": _sum_usage(header["usage"], repair_usage),
                        "outcome": "reviewed",
                        "concerns": fixed.get("concerns") or [],
                        "self_review_record":
                            fixed.get("self_review_record"),
                        "meta": {**meta,
                                 "schema_repair": {"errors": formatted,
                                                   "attempts": 1}}}
            _strip_null_optionals(repaired, _resolved_branch("review"))
            if not (_record_schema_errors(repaired)
                    or _citation_errors(repaired, valid_ids)):
                log.info("review: record repaired — submitting")
                return repaired
            log.warning("review: repaired record STILL fails contract")
        return {**header,
                "usage": _sum_usage(header["usage"], repair_usage),
                "outcome": "deferred",
                "reason": "review record failed its completion-record "
                          "contract; repair turn did not converge: "
                          + formatted[0],
                "meta": {**meta, "schema_errors": formatted}}
    finally:
        refrepo.cleanup(wt)


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
