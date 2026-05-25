# hone — the merge gate & methodology governance

The methodology-change ratification flow: how the statistical model's
verdicts become concrete proposals, how an operator dispositions them,
and the five layers that keep a bad node from corrupting the
methodology. The system model is in
[`ARCHITECTURE.md`](ARCHITECTURE.md); the statistical model that feeds
the eligibility gates is in
[`ARCHITECTURE-TRAINING.md`](ARCHITECTURE-TRAINING.md); the wire
contract is in [`API.md`](API.md).

## The merge gate

A methodology change is the single highest-blast-radius mutation in the
system — every review is graded against it. So draft nodes
**propose**; a **human dispositions**. Two orthogonal axes:

**The node's axis — recommendation** (what a draft task found).
Each proposal is tagged one of: `graduate` · `prune-redundant` ·
`prune-ineffective` · `consolidate` · `revise`.

**The human's axis — outcome** (what to do about it). The human
dispositions each queued proposal as one of:

1. **Accept** — apply it; hone-core commits the (optionally
   human-edited) payload and **bumps the methodology version**.
2. **Defer** — sound but thin; records a `defer_watermark`; not
   re-surfaced until the candidate's evidence materially grows past it.
3. **Reject** — permanent no, **scoped to the `(recommendation,
   subject)` pair**: the candidate itself stays active and applied;
   only that pairing is suppressed. The rejected row *is* the
   suppression-log entry.
4. **Return for redraft** — recommendation stands, wording doesn't;
   carries a `feedback_note`; hone-core re-tasks a draft node,
   and the new proposal lands linked via `parent_id`. Bounded — see
   the redraft cap.

**The queue — `methodology_proposals`** (hone-core table; also the
suppression log — rejected rows):

```
id · created_at · decided_at · decided_by
type           METHODOLOGY_PROPOSAL_TYPE_GRADUATE | PRUNE_REDUNDANT
             | PRUNE_INEFFECTIVE | CONSOLIDATE | REVISE
payload        JSON — the node's full proposal entry: { recommendation,
               subject (candidate id(s)), rationale, payload (the concrete
               change, by recommendation — see API.md → draft record),
               base_methodology_version, parent_id (redraft lineage),
               evidence_snapshot (pooled stats at proposal time),
               session_lineage (which sessions and which holdout patchsets
                                contributed evidence to this proposal) }
state          METHODOLOGY_PROPOSAL_STATE_PENDING | ACCEPTED | DEFERRED
             | REJECTED | RETURNED
redraft_count  the redraft-cap counter (incremented on RETURNED only when
               the redraft was quality-feedback driven, not staleness)
note           the operator's disposition note / feedback for redraft
```

The `session_lineage` field is new: every proposal records which
training sessions and which specific holdout patchsets supplied the
evidence behind it. The merge-gate UI surfaces this in the evidence
panel so the human can see exactly what data supports the recommended
change.

### Draft-task trigger logic

A draft task is enqueued when hone-core's deterministic eligibility
gates flag one or more subjects as warranting AI-authored proposals.
The decision *that* a draft is warranted is core's; the decision
*what* to propose is the node's. This separation keeps the gating
arithmetic outside the AI loop.

**The end-to-end pipeline.** Each new review or train result
triggers a six-step cycle:

1. **Counter update.** Core atomically applies the result's
   per-candidate and per-check outcomes to the pooled counters in a
   single transaction.
2. **Eligibility re-evaluation.** In the same transaction (or
   immediately after, depending on cost), core re-runs the deterministic
   eligibility gates affected by the updated counters. Most gates are
   per-subject and only need that subject's counters; `consolidate` is
   the exception — it's pair-wise, so an update to candidate A may flip
   the consolidate gate involving candidate B.
3. **Flag persistence.** Each eligibility transition from `false` to
   `true` writes a row into `eligibility_flags`:
   `(subject_kind, subject_id, kind, evidence_snapshot, set_at)`.
   Transitions from `true` to `false` (evidence regressed) clear the
   row. The table is the durable state that lets the draft pipeline
   survive process restarts.
4. **Suppression filtering.** Before a flag becomes actionable, core
   checks the `methodology_proposals` suppression log. If
   `(kind, subject_id)` matches a previously *Reject*-dispositioned
   proposal, the flag is marked `suppressed_at` and never enters a
   draft batch. This is how *Reject* takes permanent, scoped effect.
5. **Defer watermark check.** For *Defer*-dispositioned subjects, the
   flag is marked `defer_watermark_at` until the relevant counter has
   grown past the recorded watermark by `defer_growth_factor`
   (configurable; default 20%). Sound-but-thin evidence must mature
   before re-surfacing.
6. **Draft task enqueue check.** Periodically (and on each flag-set),
   core asks: are there unsuppressed and watermark-cleared eligibility
   flags AND no currently outstanding draft task? If yes, enqueue a
   draft task carrying a snapshot of currently-eligible flags (subject
   to the batching rules below).

The trigger is therefore **event-driven off result ingestion**, not a
cron poll — candidate application during reviews is the heartbeat
that drives it, but the cleanup itself is never inline in a review
(it needs the whole-corpus view and the merge gate).

**The six eligibility gates.** Each recommendation type has its own
deterministic gate. All six are pure arithmetic over the counters,
session-aggregated evidence, and statistical results (see
[`ARCHITECTURE-TRAINING.md`](ARCHITECTURE-TRAINING.md) →
*The statistical model behind transitions* for the bootstrap CI, ICC,
TOST, CUSUM, mixed-effects, and Bayesian-posterior machinery the
gates consume). No AI involvement at this layer.

- **`graduate_eligible(candidate_id)`** — from *Trigger 1*. All four
  must clear: stratified 10-fold CV catch rate with bootstrap 99% CI
  lower bound exceeding `acceptance_lower_bound` (default 0.25); ICC
  ≥ `icc_threshold` (default 0.6); holdout TOST equivalence at
  margin ±0.10; BH-FDR-corrected significance at q=0.05.
  Re-evaluated on counter updates AND on training-session
  `analyzed` transitions.
- **`prune_ineffective_eligible(check_id)`** — from *Trigger 2*.
  All four signals must agree: CUSUM h-bound crossed (utility drift
  below historical mean); mixed-effects β negative with p < 0.05
  (stratification-adjusted drift confirmed); counterfactual ablation
  on holdout shows equivalent performance without the check;
  Bayesian posterior P(useful) below `bayes_useful_threshold`
  (default 0.05). Re-evaluated on the same triggers as
  `graduate_eligible`.
- **`prune_redundant_eligible(check_id)`** — deterministic ratio
  test. Both conditions must hold: `unique_catches / total_catches`
  below `redundancy_threshold` (default 0.15 — over 85% of this
  check's catches are also caught by some other check), AND
  `total_catches` above `redundancy_min_sample` (default 30 —
  enough catches to make the ratio meaningful). Re-evaluated on
  each counter update. Core's gate is a necessary signal; the draft
  node's job is to confirm or decline by judging whether the textual
  overlap actually supports redundancy (two checks could co-fire
  85% of the time on incidental grounds — different bug classes
  surfacing together in similar patches — without being
  conceptually redundant).
- **`consolidate_eligible([id_a, id_b])`** — pair-wise gate. Three
  conditions: both subjects have at least `consolidate_min_sample`
  catches (default 30); co-firing ratio above
  `consolidate_cofire_threshold` (default 0.7); neither subject has
  a pending consolidate proposal involving anyone else (avoid
  conflicting proposals in flight). Re-evaluated on counter updates.
  Like prune-redundant, the AI confirms or declines the conceptual
  overlap.
- **`revise_eligible(subject_id)`** — from *Trigger 3*. Fires when
  accumulated `revise_existing` proposals from train tasks cluster
  strongly: ≥ 8 proposals targeting this subject; from ≥ 5 distinct
  training sessions; spanning ≥ 3 distinct strata; cluster mean
  cosine similarity ≥ 0.75; paired A/B test on a holdout slice with
  Cliff's δ > 0.2; holdout confirmation passes. Re-evaluated when a
  new `revise_existing` proposal lands and on session-`analyzed`
  transitions.
- **`severity_scale_revise_eligible`** — rubric-wide (no specific
  subject). Temporal-drift detector measures inter-window agreement
  on severity assignments for similar findings: group findings by
  `(check_or_candidate_id, stratum_label, severity)` and partition
  into time windows; for each group with sample size ≥
  `drift_min_sample` (default 20), compute Krippendorff's α across
  windows; flag drift when α below `drift_alpha_threshold`
  (default 0.6) on `drift_min_groups` (default 3) groups
  simultaneously. Re-evaluated on a fixed cadence (weekly default),
  not event-driven — severity is per-finding and needs accumulation
  before drift is measurable. The specific drift coefficient and
  thresholds are calibration tunables; the empirical values will
  settle once enough data has accumulated.

**Batching: snapshot, cap, debounce.** Three rules govern how
eligibility flags become draft tasks:

- **Snapshot at enqueue.** A draft task carries a snapshot of the
  eligibility flags at the moment of enqueue. Subsequent flag
  changes do not affect the in-flight task; they wait for the next
  cycle. This gives the node a stable view to reason about — the
  time between enqueue and node claim can be hours.
- **Size cap.** At most `draft_batch_max` (default 10) eligibility
  flags per draft task. Beyond that, the task becomes unwieldy for
  both the node (context-window pressure) and the merge-gate
  operator (too many proposals to disposition coherently). When more
  flags exist than the cap permits, core prioritises:
  1. `revise-severity-scale` (rubric-wide; affects everything else)
  2. `prune-ineffective` and `prune-redundant` (housekeeping;
     reduces noise in subsequent reasoning)
  3. `consolidate` (also housekeeping)
  4. `graduate` (most common; can afford to wait)
  5. `revise` (slowest-moving; usually accumulates more evidence
     anyway)
  Within a kind, oldest flag first (FIFO by `set_at`). Overflow
  flags stay set; they enter the next batch once the current task
  terminates.
- **Debounce.** At most one outstanding draft task at any time. When
  a draft task terminates (every proposal in it reaches a terminal
  disposition — *Accept* / *Defer* / *Reject* — or every redraft
  lineage in it resolves), the next batch becomes claimable.

The claim payload carries `{the eligible-flag snapshot, per-flag
evidence summaries from the eligibility gate that fired,
candidate-pool stats, check-pool stats, the active methodology, the
rejected-proposal suppression log, recent session evidence
summaries, redraft context if applicable}`. The node posts a batch
of proposals; each enters the `methodology_proposals` queue as
`pending`; a human dispositions each in hone-core's web UI; *Accept*
applies the change and writes a `methodology_versions` row.

**Disposition feedback into future eligibility.** The four
dispositions affect later cycles distinctly:

- **Accept** changes the subject's structural state (a candidate
  becomes a check, a check is pruned, a rubric is revised); the
  eligibility flag no longer fires because the gate's precondition
  no longer applies. The flag clears automatically on the next
  re-evaluation.
- **Defer** records a `defer_watermark` on the eligibility flag —
  the counter value past which the flag is allowed to re-surface.
  The flag clears immediately; the next time counter growth exceeds
  the watermark by `defer_growth_factor`, the flag re-fires.
- **Reject** writes the suppression log entry. Future re-evaluations
  still detect the eligibility but mark the flag `suppressed_at`;
  the flag never enters a draft batch. The suppression is scoped to
  the specific `(recommendation, subject)` pair — other
  recommendation types for the same subject remain unsuppressed.
- **Return for redraft** spawns a redraft-specific draft task
  outside the normal eligibility pipeline; the `parent_id` lineage
  tracks the chain. Counts toward the redraft cap only if quality-
  feedback-driven (see *Redraft cap* below).

**Edge cases worth being explicit about:**

- **Session-driven batch re-evaluation.** When a training session
  transitions to `analyzed`, all session-affected subjects have
  their eligibility re-evaluated as a batch. Sessions produce a lot
  of aggregate evidence simultaneously, and batching keeps the
  re-evaluation coherent. A session transition can flip multiple
  eligibility flags in one cycle.
- **Re-preparation cascade.** When a patchset is re-prepared (the
  re-preparation policy), its concerns may need re-evaluation, which
  affects `review_evaluations`, which affects counters, which feeds
  the normal pipeline. Re-preparation is not itself a draft-task
  trigger; it cascades through the normal counter-update path.
- **Cold start.** With zero data, no eligibility flags fire. The
  first draft task only enqueues once enough evidence has accumulated
  to clear at least one gate — possibly weeks on a slow-moving
  corpus. The system bootstraps usable without a draft pass.

hone-core *applies* an accepted change deterministically — the node
already drafted the change, so committing it is a text substitution + a
version bump, no AI. **Staleness:** if the methodology version moved
while a proposal sat in the queue (`base_methodology_ver` ≠ current),
hone-core flags it; the safe disposition is *redraft*.

**Redraft cap — the loop's stopping condition.** The redraft loop is
human-driven: it advances only while the human keeps choosing *Return
for redraft*, and Accept / Defer / Reject each terminate the lineage.
To bound a node and human that fail to converge, a **configurable cap**
(hone-core config, default **3**) limits the `parent_id` chain — once a
proposal is the 3rd *quality-feedback* redraft in its lineage, the
*Return for redraft* option is withheld and the human must Accept
(hand-editing the payload in the UI if needed), Defer, or Reject. This
guarantees every lineage terminates. Redrafts forced purely by
**staleness** (the methodology version moved underneath the proposal)
do **not** count toward the cap — that is the target moving, not the
node failing to converge.

The methodology is therefore hone-core-managed, **versioned** data.
Each claim's payload embeds the compiled methodology slice for the
active version; the work-item records that version, and the completion
record echoes it — an in-flight task finishes on the version it
started, even if the operator accepted a new version mid-task.

## Guarding the methodology from a bad node

The methodology is the highest-value mutable artifact, and a draft
node could be malicious or simply buggy. Five layers keep a bad node
from corrupting it — hone-core stays non-AI throughout, validating
*shape*, never *meaning*.

**1 — A node submits a typed proposal, never a methodology.** A
draft node cannot upload or replace a methodology file; it submits
a typed, scoped proposal (`graduate` / `prune-redundant` /
`prune-ineffective` / `consolidate` / `revise` / `revise-severity-scale`),
and the hone-core composes the new version itself from
`{current version + the validated delta}`. Whole-file truncation or an
empty-file replacement is therefore not in the threat model. Only
`graduate` carries new methodology prose;
`prune`/`consolidate`/`revise` touch only the structured candidate
table; `revise-severity-scale` touches only the `severity_scale` block
nested under `report_finalization` and is **gated by the strictest
evidence requirements** (a measured reduction in temporal drift of
severity assignments on similar patches across time windows — the
single-node analog of inter-node drift; see the statistical-gate
validation in layer 3). Graduated prose is a **bounded adaptation of
the candidate practice** — itself already evidence-backed — not free
authoring: it must stay traceable to that candidate. A graduated
candidate becomes a new **check** — an entry in the top-level `checks`
set, run under Stage 2 — candidate practices are bug-class checks by
nature, so that is the only shape a graduation takes. Checks are an
**unordered set**: each is an independent bug-class analysis applied
in any order, so a graduated check simply joins the set — there is no
position to choose. Anything with a genuine processing-order
dependency is a `stage` (the ordered 0/1/2/3/S macro-structure), never
a check. Through the merge gate a node can therefore *only add a check
or refine the severity rubric with evidence*; it can never add or
alter a principle, a stage, an existing check, or any other
scaffolding. Those change only via the operator's import path (export
→ edit → re-import).

**2 — Mechanical validation at submission.** Before a proposal enters
the `pending` queue hone-core runs deterministic, judgement-free
checks; any failure rejects it at submission, so it never reaches a
human:
- *non-empty* payload, valid UTF-8, no control characters, within length bounds;
- *composes cleanly* — applying the delta yields a methodology that
  still parses, with every existing stage intact;
- *scoped* — a `graduate` adds exactly one **check** (one entry to the
  top-level `checks` set) and removes exactly its candidate, altering
  no principle, stage, existing check, or scaffolding; a `prune`
  touches only its candidate;
- *proportionate* — reject a net shrink, or an addition wildly larger
  than the candidate it graduates;
- *traceable* — the graduated text must recognizably derive from the
  candidate it claims (containment / similarity).

**3 — Statistical-gate validation.** A graduation proposal is also
checked against the five gates from
[`ARCHITECTURE-TRAINING.md`](ARCHITECTURE-TRAINING.md) →
*Three transitions*; a proposal whose subject candidate has not
cleared bootstrap CI, ICC, holdout TOST, and FDR is rejected at
submission. Similarly for prune and revise proposals. This layer
makes the statistical model *enforced*, not advisory: a draft node
cannot propose a graduation for a candidate that does not satisfy
the criteria, even by mistake.

**4 — An informed human gate.** The merge gate already requires a
human *Accept*; the proposal is presented with an **evidence panel**
so the decision is informed, not a rubber stamp — the diff, the
magnitude metrics from layer 2, the candidate's pooled stats
(Applied / Confidence / unique-catches), the bootstrap CI and holdout
TOST results from layer 3, **the review-level coverage and FP rates
of the parent reviews that fired this candidate** (from
`review_evaluations`), **the introduced-vs-preexisting split of the
candidate's catches** (from the two parallel `severity_witness`
histograms — a candidate that catches mostly pre-existing findings
is a different signal from one that catches patch-introduced
defects, and the panel makes that visible), the session lineage
showing which sessions and holdout patchsets supplied the evidence,
the originating verified misses, and the submitting node's identity.

**5 — Node reputation.** Proposals that fail layer-2 or layer-3
validation, train results that fail schema validation, or proposals
that are *Rejected* at the gate are counted per enrolled node; a node
whose proposals are consistently invalid or rejected is flagged for
**enrollment revocation** — its tokens are revoked and it must
re-enroll through the operator gate. A bad-acting node is caught by its
track record, through the existing node-enrollment auth model.

A sixth option — having other nodes **vote** on a proposal — is
deliberately *not* adopted: it would rest trust on an honest-majority
assumption about partially-untrusted nodes, weaker than trusting the
human gate. If ever added it would be an advisory signal into the layer
4 evidence panel, never an autonomous decision.
