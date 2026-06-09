# hone — training sessions & the statistical model

The deliberate side of methodology evolution: how operator-triggered
**training sessions** overlay structure on the natural per-comment
train flow, and the **statistical model** that decides when a
candidate has accumulated enough evidence to graduate, when a check
has decayed enough to prune, or when a cluster of revision proposals
has converged. The system model is in
[`ARCHITECTURE.md`](ARCHITECTURE.md); the per-comment train task
itself lives in
[`ARCHITECTURE-WORK-LIFECYCLE.md`](ARCHITECTURE-WORK-LIFECYCLE.md);
the merge-gate disposition flow that consumes the model's verdicts
lives in
[`ARCHITECTURE-MERGE-GATE.md`](ARCHITECTURE-MERGE-GATE.md).

## Training sessions

A training session is the **only** way trains run. The corpus accumulates
inertly — patchsets gathered, prepared, blind-reviewed, with their
maintainer comments stored in `messages` — and an operator launches a
training session when the evidence is worth converting into counter
movement and proposal generation. There is no continuous per-comment
training flow; comments do nothing until a session selects their
patchset.

### What a session is, mechanically

A training session is a curated set of patchsets, each assigned a
**role** (`pool` or `holdout`) and a **stratum label**, with a set of
train work-items created by the session orchestrator. The session has
its own lifecycle (`draft` → `ready` → `in_progress` → `complete` →
`analyzed`); the train work-items run through the normal claim protocol;
the session's completion fires when every linked work-item has
terminated.

Every train work-item carries a non-null `training_session_id`,
`session_role`, and `stratum_label` — there is no NULL-session train.
The `session_role` is the only field that changes node behaviour: a
`pool` train generates new candidate and revision proposals from any
verified miss, a `holdout` train suppresses proposal generation entirely
(its job is unbiased measurement, not methodology evolution).

**Holdout trains do not update pooled counters.** Specifically, a
holdout-role train does not advance `methodology_candidates.applied /
catches / unique_catches` or the `severity_witness_introduced` /
`severity_witness_preexisting` histograms. The per-train completion
record persists in `work_items.record` and is the raw source the
statistical gates' graduation TOST and other pool-vs-holdout
computations query on demand. The pooled counters represent
*pool-driven* evidence; the merge-gate evidence panel surfaces the
holdout side separately (e.g., "Holdout TOST: passed, p=0.008").

### Why sessions exist

Three properties the corpus alone can't supply:

1. **Strata coverage.** The corpus is heavily skewed (driver patches
   dominate, sparse strata like `kernel:locking` produce few comments).
   Sessions enforce per-stratum floors so the evidence behind a
   methodology change is balanced across the kernel.
2. **Held-out pools.** Transition decisions (graduate, prune, revise)
   need patchsets the methodology has never been tuned against. Because
   training only happens during a session and holdout-role trains
   neither move counters nor generate proposals, "strict virginity" is
   a real guarantee: a holdout patchset has been measured but never
   tuned against.
3. **Auditability.** When a candidate graduates six months from now, a
   human at the merge gate needs to see "what evidence supported
   this?" Session lineage answers concretely: "these specific sessions,
   with these stratum mixes, against these holdout pools."

### Session profiles

Profiles are presets on the session-draft page that encode a selection
strategy, default size and pool/holdout ratio, reuse policy, and a
statistical purpose. Seven profiles cover the operationally meaningful
intents:

| Profile | Intent | Size | Holdout % | Selection bias |
| --- | --- | --- | --- | --- |
| **Standard** | Routine methodology advancement | 300 | 20% | Proportional stratified |
| **Targeted graduation** | Confirm one candidate ready to graduate | 500 | 40% | Over-weight strata where candidate fires |
| **Targeted prune** | Stage evidence for retiring one check | 400 | 30% | Over-weight strata where check fires |
| **Coverage repair** | Fill thin strata | 200 | 5% | Restricted to gap strata |
| **Holdout refresh** | Replenish virgin holdout reserve | 100 | 100% | Balanced for future decision distribution |
| **Exploratory** | Surface methodology blind spots | 500 | 10% | Inverse-weighted by current candidate coverage |
| **Custom** | Manual control | operator | operator | operator |

For Targeted profiles the operator picks the specific candidate or check
to target; the session is shaped around that decision and consumes a
larger share of the holdout pool. For Coverage Repair and Exploratory the
goal is proposal generation in under-served areas, so holdout is small.
For Holdout Refresh the session's primary product is replenished holdout
inventory, with all selected patchsets reserved to `holdout` role.

### The session-draft page

A profile choice is a starting point; the page is an **advisor**, not a
form. When the operator picks a profile and adjusts knobs, hone-core
re-solves the selection against a corpus snapshot and re-runs the
warning evaluator. The page has three regions:

**Region 1 — Configuration.** Profile picker; size slider; holdout-share
slider; per-stratum floor slider; reuse-policy toggles; advanced
per-stratum quota table when expanded. For Targeted profiles, the
target candidate or check picker.

**Region 2 — Plan summary.** Pool/holdout counts; total train work-items
projected; wall-clock estimate at current node throughput; holdout
headroom expressed as **future decisions supportable** (not weeks — see
*Holdout headroom* below); per-stratum selection table.

**Region 3 — Advisory panel.** Warnings sorted by severity, each with
evidence, optional one-click corrective actions, and a stable machine
identifier (the warning code) for documentation lookup and bug-report
correlation.

Live preview: the solver runs on every knob change, debounced to ~100ms.
At 100k+ corpus the solver is fast enough for interactive use; warning
evaluation against historical session aggregates is cached.

### The warning taxonomy

Warnings fall into four severity levels:

- **Block** — the plan cannot commit; the commit button is disabled.
- **Warn** — the plan can commit but the operator is about to do
  something risky and should acknowledge it.
- **Note** — informational.
- **Tip** — proactive suggestion.

The rule engine evaluates each rule against the proposed plan and the
corpus snapshot. Rules include:

| Code | Severity | Fires when |
| --- | --- | --- |
| `HOLDOUT_INSUFFICIENT_FOR_SESSION` | Block | requested holdout cannot be satisfied; offer fallback to "holdout-eligible" or Holdout Refresh first |
| `HOLDOUT_STRATUM_BOTTLENECK_AFTER_SESSION` | Warn | committing would drop the sparsest constrained stratum below the per-stratum floor |
| `HOLDOUT_DEPLETION_PROJECTED` | Note | at current consumption rates, decision capacity falls below floor within projected window |
| `HOLDOUT_STRATUM_IMBALANCE` | Warn | holdout selection is concentrated in strata that don't match likely future decision distribution |
| `OLD_VIRGIN_HOLDOUT` | Warn | proposed holdout includes patchsets older than the recent-window threshold relative to current methodology age |
| `SESSION_TOO_SMALL_FOR_TARGET` | Warn | Targeted session's holdout size won't yield a decisive CI; show projected width |
| `STRATUM_UNDERSAMPLED` | Note | a stratum got fewer than the per-stratum floor |
| `STRATUM_OVERSAMPLED` | Note | a stratum has accumulated enough evidence across prior sessions that this session's contribution is wasted there |
| `EVIDENCE_DILUTED_FOR_TARGET` | Warn | Targeted session's selection bias was too weak — target's fire-stratum distribution and session's stratum distribution mismatch |
| `HEAVY_POOL_REUSE` | Note | over the configurable threshold of pool is from prior sessions |
| `EXCESSIVE_WORK_ITEMS` | Warn | session would generate more train work-items than the configurable threshold |
| `NODE_FLEET_CAPACITY_LOW` | Note | active node count below typical; session will complete more slowly |
| `METHODOLOGY_VERSION_CHURN` | Warn | methodology version has changed N times recently; another change during this session would mix versions |
| `NO_CANDIDATES_NEAR_THRESHOLDS` | Tip | suggests Exploratory or Coverage Repair instead of Standard |
| `STRATUM_DRIFT_DETECTED` | Note | corpus stratum distribution has shifted since the last session of this profile |
| `OVERLAPPING_TARGETED_DECISION` | Block | another in-progress or recent session targeted the same candidate / check |
| `AUTHORITATIVE_HEURISTIC_MIX_HIGH` | Note | proposed selection mixes >X% heuristic-mode patchsets; surfaces because heuristic patchsets contribute less weight to some statistics |

The list is extensible — new failure modes get new rules. The rule
engine is a pure function over `(profile, knobs, selection_plan,
snapshot)`; rules can be added without disturbing existing ones.

### Holdout headroom

At small corpus, holdout depletion is measured in weeks: "8 weeks of
runway." At 100k+ corpus, weeks is the wrong unit — the holdout pool is
effectively inexhaustible by volume, and the binding constraint is
different. The plan summary card shows three things:

- **Primary number: decision capacity.** "Holdout headroom: ~180
  decisions." Computed from (virgin holdout count, scoped to recent
  patchsets via `recent_window_months`) ÷ (typical decision cost).
  This is the question the operator is asking.
- **Secondary line: bottleneck stratum.** When one stratum's virgin
  inventory is materially smaller than the others, that stratum is the
  binding constraint regardless of aggregate runway. The card surfaces
  it explicitly: `bottleneck: kernel:locking · heavy (12 virgin)`. If
  no stratum is constrained, the line hides.
- **Trend indicator.** An arrow showing whether headroom is growing
  (gather is bringing in new patchsets faster than sessions consume
  them) or shrinking session-over-session.

The aggregate-volume framing of small-corpus operation does not survive
the transition to large corpus; the decision-capacity framing does.

### Selection algorithm

The selection unit is the **patchset**, not the message — stratification
axes (subsystem, patch_size, patch_type, review_intensity) are
patchset-grained, and holdout integrity requires whole-patchset
reservation (splitting one patchset's comments between pool and holdout
would leak signal across the boundary). Comments are filtered at
materialisation time, after the patchsets have been chosen.

When the operator commits a draft, the `draft → ready` transition runs:

1. **Eligibility filter.** Patchsets with `origin = gathered` (uploaded
   patchsets are submissions, never training data — and independently of
   any selector, `enqueue_session_train` refuses uploaded-origin
   patchsets at the choke point every train work-item passes through),
   with `prepare` complete, with an `ai_reviews` row from `hone-node`,
   with `review_intensity.bucket_overall ≠ none`, and not currently in
   another active session.
2. **Holdout reservation first.** Select the target number of holdout
   patchsets, stratified per spec, drawn from patchsets that have never
   appeared in any prior session in any role (**strict virginity** — now
   a real guarantee, since trains only happen inside sessions). When
   `HOLDOUT_INSUFFICIENT_FOR_SESSION` fires and the operator accepts the
   fallback, the pool widens to "holdout-eligible" (never in a holdout
   role, but pool roles allowed).
3. **Pool selection.** Target the remaining patchsets, stratified. Pool
   reuse across prior sessions is permitted but tracked; the
   `prefer_unused` toggle biases selection toward never-used patchsets
   where available.
4. **Materialise — patchsets, then comments.** Insert
   `training_session_patchsets` rows (one per selected patchset,
   carrying role and stratum). For each selected patchset, walk its
   `(patch-message, comment)` pairs and create one train work-item per
   pair that passes the trainability filters (below). The
   work-item carries the session id, role, stratum, the patch's
   `message_id`, and the comment's `comment_message_id`. Update
   `patchset_session_history`.
5. **Session transitions to `ready`.**

**Comment trainability filters** — a `(patch-message, comment)` pair
becomes a train work-item only if:

- *Structural (always apply):*
  - The comment's `parent_message_id` is a `MSG_TYPE_PATCH` row in
    `messages` (comments-on-cover-letter and nested-thread comments are
    out of scope — the "for that patch" precision the train task's
    semantics rely on).
  - The comment's author isn't a known bot (lkp@intel.com, kernel test
    robot, syzbot, patchwork, …).
  - The comment isn't a self-reply (author doesn't match the
    patchset's `From:` or any `Signed-off-by:` attributable to the
    submitter).
  - The comment body is non-empty.
- *Quality (session-config knobs):*
  - Skip trailer-only replies (just `Acked-by:` / `Reviewed-by:` /
    `Tested-by:` — no review feedback to evaluate). Default: skip.
  - Optionally restrict to in-scope replies — authors in
    `patchset_metadata.maintainer.authoritative_set` or
    `authoritative_reviewer_set`. Default: include both in-scope and
    out-of-scope.

The per-reply classification (`trailer_only / light / substantive /
deep` and `in_scope`) is produced by the prepare task and stored under
`patchset_metadata.review_intensity.per_reply`, so the materialisation
step reads it without re-classifying.

From `ready` onward the work-items flow through the normal claim
protocol. The session-progress page polls
`training_session_patchsets.completion_state` to render progress.

### Aggregation and analysis

When `train_work_items_done = train_work_items_total` across every
session-patchset, the session transitions to `complete`. The operator
(or an automatic trigger) runs analysis, which transitions it to
`analyzed`:

- Per-candidate counter updates were applied incrementally during the
  session — but only by `pool`-role trains; holdout trains record their
  per-train data without moving counters (see *What a session is,
  mechanically*).
- `review_evaluations` rows are written per `(patchset, session)` —
  when every train in scope for that pair has terminated, the
  aggregator produces one row scoped to the session's selected
  comments. A patchset re-used across sessions accumulates one
  `review_evaluations` row per session, giving the merge-gate evidence
  panel a session-grained view of how the same review fared under each
  evidence pass.
- The session writes its `stats` JSON: per-stratum catch rates, severity-
  weighted catch rates split by `pool` vs `holdout`, missed-major /
  missed-critical counts, proposal counts and clustering, etc.
- Any candidate whose pooled counters crossed a graduation or prune
  threshold during the session triggers a `draft_task` via the
  existing event-driven mechanism — sessions do not bypass the merge
  gate, they feed it.
- The statistical machinery described below runs on the session's
  aggregated evidence; pool-vs-holdout comparisons (graduation TOST,
  counterfactual ablation) read per-train records partitioned by
  `session_role`.

## The statistical model behind transitions

A methodology mutation — graduating a candidate to a check, pruning a
check, revising a check or candidate — is the highest-stakes operation
in the system. The merge gate ensures a human ratifies; the statistical
model ensures the *evidence* presented to that human is defensible.

The model operates entirely in hone-core (arithmetic over counters); the
nodes produce the counters. The model assumes the single-node corpus
deployment that hone is built for (one node fleet, not heterogeneous
inter-node consensus); statistical confidence is constructed from
**patchset diversity**, **temporal independence across sessions**, and
**held-out evaluation** rather than from inter-node agreement.

**Why a rate, not a count.** A raw counter is meaningless at scale.
Catches 50 / Applied 5000 is far worse than Catches 5 / Applied 10:
the first is a 1% rate over a substantial sample; the second is a 50%
rate that's plausibly noise. At thousands of patches per day a fixed
`Applied ≥ 5` graduation trigger would fire within minutes on any new
candidate; rates plus sample-size gates are what make decisions
defensible at corpus scale.

**Sample-size gates and the rule of three.** An observed rate from a
few trials is noise. Rule of three: 0 catches in N trials bounds the
true rate only below ≈ 3/N — so 0/5 is consistent with a rate as high
as 60%, and even 0/30 only rules out rates above ~10%. The eligibility
gates (below) require minimum sample sizes precisely because rates
computed below that floor cannot distinguish a dud from a candidate
that just hasn't accumulated evidence yet. Reference points for a
0-catch candidate: Applied 30 ⇒ rate confidently below ~10%; Applied
60 ⇒ below ~5%; Applied 300 ⇒ below ~1%. The default
`redundancy_min_sample` of 30 is the floor for *any* prune decision;
the graduation gates require materially more evidence.

### Three transitions

**Candidate → check (graduation).** A candidate has accumulated enough
evidence to enter the permanent methodology. Five gates must clear:

1. **Stratified cross-validated catch rate.** Across k stratified folds
   (k = 10 by default) of the session's evaluation data, compute
   per-fold catch rate of this candidate against missed maintainer
   comment points.
2. **Bootstrap confidence interval.** Bootstrap-resample the per-fold
   estimates (10,000 iterations) for a 99% CI on mean catch rate. The
   lower bound must exceed `acceptance_lower_bound` (0.25 default).
3. **Repeated-trial reproducibility.** Across runs on the same patchsets
   at temperature, the candidate's catches must be reproducible — ICC
   ≥ `icc_threshold` (0.6 default). A candidate whose catches don't
   replicate is noise.
4. **Holdout confirmation.** Catch rate on the session's `holdout`
   patchsets must be statistically equivalent to the pool catch rate
   (two-one-sided test for equivalence, margin ±0.10, α = 0.01). A
   drop on holdout indicates overfitting to the pool.
5. **Multiple testing correction.** Across all candidates evaluated in
   the same window, Benjamini–Hochberg at `fdr_q` (0.05 default).

A candidate that clears all five gates becomes eligible for graduation;
hone-core enqueues a `draft_task` of type `graduate`, which a
node drafts and the human ratifies.

**Check → pruned.** A check's utility has decayed and it should be
removed. Four signals must agree:

1. **CUSUM utility monitoring.** For each accepted check, per-round
   utility `U_t = (true_concerns − λ × false_concerns) / applications`
   is tracked; CUSUM with slack κ and threshold h flags when recent
   utility has drifted below the historical mean.
2. **Stratification-adjusted drift test.** A mixed-effects model
   isolates true utility decay from compositional shifts in the
   patchset mix. `β < 0` significantly at α = 0.01.
3. **Counterfactual ablation on holdout.** Running reviews with and
   without the suspect check, paired by patchset. Paired Wilcoxon
   signed-rank test on concern-quality scores; removing the check must
   not significantly degrade quality.
4. **Bayesian posterior on usefulness.** A Beta-Binomial posterior on
   the check's true catch rate, updated every session. Pruning
   eligible when posterior probability that the true catch rate
   exceeds the minimum-useful threshold drops below 0.05.

All four signals must agree before a `prune-ineffective` draft
task is enqueued.

**Revision.** An existing check or candidate should be reworded for
broader applicability. Detected through clustering of `revise_existing`
proposals from train tasks:

1. **Accumulation.** Every `revise_existing` proposal targeting a given
   item is recorded with its draft text, originating session,
   stratum, and round.
2. **Clustering.** Embed and DBSCAN-cluster the draft texts. For a
   cluster to qualify:
   - ≥ 8 distinct proposals (configurable)
   - Spans ≥ 5 distinct sessions
   - Spans ≥ 3 distinct strata
   - Mean intra-cluster cosine similarity ≥ 0.75
3. **Synthesis.** The centroid-nearest draft (or an LLM-synthesised
   merge of the cluster) becomes the candidate revision.
4. **Paired A/B on validation pool.** Run reviews with original vs
   revision, paired by patchset. Paired Wilcoxon at α = 0.01, with
   Cliff's δ > `cliff_delta_minimum` (0.2 default). No significant
   degradation on any major stratum (Bonferroni-corrected subgroup
   analysis).
5. **Holdout confirmation.** Improvement replicates on holdout with
   the same significance and effect-size thresholds.

A cluster that clears all gates triggers a `revise` draft task.

### Cross-cutting machinery

- **Cooldown.** After any transition, the affected item enters a
  3-round cooldown (configurable) during which no further transitions
  on it are evaluated. Prevents oscillation.
- **Recency weighting.** Patchsets are weighted by recency in utility
  calculations; the `recent_window_months` setting controls the
  weighting horizon.
- **Cold rounds.** Periodically, sessions run with a minimal
  methodology (only baseline stages enabled) to surface concerns the
  current methodology misses entirely. Counters anchoring bias from
  accumulated stages. Operationally, this is a special session
  profile (not in the routine set, used for periodic audit).
- **Spot-check audit.** A configurable fraction of prepared patchsets
  and train results is pulled for manual operator review. Tree-aware
  preparation and per-point match attribution both introduce failure
  modes that statistical tests downstream cannot catch. Each sampled
  record carries its `self_review_record` (a structured outcome from
  the node's adversarial self-review pass — present on every
  success-path completion record across all four operations); the
  audit verifies that the recorded outcomes (upheld / revised /
  dropped, per challenge) actually match the substantive output. A
  concern recorded as "dropped" by the self-review must in fact not
  appear in `concerns[]`; a severity recorded as "revised from major
  to moderate" must in fact be `moderate`. Mismatches are the
  primary diagnostic for "the node performed the adversarial pass
  in name only."

### Known risks (single-node operation)

Statistical machinery does not eliminate these; they require
operational monitoring:

- **Systematic blind spots.** A single hone-node has consistent reasoning
  patterns; categories it reasons poorly about will never get strong
  candidate proposals and the methodology will inherit that blind
  spot. Mitigated by periodic manual audit and occasional cross-checks
  against a second model.
- **Mode collapse.** The methodology may drift toward reflecting what
  the node is good at noticing rather than what maintainers actually
  care about. Mitigated by tracking concern-vs-comment alignment as a
  top-level health metric.
- **Anchoring on early stages.** Existing stages bias future proposals
  to be consistent with the existing methodology. Mitigated by cold
  rounds.
- **False stationarity.** The kernel evolves; patchsets from 2018 are
  stylistically different from patchsets from 2025. Mitigated by
  recency weighting and the `OLD_VIRGIN_HOLDOUT` warning.
