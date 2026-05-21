# hone — REST API (v1)

The contract between **hone-core** and the **AI nodes** (plus a small
admin surface). Companion to `ARCHITECTURE.md` — that file is the model, this
is the wire contract. Harness machinery — **not** part of
`~/PATCH-REVIEW-METHODOLOGY.md`.

Scope: the node-facing API and the admin endpoint. The merge-gate human web UI
is out of scope (see `ARCHITECTURE.md`).

## Conventions

- **Base path** `/v1`. All request/response bodies are JSON
  (`Content-Type: application/json`) except the patch blob (a binary
  download).
- **TLS required** on every request.
- **Status codes** — `200` ok · `201` created · `204` no content (e.g. an
  empty work queue) · `400` malformed request · `401` bad/missing fleet
  secret · `403` bad/unknown/revoked client key · `404` not found · `409`
  conflict (a lapsed or already-resolved claim) · `422` body fails schema
  validation · `5xx` server fault.
- **Error body** — `{"error": {"code": "<machine-code>", "message": "<text>"}}`.
- **Retry discipline** (see `ARCHITECTURE.md` → Node resilience): a node
  retries `5xx` / timeouts with exponential backoff; it does **not** retry
  `4xx` — those are misconfiguration or a lapsed claim, which retrying cannot
  fix.
- **Idempotency** — `POST …/result` is idempotent on `claim_id` (below).
  `POST /v1/claims` is *not* idempotent, but a claim whose response is lost
  self-heals: the lease expires and the work is re-offered.

## Authentication

Two credentials, both presented on **every** node request, over TLS:

| Header | Credential | Role |
| --- | --- | --- |
| `X-HONE-Fleet-Secret` | the hone-core shared-secret | coarse fleet/transport gate — every node has it |
| `X-HONE-Client-Key` | the client key | tenant identity — pre-registered, per-client |

A missing/bad fleet secret → `401`; a missing/unknown/revoked client key →
`403`. Admin endpoints instead use `X-HONE-Admin-Token` (an operator
credential, distinct from any client key).

---

## POST /v1/claims — claim the next task

A node claims one unit of work for its client. Atomic (`ARCHITECTURE.md` →
claim protocol): two nodes never get the same task; a dead node's claim is
re-offered once its lease lapses.

**Request** `{ "worker_id": "<node id>", "task_types": ["review"] }` —
`task_types` is what this node can handle (`review`, `maintenance`); default
`["review"]`.

**Response `200`** — a review-task claim:
```json
{
  "claim_id": "<opaque>",
  "task_type": "review",
  "lease_expires_at": 1779360000,
  "methodology_version": 1,
  "patchset": {
    "root_message_id": "<msgid>",
    "source": "linux-arm-msm",
    "subject": "[PATCH 0/3] …",
    "base_commit": "<sha>"
  },
  "blob_url": "/v1/patchsets/<root_message_id>/blob",
  "source_review_url": "/v1/patchsets/<root_message_id>/source-review"
}
```
A `maintenance` task carries a different claim payload — see **The
maintenance-task payloads** below.

**Response `204`** — the queue is empty; the node waits its poll interval
(`ARCHITECTURE.md` → claim loop) and asks again.

`claim_id` is the opaque handle for `heartbeat` and `result`.
`methodology_version` lets the node tell whether its cached methodology is
current; if not, it re-fetches `GET /v1/methodology`.

## POST /v1/claims/{claim_id}/heartbeat — extend the lease

A node reviewing a patchset calls this periodically (the heartbeat interval,
default 5 min) so a long review is not reclaimed.

**Request** `{ "worker_id": "<node id>" }` ·
**Response `200`** `{ "lease_expires_at": 1779361800 }` ·
**`409`** the claim is no longer this worker's (the lease lapsed and it was
reclaimed) — the node should stop and discard the work.

## POST /v1/claims/{claim_id}/result — submit the completion record

The node submits its completion record — a **review completion record** or a
**maintenance-task record**, per the claim's `task_type` (both spec'd below) —
the one terminal call for a claim.

**Request** — the review completion record (JSON). **Response `200`**
`{ "recorded": true, "claim_id": "<opaque>" }`.

- **Idempotent on `claim_id`** — a re-submit after a lost response is a safe
  no-op that returns the same `200`.
- **`409`** — the claim lapsed (outage outlasted the lease, work reclaimed);
  the node discards the stale result, the reclaim already covered it.
- **`422`** — the record failed validation; the node has a bug. Not retried.

On a valid record hone-core, all mechanically: records the outcome
(`reviewed` / `unappliable` / `deferred`); bumps each applied candidate's
Applied/Catches counters from `candidate_outcomes`; inserts each
`candidate_nominations` entry as a new candidate (status `candidate`, counters
zeroed); stores `findings` / `source_comparison` / `coverage` / `usage`; and
releases the claim.

## GET /v1/patchsets/{root_message_id}/blob — the patch archive

**Response `200`** — `application/zstd`, the patchset's `.tar.zst` archive
(`patch0.patch …`; see `SOURCES.md`). How a node obtains the patches. The base
*tree* is not served — the node checks `base_commit` out in its own reference
repo.

## GET /v1/patchsets/{root_message_id}/source-review — the gathered source's review

**Response `200`** — JSON: the external review the patchset was gathered with
(an AI bot's findings, or human reply threads). The node compares its own
blind findings against this to produce `source_comparison`. **Fetch it only
after forming your own findings** — the review must be blind (methodology
discipline; the API does not enforce ordering).

## GET /v1/methodology — the distilled methodology

**Response `200`** — the distilled methodology the node reviews against, as
JSON: `{ version, principles[], stages[], checks[], candidates[],
severity_scale, report_finalization }` (`ARCHITECTURE.md` → distill;
structure per `methodology.schema.yaml`, plus the `candidates[]` array). A
node applies `checks` + `candidates` alike; the split tells it which
applications to report as candidate outcomes. Optional `?version=N` pins a
specific version (for a node finishing an in-flight review).

## POST /v1/clients — pre-authorize a client (admin)

Auth: `X-HONE-Admin-Token`. **Request** `{ "name": "<client name>" }` ·
**Response `201`** `{ "client_key": "<generated>", "name": "<client name>" }`.
The returned key is what that client's nodes present as `X-HONE-Client-Key`.

---

## The review completion record

The body of `POST /v1/claims/{claim_id}/result`. One record per claim.

```
{
  worker_id            : the node submitting
  methodology_version  : the version this review applied (in-flight reviews
                         finish on the version they started)
  outcome              : "reviewed" | "unappliable" | "deferred"

  # outcome = unappliable | deferred  →  only:
  reason               : text (patch will not apply / base unobtainable)

  # outcome = reviewed  →  the full review:
  verdict              : "clean" | "issues" | "blocked"   (overall call)

  findings[]           : the blind review's findings — each:
      severity         : critical | major | moderate | minor | nit
      anchor           : { patch: "N/M", file: "...", line: <int> }
      text             : the inline review comment (per report_finalization)
      produced_by      : the stage / check / candidate id that surfaced it
      preexisting      : bool (a preexisting-issues / Tier-2 finding)

  coverage[]           : per stage and per check — { id, status: "applied"
                         | "n/a", note } — the completeness record: proof
                         the review was whole, not partial.

  candidate_outcomes[] : per active candidate the node applied — each:
      { candidate_id, applied: true, fired: bool, finding_ref }
                         → feeds the Applied / Catches counters.

  source_comparison[]  : per finding in the gathered source's review — each:
      { source_finding, verdict: "match" | "miss" | "source-FP",
        justification }
      we_win[]         : real issues we found that the source missed

  candidate_nominations[] : NEW candidate practices proposed from a verified
                         miss — each:
      { proposed_id: <slug>, title, body, origin: <the miss/finding that
        motivated it> }

  residual_risk        : known limitations of this review
  usage                : { tokens, tool_uses, duration_ms }
}
```

What each part exists for:

| Field | Purpose |
| --- | --- |
| `outcome` / `reason` | terminal disposition incl. the non-review outcomes |
| `verdict` + `findings[]` | the deliverable review — anchored, severity-tagged |
| `coverage[]` | proves completeness (the old per-patch `records` content) |
| `candidate_outcomes[]` | the self-honing signal — Applied / Catches counters |
| `source_comparison[]` | the measurement — our review vs the source's |
| `candidate_nominations[]` | grows the methodology — a node proposing a new candidate practice |
| `usage` | per-review cost accounting |
| `methodology_version` | which methodology version graded this review |

A `miss` in `source_comparison[]` is what motivates a `candidate_nomination` —
the record carries both, so hone-core sees the lineage. Candidate
*nomination* (a brand-new practice, here) is distinct from candidate
*graduation* (an existing candidate → a check, done by a maintenance task —
`ARCHITECTURE.md` → merge gate); only nomination belongs in a review record.

---

## The maintenance-task payloads

A **maintenance task** is the AI work behind the merge gate
(`ARCHITECTURE.md`). hone-core enqueues one — debounced, at most one
outstanding — when a review result pushes a candidate's pooled counters across
a `SCORING.md` threshold. A node claims it, evaluates the candidate set
against the methodology, and returns a batch of methodology-change
**proposals** for the human merge gate. Two kinds:

- **holistic** — evaluate the whole candidate set; the default, triggered by
  candidate movement.
- **redraft** — re-draft one prior proposal per a human's feedback (the
  merge-gate *Return for redraft* outcome).

### Claim — `POST /v1/claims` response, `task_type: "maintenance"`

```json
{
  "claim_id": "<opaque>",
  "task_type": "maintenance",
  "maintenance_kind": "holistic",
  "lease_expires_at": 1779360000,
  "base_methodology_version": 1,
  "candidates": [
    { "id": "<slug>", "title": "…", "body": "…", "stage": "2",
      "status": "candidate", "applied": 17, "catches": 2,
      "unique_catches": 1, "confidence": 2, "origin": "…" }
  ],
  "suppression_log": [
    { "recommendation": "graduate", "subject": "<slug>", "note": "…" }
  ],
  "methodology_url": "/v1/methodology?version=1"
}
```

- `candidates` carries the **full pooled stats** (more than
  `GET /v1/methodology` distils) — the node needs Applied / Catches /
  unique-catches to judge graduation eligibility and to write evidence.
- `suppression_log` — rejected `(recommendation, subject)` pairs the node must
  **not** re-propose (`ARCHITECTURE.md` → merge gate, Reject).
- `methodology_url` — the node `GET`s the full methodology to judge redundancy
  against existing checks and to author a graduated check in-register.

A **redraft** claim adds: `"maintenance_kind": "redraft"`, `redraft_of` (the
prior proposal's id), `prior_proposal` (`{recommendation, subject, payload,
rationale}` — the proposal as last drafted), and `feedback_note` (the human's
redraft feedback).

### Result — `POST /v1/claims/{claim_id}/result`, maintenance record

```
{
  worker_id            : the node submitting
  methodology_version  : the base_methodology_version from the claim
  outcome              : "completed" | "failed"
  reason               : text          # outcome = failed
  proposals[]          : 0+ for holistic (empty = "nothing should change",
                         a valid outcome); exactly 1 for a redraft
  usage                : { tokens, tool_uses, duration_ms }
}
```

Each `proposals[]` entry — the AI-authored part only:

```
recommendation : "graduate" | "prune-redundant" | "prune-ineffective"
               | "consolidate" | "revise"
subject        : the candidate id (a list of ids for "consolidate")
rationale      : the node's reasoning — shown in the merge-gate evidence panel
payload        : the concrete change, by recommendation —
   graduate          → the drafted check { id, stage, title, body }; `id` is
                       the candidate's slug, `body` a bounded adaptation of
                       the candidate, traceable to it
   prune-redundant   → { subsumed_by: "<check id>" }
   prune-ineffective → { }           (drop `subject`; its stats are the case)
   consolidate       → { merged: <a candidate { id, title, body, stage }> }
   revise            → { new_body: "<text>" }
redraft_of     : <proposal id>        # redraft results only — links the new
                                      # proposal to its parent
```

On receipt hone-core runs layer-2 mechanical validation
(`ARCHITECTURE.md` → guarding) on each proposal; queues each valid one in
`methodology_proposals` as `pending` — attaching the evidence snapshot,
`base_methodology_version`, and `created_at` / `parent_id` itself; and rejects
a malformed proposal (counting against the node's reputation). Idempotent on
`claim_id`, like any result.

---

## Open / not yet specified

- A formal JSON Schema for the completion record (mirroring
  `methodology.schema.yaml`'s role for the methodology).
- Whether the shared-secret is upgraded from a presented header to per-request
  signing.
- Pagination / listing endpoints, if any prove necessary for operators.
