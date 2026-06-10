# hone — work lifecycle, review output & aggregation

How a claim becomes a completed task: node resilience, the work-item
state machine and claim protocol, the shape of a review's concerns,
the train task's payload and response, and the review-level
aggregation that turns per-comment trains into per-patchset coverage
and FP-rate metrics. The system model is in
[`ARCHITECTURE.md`](ARCHITECTURE.md); the wire contract in
[`API.md`](API.md).

## Node resilience — retry & backoff

A node depends on hone-core for every interaction. When the network or
hone-core is unreachable it must degrade gracefully, never crash, and
never lose or double-count work.

- **Backoff.** Transient failures — connection errors, timeouts, `5xx`, `429`
  — are retried with **exponential backoff + jitter**, *indefinitely* (a
  worker node has nothing to do but reconnect; this covers the node's initial
  connection at startup too). Initial and maximum backoff are configuration
  options, defaulting to **1 s** and **5 min**; `429` honours `Retry-After`.
- **Refresh, then fail fast.** A `401` on the main API means the access token
  has expired — the node refreshes it (`POST /v1/oauth/token`, the refresh
  grant) and retries the call once. A refresh that itself fails permanently, a
  `403` (the node's enrollment was revoked), or a bad fleet secret are *not*
  transient — the node surfaces the error and stops rather than spinning;
  retrying cannot fix a revoked node or a bad credential.
- **Idempotent submission.** `POST …/result` is idempotent, keyed on the claim
  id — a re-submit after a lost response is a safe no-op. Heartbeat and the
  `GET`s are naturally idempotent. A claim whose *response* is lost simply
  leaks until its lease expires and is re-offered — the lease is the safety
  net, so claim retries need no special handling.
- **Working through an outage.** A node mid-task when hone-core goes
  away finishes the task *locally* (it already holds the blob + base tree),
  **persists the claim and the completed result to its scratch storage**, and
  retry-submits until hone-core returns. A node restart resumes that
  submit from scratch storage.
- **Lapsed claims.** If an outage outlasts the lease, hone-core has
  reclaimed the patchset; on reconnect the node may be told its claim lapsed
  and then discards the stale result — the reclaim already covered the work.
  Lease + idempotent submit + scratch persistence ⇒ no work lost, none
  double-counted.

## Work lifecycle & the claim protocol

hone-core does not push work; a node **claims** it. One atomic SQL
`UPDATE … RETURNING` flips the oldest available `work_items` row to
`claimed` and stamps `claimed_by` / `claimed_at`. SQLite serializes
writers, so two nodes never claim the same item. A claim request carries
the `task_types` the node can handle (`prepare`, `review`, `train`,
`draft`); the queue returns the oldest claimable item of an accepted
type, picked by the ownership-aware claim order below.

**Work-item origin & the per-user queue.** Every `work_items` row
carries a `requested_by_user_id` origin: NULL for **system** work (the
gather auto-enqueue, the session orchestrator), a user id for work that
user requested from the UI (the Request-review button stamps the
clicking user; the config-token admin stamps NULL). A node, in turn, is
optionally **owned** by a user (`nodes.owner_user_id` — see
`ARCHITECTURE.md` → *Auth, enrollment & transport* → *Node ownership*).
The claim matches the two, in order:

1. A node with an owner serves its owner's items first (FIFO within
   the accepted types).
2. Otherwise — no owner, or the owner's queue is empty — a node with
   `handles_system` set falls back to the **system pool**: system-origin
   items plus **orphan rescue**, user items whose requester currently
   owns no active node. A rescued item has no dedicated server, so the
   pool absorbs it rather than letting it starve; the rule is evaluated
   at claim time, so pairing a node later takes the user's queue back,
   and revoking a user's last node releases their pending items to the
   pool.
3. Otherwise `204 No Content`. A freshly-paired node is **user-only**
   (`handles_system=0`) until its owner opts in from the node detail
   page; an ownerless (admin-approved) node gets `handles_system=1` and
   serves the pool — the classic interchangeable fleet member.

**Work-item lifecycle** (one shared state machine for `type ∈ {prepare,
review, train}`):

```
  (corpus: gathered)
        │
        ▼  auto-enqueue trigger fires (see below)
   claimable ──► claimed ──┬─► completed      (terminal — record produced)
        ▲           │      ├─► unappliable    (terminal — patch won't apply, unfixable)
        │           │      └─► deferred       (base tree unobtainable)
        └───────────┴────── lease expires / deferred ──► re-claimable
```

The state is a lifecycle *class*, not a task-type outcome. Every successful
outcome (`prepared` / `reviewed` / `trained`) lands on the **completed**
class; the per-task richness lives in the completion record's `outcome`
field, persisted on the row under `work_items.record`.

- **claimable** — enqueued; not yet claimed.
- **claimed** — a node holds it; `claimed_at` stamps the lease. The
  **lease time** is a configuration option, default **30 min** — it
  bounds how long a silent node's work is held before re-offer, *not* how
  long a task may take. The node **heartbeats** to extend the lease (the
  heartbeat interval is a separate config option, default **5 min** —
  comfortably shorter, so a healthy node never loses its claim; only a
  node unreachable for the whole lease window is treated as dead and
  re-offered).
- **completed** — terminal; the completion record is stored on the
  work-item row; for a review task a hone-node `ai_reviews` row is
  written; for a prepare task a `patchset_metadata` row is written; for
  a train task the per-candidate counters are updated and any proposals
  enter the `methodology_proposals` queue.
- **unappliable** — terminal; the node obtained the base tree but the
  patch will **not** apply (`git apply --check` fails) and the node could
  not reconcile it, so no record was produced. Recorded with the node's
  reason. Distinct from `deferred`: `deferred` is a missing *tree* (retry
  later); `unappliable` is an unworkable *patch* (re-claiming won't help).
  Also a source-quality signal — frequent `unappliable` patchsets from a
  source flag bad patch reconstruction. Note that `unappliable` does not
  apply to `prepare` tasks: a prepare task does not require the patch to
  apply, only to be characterised; an undeclared or unreachable base
  commit demotes the task to `heuristic` mode and it still terminates as
  `completed`.
- **deferred** — the node could not obtain the base *tree*; the work-item
  re-arms to `claimable` after the lease elapses. For `prepare` tasks
  this is rare — even with no base commit available the node falls back
  to heuristic mode and produces a row; `deferred` only fires if the
  node's tree is entirely uninitialised. For `review` tasks it is the
  normal degradation when the declared base isn't fetchable.

**Patchset-level skip.** A `PatchsetRef` from a gather module can carry a
`skip_reason` (e.g. lore's unresolved-date case) — or the list-tag filter
skips it because no enabled tag matched. Skipped patchsets are recorded
with `state=skipped` on the `patchsets` row; no work-items of any type
are enqueued; they are never picked or re-offered.

**Enqueue triggers**:

- **prepare — auto-enqueued.** In `core_db.maybe_enqueue_prepare`, fired by
  `gather._ingest_ref` after a non-skipped `patchset` lands → one `prepare`
  work-item (idempotent, one per root Message-ID).
- **review — operator-triggered, not auto-enqueued.** The operator requests
  a review from the patchset detail page (`POST /review-requests/<root>` →
  `core_db.maybe_enqueue_review`), which enqueues one `review` work-item per
  patchset (idempotent, one per root Message-ID), stamped with the
  requesting user as its origin so it routes onto their own nodes' queue
  (an admin-triggered review is a system item). The trigger and the
  delete-review button are **maintainer-gated**, with one exception: the
  uploader of an uploaded patchset may act on their own (see
  `core/ui.py` → `_can_act_on_patchset`). The work-item **re-arm**
  badges (release-deferred, retry-unappliable) and the **Cancel** button
  are stricter — **admin only**: they mutate fleet scheduling (the
  re-armed row keeps its original origin), so they're operator actions,
  not per-user ones. Cancel applies to UNHELD items only (claimable /
  deferred — no node holds a claim) and deletes the row outright; a
  cancelled claimable review re-arms the patchset's Request-review
  button. Auto-enqueueing review at
  gather time would flood the queue, so it is a deliberate per-patchset
  action. The request is only offered once a `patchset_metadata` row exists,
  so preparation still precedes the review — reviewers cited against the
  authoritative maintainer set, and bot-only / malformed patchsets filtered
  out before review compute is spent on them.

Comments are upserted into `messages` but **never** auto-enqueue work
items. Training is exclusively session-driven: the corpus accumulates
inertly, and the session orchestrator creates train work-items at
`draft → ready` materialisation from the selected patchsets' comments
(see [`ARCHITECTURE-TRAINING.md`](ARCHITECTURE-TRAINING.md) →
*Selection algorithm*).

**Session-linked trains.** Every `work_items` row of `type = train`
carries a non-null `training_session_id`, `session_role` (`pool` /
`holdout`), `stratum_label`, and `comment_message_id` (the specific
maintainer comment the train evaluates against the patch identified
by `message_id`). The node sees all four in the claim payload; the
node's behaviour depends on `session_role` as described in *The train
task payload* below.

**Crash recovery:** a dead node's claim goes stale; once the lease
elapses the claim protocol re-offers it (to whichever nodes the item's
origin makes eligible). No work is lost or stuck. This is what makes
the worker tier a pool of interchangeable nodes — within their
ownership scope — rather than one-shot tasks.

## Review output: concerns and scoping

A review operation produces an `ai_reviews` row containing a flat list of
concerns about the patchset as a whole. But a patchset is a series of
patches, and a concern's scope matters for everything downstream — a
per-comment train can only evaluate concerns that are actually about the
patch the comment targets, and review-level aggregation can only compute
coverage and false-positive rate if the per-concern scope is known.

Each concern in `ai_reviews` therefore carries an explicit **patch
scope**:

```json
{
  "concern_id": "rev-c-007",
  "stage_id": "...",
  "candidate_or_check_id": "c-0118",
  "text": "...",
  "severity": "major",
  "is_preexisting": false,
  "patch_scope": {
    "kind": "patch | series | cross_patch",
    "patches": ["msg-id-of-patch-3"],
    "spans_lines_in_diff": [42, 43]
  }
}
```

- `kind: patch` — the concern is about exactly one patch in the series.
  The `patches` list has one element: the root Message-ID of that
  patch. `spans_lines_in_diff` anchors per-line where the concern
  applies.
- `kind: series` — the concern is about the patchset as a whole
  (cover-letter-level, architectural). `patches` is empty;
  `spans_lines_in_diff` is null.
- `kind: cross_patch` — the concern spans multiple patches (a race
  between patches 5 and 9, an API change rippling through several
  patches). `patches` enumerates the relevant Message-IDs.

`is_preexisting` is **true** when the finding originates from the
`preexisting-issues` Stage 2 check — code in the hunk context lines or
in the modified function's wider body that the patch doesn't introduce
but is reviewable anyway. It is **false** for findings about
patch-introduced code, which is the default. Pre-existing findings
have two structural consequences downstream: their effective
`blocks_merge` is forced to `false` regardless of the rubric's
per-severity blocking signal (the rule the existing
`report_finalization` prose states), and they are excluded from
review-level FP-rate denominators (a maintainer not engaging with a
pre-existing finding is the expected outcome, per the methodology,
not a false positive). See *Review-level aggregation* below for the
FP-rate handling.

The review node assigns the scope and the pre-existing flag at the
moment the concern is authored — it already knows which patch(es) a
concern is about and which check fired it; the structured record just
makes those explicit. The review operation's methodology slice
(`operations.review`) requires both fields.

Scoping has three downstream consumers:

1. **Per-comment train filtering.** A train task evaluating against a
   comment on patch 3 considers only concerns whose scope includes
   patch 3: `kind: patch` with patch 3 in `patches`, plus all `series`
   and `cross_patch` concerns. This filtered set is the "concerns
   considered" for the train task and is reported back in the
   response — the train node's reasoning is anchored to a structured
   filter rather than a fuzzy textual judgement.
2. **Review-level aggregation.** Coverage, false-positive rate, and
   redundancy are computed across all per-comment trains for the
   patchset; the scope is what lets the aggregation tell whether a
   concern was un-matched because it was irrelevant to a comment or
   because it was a false positive. See *Review-level aggregation*
   below.
3. **Merge-gate evidence.** When a candidate is proposed for
   graduation, the evidence panel surfaces the parent reviews'
   coverage and FP rates so the human dispositioning the proposal
   sees the review-level context, not just the per-candidate counters.

## The train task — payload and response

Train is the highest-information task in the system: it is what produces
the per-candidate counters that drive every methodology change. The
shape of the payload and the shape of the response are both load-bearing
for the statistical machinery, and worth documenting in detail.

### Payload

A train claim payload contains:

- The compiled methodology slice (per `ARCHITECTURE.md` →
  *Methodology storage*) — `core` plus `operations.train`.
- The patchset's compiled patch messages (cover letter + patches).
- The single maintainer comment being trained against, with its
  parent patch identified (the comment threads under one specific
  patch in the series).
- The node's prior `ai_review` for this patchset — the full scoped
  concern list (see *Review output: concerns and scoping* above). The
  train node filters this to **concerns in scope for the patch the
  comment targets**: any `kind: patch` concern whose `patches`
  contains the patch under evaluation, plus every `kind: series` and
  `kind: cross_patch` concern. This filtered set is the "concerns
  considered" for the train task and is reported back in the
  response, so review-level aggregation can later distinguish
  irrelevant-to-this-comment from false-positive across the patchset.
- The patchset's `patchset_metadata` row (so the node knows the stratum
  and can ground its reasoning in the corpus metadata).
- **Session metadata** (always present — every train belongs to a
  session):
  - `training_session_id` — the session this train belongs to.
  - `session_role` — `pool` or `holdout`.
  - `stratum_label` — the stratum this patchset was classified into for
    the session.

The node's response **must echo all three session-metadata fields back**
so core can route the result without re-joining. `methodology_version`
is NOT echoed back: hone-core stamped it on the `work_items` row at
claim time, so the in-flight task is already pinned to its version —
the "an in-flight task finishes on the version it started" guarantee is
enforced by core, not by the node's report.

`session_role` is the only payload field that **changes node behaviour**:
- `pool`: the node generates new candidate proposals or revision
  proposals from any verified miss. hone-core then advances the
  candidate's pooled `applied / catches / unique_catches` counters and
  `severity_witness` histograms on receipt.
- `holdout`: the node does **not** generate proposals, and hone-core
  does **not** advance the pooled counters / histograms on receipt.
  The per-train completion record still persists in `work_items.record`
  — it's the raw evidence the statistical gates' pool-vs-holdout
  computations (graduation TOST, counterfactual ablation) query on
  demand. Holdout's purpose is unbiased measurement; treating it as
  a counter-updating channel would defeat that.

### Response

The train response is structured around the **comment-point** — the
smallest unit of "thing the maintainer said." A single maintainer reply
may contain multiple distinct points (a substantive objection, a code
suggestion, a style nit); the methodology can catch some and miss
others independently. Lumping a comment into a single caught/missed
verdict throws away the information that makes statistical aggregation
meaningful.

The full field shape, types, and enum values live in
`common/schema/completion-record.schema.yaml` (the `train_record` branch). At a
glance, the shape decomposes as:

```
concerns_considered[]   the in-scope prior-review concerns the train evaluated
comment_points[]        the maintainer's reply broken into atomic points
                         (kind, severity, spans_lines_in_diff)
point_matches[]         per-point match against in-scope concerns
                         (caught / partially_caught / missed / not_applicable,
                          addressing concerns + candidates + checks)
candidate_outcomes[]    per-active-candidate applied/fired/caught-points data
check_outcomes[]        same shape, keyed by check_id
summary                 derived rollups (totals, distribution, weighted rate)
proposals[]             new_candidate / revise_existing drafts (pool role only)
node_notes              warnings, confidence, confidence_reason
```

Plus the three echoed session fields (`training_session_id`,
`session_role`, `stratum_label`) and `self_review_record`.

Key invariants (beyond the schema's structural checks):

- `concerns_considered` is the **complete** set of prior-review
  concerns the train node evaluated against this comment's points —
  every concern whose `patch_scope` placed it in scope for the patch.
  Every `concern_id` appearing in `point_matches[*].addressing_concerns`
  or `candidate_outcomes[*].fired_concerns` MUST also appear in
  `concerns_considered`. The set is the denominator that review-level
  aggregation uses to distinguish irrelevant-to-this-comment from
  false-positive across the patchset.
- A `miss_rationale` is **required** when `match_status: missed`, drawn
  from a defined set: `no_relevant_stage`, `relevant_stage_didnt_fire`,
  `relevant_stage_fired_but_wrong_conclusion`,
  `relevant_stage_too_narrow_in_scope`, etc. These rationales steer
  proposal generation: `no_relevant_stage` motivates a `new_candidate`;
  `relevant_stage_too_narrow_in_scope` motivates a `revise_existing`.
- A point's `was_unique` flag (under `caught_points` for a candidate) is
  true if and only if *no other candidate or check* in the same train task also
  caught it. This is what makes the pooled `unique_catches` counter
  accurately computable — without per-point attribution, uniqueness is
  estimated.
- `not_applicable` exists for points the methodology could not reasonably
  catch (a maintainer's open question, a non-technical comment). Treating
  these as `missed` pollutes catch-rate denominators; marking them
  `not_applicable` lets aggregation exclude them.
- The `summary` block is **derived** from the detail blocks. Core
  validates that it is internally consistent before accepting the result
  — a cheap check that catches node bugs early.
- For `session_role: holdout`, `proposals` MUST be empty. Core enforces
  this at schema-validation time; violations get rejected and counted
  against node reputation.

### Schema validation at core

Every train response passes a deterministic, judgement-free schema
check before counters are touched — JSON Schema validation against
`common/schema/completion-record.schema.yaml` plus the cross-reference checks
listed in that schema's header (point_id / concern_id resolution,
holdout proposals[] empty, summary derived from detail blocks, prior
`ai_review` scope match).

A failure rejects the result, the work-item is re-claimed, and the
rejection counts toward the node's reputation under the existing
fleet-management model. At thousands of train tasks per session, even
a 1% malformed rate compounds into significant statistical
contamination if not caught.

## Review-level aggregation

A per-comment train measures one comment against the scoped subset of
the prior review. It cannot, by itself, say whether a concern in the
review is a **false positive** (un-matched against any maintainer
point in the session's selected slice) or whether the review's overall
**coverage** of those points was high or low. Those are review-level
questions, and the per-comment data has to be aggregated across the
session's full set of trains for the patchset to answer them.

hone-core runs a deterministic **review-level aggregation** workflow
per `(patchset, session)` — once every train belonging to that pair
has terminated, the aggregator consumes the per-comment results and
writes a `review_evaluations` row scoped to the session.

### Trigger

The aggregation fires for a `(patchset, session)` pair when
`count(claimable | claimed | unappliable | deferred trains for that
pair) = 0` AND `count(completed trains for that pair) > 0`. In other
words: every train the session created for the patchset has reached a
terminal state, and at least one of them produced a result.

`review_evaluations` is keyed on `(patchset_id, ai_review_id,
session_id)` — a patchset re-used across sessions produces a
`review_evaluations` row per session, giving the merge-gate evidence
panel a session-grained view of how the same review fared under each
evidence pass. The aggregation includes both pool-role and
holdout-role train inputs (the eval measures the *review's* quality,
not the methodology's training trajectory — pool/holdout splits don't
matter at this layer).

### What it computes

Per concern in the prior review:

- **Verdict**: `matched_any | unmatched_in_scope | unmatched_preexisting | out_of_scope`.
  - `matched_any` — at least one per-comment train reported this
    concern in `point_matches[*].addressing_concerns` with
    `match_status: caught | partially_caught`. A pre-existing concern
    that *was* engaged with by a maintainer comment lands here just
    like any other catch — the pre-existing flag does not suppress
    catches, only unmatched-FP attribution.
  - `unmatched_in_scope` — the concern appeared in
    `concerns_considered` of one or more trains with
    `is_preexisting: false`, but was never listed as addressing any
    comment point that those trains counted. **This is a false
    positive at the review level.**
  - `unmatched_preexisting` — the concern appeared in
    `concerns_considered` with `is_preexisting: true` but was never
    engaged with by any maintainer comment. **This is not a false
    positive** — non-engagement is the expected outcome for
    pre-existing findings, per the methodology's
    `preexisting-issues` check. Counted separately for visibility
    but excluded from `fp_rate`.
  - `out_of_scope` — the concern never appeared in any train's
    `concerns_considered`. Either no comments landed on the patches
    it scopes to, or the scope is a single patch with no comments.
    Not a false positive — just unconsumed evidence.

Per-comment-point:

- **Caught / partially-caught / missed / not-applicable** carries over
  from the per-comment trains; aggregation just sums them.

Review-level rollups:

- `coverage_rate` = (caught + partially_caught) ÷ (total points −
  not_applicable), across all per-comment trains for the patchset.
- `severity_weighted_coverage_rate` — same, weighted by point
  severity.
- `fp_rate` = `unmatched_in_scope` concerns ÷ (`matched_any` +
  `unmatched_in_scope`) concerns. **`unmatched_preexisting` concerns
  are excluded from both numerator and denominator** — they are
  expected non-engagements, not false positives, and including them
  would inflate the denominator with concerns that were never
  expected to be matched.
- `preexisting_unmatched_count` — the count of `unmatched_preexisting`
  concerns, reported separately for visibility. A review with many
  pre-existing findings that no maintainer engaged with is not a
  high-FP review; it is a review that did its job per the
  `preexisting-issues` check (flagged the issues for the maintainer's
  consideration; non-engagement was the expected outcome).
- `redundancy_pairs` — pairs of concerns both reported as
  `addressing_concerns` for the same `(comment, point)` across the
  same train, or for any point that matches the same underlying
  maintainer issue across different trains. A heuristic, deliberately
  loose; the deterministic version flags only intra-train co-occurrence
  on the same point.
- `had_missed_critical` / `had_missed_major` — promoted from
  per-train summary fields.

Per-candidate and per-check outcomes are already aggregated into the
pooled counters incrementally; review-level aggregation does not
re-derive them, but it does write a small per-review breakdown for
each candidate that fired (so the merge gate can show "this
candidate fired in 4 reviews; in 1 of them the parent review had a
high FP rate" as context).

### Table shape

```
review_evaluations
  patchset_id · ai_review_id · session_id    (PK)
  evaluated_at · trains_consumed
  coverage_rate · severity_weighted_coverage_rate
  fp_rate · preexisting_unmatched_count · redundancy_pairs
  had_missed_critical · had_missed_major
  per_concern_verdicts        JSON — [{concern_id, verdict,
                                       is_preexisting, ...}]
  per_candidate_review_stats  JSON — [{candidate_id, fired, was_unique,
                                       fired_on_preexisting,
                                       parent_review_fp_rate, ...}]
  notes                        JSON — anomalies surfaced during aggregation
```

One row per `(patchset, session)`; the `evaluated_at` and
`trains_consumed` columns track when the aggregation last ran and how
many train results it consumed. Per-concern verdicts and per-candidate
stats are JSON because they're read whole (for the merge-gate evidence
panel) and never queried relationally.

### Consumers

- **The merge gate evidence panel.** When a candidate is proposed for
  graduation, the panel shows, per parent review that fired this
  candidate, the parent review's coverage rate and FP rate. A
  candidate whose evidence comes mostly from reviews with high FP
  rates is a weaker signal than one whose evidence comes from clean
  reviews, and the panel makes that visible.
- **The statistical model.** False-positive rate enters the prune
  utility formula (the `λ` term — see
  [`ARCHITECTURE-TRAINING.md`](ARCHITECTURE-TRAINING.md) →
  *Three transitions*); having a measured per-review FP rate replaces
  the previous estimated rate.
- **The spot-check audit.** Per-review evaluations are a third sample
  target (alongside prepared metadata and per-comment trains).
- **Diagnostics.** Coverage trends across the corpus signal whether
  review quality is drifting. A persistent decline triggers a
  health-metric alert.

### Idempotency

The aggregation is deterministic — same input trains, same row.
Re-running over the same `(patchset, session)` produces the same
result and atomically overwrites the row. Re-runs are cheap (a single
scan of that session's completed train records for the patchset).
