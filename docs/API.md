# hone — REST API (v1)

The contract between **hone-core** and the **hone-nodes**. Companion to
`ARCHITECTURE.md` — that file is the model, this is the wire contract.

Scope: the OAuth / enrollment endpoints and the node-facing work API. The
merge-gate and node-approval human web UIs are out of scope (see
`ARCHITECTURE.md`).

## Conventions

- **Base path** `/v1`. All request/response bodies are JSON
  (`Content-Type: application/json`).
- **TLS required** on every request.
- **Status codes** — `200` ok · `201` created · `204` no content (e.g. an
  empty work queue) · `400` malformed request, including the OAuth device-flow
  states (see *Node enrollment*) · `401` missing/invalid/expired/revoked bearer
  token, or a bad/missing fleet secret on the OAuth API · `404` not found ·
  `409` conflict (a lapsed or already-resolved claim) · `422` body fails schema
  validation · `5xx` server fault.
- **Error body** — `{"detail": "<text>"}` (FastAPI's default shape).
- **Retry discipline** (see `ARCHITECTURE-WORK-LIFECYCLE.md` → Node resilience): a node
  retries `5xx` / timeouts with exponential backoff. A `401` on the main API
  means the access token expired — the node refreshes it (see *Node
  enrollment*) and retries the call once. Other `4xx` are **not** retried — a
  lapsed claim or misconfiguration, which retrying cannot fix.
- **Idempotency** — `POST …/result` is idempotent on `claim_id` (below).
  `POST /v1/claims` is *not* idempotent, but a claim whose response is lost
  self-heals: the lease expires and the work is re-offered.
- **Severity tag enum** — every place this contract carries a finding's
  severity, the value is one of the five lowercase tags `critical` / `major`
  / `moderate` / `minor` / `nit`, matching the active methodology's
  `report_finalization.severity_scale.levels[].tag` enum.
- **Severity is per-finding.** Checks and candidate practices do **not**
  carry severity fields anywhere in this contract — severity is assigned
  per-finding at report-finalization time, and the same check or candidate
  can produce findings at any of the five severities.

## Authentication & transport

hone-core is its own OAuth 2.0 authorization server (see `ARCHITECTURE.md` →
Auth, enrollment & transport). The API has two channels, authenticated
differently:

| Channel | Endpoints | Credential |
| --- | --- | --- |
| **OAuth / enrollment** | `POST /v1/oauth/*` | `X-HONE-Fleet-Secret` — the fleet-wide shared secret |
| **Main API** | everything else under `/v1` | `Authorization: Bearer <access_token>` |

A node obtains its bearer token by **enrolling** — the device authorization
grant, next section. The access token is short-lived; on a `401` the node
refreshes it via `POST /v1/oauth/token` and retries. The fleet secret is used
**only** on the OAuth endpoints — it is the gate that lets a fleet member
*begin* enrollment, and it is never sent on the main API.

A missing/bad fleet secret on an OAuth endpoint → `401`. A missing, invalid,
expired, or revoked bearer token on the main API → `401`.

**TLS.** hone-core generates its own certificate authority and server
certificate on first startup and serves HTTPS directly. A node receives
hone-core's CA certificate in its enrollment token response and validates the
TLS of every main-API call against it; the OAuth channel, contacted before the
node holds the CA, is trusted on first use and authenticated by the fleet
secret. Every request is over TLS.

---

## Node enrollment — the device authorization grant

A node is added to the fleet by **enrolling itself**, gated by an operator's
approval — the OAuth 2.0 Device Authorization Grant (RFC 8628). Every
enrollment request carries `X-HONE-Fleet-Secret`; none carries a bearer
token (the node has none yet).

### POST /v1/oauth/device_authorization — begin enrollment

The node's first call to hone-core.

**Request** `{ "node_name": "<optional label>",
              "task_types": ["prepare", "review", "train", "draft"] }` —
the node's self-description, shown to the operator at approval. Both
fields optional; `task_types` defaults to
`["prepare", "review", "train", "draft"]` (every task type). A node
declaring support for `prepare` must have a kernel git repo and the
tree-fetch capability the prepare task requires (see `ARCHITECTURE.md`
→ *The patchset-metadata layer*); a node declaring support for `draft`
will receive whole-corpus pooled stats and an eligibility-flag snapshot.

**Response `200`**
```json
{
  "device_code": "<opaque secret>",
  "user_code": "WDJB-MJHT",
  "verification_uri": "https://<core>/enroll",
  "verification_uri_complete": "https://<core>/enroll?code=WDJB-MJHT",
  "expires_in": 900,
  "interval": 5
}
```
The node logs `user_code` + `verification_uri` for the operator, then polls
`POST /v1/oauth/token` every `interval` seconds. `device_code` is the node's
secret handle for that polling; `user_code` is the short code the operator
types. The pending enrollment expires `expires_in` seconds after issue.

### Operator approval

Out of scope for this wire API: the operator opens hone-core's web UI, enters
the `user_code`, reviews the node's self-description, and approves or denies
it (`ARCHITECTURE.md` → Node management). This human approval is the trust
anchor for adding a node to the fleet.

### POST /v1/oauth/token — obtain / refresh tokens

Two grants, both carrying `X-HONE-Fleet-Secret`.

**Device-code grant** — the node polls with
```json
{ "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
  "device_code": "<from device_authorization>" }
```

**Refresh grant** — renew an expired access token:
```json
{ "grant_type": "refresh_token", "refresh_token": "<opaque>" }
```

**Response `200`** — enrollment approved, or refresh accepted:
```json
{
  "access_token": "<opaque>",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "<opaque>",
  "ca_cert": "-----BEGIN CERTIFICATE-----\n…"
}
```
`ca_cert` — hone-core's self-generated CA certificate, PEM — lets the node
validate the TLS of every later main-API call. It is returned on the
device-code grant (first enrollment); the node persists it with the tokens.

**Response `400`** — the device-code grant is not yet complete; `error.code`
is one of (RFC 8628):

| `error.code` | Meaning — node action |
| --- | --- |
| `authorization_pending` | the operator has not approved yet — keep polling |
| `slow_down` | polling too fast — add 5 s to the interval |
| `access_denied` | the operator denied the enrollment — stop |
| `expired_token` | the `device_code` expired — restart from `device_authorization` |
| `invalid_grant` | unknown/spent `device_code` or `refresh_token` — stop |

A bad/missing fleet secret → `401`.

---

## The work API

Three node-facing endpoints — `POST /v1/claims`, `…/heartbeat`,
`…/result` — and nothing else. The claim payload is **self-contained**:
hone-core packages the patches (or per-task evidence), any prior `ai_review`
the task needs, and the methodology slice for this task type into the
response, so a node never has to fetch the corpus or the methodology
separately. There is no `GET /v1/patchsets/.../blob`, no
`GET /v1/methodology` — those were predecessors of this design.

### The compiled methodology slice

Every claim payload contains a `methodology` field carrying a slice of the
canonical methodology, compiled for the task type. The shape is consistent
across task types — `operations` varies, `core` does not:

```json
{
  "methodology": {
    "core": {
      "principles": [
        { "id": "...", "title": "...", "body": "..." }
      ],
      "stages": [
        { "id": "0", "title": "...", "applies": "...", "body": "..." }
      ],
      "checks": [
        { "id": "...", "stage": "2", "title": "...", "body": "..." }
      ],
      "candidates": [
        { "id": "c-0118", "stage": "2", "title": "...", "body": "..." }
      ],
      "documentation_review": {
        "title": "...", "body": "..."
      },
      "report_finalization": {
        "body": "...",
        "severity_scale": {
          "weighting": { "critical": 2.0, "major": 1.0,
                         "moderate": 0.5, "minor": 0.25, "nit": 0.1 },
          "uncertainty_rule": "default_to_lower",
          "cross_kind_defaults": {
            "series": "major", "cross_patch": "major", "patch": null
          },
          "levels": [
            { "tag": "critical",
              "meaning": "...",
              "blocks_merge": true,
              "criteria": [{"id": "memory-unsafety", "description": "..."}],
              "anchors": [] },
            "..."
          ]
        }
      }
    },
    "operations": {
      "<task_type>": { "guidance": "...", "return": "..." }
    }
  }
}
```

Notable conventions:

- `checks` and `candidates` are flat arrays at `core`'s top level — the two
  arrays a node consults to know what bug-class checks to apply (the
  permanent ones plus the experimental ones being trialed). Neither array's
  entries carry a `severity` field.
- `severity_scale` is nested under `report_finalization` — it is part of
  how findings are tagged in the delivered review. The five lowercase tags
  are fixed (`critical`, `major`, `moderate`, `minor`, `nit`); the
  rubric-wide policy (`weighting`, `uncertainty_rule`,
  `cross_kind_defaults`) and the per-level `criteria` / `anchors` evolve
  through merge-gate ratification or operator import. See
  `methodology.schema.yaml` for the formal shape.
- A `prepare` claim carries a narrower slice — `{ core: { principles
  }, operations: { prepare } }` — because prepare consults the
  cross-operation principles (e.g., `set-current-date` for
  relative-date reasoning, `absence-is-not-proof` for tree-query
  discipline) but does not apply stages, checks, candidates, or the
  report-finalization rubric. A node working any other task type
  receives the full `core` block and uses it as the substantive
  review reference, with `operations.<task_type>` as the per-task
  instruction set.

### POST /v1/claims — claim the next task

A node claims one unit of work. Atomic (`ARCHITECTURE-WORK-LIFECYCLE.md` → claim protocol):
two nodes never get the same task; a dead node's claim is re-offered once its
lease lapses.

**Request** `{ "worker_id": "<node id>",
               "task_types": ["prepare", "review", "train", "draft"] }` —
`task_types` is what this node can handle; default
`["prepare", "review", "train", "draft"]` (every task type). hone-core
matches the request against the queue and returns the oldest claimable
item of an accepted type.

**Response `200` — a prepare-task claim:**
```json
{
  "claim_id": "<opaque>",
  "task_type": "prepare",
  "lease_expires_at": 1779360000,
  "methodology_version": 7,
  "methodology": {
    "core": { "principles": [ ... ] },
    "operations": { "prepare": { "guidance": "...", "return": "..." } }
  },
  "patchset": {
    "root_message_id": "<msgid>",
    "subject": "[PATCH 0/3] …",
    "declared_base_commit": "<sha or git-describe string or null>",
    "submitter_email": "...",
    "n_patches": 3
  },
  "patches": [
    { "message_id": "<msgid>", "part_index": 1,
      "subject": "[PATCH 1/3] …", "body": "<the .patch text>" }
  ],
  "cover_letter_body": "<the 0/N cover letter text, if present>",
  "thread_messages": [
    { "message_id": "<msgid>", "author_email": "...",
      "in_reply_to": "<msgid>", "body": "..." }
  ]
}
```

A prepare task characterises the patchset for the corpus: it produces
structured metadata (subsystem, patch_size, maintainer, patch_type,
review_intensity, tree_state). The node owns tree access — it resolves
`declared_base_commit`, fetches missing trees, runs `git apply --check`,
and reports the resolution in its completion record (see
`ARCHITECTURE.md` → *The patchset-metadata layer*).

**Response `200` — a review-task claim:**
```json
{
  "claim_id": "<opaque>",
  "task_type": "review",
  "lease_expires_at": 1779360000,
  "methodology_version": 7,
  "methodology": {
    "core": { ... },
    "operations": { "review": { "guidance": "...", "return": "..." } }
  },
  "patchset": {
    "root_message_id": "<msgid>",
    "subject": "[PATCH 0/3] …",
    "base_commit": "<sha>",
    "submitter_email": "...",
    "n_patches": 3
  },
  "patchset_metadata": {
    "subsystem": ["..."], "patch_size": { "bucket": "...", ... },
    "maintainer": { ... }, "patch_type": { "primary": "...", ... },
    "review_intensity": { ... }, "tree_state": { ... }
  },
  "patches": [
    { "message_id": "<msgid>", "part_index": 1,
      "subject": "[PATCH 1/3] …", "body": "<the .patch text>" }
  ]
}
```

`patchset_metadata` is the structured output of the prepare task that
preceded this review; the review node consults it for stratification
context (which subsystem, what kind of patch, what tree state). A review
is gated on prepare having terminated — the work queue will not offer a
review claim until its `patchset_metadata` row exists.

**Response `200` — a train-task claim:**
```json
{
  "claim_id": "<opaque>",
  "task_type": "train",
  "lease_expires_at": 1779360000,
  "methodology_version": 7,
  "methodology": {
    "core": { ... },
    "operations": { "train": { "guidance": "...", "return": "..." } }
  },
  "training_session_id": "ses-018",
  "session_role": "pool",
  "stratum_label": "driver:net · moderate",
  "patchset": { ... },
  "patchset_metadata": { ... },
  "patch": {
    "message_id": "<msgid>", "part_index": 2,
    "subject": "[PATCH 2/3] …", "body": "<the .patch text>"
  },
  "comment": {
    "message_id":   "<msgid>",
    "author_name":  "Reviewer Name",
    "author_email": "...",
    "body":         "<the reviewer's reply text>"
  },
  "ai_review": {
    "concerns": [
      {
        "concern_id": "rev-c-007",
        "stage_id": "2",
        "candidate_or_check_id": "c-0118",
        "text": "...",
        "severity": "major",
        "is_preexisting": false,
        "patch_scope": {
          "kind": "patch",
          "patches": ["msg-id-of-patch-2"],
          "spans_lines_in_diff": [42, 43]
        }
      }
    ]
  }
}
```

A **train task** is per-patch-per-comment: it asks the node to compare
*one* reviewer comment (`comment`) on *one* patch (`patch`) against our
prior hone-node review (`ai_review.concerns`) and the methodology. Train
is gated on the patchset already carrying an `ai_review` of source
`hone-node` — see *The completion record* below for what a train
returns.

The three session fields (`training_session_id`, `session_role`,
`stratum_label`) are **always present** — every train belongs to a
session; there is no NULL-session train:

- `training_session_id` — the session this train belongs to. Echoed in
  the completion record so aggregation can attribute results.
- `session_role` — `pool` or `holdout`. A `pool` train generates new
  candidate proposals from any verified miss; hone-core advances the
  candidate's pooled counters on receipt. A `holdout` train still
  produces the full comparison but suppresses proposals (`proposals[]`
  must be empty) AND does not advance the candidate's pooled counters
  on receipt — the per-train record persists in `work_items.record`
  as the raw evidence the statistical gates' pool-vs-holdout
  computations query on demand.
- `stratum_label` — the stratum this train falls under (subsystem +
  patch_type + review_intensity bucket, however the session's
  stratification spec slices). Echoed in the completion record.

The `concerns[]` list filters automatically to the in-scope concerns
for the patch the comment targets (per `patch_scope`); the train node
reports back which concerns it actually considered (see *Train record*
below).

**Response `200` — a draft-task claim:**
```json
{
  "claim_id": "<opaque>",
  "task_type": "draft",
  "lease_expires_at": 1779360000,
  "methodology_version": 7,
  "methodology": {
    "core": { ... },
    "operations": { "draft": { "guidance": "...", "return": "..." } }
  },
  "eligibility_flags": [
    {
      "flag_id": "elig-2026-05-25-001",
      "kind": "graduation_eligible",
      "subject_kind": "candidate",
      "subject_id": "c-0118",
      "evidence_snapshot": {
        "bootstrap_ci_lower": 0.31,
        "icc": 0.72,
        "holdout_tost_p": 0.008,
        "fdr_q_corrected": 0.024,
        "supporting_sessions": ["ses-018", "ses-021"]
      },
      "set_at": 1779358200
    },
    {
      "flag_id": "elig-2026-05-25-002",
      "kind": "prune_ineffective_eligible",
      "subject_kind": "check",
      "subject_id": "chk-old-naming-001",
      "evidence_snapshot": {
        "cusum_h_crossed_at": 1779100000,
        "mixed_effects_beta": -0.18,
        "mixed_effects_p": 0.003,
        "counterfactual_p": 0.041,
        "bayesian_posterior_useful": 0.03
      },
      "set_at": 1779358200
    }
  ],
  "candidate_pool_stats": [
    {
      "id": "c-0118", "title": "...", "body": "...",
      "applied": 142, "fired": 87, "unique_catches": 41,
      "severity_witness_introduced": {
        "critical": 0, "major": 12, "moderate": 18, "minor": 9, "nit": 2
      },
      "severity_witness_preexisting": {
        "critical": 0, "major": 4, "moderate": 6, "minor": 3, "nit": 1
      },
      "origin": "<the miss that originated it>"
    }
  ],
  "check_pool_stats": [
    {
      "id": "chk-old-naming-001", "title": "...", "body": "...",
      "applied": 320, "fired": 18, "unique_catches": 3,
      "severity_witness_introduced": { ... },
      "severity_witness_preexisting": { ... }
    }
  ],
  "review_evaluations_summary": {
    "mean_coverage": 0.74, "mean_fp_rate": 0.13,
    "recent_trend": "stable | improving | declining",
    "preexisting_unmatched_mean": 1.2
  },
  "rejected_proposal_log": [
    { "kind": "graduate", "subject_id": "c-0118",
      "rejected_at": 1778800000, "note": "..." }
  ],
  "recent_session_evidence": [
    { "session_id": "ses-018", "completed_at": 1779000000,
      "per_candidate_stats": [ ... ] }
  ],
  "redraft_context": null
}
```

A **draft task** carries the eligibility-flag snapshot from
`ARCHITECTURE-MERGE-GATE.md` → *Draft-task trigger logic*: hone-core's deterministic
gates have flagged one or more subjects as warranting AI-authored
proposals, and the node's job is to draft the actual proposals. The
node may decline a flag (with rationale) or defer it; not every
eligibility flag becomes a proposal.

`eligibility_flags[]` is the array of currently-actionable flags
(post-suppression, post-watermark filtering). At most `draft_batch_max`
flags (default 10) appear in any single draft claim; overflow flags
wait for the next batch.

Each flag's `evidence_snapshot` shape varies by `kind`:

- `graduation_eligible` — `bootstrap_ci_lower`, `icc`,
  `holdout_tost_p`, `fdr_q_corrected`, `supporting_sessions[]`,
  `supporting_holdout_patchsets[]`.
- `prune_ineffective_eligible` — `cusum_h_crossed_at`,
  `mixed_effects_beta`, `mixed_effects_p`, `counterfactual_p`,
  `bayesian_posterior_useful`, `supporting_sessions[]`.
- `prune_redundant_eligible` — `unique_catches`, `total_catches`,
  `redundancy_ratio`, `co_firing_with[]` (other checks that fire on
  the same findings).
- `consolidate_eligible` — `subject_ids[]` (the pair), each subject's
  `catches`, `co_fire_count`, `cofire_ratio`.
- `revise_eligible` — `cluster_size`, `cluster_sessions_spanned`,
  `cluster_strata_spanned`, `cluster_mean_cosine`,
  `cluster_synthesized_text`, `ab_paired_wilcoxon_p`,
  `ab_cliffs_delta`.
- `severity_scale_revise_eligible` — `drift_groups[]` (per group:
  `check_or_candidate_id`, `stratum_label`, `severity`,
  `krippendorff_alpha`, `sample_size`).

The `evidence_snapshot` is the gate's verdict — the node cites it in
its proposal rationale but does not recompute it.

`candidate_pool_stats` and `check_pool_stats` are pooled stats for
**every** active candidate and check, not just the eligible ones — the
node needs the full picture to reason about consolidation
opportunities and redundancy.

`redraft_context` is non-null only when this draft claim is a redraft
of a prior proposal that was *Return for redraft*-dispositioned. When
present:
```json
{
  "redraft_of": "prop-2026-05-20-xyz",
  "parent_proposal": { "recommendation": "...", "subject_kind": "...",
                       "subject_ids": [...], "payload": { ... },
                       "rationale": { ... } },
  "feedback_note": "the operator's redraft feedback"
}
```

**Response `204`** — the queue is empty; the node waits its poll interval
(`ARCHITECTURE.md` → claim loop) and asks again.

`claim_id` is the opaque handle for `heartbeat` and `result`.
`methodology_version` is the version this claim was issued under. hone-core
stamped it on the `work_items` / `draft_tasks` row at claim time, so the
in-flight claim is pinned to that version even if the active methodology
rolls over before the node submits its result. The node does NOT echo it
back in the completion record — the row already has it.

### POST /v1/claims/{claim_id}/heartbeat — extend the lease

A node working a claim calls this periodically (the heartbeat interval,
default 5 min) so a long task is not reclaimed.

**Request** `{ "worker_id": "<node id>" }` ·
**Response `200`** `{ "lease_expires_at": 1779361800 }` ·
**`409`** the claim is no longer this worker's (the lease lapsed and it was
reclaimed) — the node should stop and discard the work.

### POST /v1/claims/{claim_id}/result — submit the completion record

The node submits its completion record. The **body of the request IS the
completion record** — a JSON object with a top-level `task_type` field that
selects one of four schema branches: **prepare** / **review** / **train** /
**draft** (full schema in `common/schema/completion-record.schema.yaml`).

**Response `200`** `{ "status": "ok" }`.

- **Idempotent on `claim_id`** — a re-submit after a lost response is a safe
  no-op that returns the same `200`.
- **`409`** — the claim lapsed (outage outlasted the lease, work reclaimed);
  the node discards the stale result, the reclaim already covered it.
- **`422`** — the record failed schema validation; the node has a bug. Not
  retried.

What hone-core does with a valid record, by `task_type`:

- **prepare** — writes a `patchset_metadata` row for the patchset, then
  calls `maybe_enqueue_review` to enqueue the review work-item (the
  prepare-completion-gates-review check passes once the row exists and
  every patch message has landed).
- **review** — writes an `ai_reviews` row (source = `hone-node`) for the
  patchset. A `reviewed` outcome carries `concerns[]`; `unappliable` /
  `deferred` carry only a `reason` and write no review. No train enqueue
  here — trains are session-driven, created only when the operator
  launches a session that includes this patchset.
- **train** — records the comparison and per-candidate / per-check
  outcomes; for `pool`-role trains, applies counter updates to the
  pooled stats; any `proposals[]` enqueue as `pending` in
  `methodology_proposals` (after layer-2 validation, see
  `ARCHITECTURE-MERGE-GATE.md` → guarding the methodology). When every
  train this session created for this patchset has terminated, the
  per-(patchset, session) review-level aggregation writes one
  `review_evaluations` row.
- **draft** — records each eligibility-flag disposition; queues each
  proposal that the node proposed (not declined or deferred) into
  `methodology_proposals` (also layer-2 validated); records each
  decline / defer for audit.

---

## The completion record

The body of `POST /v1/claims/{claim_id}/result` — one record per claim,
discriminated by a top-level `task_type` field. The full field shape,
types, enums, and required/optional rules live in
`common/schema/completion-record.schema.yaml` (JSON Schema, draft 2020-12) —
that schema is the source of truth and hone-core validates every record
against it (a failure returns `422`). This section is the prose companion:
it carries the discipline and invariants the schema cannot express.

All four branches share a header — `task_type`, `worker_id`, `outcome`
(branch-specific), `model`, and `usage` (`input_tokens`, `output_tokens`,
`duration_ms`). The `methodology_version` is NOT part of the record:
hone-core stamps it on the `work_items` / `draft_tasks` row at claim time
(the same version it put in the claim payload), so the node never echoes
it and never decides it. Every success-path outcome (prepare → "prepared",
review → "reviewed", train → "trained", draft → "drafted") additionally
carries `self_review_record` — see below.

### The self-review record (shared structure)

Every success-path completion record carries a top-level
`self_review_record` field capturing the outcome of the node's
adversarial self-review pass. The pass is the last stage of every
operation before emission (review's Stage 3, prepare's P4, train's
T5, draft's D6); see each operation's `guidance` block in
`core/default-methodology.yaml` for the per-operation adversarial
questions the node walks. The wire structure is uniform across
operations; the `target_kind` and `challenge_kind` free-form strings
follow operation-specific conventions.

```
self_review_record
  summary       text (required, non-empty) — brief prose describing
                what the adversarial pass surfaced. Required even
                when no challenges arose; says e.g. "reviewed 5
                concerns against the 5 adversarial questions; none
                required revision"
  challenges[]  array (may be empty when no challenges arose). Each:
    target_kind     string — what was challenged. Conventions per
                    operation: review uses "concern" / "location";
                    train uses "match_verdict" / "point_decomposition"
                    / "candidate_credit"; draft uses "proposal" /
                    "batch" / "alternative_consideration"; prepare
                    uses "claim" / "verification" / "field".
    target_id       string — a stable reference: concern_id, point_id,
                    proposal_id, or the field path in prepare (e.g.,
                    "subsystem.primary" — prepare metadata sits flat
                    on the record, no "metadata." prefix), etc.
    challenge_kind  string — the adversarial question category.
                    Per-operation conventions in the guidance prose;
                    examples: "evidence_check",
                    "severity_calibration", "scope_check",
                    "wording_check", "alternative_consideration",
                    "mode_honesty".
    challenge_text  string — the specific question that was asked.
    outcome         "upheld" | "revised" | "dropped"
    outcome_note    string (required) — what happened. For "upheld":
                    what verified it. For "revised": what changed and
                    why. For "dropped": why it was removed and the
                    consequent change in the substantive output (e.g.,
                    "concern rev-c-009 removed from concerns[]").
```

**Empty `challenges`, non-empty `summary` is required.** The summary
forces articulation even when nothing was challenged. An empty
summary plus empty challenges signals "I skipped the adversarial
pass," which the spot-check audit can detect and which the
completion-record schema rejects (`summary` carries `minLength: 1`).

**Why structured rather than prose**: the spot-check audit verifies
that nodes are actually doing the adversarial pass, not merely
claiming to. A structured record lets the audit sample (target_id,
outcome) pairs and verify them against the substantive output — the
concern was actually dropped, the severity was actually downgraded,
the proposal disposition actually changed.

### The prepare record (`task_type: "prepare"`)

Outcome is `"prepared"` or `"uncharacterisable"`. A prepared record
carries the five structured metadata fields (`subsystem`, `patch_size`,
`maintainer`, `patch_type`, `review_intensity`) plus `tree_state`,
`preparation_notes`, and `patchset_id` — flat at the top level of the
record, no `metadata` wrapper. An uncharacterisable record carries
only a `reason`. The full per-field shape lives in
`core/default-methodology.yaml` → `operations.prepare.guidance`
(the prompt's Output Contract) and is enforced by the schema.

The load-bearing discipline the schema cannot enforce:

`preparation_notes.mode` reports how the node operated overall:

- `authoritative` — the patchset's `base-commit:` trailer was honoured
  and the resolved sha was in a reachable tree; tree-derived fields
  were resolved against the actual tree.
- `heuristic` — no trailer or no tree access; the node inferred
  metadata from the patches and thread alone; downstream stratification
  weights these lower.
- `mixed` — partial: some fields authoritative, others heuristic.

The per-field `source` (`tree` | `thread` | one variant per field) is
declared independently — a record can be authoritative-mode overall
while `review_intensity.source` is `thread` (the reply-substance
classification has no tree-based procedure). The per-field source is
the audit trail for which fields rest on tree queries vs. inference.

The `self_review_record` from P4 is required when outcome = "prepared".
target_kind conventions for prepare: `"claim"` (a metadata field's
value), `"verification"` (a check the node should have performed),
`"field"` (a field path challenged for honesty).

### The review record (`task_type: "review"`)

Outcome is `"reviewed"`, `"unappliable"`, or `"deferred"`. A reviewed
record carries `concerns[]`; the other outcomes carry only a `reason`.

Each concern has a stable `concern_id` (so train responses can address
it individually), `stage_id`, `candidate_or_check_id`, `text`,
`severity`, `is_preexisting`, `patch_scope`, and `locations[]`. The
semantic rules the schema cannot enforce:

- `patch_scope.kind` is `"patch"` | `"series"` | `"cross_patch"`, with
  `patches[]` listing the Message-IDs the concern pertains to —
  per-comment trains filter the in-scope concerns by this scope.
- `is_preexisting` is true exactly when the finding originates from
  the `preexisting-issues` Stage 2 check (code in hunk context lines
  or in the modified function's wider body that the patch doesn't
  introduce but is reviewable anyway). Pre-existing findings have
  their `blocks_merge` overridden to false regardless of severity, and
  are excluded from review-level FP-rate denominators — see
  ARCHITECTURE-WORK-LIFECYCLE.md → review-level aggregation.
- Concerns do not carry a `severity_rationale` field in v1; the broader
  severity-as-versioned-methodology rollout (see ARCHITECTURE.md → To
  Build) will add it as a follow-up.

The `self_review_record` from Stage 3 plus the per-concern adversarial
pass is required when outcome = "reviewed". target_kind conventions
for review: `"concern"`, `"location"`, `"stage3"`.

### The train record (`task_type: "train"`)

A train task compares *one* reviewer comment on *one* patch against our
prior `ai_review` for the patchset. Outcome is `"trained"`,
`"unappliable"`, or `"deferred"`; the failure outcomes carry only a
`reason`. A trained record decomposes the comparison into structured
pieces so aggregation can compute coverage and FP rates without parsing
prose: `concerns_considered[]` (every in-scope prior-review concern the
train evaluated), `comment_points[]` (the maintainer's comment broken
into atomic points), `point_matches[]` (per-point matching against
in-scope concerns), `candidate_outcomes[]` and `check_outcomes[]`
(per-subject application/firing data), `summary` (derived rollups),
`proposals[]`, plus the three echoed session fields
(`training_session_id`, `session_role`, `stratum_label`) — all
non-null, since every train belongs to a session.

**Holdout role.** When `session_role` is `"holdout"`, `proposals[]`
MUST be empty — a holdout train still produces the full comparison but
suppresses new candidate proposals AND, on receipt, hone-core does not
advance the candidate's pooled counters or `severity_witness`
histograms. The per-train record still persists for statistical gates
to query.

**Cross-field invariants** (beyond JSON Schema validation):

- Every `concern_id` referenced in `point_matches[*].addressing_concerns`
  or `candidate_outcomes[*].fired_concerns` MUST appear in
  `concerns_considered`.
- Every `point_id` referenced in `point_matches` MUST exist in
  `comment_points`.
- `concerns_considered` is the **complete** set of prior-review concerns
  the train evaluated — every concern whose `patch_scope` placed it in
  scope. It is the denominator for review-level coverage and FP-rate
  computation.

The `self_review_record` from T5 is required when outcome = "trained".
target_kind conventions for train: `"match_verdict"` (a point-match
classification), `"point_decomposition"` (a comment-point boundary),
`"candidate_credit"` (a candidate outcome's caught_points),
`"proposal"` (a proposed new_candidate or revise_existing, pool role
only).

### The draft record (`task_type: "draft"`)

A draft task produces methodology change proposals for the merge gate.
Outcome is `"drafted"` or `"failed"`; failure carries only a `reason`.
A drafted record dispositions every `eligibility_flag` from the claim
payload via `eligibility_dispositions[]` (one entry per flag with
disposition `"propose"` / `"decline"` / `"defer"`, with the
corresponding rationale field for non-propose dispositions), and
carries `proposals[]` (one per "propose"),
`cross_proposal_dependencies[]` (`supersedes` / `requires` /
`conflicts_with` declarations between proposals in the same batch),
and `node_notes` (warnings, confidence, overflow flags deferred).

Each proposal carries a `recommendation` of `graduate`,
`prune-redundant`, `prune-ineffective`, `consolidate`, `revise`, or
`revise-severity-scale`; the schema's discriminated `oneOf` enforces
the correct `payload` shape per recommendation.

**Per-recommendation constraints the schema cannot fully express:**

- **`graduate`** — `graduated_text` is a bounded adaptation of the
  candidate's body (similarity-checked at layer-2). No
  `graduated_severity` field: checks do not carry severity (severity
  is per-finding).
- **`consolidate`** — at least two sources, all the same kind (no
  mixing candidates with checks).
- **`revise-severity-scale`** — each `delta.operations[].op` (one of
  `add_anchor` / `modify_criterion` / `reweight_level` /
  `modify_meaning` / `modify_uncertainty_rule` /
  `modify_cross_kind_default`) references an existing rubric element
  by level tag and (where relevant) criterion or anchor id. The
  resulting rubric must still pass `common/schema/methodology.schema.yaml`
  validation. `drift_evidence` echoes the eligibility flag's
  drift_groups so the merge-gate evidence panel shows what justified
  the proposal.

**A "drafted" record with zero proposals is valid** — every eligibility
flag may be dispositioned as decline or defer. The flags remain set in
core; the next draft cycle will offer them again (unless decline
rationale persuades the operator to suppress them manually).

**Layer-2 mechanical validation** runs on every proposal at result
submission (`ARCHITECTURE-MERGE-GATE.md` → guarding). Beyond the schema's
structural checks: `subject_ids` references existing candidates /
checks; for `graduate` the proposed `graduated_check_id` is not
already in use and `graduated_text` is similar enough to the
candidate's body; for `prune-redundant` the `redundant_with_id`
references an existing check; for `revise-severity-scale` operations
reference existing rubric elements and the resulting rubric
round-trips through schema validation.

A proposal that fails validation is rejected at submission (counted
against the node's reputation, ARCHITECTURE-MERGE-GATE.md → guarding layer 5).
The valid proposals queue in `methodology_proposals` as `pending` for
the merge-gate operator's disposition.

The `self_review_record` from D6 is required when outcome = "drafted".
target_kind conventions for draft: `"proposal"` (challenged for
acceptance / wording / alternatives), `"batch"` (the whole batch
challenged for overload), `"alternative_consideration"` (an alternative
weighed against a proposal), `"recommendation_authority"` (a payload
challenged for staying within bounded authoring authority).

---

## Open / not yet specified

- Whether the fleet secret on the OAuth API is upgraded from a presented
  header to per-request signing.
- Whether the refresh token rotates on use (it currently does; an
  already-rotated refresh token returns `null` from
  `rotate_refresh_token`).
- Pagination / listing endpoints, if any prove necessary for operators
  beyond what the web UI surfaces.
- `severity_rationale` on every finding (criterion-and-anchor ID
  references rather than free-form prose) — deferred to the broader
  severity rollout. v1 records concerns and points with a `severity`
  field only.
