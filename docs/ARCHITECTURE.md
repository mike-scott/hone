# hone — architecture

hone is a service that reviews Linux kernel patchsets. How the system is
shaped: operational steps live in `PROCEDURE.md`; this file is the *model*
behind them.

> **Status.** This describes the **target** architecture — a containerized
> service. The repo today carries the design (this doc and its companions)
> plus a working core of the implementation: the FastAPI app and the full
> `/v1` contract, the GATHER loop, the operator UI, the complete SQLite
> schema, and the `prepare` node task (deterministic Tier-0 + LLM Tier-1)
> are built. Still to build: the `review` / `train` / `draft` node
> handlers, and the self-honing machinery that turns train results into
> methodology change — review-level aggregation, the statistical model,
> the eligibility-gate / draft-trigger pipeline, the training-session
> orchestrator, and the merge gate. For most of those the DB tables and
> CRUD primitives already exist but the driving logic does not. See *Today
> vs. target* at the end for the concrete inventory.

## Governing principle

**All AI runs on worker nodes; hone-core is deterministic.** Anything
that needs judgement — reviewing a patch, characterising it for the corpus,
judging a candidate practice, authoring methodology text — happens on a node.
Anything mechanical — scheduling, queuing, counting, routing, selecting,
warning, auth, storage — is hone-core. Every design decision below follows
from this one line.

The corollary: the statistical machinery that decides when a candidate
practice has accumulated enough evidence to graduate, when a check has
decayed enough to prune, or when a revision proposal has converged across
sessions, is entirely arithmetic over the per-candidate counters that the
nodes produce. hone-core does the arithmetic; the nodes produce the
counters; a human ratifies the result.

## Two components

```
            HONE-CORE                                      WORKER TIER
  ┌────────────────────────────────┐
  │ containerized web service       │        ┌──────────────────────────────┐
  │ FastAPI + SQLite(WAL), 1 instance│        │ hone-node (containerized)     │
  │ NO AI · NO kernel repo           │        │  - Claude token               │
  │                                  │        │  - owns its own kernel repo   │
  │  · GATHER (supervised) from      │  REST  │  - scratch storage            │
  │     lore (and other future       │◄──────►│                               │
  │     sources)                     │        │                               │
  │  · serve REST: claims, results,  │  /TLS  │  task worker:                 │
  │    node enroll                   │        │   claim → do AI work → report │
  │  · mechanical self-honing        │        │   (prepare · review · train · │
  │  · training-session orchestrator │        │    draft tasks)         │
  │  · owns hone.db                  │        │                               │
  └────────────────────────────────┘          └──────────────────────────────┘
```

### hone-core

A containerized web service — **FastAPI + SQLite (WAL mode), one instance**,
no AI, and **no kernel git repo**. Six jobs:

1. **GATHER** (scheduled) — stream patchsets *and* messages from the data
   sources (`core/gather-modules/`, see `SOURCES.md`) into the corpus. The
   producer that feeds the pipeline. A supervisor runs one asyncio task per
   enabled source; each resumes from an opaque per-source **cursor**
   (`gather_state`), is floored at the module's `since_date`, and may run
   long (a throttled backfill). A source is re-spawned every interval — but
   never while its previous task is still running — and a task whose
   heartbeat stalls is cancelled. The interval is a hone-core
   **configuration option**, defaulting to **every 10 minutes**.
2. **Serve the REST API** — hand out work claims (with the methodology and
   the patches baked into the claim payload), accept completion records,
   authenticate nodes.
3. **Mechanical self-honing** — pooled candidate-practice counters,
   graduation-eligibility checks, threshold prunes (see *The statistical
   model behind transitions*), bootstrap CI / TOST computation over
   session-aggregated evidence. All arithmetic, no judgement.
4. **Training-session orchestration** — solve stratified selections against
   the corpus, run the advisory rule engine that surfaces warnings on the
   session-draft page, materialise selected patchsets into work-items,
   aggregate session results into the statistical machinery above.
5. **Own `hone.db`** — the corpus (patchsets, messages, ai_reviews,
   patchset_metadata, list_tags), the versioned methodology + candidates +
   proposals, the work queue, the training sessions, the node fleet, the
   per-source gather cursors.
6. **Serve the operator web UI** — the human-facing management surface; see
   *Operator web UI* below.

SQLite's WAL mode gives concurrent readers + a serialized writer — exactly
what the atomic claim protocol needs. One instance; the node tier scales
independently of it.

### hone-node

A containerized worker that **starts from scratch**. Given only its start
parameters — a Claude API token, the hone-core URL, and the fleet secret —
plus a mapped storage volume, it bootstraps everything else itself. On first
start it **enrolls into the fleet** (the device-authorization grant; see
*Auth, enrollment & transport*), obtaining its bearer credentials and
hone-core's CA certificate before it does any work. The node image bakes in
**nothing** deployment- or domain-specific (no methodology, no kernel tree):
on start it fetches the methodology from hone-core when needed, and it
**builds its own reference kernel git repo at runtime** in its storage
volume — so it can check out any base commit, with no shared tree to thrash
and nothing to pre-provision.

A node's work is **pure analysis** — it reads patches, comments, and
surrounding code and reasons about them. It does **not** build the kernel
or run review tooling (checkpatch, smatch, sparse, sanitizers, `dtbs_check`).
It uses only lightweight, toolchain-free git against its reference repo —
reading code at a base commit, running `git apply --check` to confirm a
patch applies, running `scripts/get_maintainer.pl` against `MAINTAINERS` at
a base commit — so the node container carries no kernel build toolchain,
and a node's outputs are reproducible reasoning rather than tool runs.

A node is a **task worker**: it claims tasks over the REST API, does the
AI work, and reports back. Four task types:

- **prepare task** — characterise one patchset for the corpus. Produces
  the structured per-patchset metadata (subsystem, patch_size, maintainer,
  patch_type, review_intensity, tree_state) that drives stratification. See
  *The patchset-metadata layer* below, and
  [`ARCHITECTURE-PREPARE.md`](ARCHITECTURE-PREPARE.md) for the target
  tiered design that splits prepare into a deterministic code phase (cgit
  lookups, no LLM, no tree) and an LLM-judgment phase, confining the
  kernel tree to `review`.
- **review task** — review one patchset, blind. Produces an `ai_reviews`
  row keyed on the patchset's root Message-ID.
- **train task** — measure our review against *one* reviewer comment on
  *one* patch: decompose the comment into points, classify each against
  our concerns, and (in pool role only) propose new candidates or
  revisions from any verified miss. Created exclusively by the
  training-session orchestrator at the `draft → ready` materialisation
  step — one train per `(patch, comment)` pair within a session's
  selected patchsets, filtered by comment trainability criteria (see
  `ARCHITECTURE-TRAINING.md`). pool-role trains advance the candidate's
  pooled counters on receipt; holdout-role trains do not.
- **draft task** — evaluate the candidate set against the methodology
  and propose changes (see *The merge gate*).

A node runs a continuous **claim loop** — claim, do the work, submit, then
immediately claim again while the queue yields work. On an empty claim
(`204 No Content`) it waits a **poll interval** before retrying; the poll
interval is a configuration option, default **60 s**. This idle poll is
distinct from the failure backoff below: the poll timer paces a *healthy
but empty* queue, the backoff handles an *unreachable* hone-core.

The Claude token never leaves the node; hone-core never sees it and
never calls Claude.

The node's retry / backoff discipline — indefinite exponential backoff
on transient faults, fail-fast on revoked enrolment, idempotent result
submission, and scratch persistence across outages — lives in
[`ARCHITECTURE-WORK-LIFECYCLE.md`](ARCHITECTURE-WORK-LIFECYCLE.md) →
*Node resilience*.

## Operator web UI

hone-core serves a **server-rendered management UI** alongside the REST
API — one FastAPI app: `/v1/*` for nodes (JSON), the rest for operators
(HTML). The stack is **Jinja2 templates + Bootstrap 5 + HTMX** — the
**AdminLTE 4** admin theme gives the layout (a fixed sidebar + header, with
a light/dark toggle), and HTMX gives live updates and in-page actions with
no single-page-app and no JavaScript build step (AdminLTE, Bootstrap,
Bootstrap Icons and HTMX are vendored static assets, which is why the
hone-core image needs no Node toolchain).

Pages:
- **Queue** (`/`) — the home page. The work queue (prepare + review + train
  items), with a two-axis chip filter — type (prepare / review / train) ×
  state (claimable, claimed, completed, unappliable, deferred). Per-type and
  per-state counts ride along the chips.
- **Nodes** (`/nodes`) — the live node fleet and the **pending-enrollment
  queue**: an operator enters a node's device-grant *user code* here to
  approve it and admit it to the fleet (see *Auth, enrollment & transport*).
- **Enroll** (`/enroll`) — the verification URL a node logs on startup; the
  operator pastes the user code here to look the enrollment up and approve
  or deny it.
- **Sessions** (`/sessions`) — the training-session list (drafts, active,
  complete, analysed) and the **session-draft page** (`/sessions/draft`)
  where an operator composes a new session against a corpus snapshot. See
  *Training sessions* below.
- **Settings** (`/settings`) — view the deployment configuration and edit
  the operator-tunable runtime config (`config.yaml`) **plus the list-tag
  gather filter** (see *Configuration & the Settings page* and `SOURCES.md`
  → *List-tag filter*).
- **Merge gate** — disposition the `methodology_proposals` queue (see *The
  merge gate*). Planned; not yet implemented.
- Reporting pages — later. (The patchset and work-item detail views are
  built.)

Operators authenticate with a human login (session-based), distinct from the
header credentials a node presents.

## Persistent storage

Each service maps **one local volume** as its data store; everything else
in a container image is ephemeral. hone-core's volume holds its entire
owned state (the database, TLS material, config, methodology files, and
public-inbox archives); the node's volume holds its reference kernel
clone and task scratch and starts empty (the node self-populates it).
The concrete volume contract — paths, env vars, contents — is in
`DEPLOYMENT.md` → *Volume contract*.

## Configuration & the Settings page

hone-core's configuration is in two tiers; the operator web UI's
**Settings** page is the surface for the second.

**Deployment configuration** — environment variables set at container
start (secrets, hostname / port, data-volume paths). The full
deployment env-var contract is in `DEPLOYMENT.md` → *Env-var contract*.
Changing any of these is a **redeploy**, not a UI action — the
Settings page shows them **read-only** (secrets masked), for reference
only.

**Operator-tunable configuration** — the cadences, lifetimes, thresholds,
and statistical parameters an operator may adjust on a running instance.
These live in a YAML file on the data volume, `HONE_CONFIG` (default
`/data/config.yaml`), which the Settings page **edits**:

```yaml
# /data/config.yaml — hone-core operator-tunable settings
gather:
  interval_seconds: 600
  sources: [lore]          # the enabled gather sources ([] = none/paused)
work_queue:
  lease_seconds: 1800
  heartbeat_seconds: 300
enrollment:
  access_token_ttl: 3600
  refresh_token_ttl: 0     # 0 = no expiry
  device_code_ttl: 900
  device_poll_interval: 5
merge_gate:
  redraft_cap: 3
sessions:
  default_size: 300
  default_holdout_share: 0.20
  per_stratum_floor: 30
  holdout_reserve_floor_decisions: 30   # warn below this many future decisions
  holdout_reserve_floor_per_stratum: 10
  recent_window_months: 18               # for "recent virgin" runway scoring
statistics:
  fdr_q: 0.05
  individual_alpha: 0.01
  bootstrap_iterations: 10000
  acceptance_lower_bound: 0.25
  icc_threshold: 0.6
  cliff_delta_minimum: 0.2
  cusum_h_sigmas: 4.0
  cusum_kappa_sigmas: 0.5
  cooldown_rounds: 3
```

**Layering.** On startup hone-core resolves its effective tunable config as
*built-in defaults* overlaid by *`config.yaml`*. On first run, when no
`config.yaml` exists, hone-core writes one from the defaults — an env var
for a tunable key, if set, seeds that key (a convenience for
infrastructure-as-code). Thereafter **`config.yaml` is authoritative**: the
Settings page is the way to change a tunable, and an operator's edit is
never silently overridden by a stale env var.

| Group | Setting | Default |
| --- | --- | --- |
| GATHER | cron cadence | 600 s |
| GATHER | enabled sources | all installed |
| Work queue | claim lease | 1800 s |
| Work queue | heartbeat interval | 300 s |
| Enrollment | access-token TTL | 3600 s |
| Enrollment | refresh-token TTL | no expiry |
| Enrollment | device-code TTL | 900 s |
| Enrollment | device poll interval | 5 s |
| Merge gate | redraft cap | 3 |
| Merge gate | draft batch max | 10 |
| Merge gate | defer growth factor | 0.20 |
| Sessions | default size | 300 patchsets |
| Sessions | default holdout share | 20% |
| Sessions | per-stratum floor | 30 |
| Sessions | holdout reserve floor (decisions) | 30 |
| Sessions | holdout reserve floor (per stratum) | 10 |
| Sessions | recent window | 18 months |
| Statistics | FDR q | 0.05 |
| Statistics | individual α | 0.01 |
| Statistics | bootstrap iterations | 10,000 |
| Statistics | cross-validation folds (k) | 10 |
| Statistics | acceptance lower bound | 0.25 |
| Statistics | ICC threshold | 0.6 |
| Statistics | TOST equivalence margin | ±0.10 |
| Statistics | repeated-trial sample size | 5 |
| Statistics | Cliff's δ minimum | 0.2 |
| Statistics | CUSUM h | 4.0σ |
| Statistics | CUSUM κ | 0.5σ |
| Statistics | CUSUM utility λ (false-positive penalty) | 0.5 |
| Statistics | Bayesian useful threshold | 0.05 |
| Statistics | cooldown after transition | 3 rounds |
| Revision clustering | minimum cluster size | 8 |
| Revision clustering | minimum sessions spanned | 5 |
| Revision clustering | minimum strata spanned | 3 |
| Revision clustering | mean cosine similarity | ≥ 0.75 |
| Eligibility | redundancy ratio threshold | 0.15 |
| Eligibility | redundancy minimum sample | 30 |
| Eligibility | consolidate minimum sample | 30 |
| Eligibility | consolidate cofire threshold | 0.70 |
| Severity drift | minimum sample per group | 20 |
| Severity drift | Krippendorff's α threshold | 0.6 |
| Severity drift | minimum groups | 3 |

Tighter α (0.01 rather than 0.05) is used throughout the per-test
gates because single-node operation provides less independent
evidence per decision than multi-node operation. Tune these values
empirically against observed false-positive rates over the first
several operational windows before treating them as stable.

**Applying a change.** hone-core holds the resolved config in memory;
saving the Settings form validates the inputs, writes `config.yaml`, and
updates the in-memory copy. A setting consulted per operation (the claim
lease, the token TTLs) takes effect on the next operation; one consulted by
a long-running task (the GATHER cadence) takes effect on that task's next
iteration. **No restart is needed** for a tunable; only deployment config
requires one.

**Page shape.** A grouped form — one section per group above, each input
labelled with its unit and default. **Save** is a redirect-after-POST;
invalid input (a non-positive interval, an unknown gather source) is
rejected with the field flagged and `config.yaml` left untouched. A
separate read-only panel lists the deployment configuration.

**Authentication.** The Settings page **mutates** hone-core's behaviour,
so it sits behind operator authentication. The whole operator UI is gated
by session-based login (`core/auth.py`): email/password (Argon2id) or
Google SSO, with an admin-approval gate for self-registered accounts; the
configured `HONE_ADMIN_TOKEN` is a separate config-admin identity with
exclusive access to user management.

**Not in scope here.** Methodology import/export (export the DB methodology
to YAML, re-import an edited one — see *Methodology storage*) is operator
configuration of a different kind; it may be surfaced on or beside the
Settings page, but the YAML methodology is not part of `config.yaml`.

## Data model

`hone.db` is one SQLite file. The schema is **`_SCHEMA_V1`** — one
migration creates the whole schema from a single seed. Tables, grouped
by concern:

| Group | Tables | Holds |
| --- | --- | --- |
| **Corpus** | `patchsets`, `messages`, `ai_reviews`, `patchset_metadata`, `review_evaluations` | one row per patchset (root Message-ID + declared `base_commit`); the per-message store (covers, patches, comments) keyed on Message-ID; one `ai_reviews` row per `(patchset, source)` with **scoped** concerns (see *Review output: concerns and scoping*); one `patchset_metadata` row per patchset with the structured metadata produced by the `prepare` task; one `review_evaluations` row per `(patchset, ai_review, session)` — written when every train a session created for the patchset has terminated; a patchset re-used across sessions produces one eval row per session |
| **List-tag filter** | `list_tags`, `patchset_tags` | the universe of mailing lists (origin = `manifest` from the lore manifest, or `observed` once seen on a patchset) and the per-patchset tag set; an operator-enabled tag set gates ingest |
| **Methodology** | `methodology_versions`, `methodology_candidates`, `methodology_proposals`, `eligibility_flags` | the versioned methodology document (active + superseded); the candidate practices with their pooled `applied` / `catches` / `unique_catches` counters and **two parallel `severity_witness` histograms** — `severity_witness_introduced` across the five tags for findings the candidate has produced on patch-introduced code, and `severity_witness_preexisting` for findings on pre-existing code (per the concern's `is_preexisting` flag). Two histograms rather than one so the merge gate can show the introduced-vs-preexisting split directly without re-querying concerns; the human-dispositioned `methodology_proposals` queue + suppression log; and the `eligibility_flags` table — per-`(subject_kind, subject_id, kind)` rows recording which subjects currently satisfy which deterministic eligibility gate (graduate, prune-redundant, prune-ineffective, consolidate, revise, revise-severity-scale), with the evidence snapshot at flag-set time, a `suppressed_at` timestamp (set when the suppression log filters the flag), and a `defer_watermark_at` recording the counter value past which the flag was deferred — together driving the draft-task trigger pipeline described in *The merge gate* |
| **Work queue** | `work_items`, `draft_tasks` | the unified prepare + review + train queue (`type` = prepare / review / train; one prepare per patchset, one review per patchset, one train per session-selected `(patch, comment)` pair); draft tasks for merge-gate work |
| **Training sessions** | `training_sessions`, `training_session_patchsets`, `patchset_session_history` | operator-triggered batch overlays on the work queue; the per-session membership with role and stratum; a denormalised history table for fast "has this patchset been used in any session?" queries |
| **Nodes & auth** | `nodes`, `node_enrollments`, `node_tokens` | enrolled nodes + the device-authorization-grant enrollments + the issued access/refresh token pairs (hashed only) |
| **Gather state** | `gather_state` | one row per gather module — the opaque resume cursor (see `SOURCES.md`) |

A patchset is gathered once; a comment lands as a separate `messages` row
and is inert at gather time. Comments never auto-enqueue work — training is
session-driven: the session orchestrator creates one `train` work-item per
selected `(patch, comment)` pair at `draft → ready` materialisation (see
*Training sessions* and `ARCHITECTURE-TRAINING.md`).

**Dedup.** A patchset's identity is the **root Message-ID** of its thread,
so the same submission gathered via two sources is one `patchsets` row.
The same Message-ID across `messages`, `ai_reviews`, `work_items` rides
UNIQUE constraints, so re-ingestion — a `git fetch`, a list crosspost, a
re-run — is an idempotent no-op; `hone.db` is the sole dedup authority.
Revisions (`v1` → `v2` → …) are distinct submissions, linked by
`change_id` for reporting.

**Small-int enums.** Low-cardinality columns (`state`, `type`, `origin`,
`severity`, `role`) are stored as small integers with `CHECK`
constraints in DDL and named constants in `core_db.py`
(`WORK_ITEM_TYPE_PREPARE`, `WORK_ITEM_TYPE_REVIEW`, `MSG_TYPE_COMMENT`,
`LIST_TAG_ORIGIN_MANIFEST`, `SESSION_ROLE_POOL`,
`SESSION_ROLE_HOLDOUT`, …). A shared 5-level
`SEVERITY_NIT|MINOR|MODERATE|MAJOR|CRITICAL` scale (mapping to the
lowercase YAML tags `nit` / `minor` / `moderate` / `major` / `critical`
in `report_finalization.severity_scale`) is used everywhere severity
appears. Severity is a **per-finding** property — it tags `ai_reviews`
concerns, train-task comment points, and completion-record findings,
each at the moment of finding. Checks and candidate practices do **not**
carry severity fields; the same check can produce findings at any
severity depending on the specific impact and reachability of the
instance.

**Counter semantics.** The pooled `applied` / `catches` / `unique_catches`
counters on `methodology_candidates` have precise increment rules that
matter for downstream statistical aggregation:

- **`applied`** increments on a review where the candidate's pattern was
  *present* in the patch and the check was actually exercised. Reviews
  where the pattern was absent — the candidate had nothing to evaluate —
  do **not** count. This is what makes a rate meaningful: `catches /
  applied` is the candidate's catch rate *over patches the candidate
  applied to*, not over all patches in the corpus.
- **`catches`** increments on an applied review where the candidate
  caught a real, code-verified issue — confirmed by per-comment train
  results that matched the candidate's finding against a maintainer
  comment point (or by spot-check audit on findings without
  corresponding comments).
- **`unique_catches`** is the subset of `catches` where the blind
  baseline review (the methodology without this candidate) would have
  *missed* the issue. A catch the baseline finds anyway via existing
  checks is not value the candidate added. This is the counter that
  governs prune-redundant eligibility: a candidate whose catches are
  almost all non-unique is redundant with existing checks regardless of
  its raw catch rate.

The `severity_witness_introduced` and `severity_witness_preexisting`
histograms increment when a candidate fires and produces a finding,
keyed by the finding's severity tag and `is_preexisting` flag. They are
populated from the per-train candidate_outcomes rather than from
per-review aggregation, so a finding contributes to the histogram only
after it has been measured against a maintainer comment in a train.

**Reviewer tracking is out of scope.** A comment is one `messages` row
with its `author_name` / `author_email`; there is no identity merging
across crossposts and no per-reviewer accuracy / confidence scoring.
The `patchset_metadata.maintainer` block records the authoritative
maintainer set from `MAINTAINERS` per patchset, but does not score them.

### `patchset_metadata` columns

The row is produced by the `prepare` task and carries the structured
metadata the rest of the system reads for stratification, eligibility,
and downstream filtering. The full shape is described in *The
patchset-metadata layer* below; the table itself is one column per top-level
JSON field, plus a `methodology_version` column (the version under which
this row was prepared — pins the prepare prompt the node ran since prompts
live under `operations.prepare.guidance` in the methodology), a
`node_tree_revision` column (the node's kernel-tree HEAD when the row was
produced), and a `mode` column (`authoritative` / `mixed` / `heuristic`).
Re-preparation is supported by keying off `(patchset_hash,
methodology_version, node_tree_revision)` — a row prepared in heuristic
mode because the node's tree didn't yet contain the declared base-commit
may become eligible for authoritative re-preparation once another node has
a fresher tree.

### `training_sessions` and related columns

```
training_sessions
  id · created_at · created_by · status · profile
  target_pool_size · target_holdout_size
  actual_pool_size · actual_holdout_size
  stratification_spec     JSON — the strata targeted and at what quotas
  methodology_version     active version at session start (for reproducibility)
  corpus_snapshot_at      timestamp; the snapshot the draft was solved against
  completed_at · stats    JSON summary written when status reaches `complete`

training_session_patchsets
  session_id · patchset_id · role (pool | holdout) · stratum_label
  selected_at · completion_state (pending | partial | complete)
  train_work_items_total · train_work_items_done

patchset_session_history     (denormalised, for fast lookups)
  patchset_id · session_id · role · used_at
```

Every `work_items` row of `type = train` carries a non-null
`training_session_id` FK (plus `session_role`, `stratum_label`, and the
`comment_message_id` that names the specific maintainer comment the
train evaluates) — there is no NULL-session train; the session
orchestrator creates every train at materialisation. The
`patchset_session_history` table is a redundant index over
`training_session_patchsets` — present because the "find me N patchsets
that have never been in any session in any role" query runs on every
session draft and benefits from a flat index rather than a join.

Session lifecycle: `draft` → `ready` → `in_progress` → `complete` → `analyzed`.
The `draft` state is the assembly phase where the operator iterates with
the session-draft page; transitioning to `ready` materialises the selection
into `training_session_patchsets` rows and links / enqueues the relevant
`work_items`. `complete` fires when every linked train work-item reaches a
terminal state; `analyzed` fires when the statistical aggregation step has
run over the session's results.

## The patchset-metadata layer

Every patchset gathered into the corpus is **prepared** before it is used
for anything else. Preparation produces the structured metadata that
drives stratified selection, eligibility, and the source-quality
gradations that statistical aggregation depends on. Without preparation,
the corpus is undifferentiated — a 100k-patchset pile that cannot be
sampled defensibly.

### The `prepare` task

A `prepare` work-item is auto-enqueued by `gather._ingest_ref` the moment
a new patchset lands. It is the same shape as a review or train work-item
in the queue, the same lifecycle, the same lease and heartbeat behaviour.
The node task is **AI metadata extraction**: read the patchset's raw
messages, optionally consult the local kernel tree at the patchset's
declared `base-commit`, and emit a structured JSON record. The full prompt
lives in the methodology at `operations.prepare.guidance` (same shape and
storage as every other task type's prompt — see *Methodology storage* →
*Compile* for the per-task slicing). The architectural commitments are:

- **The node owns all tree access.** hone-core never sees a kernel tree,
  never tells a node about a tree, and presents no tree-related fields in
  the prepare payload. The node discovers `base-commit:` trailers itself,
  resolves them against its local tree, and decides its operating mode
  accordingly.
- **Discovery is mandatory; fabrication is forbidden.** The node only
  reports a base commit when an actual `base-commit:` trailer was present
  in the patchset. Inferring a base from timestamps or context produces
  authoritative-looking metadata that is actually wrong — heuristic mode
  is the honest answer when no trailer exists.
- **Three modes, made explicit.** Each prepared row carries a `mode`:
  `authoritative` (tree available + base commit declared + base commit in
  the tree + `scripts/get_maintainer.pl` readable at base),
  `heuristic` (one or more of those fails — the metadata is derived from
  email content only), or `mixed` (some fields authoritative, some
  heuristic; typically because `MAINTAINERS` was unparseable at base but
  the rest of the tree was usable). Every individual metadata field also
  carries a `source: tree | thread | mixed` indicating how it was derived.

### The five metadata fields

| Field | What it captures | Used for |
| --- | --- | --- |
| `subsystem` | canonical `MAINTAINERS` section name(s) the patch touches, primary + secondaries, plus `S:` status and `T:` tree | stratification axis 1; the primary stratum dimension |
| `patch_size` | precise diff counts (lines added/removed, files modified/added/deleted/renamed, hunks), bucket (trivial / small / medium / large / huge), series length, and churn ratio against base-commit file sizes | secondary stratification; eligibility filtering |
| `maintainer` | the authoritative set of `M:` maintainers, `R:` reviewers, and `L:` lists from `MAINTAINERS` for every modified file, plus cross-references against the email thread (cc-coverage, list-coverage, engagement rate) | identifying primary maintainer, scoping reviewer authority |
| `patch_type` | primary classification (bugfix / feature / refactor / cleanup / performance / documentation / build / revert / merge_prep / unknown) + secondary tags (security / stable / rfc / resend / uapi_change / abi_change / dt_binding / selftest / tooling / tagged_for_tree), with verified `Fixes:` trailer resolution and file-history signal | stratification refinement; eligibility |
| `review_intensity` | per-reply substance classification (trailer_only / light / substantive / deep) — emitted both as a `per_reply` array (`[{message_id, substance, in_scope}, …]`) and as aggregate counts — plus in-scope vs out-of-scope split (against `maintainer` authoritative set), bucketed overall and in-scope, plus `had_nack` / `had_v_next` / `had_authoritative_ack` flags | stratification axis 2; the secondary stratum dimension; eligibility filter (`bucket_overall = none` excludes a patchset from training entirely); the per-reply array is the trainability filter the session orchestrator reads at materialisation |

The `tree_state` block alongside the five fields records the base-commit
provenance (declared / source / in-tree / timestamp / kernel version at
base / applies cleanly / prerequisite patch IDs) so downstream consumers
can reason about how much the metadata can be trusted.

### Bot-and-self filtering

Across every field, **bots are never counted as humans** (lkp@intel.com,
kernel test robot, 0day, syzbot, patchwork, etc. — the canonical exclude
list is in the prompt). **Self-replies are never counted as review** (a
submitter replying to their own patch produces an `author_name` /
`author_email` match against the patch's `From:` and any `Signed-off-by:`
attributable to them). Both filters apply to `maintainer.all_engaged`,
`review_intensity.*`, and every count derived from the thread. Stratum
sampling is only as honest as these filters; getting them wrong
contaminates every downstream statistic.

### Re-preparation

A patchset prepared in `heuristic` mode because the node's tree did not
contain the declared base-commit may become eligible for authoritative
re-preparation later, once a node with a fresher tree is available.
hone-core's re-preparation policy:

- Patchsets prepared with `base_commit_source: none` (no trailer present)
  are never re-prepared — heuristic mode is permanent for those.
- Patchsets prepared with `base_commit_source: trailer` but `base_in_tree:
  false` are flagged eligible for re-preparation. A periodic job
  (configurable cadence, default weekly) re-enqueues `prepare` work-items
  for them; nodes claim and process them as normal, and the new row
  supersedes the old (the old is retained for audit but not used for
  stratification).
- Patchsets with a newer `methodology_version` available are also
  re-prepared when the new version's `operations.prepare.guidance` or
  `principles` differ from the version at preparation time — re-prep
  in bounded batches to avoid swamping the queue. Methodology bumps
  that don't touch prepare-relevant content (a new candidate practice,
  for instance) do not trigger re-prep.

The `methodology_version` and `node_tree_revision` columns on
`patchset_metadata` are the join keys for these decisions.

### Quality as a stratification dimension

Source-quality is not just metadata — it is itself a sampling axis.
Authoritative-mode patchsets are preferred for holdout reservation
(transition-decision confidence depends on metadata reliability);
heuristic-mode patchsets are usable for pool but contribute less weight
in some statistical computations. The session-draft page surfaces the
authoritative-vs-heuristic split of any proposed selection so the operator
sees it as a primary fact, not a buried filter.

## Methodology storage

The review methodology is **DB-resident and versioned** — the canonical
copy lives in hone-core's global-methodology tier, with YAML as the
import/export format. Three operations on the store:

- **Import** — load a methodology from its portable representation —
  `core/default-methodology.yaml`, a structured YAML file — into the DB as
  a new version, **after validating it against
  `common/schema/methodology.schema.yaml`** (a JSON Schema; a malformed methodology
  is rejected, not imported). `core/default-methodology.yaml` ships in
  the repo and bootstraps DB v1; import also brings human-edited
  revisions back in.
- **Export** — render the DB methodology back to YAML (the
  `core/default-methodology.yaml` format) — for human reading, offline
  editing, backup, and git-tracking; the operator can export over the
  default and commit it.
- **Compile** — assemble the methodology slice a particular task needs
  from the canonical store and **embed it in the claim payload** (there is
  no separate `GET /v1/methodology` endpoint; the wire contract makes the
  claim self-contained — see `API.md`). The compiled shape is
  `{ core: { principles, stages, checks, candidates,
  documentation_review, report_finalization }, operations: {
  <task_type>: { guidance, return } } }` — `core` is what to review
  against; `operations.<task_type>` is the per-task instruction set (a
  review task gets `operations.review`; a train task gets
  `operations.train`; a draft task gets `operations.draft`). A prepare
  task receives a narrower slice — `{ core: { principles },
  operations: { prepare } }` — because prepare consults the
  cross-operation principles (e.g., `set-current-date` for
  relative-date reasoning, `absence-is-not-proof` for tree-query
  discipline) but does not apply stages, checks, candidates, or
  the report-finalization rubric. The `severity_scale` rubric is
  nested under `report_finalization` (see *The `severity_scale`
  block* below) — it is part of how findings are tagged in the
  delivered review.
  **Graduated checks and candidate practices ride in `core` as two
  arrays** — `checks` (the permanent methodology) and `candidates` (the
  experimental layer being trialed); a node applies both, and the split
  lets a *train* task report per-candidate outcomes (applied / fired /
  unique_catch) for the pooled counters. Each `candidates` entry keeps
  its `id` and current pooled stats. A node never sees the raw canonical
  store.

Import/export keep the methodology portable and human-editable;
compilation keeps what reaches a node lean and self-contained. The merge
gate mutates the canonical store (a new version); the next claim issued
picks up the new version, and an in-flight task finishes on the version
it started — core stamps `methodology_version` on the `work_items` /
`draft_tasks` row at claim time (the same number it puts in the claim
payload), so the row's column pins the version even as the active one
rolls over. The node neither echoes nor decides the version: the row is
authoritative.

### The `severity_scale` block

The five severity tags (`critical` / `major` / `moderate` / `minor` /
`nit`) are themselves versioned methodology — a rubric refined as the
corpus reveals where the same node disagrees with itself on similar
patches over time. The rubric is not a static enum baked into core
code.

**Severity is per-finding, not per-check.** Checks describe bug
classes to investigate; severities are assigned to individual findings
at report-finalization time, based on the specific impact and
reachability of *this* instance. The same check (`concurrency`, say)
can produce findings at `critical`, `major`, `moderate`, `minor`, or
`nit` depending on what it found — a reachable AB-BA deadlock vs. a
missing `READ_ONCE` on a bounded flag vs. a misleading comment near a
lock acquisition. The methodology's existing `report_finalization`
prose makes this explicit: "Severity is *impact × reachability*, not
impact alone." Consequently checks and candidate practices do **not**
carry severity fields; only findings do.

Every operation that emits a finding consults the `severity_scale`
block in its compiled methodology slice: review concerns, train-task
comment points, and draft-task proposals all assign per-finding
severities against the active rubric.

**Location and structure.** `severity_scale` is nested under
`report_finalization` in the methodology YAML — it is part of how
findings are tagged in the delivered review. The existing methodology
ships a working rubric (each level has a `tag`, a `meaning` paragraph,
and a `blocks_merge` boolean); the architecture extends each level
with structured criteria and anchors, and adds rubric-wide weighting
and policy fields:

```yaml
# Shape sketch — see common/schema/methodology.schema.yaml for the formal
# definition and core/default-methodology.yaml for the seed values.
report_finalization:
  body: |
    ...the report-finalization procedure, including the modifiers
    (impact × reachability; pre-existing findings are non-blocking
    regardless of tag)...
  severity_scale:
    weighting:           { critical, major, moderate, minor, nit }   # non-negative floats
    uncertainty_rule:    default_to_lower | default_to_higher | require_rationale
    cross_kind_defaults: { series, cross_patch, patch }   # severity_tag or null per scope
    levels:
      - tag: critical    # one entry per tag; critical, major, moderate, minor, nit
        meaning:         # prose summary, preserved verbatim from the seed
        blocks_merge:    # true for everything except nit
        criteria:        # [{ id, description }] — stable IDs referenced by severity_rationale
        anchors:         # [{ id, description, example_patch?, ... }] — empty at seed
```

Five properties of this structure matter for the rest of the system:

- **The existing `tag`, `meaning`, `blocks_merge` fields are
  preserved verbatim.** The `meaning` prose is the human-readable
  summary of each level; a node still reads it. The new structured
  fields are additive — they don't replace what's there.
- **`blocks_merge` rides on each level.** Every level except `nit`
  blocks the merge — a long-standing methodology rule. Findings
  inherit the block-or-not signal from the rubric directly, not from
  separate rule code.
- **`criteria` carry stable IDs.** When a node assigns a severity
  to a finding, its `severity_rationale` references the criteria by
  ID — `matched_criteria: [memory_unsafety, crash_deadlock_normal]`.
  The spot-check audit and statistical workflows verify a node's
  claimed match against the criterion text without parsing free-form
  prose.
- **`anchors` are first-class but start empty.** Calibration anchors
  are how a node converges with itself across patches and across time
  on similar bug classes. They have IDs, optional pointers to real
  patches in the corpus by Message-ID, and a node's rationale
  references them. Anchors accumulate as audit reveals drift; the
  seed methodology ships with empty anchor arrays.
- **`weighting`, `uncertainty_rule`, `cross_kind_defaults` are
  rubric-wide.** Statistical aggregation's
  `severity_weighted_catch_rate` uses the weights; the uncertainty
  rule decides ambiguous cases ("what if a finding fits two levels");
  the cross-kind defaults specify default severity for concerns
  whose `patch_scope.kind` is `series` or `cross_patch` (where the
  scope itself elevates impact).

**The `blocks_merge` derivation rule.** Each level's `blocks_merge`
in the rubric is the default for findings of that severity. A
finding's *effective* `blocks_merge` is overridden to `false` when
the finding's `is_preexisting` flag is true, per the existing
`report_finalization` prose ("Pre-existing findings are non-blocking
regardless of tag"). The override is structural, not a per-review
judgement: a `critical`-tagged finding from the `preexisting-issues`
check carries `is_preexisting: true` and therefore does not block
the merge, while the same severity tag on a patch-introduced finding
does. The merge gate and the report-finalization output both consult
this derived signal, not the rubric value alone.

**Schema validation at import.** `common/schema/methodology.schema.yaml`
enforces:

- `severity_scale` is required under `report_finalization`. The
  existing schema has it as a bare array of levels; the extended
  schema makes it an object with `levels`, `weighting`,
  `uncertainty_rule`, and `cross_kind_defaults` as siblings.
- `levels` contains exactly five entries with the tags `critical`,
  `major`, `moderate`, `minor`, `nit` (the existing enum, unchanged).
- Every level keeps its required `tag`, `meaning`, `blocks_merge`
  and gains required `criteria` (non-empty array) and `anchors`
  (possibly empty array, but the field must be present).
- Every criterion has a unique-within-level `id` matching the slug
  pattern already used by `principles` and `checks`.
- Every anchor has a globally-unique `id`.
- `weighting` is non-negative and monotonically non-decreasing from
  `nit` through `critical`.
- `uncertainty_rule` is one of the bounded enum values.

The schema does **not** cross-reference severity from `checks` or
`candidates` back to the rubric, because checks and candidates do not
carry severity fields. The cross-reference path is the other
direction: pooled `severity_witness` histograms on candidate
practices accumulate from finding-time severity assignments, and the
audit / statistical workflows verify the histogram bins against the
five tags in `levels`.

**Semantic validation at import** (beyond schema):

- Anchor `example_patch` references resolve in `patchsets` — warning,
  not rejection, because anchors can be added before the referenced
  patchset is gathered.
- Every criterion ID referenced from anchors or prompt guidance exists.
- Weighting is strictly monotonic (warning if any two adjacent levels
  have equal weights).

**Export stability.** Round-trip determinism: levels emitted in the
order the existing seed methodology uses (`critical` first, then
`major`, `moderate`, `minor`, `nit` — most severe first, as the
existing seed comment puts it); criteria and anchors within a level
in ID order; keys within objects in a defined order. Operators diff
exports against version-controlled copies; non-deterministic ordering
would generate noise that hides real rubric changes.

**Two paths for rubric refinement.**

- **Import path** (trusted-human). The operator exports the
  methodology, edits `severity_scale` directly in YAML, and
  re-imports. Schema and semantic validation run; the new version is
  written; the next claim picks it up. This bypasses the merge gate —
  import is the established escape hatch for human authoring of
  methodology content, the rubric included.
- **Merge gate path** (AI-proposed). A draft node may propose
  a `revise-severity-scale` change with evidence — typically a
  measured reduction in **temporal drift** of severity assignments
  on similar patches across time windows (the single-node analog of
  inter-node drift). Statistical-gate validation applies; a human
  ratifies.

**One version axis.** Both paths bump `methodology_version`. The
severity_scale block has no separate version of its own; it changes
only through methodology-version bumps. The wire contract therefore
pins everything a node consulted with one number — `methodology_version`
on every claim payload and every completion record. Cross-version
analyses that need to know whether the rubric changed compare the
`severity_scale` block across `methodology_versions` rows in the
canonical store; the API never sees a separate axis.

**Default rubric.** `core/default-methodology.yaml` ships with the
existing `meaning` prose for each level (preserved verbatim from the
seed) plus structural criteria decomposed from that prose. Anchors
start empty and accumulate as the corpus reveals calibration drift.
The default's comments note that the rubric is the starting point
and that anchors should be added as drift becomes apparent before
relying on severity-weighted metrics for transition decisions.

## Auth, enrollment & transport

hone-core is its own **OAuth 2.0 authorization server**. There is no
external identity provider — consistent with hone-core being one
self-contained, deterministic instance.

**Two channels, authenticated differently.**

- The **OAuth / enrollment API** (`/v1/oauth/*`) is the *bootstrap*
  channel. A brand-new node has only three things: the hone-core URL, the
  **fleet secret**, and its Claude API token. The fleet secret — a
  fleet-wide shared secret every node is given — gates the OAuth API and
  **nothing else**: it is what lets a fleet member *begin* enrollment, and
  it keeps the enrollment endpoint (and the operator's approval queue)
  closed to anyone outside the fleet.
- The **main API** (everything else) is reached with an OAuth **bearer
  token**. The token carries the node's identity; it fully replaces the
  earlier per-request shared-secret + client-key header pair.

**Node enrollment — the device authorization grant (RFC 8628).** A node
is *added to the fleet by enrolling itself*, gated by a human:

1. On first start the node calls `POST /v1/oauth/device_authorization`
   (presenting the fleet secret) and is issued a short **user code** and a
   verification URL. It logs them and begins polling.
2. An operator opens hone-core's web UI, enters the user code, reviews the
   node's self-described metadata, names it, and approves — or denies.
   This human step is the trust anchor; it replaces the earlier admin
   "pre-register a client key" call.
3. The node's poll to `POST /v1/oauth/token` then returns an **access
   token**, a **refresh token**, and hone-core's **CA certificate** (see
   *Transport* below). The node persists all three to its data volume and
   is now a fleet member.

Access tokens are short-lived; the node refreshes them with the refresh
token over the OAuth channel. An access token is **opaque** — hone-core
stores only its hash and validates it by lookup, so revoking a node is a
single database update. (hone-core is one instance; a per-request database
lookup is the existing pattern, and opaque tokens keep revocation
immediate and add no cryptographic key management.)

**Transport — a self-provisioned TLS CA.** On **first startup** hone-core
generates, once, a private **certificate authority** and a server
certificate signed by it, and stores them on its data volume (reused on
every later start). hone-core serves HTTPS directly with that certificate
— there is no external TLS-terminating proxy to provision.

The two channels bootstrap trust differently, and that is the reason for
keeping them apart:

- The **OAuth channel** is contacted before the node holds hone-core's
  CA, so the node trusts that first connection on first use; the fleet
  secret is what authenticates the exchange.
- The **CA certificate is delivered to the node during enrollment** (in
  the token response). From then on the node validates the TLS of **every
  non-OAuth call** against that CA. "The node trusts hone-core" is
  therefore itself established through the gated, human-approved
  enrollment — not pre-provisioned out of band.

## Work lifecycle, review output & aggregation

The work-item state machine and claim protocol; the shape of a
review's concerns (with `patch_scope` + `is_preexisting`); the train
task's payload and response; and the deterministic review-level
aggregation that turns per-comment trains into per-patchset coverage,
FP-rate, and redundancy metrics — together with the node-resilience
discipline (indefinite exponential backoff on transient faults,
idempotent result submission, scratch persistence across outages) —
live in [`ARCHITECTURE-WORK-LIFECYCLE.md`](ARCHITECTURE-WORK-LIFECYCLE.md).

## Training sessions & the statistical model

The deliberate side of methodology evolution: operator-triggered
**training sessions** are the only way training happens — the corpus
accumulates inertly (patchsets gathered, prepared, blind-reviewed,
comments stored in `messages`) and a session is the operator's
"convert this evidence into counter movement and proposals" step.
The seven session profiles, the live session-draft advisor with its
warning taxonomy, holdout-headroom in decision-capacity terms, and the
selection algorithm; plus the **statistical model behind transitions** —
the five gates for graduation, the four signals for prune, the
clustering gate for revise, and the cross-cutting machinery (cooldown,
recency weighting, cold rounds, spot-check audit) — all live in
[`ARCHITECTURE-TRAINING.md`](ARCHITECTURE-TRAINING.md).

## Self-honing across the components

The methodology is global, so the comment → methodology loop spans both
components. Each part runs where its nature dictates:

| Step | Runs on | Why |
| --- | --- | --- |
| Characterise the patchset for the corpus | **prepare node** | needs AI metadata extraction against email + tree; the corpus-quality foundation |
| Produce the blind review for a patchset | **review node** | needs AI; the deliverable |
| Compare our review against one reviewer comment; decompose into points; classify per-point matches against in-scope concerns; (in pool role) propose new candidates or revisions from verified misses | **train node** | needs AI; the per-comment training step (one train per session-selected `(patch, comment)` pair) |
| Aggregate a session's per-comment train results for one patchset into review-level metrics (coverage, FP rate, redundancy, per-concern verdicts) and write a `review_evaluations` row per `(patchset, session)` | **hone-core** | deterministic aggregation over per-comment train results; no AI needed once trains have produced their structured outputs |
| Apply per-candidate counter updates; check graduation-eligibility and threshold prunes; run bootstrap CI / TOST / CUSUM / Bayesian posterior over session-aggregated evidence | **hone-core** | pure arithmetic over the pooled counters |
| Cluster revision proposals across sessions for Trigger 3 | **hone-core** | embedding + DBSCAN are deterministic; arithmetic over counters |
| Judge redundancy / coherence; **author** the change — a bounded, candidate-anchored adaptation | **draft node** | needs AI *and* the whole-corpus view a single review never has |
| Disposition a methodology proposal | **a human** | the merge gate (below) |

A *prepare* is the corpus characterisation that gates everything else;
a *review* is the blind deliverable; a *train* is the per-comment
training step against that review, created by the session orchestrator
at the moment the operator decides this evidence is worth converting
into methodology change. Splitting them lets the corpus accumulate
continuously while training happens in deliberate, structured passes.
Preparation is split out because it is a different kind of work
(metadata extraction, not patch review) and because deferring it until
review-time would conflate "this patchset is unreviewable" with "this
patchset is uncharacterisable."

## The merge gate & methodology governance

The methodology-change ratification flow: the four operator
dispositions (Accept / Defer / Reject / Return for redraft), the
`methodology_proposals` queue, the draft-task trigger pipeline with
its six eligibility gates, batching rules, and disposition feedback —
together with the five-layer defence that keeps a bad node from
corrupting the methodology (typed proposals, mechanical validation,
statistical-gate enforcement, the informed human gate, node
reputation) — live in
[`ARCHITECTURE-MERGE-GATE.md`](ARCHITECTURE-MERGE-GATE.md).

## REST API

The hone-core↔node wire contract is specified in **`API.md`** — base
path `/v1`. It has two parts: the **OAuth / enrollment** endpoints
`POST /v1/oauth/device_authorization` and `POST /v1/oauth/token`
(fleet-secret-gated); and the **work API** — `POST /v1/claims` (claim
a task, returning a self-contained payload with the patches, the
compiled methodology slice, and any session metadata baked in),
`…/heartbeat`, `…/result` (the completion record, with a top-level
`task_type` discriminator), bearer-token-authed. There is no separate
`GET /v1/methodology` or `GET /v1/patchsets/…/blob` — the claim
payload carries what the task needs.

Operator-facing endpoints (`/sessions/draft`, the session-progress
endpoints, the merge gate, the node-enrollment approval) are **human
web UI** on hone-core, not node-facing APIs.

## Today vs. target

**In the repo today** — the design (this doc and its companions) plus a
substantial implementation.

hone-core:

- `common/schema/` — the shared JSON Schemas: `methodology.schema.yaml`
  (with `severity_scale` already the extended object — `levels` +
  `weighting` + `uncertainty_rule` + `cross_kind_defaults`, per-level
  `criteria` / `anchors`) and `completion-record.schema.yaml` (all four
  task branches, `self_review_record`, scoped `patch_scope` +
  `is_preexisting` concerns, the structured `train_record`).
- `core/default-methodology.yaml` — the canonical methodology, imported
  on first run as version 1; its `report_finalization.severity_scale`
  ships the extended structure with per-level criteria.
- `core/core_db.py` — the full `_SCHEMA_V1`: every table the data model
  describes, including `patchset_metadata`, `review_evaluations`,
  `methodology_candidates` (with both `severity_witness` histograms),
  `methodology_proposals`, `eligibility_flags`, `draft_tasks`, and the
  three training-session tables, plus the CRUD helpers for each.
- `core/main.py` + `core/api.py` — the FastAPI app and the full `/v1`
  wire contract: the OAuth device-grant endpoints, `POST /v1/claims`
  (self-contained per-task payloads built for all four task types),
  `…/heartbeat`, `…/release`, `…/result`, and the compiled methodology
  slice.
- `core/ui.py` + `core/templates/` + `core/static/` — the server-rendered
  operator UI (Queue, Nodes, Enroll, Settings, patchset and work-item
  detail) on vendored AdminLTE / Bootstrap / HTMX assets, gated by HTTP
  Basic auth against `HONE_ADMIN_TOKEN`.
- `core/gather.py` + `core/gather-modules/{gather_api.py, lore.py}` — the
  GATHER supervisor (per-source asyncio tasks, cursor resume, stall
  cancel) and the `lore` source.
- `core/methodology_format.py` + the compile step in `api.py` —
  methodology import (schema-validated), export (deterministic YAML), and
  per-task compile.
- `core/tls.py` (first-start CA + server certificate),
  `core/runtime_config.py` + `core/config.py` (the layered tunable config).
- `core/Dockerfile` + `core/docker-compose.yml`,
  `node/Dockerfile` (with `perl`, for `get_maintainer.pl`) +
  `node/docker-compose.yml` + `node/docker-compose.override.yml` — the
  per-tier images, compose deployments, and the local-testing override.

hone-node:

- `node/runner.py` + `node/client.py` — the claim loop (claim → work →
  submit, 204 poll, indefinite backoff, idempotent submit) and the
  device-grant enrollment with CA-pinned TLS and token refresh.
- `node/tasks.py` `handle_prepare_task` — implemented: it runs the
  deterministic **Tier-0** phase (`node/cgit.py` multi-tree base
  resolution, `node/maintainers.py` `get_maintainer.pl` runner,
  `node/tier0.py` field assembler) and overlays the **Tier-1** LLM
  judgment phase (`node/ai.py` `call_claude`); see
  [`ARCHITECTURE-PREPARE.md`](ARCHITECTURE-PREPARE.md).
- `node/refrepo.py` — the reference kernel repo with the `base_tree`
  fetch hint.

**To build** — the remaining AI deliverables and the self-honing
machinery that converts train results into methodology change. Several
items below already have their DB tables and CRUD primitives in place
(noted *schema present*); what is missing is the logic that drives them.

*Node AI handlers*

- `handle_review_task` / `handle_train_task` / `handle_draft_task` in
  `node/tasks.py` still raise `NotImplementedError`. (`handle_prepare_task`
  is done — see *In the repo today*.)

*The comment → methodology loop (hone-core)*

- Per-candidate counter updates on train receipt — `bump_candidate` /
  `bump_severity_witness` exist, but `POST /v1/claims/{id}/result` does
  not yet call them (a `pass` / TODO on the train path), so the pooled
  counters and both `severity_witness` histograms stay unpopulated.
  *(schema present.)*
- Review-level aggregation — the `review_evaluations` table and its
  read/write helpers exist, but the per-`(patchset, session)` trigger and
  the aggregation pass (per-concern verdicts; coverage / FP-rate /
  redundancy; the `unmatched_preexisting` verdict and the
  `preexisting_unmatched_count` rollup) are not implemented. *(schema
  present.)*
- The draft-task trigger pipeline — `eligibility_flags` / `draft_tasks`
  and their CRUD (set / clear / suppress / defer-watermark, enqueue,
  claim, debounce) exist, but the six eligibility-gate computations
  (`graduate_eligible`, `prune_ineffective_eligible`,
  `prune_redundant_eligible`, `consolidate_eligible`, `revise_eligible`,
  `severity_scale_revise_eligible`) and the counter-update / session-
  `analyzed` re-evaluation hooks that set the flags are not. *(schema
  present.)*
- The statistical-aggregation module — bootstrap CI, TOST, CUSUM,
  Bayesian posterior, ICC, revision clustering (embedding + DBSCAN), and
  FDR correction. Nothing computes these yet; the draft payload's
  evidence summaries are placeholders.
- The training-session orchestrator — the three session tables and their
  CRUD exist, but the stratified selection solver, the `draft → ready`
  materialisation that creates one train work-item per `(patch, comment)`
  pair (with the comment trainability filters), and the
  `in_progress → complete → analyzed` lifecycle driver are not built.
  *(schema present.)*
- The merge gate — `methodology_proposals` and its add / list / decide
  helpers exist (Reject → suppress, Defer → watermark are wired at the DB
  layer), but the version bump + change application on Accept, layer-2
  mechanical validation, layer-3 statistical-gate validation, and
  node-reputation scoring are not. *(schema present.)*
- The re-preparation policy job — the periodic re-enqueue of `prepare`
  for heuristic / stale `patchset_metadata` rows (the table already
  records `methodology_version` / `node_tree_revision` / `mode`).

*Operator surfaces*

- The session-draft UI (profile picker, plan preview, advisory panel,
  live re-solve via HTMX), the session-progress UI, the merge-gate UI
  (the `methodology_proposals` queue with the session-lineage and
  parent-review coverage / FP-rate evidence panel), and reporting pages —
  none of these operator surfaces exist yet.
- The spot-check audit workflow — a sampler over prepared metadata, train
  results, and `review_evaluations`, the disposition UI, and the feedback
  path into node reputation / health metrics. Not started.
- The session-based operator login — the UI is gated by HTTP Basic auth
  against `HONE_ADMIN_TOKEN` today; the richer session-based login is the
  target.

*Further refinements (after the machinery lands)*

- The `revise-severity-scale` recommendation type from draft tasks, with
  its temporal-drift evidence requirement and merge-gate integration.
- The broader severity rollout: `severity_rationale` fields referencing
  `severity_scale` criterion / anchor IDs on every finding;
  per-methodology-version `severity_witness` histograms (forked at the
  version boundary); severity-weighted aggregation metrics; the
  version-pinned rubric in the spot-check audit UI.
- FP-rate reconciliation in the prune utility formula — replace the
  estimated `λ` penalty with measured per-review FP rates from
  `review_evaluations` — and review-intensity weighting of FP rate
  (weight by comment count, or exclude below a minimum-comment
  threshold).
- The prepare Tier-2 enrichment move — relocate `applies_cleanly` /
  `churn_ratio` / `file_activity` / `fixes_verified` from prepare to the
  review record, landed atomically with `handle_review_task` (see
  [`ARCHITECTURE-PREPARE.md`](ARCHITECTURE-PREPARE.md) → *Migration
  sequence*).

Note: the `patch_scope` + `is_preexisting` concern shape, the four-branch
completion-record schema, and the extended `severity_scale` block — all
listed as "to build" in earlier revisions — are now present in
`common/schema/` and `core/default-methodology.yaml`; what remains for
them is the consuming logic above (the review operation that emits them
and the aggregation that reads them).

**Open / not yet specified:** see `API.md` → *Open* for unresolved
wire-contract corners (fleet-secret upgrade, refresh-token rotation
semantics, listing endpoints).
