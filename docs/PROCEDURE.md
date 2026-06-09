# hone — operator runbook

The operator's view of running hone. The model lives in `ARCHITECTURE.md`;
the wire contract is `API.md`; the data sources are `SOURCES.md`; the
methodology is `core/default-methodology.yaml` validated by
`common/schema/methodology.schema.yaml`. This doc is the short procedural companion —
what an operator actually does to deploy, run, and disposition the system.

The system runs itself once configured. The operator's recurring actions
are few: first-run setup, enrolling nodes, watching the queue,
dispositioning proposals at the merge gate, launching the occasional
training session, and tuning runtime config. Each is below.

## First-run setup

Deploy hone-core with the deployment env vars (`HONE_HOSTNAME`,
`HONE_FLEET_SECRET`, `HONE_ADMIN_TOKEN`, …) — see `DEPLOYMENT.md` for
the full env-var contract and the `docker compose up` invocation. The
service comes up clean with no lore archive present — `lore.list()` is
a no-op until one exists — so provision the archive before useful
gathering begins.

```sh
# Interactive: the bundled helper. Uses lore.since_date as
# --shallow-since so the download is bounded to the window gather
# walks (a few hundred MB to ~1 GB for a recent floor).
python3 core/gather-modules/lore.py clone
```

For unattended deployments, set `HONE_LORE_AUTOCLONE=1` in `core/.env`
and hone-core runs the same clone in a background task on startup.
Gather picks up the archive on the next supervisor tick either way —
no restart needed.

After the archive is in place, open the Settings page and **enable the
list tags** you want gathered. Without any tag enabled the filter is
off and every list is in scope — useful for an experiment, but real
triage means ticking the lists you care about.

**Training does not start automatically.** Gather, prepare, and review
all run continuously once enabled, so the corpus accumulates and
hone-node produces blind reviews unattended. But the methodology never
advances until you launch a **training session** (Sessions page →
session-draft). Comments land in the corpus inertly; a session is the
operator's deliberate "convert this evidence into counter movement
and proposals" step. See *Training sessions* below.

## Enrolling a node

Start a hone-node container with the fleet secret and the hone-core URL.
The node prints a verification URL and a user code; open `/enroll` (or
use "Pair a new node" on `/nodes`), paste the code, review the node's
self-description (its declared `task_types` and `node_name`), and
approve. Looking the code up **pairs** the enrollment to you: the
pending row appears only on your `/nodes` page, and only you (or the
admin) can approve or deny it. From that point the node enrolls itself:
bearer tokens are issued, the CA certificate is handed over, and the
node begins claiming.

A node you pair is **yours**. It serves the work you request (your
Request-review clicks) and nothing else until you enable *Also handle
system work* on its detail page, which lets it fall back to the system
pool — gather-enqueued prepares, session trains — once your own queue is
empty. The config-token admin approving an unpaired code creates an
ownerless system node instead, which serves the pool from the start.
Every approved node is visible to every user on `/nodes`; delete and
configuration are owner-only, and the admin can reassign ownership from
the node detail page. Requesting work while owning no active node is
fine — the system pool picks your items up.

A node can specialise. Different `task_types` have different
requirements:

- `prepare` needs a local kernel git repo with tree-fetch capability —
  the prepare task owns base-commit resolution.
- `review` and `train` need only the Claude API token.
- `draft` receives whole-corpus pooled stats and eligibility-flag
  snapshots — context-window pressure is higher; reserve for nodes
  with more capable models.

Default `task_types` when none declared:
`["prepare", "review", "train", "draft"]` (every task type).

## Watching the queue

The home page (`/`) shows the work queue with a two-axis chip filter —
type (prepare / review / train / draft) × state (claimable / claimed /
completed / unappliable / deferred). State is a lifecycle class, not a
task-type outcome — every successful outcome (`prepared` / `reviewed` /
`trained` / `drafted`) lands on `completed`. The queue self-heals: a
claim without a heartbeat reclaims when the lease elapses; a node that
goes down has its work re-offered automatically.

**Normal**: items flow claimable → claimed → completed. Occasional
`unappliable` outcomes (patch doesn't apply to its declared base —
recorded with a reason; no review produced; the patchset doesn't
re-arm). Occasional `deferred`
outcomes (base unobtainable, will retry — the work-item re-arms
after the lease elapses).

**Problem signals**:

- The same item repeatedly flipping to `deferred` → something is
  wrong upstream, usually base-tree access or patch reconstruction.
- Reviews enqueued but no prepare completed for the patchset → the
  prepare task is blocking; check the prepare node's logs.
- `claimed` items stuck for hours with no heartbeat → node is dead
  or unreachable; the reclaim fires when the lease elapses.
- Eligibility-flag count growing on the merge gate page but no
  draft tasks running → an outstanding draft task is blocked; check
  the queue for `draft` items in `claimed` state.

The operator queues each review by hand: once a patchset has a
`patchset_metadata` row from the prepare task, its detail page offers a
**Request review** button (`POST /review-requests/<root>`) that enqueues
the review work-item. Review is **not** auto-enqueued — that keeps a
gather run from flooding the queue. Train work-items are not auto-enqueued
either: training is session-driven (Sessions page → session-draft).

## The merge gate — dispositioning proposals

Methodology proposals arrive when hone-core's deterministic eligibility
gates fire draft tasks (see `ARCHITECTURE-MERGE-GATE.md` → *The merge gate →
Draft-task trigger logic*). The merge gate page shows pending proposals
with an evidence panel for each:

- The proposal's text and recommendation type — `graduate` /
  `prune-redundant` / `prune-ineffective` / `consolidate` / `revise` /
  `revise-severity-scale`.
- The candidate's pooled stats with the two parallel
  `severity_witness` histograms — the introduced-vs-preexisting split
  is visible directly.
- Statistical-gate evidence (bootstrap CI, ICC, TOST, CUSUM,
  Bayesian posterior — varies by recommendation type).
- Parent reviews' coverage and FP rates from `review_evaluations`.
- Session lineage — which training sessions and which holdout
  patchsets supplied the evidence.
- Originating verified misses.
- The submitting node's identity.

Four dispositions per proposal:

- **Accept** — apply it; methodology version bumps.
- **Defer** — sound but thin; records a watermark; not re-surfaced
  until evidence grows past it by the configured factor.
- **Reject** — permanent no, scoped to the `(recommendation, subject)`
  pair; the candidate itself stays active but the pairing is
  suppressed.
- **Return for redraft** — recommendation stands, wording doesn't;
  carries a feedback note; node re-tasks.

The redraft cap (default 3) bounds the loop. The fourth time the
operator would Return-for-redraft, that option is withheld and they
must Accept (hand-editing the payload in the UI if needed), Defer, or
Reject. Staleness redrafts (methodology version moved underneath the
proposal) do not count toward the cap.

The eligibility-flag count indicator on the merge gate page shows the
total currently-set flags; at zero, no proposals are pending or in
flight and the methodology is settled.

## Training sessions

Sessions are operator-triggered batch evaluations that produce
statistically grounded evidence for specific candidates or for the
methodology as a whole. The Sessions page (`/sessions`) lists drafts,
active, complete, and analysed sessions; the session-draft page
(`/sessions/draft`) is where new sessions are assembled.

Seven profiles cover the common cases:

- **Standard** — routine methodology advancement.
- **Targeted graduation** — confirm one candidate is ready to
  graduate.
- **Targeted prune** — confirm one check is no longer useful.
- **Coverage repair** — focus on strata where coverage has dropped.
- **Holdout refresh** — replenish the holdout pool.
- **Exploratory** — broad-net session to surface unknown candidates.
- **Custom** — operator-defined slice.

The session-draft page is a live solver: pick a profile, the plan
preview shows what gets included, the advisory panel surfaces warnings
sorted by severity (Block / Warn / Note / Tip). The holdout-headroom
indicator shows how many future decisions the current holdout pool
supports — when it gets low, launch a Holdout refresh session.

When to launch a session: on a regular cadence to keep the methodology
advancing (Standard), or when a specific candidate has accumulated
enough evidence that statistical confirmation is the next step
(Targeted graduation / prune). Coverage repair fires when
`review_evaluations` shows a drop in coverage on identified strata.

## Tuning runtime config

The Settings page edits runtime config without a restart. The knobs
most commonly touched, and the symptom each addresses:

- `gather.interval_seconds` (default 600) — re-spawn cadence per
  source. Lower if you need fresher data; higher to back off if the
  source is being hammered.
- `claim_lease_seconds` — how long a claim is exclusively held without
  a heartbeat. Lower if nodes routinely finish faster; higher for
  slow models on large patches.
- `heartbeat_interval_seconds` (default 300) — how often a working
  node extends its lease.
- `draft_batch_max` (default 10) — maximum eligibility flags per
  draft task. Lower if proposals overwhelm the operator at the merge
  gate; higher if eligibility-flag backpressure builds.
- `redraft_cap` (default 3) — maximum quality-feedback redrafts per
  proposal lineage.
- `defer_growth_factor` (default 0.20) — counter growth past the defer
  watermark required before a deferred proposal re-surfaces.

Token TTLs (`access_token_ttl_seconds`, `refresh_token_ttl_seconds`)
are tunable but rarely changed.

## Troubleshooting

| Symptom | Likely cause | First check |
|---|---|---|
| Patchsets gathered/prepared but no reviews | Review is operator-triggered, not automatic | Request a review from the patchset detail page (prepare must be complete) |
| Patchsets not gathering at all | List-tag filter has no enabled tags matching | Settings → enable relevant list tags |
| Reviews enqueued but stuck at `claimable` | No nodes accepting `review` work | Node health; node's declared `task_types` |
| "Request review" button stays dimmed | Prepare not yet complete for the patchset | Prepare node logs and `task_types` |
| Many `unappliable` outcomes | Patch reconstruction issue or wrong list scope | Spot-check `unappliable` records' `reason` fields |
| Many `deferred` outcomes | Base-tree access issue on prepare/review nodes | Prepare or review node git fetch logs |
| Eligibility-flag count growing, no draft running | Outstanding draft task blocked | Queue → `draft` items in `claimed` state |
| Persistent high `fp_rate` in `review_evaluations` | Candidates need pruning | Wait for prune-eligibility, or launch Targeted prune session |
| Holdout-headroom indicator low | Holdout pool depleted | Launch a Holdout refresh session |
| Methodology version not advancing despite Accepts | Disposition didn't fully commit | Merge gate logs; `methodology_versions` table |
| Sessions stuck in `draft` | Operator hasn't committed | Session-draft page; resolve advisory blocks |
| Long lag between counter crossing and proposal arrival | Normal debounce — draft tasks are holistic | Wait the next draft cycle |
