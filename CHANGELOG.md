# Changelog

All notable changes to hone are recorded here, in the
[Keep a Changelog](https://keepachangelog.com) format.

## Versioning

hone uses [Semantic Versioning](https://semver.org). Until a `1.0.0`
release the project is **pre-stable**: a minor (`0.X`) bump may carry
breaking changes — wire-contract changes, schema changes, data-model
changes — and patch (`0.x.Y`) bumps are reserved for fixes that
preserve the contract.

The version constant lives in
[`common/version.py`](common/version.py); the git tag `v<version>`
mirrors it. Bump them together with the entry below on every release.

## [Unreleased]

## [0.3.0] — 2026-06-10

hone becomes multi-user. Session accounts with per-user roles replace
the shared Basic-auth gate, kernel developers upload their own patch
series for AI review and follow them on a personal dashboard, nodes
gain owners so user-requested work runs on the requester's own worker
and token budget, and the operator surfaces grow matching depth
(queue scoping and origin, pipeline state, fleet view, deferral
parking).

Pre-stable: this release carries schema migrations v4–v6 (applied
automatically on first start) and removes the operator UI's HTTP
Basic auth in favour of browser sessions; it is not backward
compatible with 0.2.0 operator workflows.

### Added

- **User accounts and session login**
  ([`core/auth.py`](core/auth.py), [`core/ui.py`](core/ui.py)):
  email + password (Argon2) and Google SSO sign-in, self-service
  registration with admin approval, profile / password settings, and
  a grantable per-user **admin** permission managed from the Users
  screen. The config-token admin remains the bootstrap path.
- **Maintainer user type**: maintainers (and admins) browse the
  gathered corpus — renamed **Corpus** — and act on any patchset; the
  per-patchset action buttons are maintainer-gated for everyone else.
- **Patchset uploads — "review my series"**
  ([`core/upload.py`](core/upload.py)): upload `git format-patch`
  files, a series mbox, or a pasted diff; a parse-preview / confirm
  flow with series-completeness validation; uploaded series are kept
  apart from the corpus and are never training data. Uploaders can
  delete their own uploads, a re-upload with changed content re-runs
  the pipeline (stale prepare metadata and AI review are dropped),
  and re-uploaded iterations of the same work link into one chain
  (migration v6) shown as a single dashboard row.
- **My patchsets dashboard**: the signed-in developer's series as a
  pipeline view — uploaded → preparing → prepared → reviewing →
  reviewed, failure states surfaced — one click from the review.
- **Per-user nodes** ([`core/core_db.py`](core/core_db.py),
  [`node/`](node/)): enrolled nodes carry an owner; user-requested
  work claims to the requester's own nodes first, with an opt-in
  fallback to the system pool (`handles_system`).
- **Claude token budgets** ([`node/ai.py`](node/ai.py)): opt-in
  daily / weekly budgets on the node, usage and exhaustion surfaced
  in the node health views.
- Claude CLI lifecycle: an update check before each prompt, the CLI
  version reported in node health, and the CLI build stamped into
  every completion record.
- Operator UI depth: a Request-prepare button and a derived Pipeline
  field on the patchset detail page; admin cancel for queued and
  deferred work items (including from the patchset page); work-item
  origin (system vs user) on the queue and detail pages; queue
  origin / started / completed columns with per-user scoping; a
  threaded patchset message view; the node fleet as one table with
  idle nodes included; timestamps in the viewer's local timezone.

### Changed

- **Settings split**: Site settings (admin-only) and User settings,
  with the theme toggle in the user menu; the work-item re-arm
  buttons are admin-only; the navbar brand now lands on My patchsets.
- **Schema**: the unreleased migrations collapsed into v4 (users,
  node ownership, work-item origin, patchset origin, deferral
  bookkeeping); the gather now parses `[PATCH vN]` series versions
  (migration v5 backfills); `users.auth_provider` constrained by a
  CHECK.
- The patchset pipeline cluster shows only the actions pertinent to
  the current state, and the work queue sorts by start date.
- The lore archive fast-forwards before each gather cycle.
- Endlessly-deferring work items back off exponentially and park
  after a cap instead of retrying every lease window.
- hone-core uses one SQLite connection per thread.
- hone-node pauses claiming when free disk falls below a floor,
  bounds the reference repo with idle gc + worktree sweeps, and the
  Claude CLI turn-timeout default is now 3600s.
- Docker: backups are no longer baked into the hone-core image.

### Fixed

- Lease-expired claims no longer show as in flight.
- A graceful-shutdown hang, and the auth-page layout broken by an
  AdminLTE `.card-title` float.

### Security

- Explicit CSRF tokens on all UI POSTs.
- Failed logins throttled per IP; login and register no longer leak
  account existence; registration email validation tightened.
- Secure-by-default session cookie; hone-core refuses to start
  without a session secret; revocation and grants take effect on the
  user's next request.
- Google OAuth `redirect_uri` pinned to the configured public URL.

## [0.2.0] — 2026-06-03

hone-node grows from a skeleton into a working two-task AI worker — a
deterministic-then-LLM **prepare** characterization and an agentic,
tree-rooted **review** — with the resilience to survive AI and network
failures without losing work. The operator UI gains fleet, node,
work-item, and patchset depth, and the review methodology matures.

Pre-stable: this release carries schema and wire-contract changes (see
**Changed**); it is not backward compatible with 0.1.0 data or nodes.

### Added

- hone-node **prepare** task — tiered patchset characterization
  ([`node/tasks.py`](node/tasks.py)): a Tier-0 deterministic resolver
  (a cgit client + named-trees registry [`node/cgit.py`](node/cgit.py),
  a `get_maintainer.pl` runner, base resolution with a
  tip-at-submission fallback for no-base series) runs **before** the
  LLM, which then characterizes tree-free with no tools so it can't
  probe for a kernel tree.
- hone-node **review** task ([`node/tasks.py`](node/tasks.py),
  [`node/refrepo.py`](node/refrepo.py)) — an agentic, tree-rooted
  review driven by the methodology: builds the reference repo at
  bootstrap, applies the whole series into a detached worktree at the
  base, and reviews base-less patchsets against a tip-at-submission
  base.
- Claude **CLI backend** ([`node/ai.py`](node/ai.py)) for Claude Code
  subscribers without API billing, alongside the SDK backend and as the
  default; an `ANTHROPIC_MODEL` knob, a streamed `stream-json` transcript
  captured as an assistant/tool trace, a tunable `HONE_CLI_TIMEOUT`
  watchdog, and non-essential outbound traffic disabled.
- Node resilience: release a claim on a non-transient abort, submit a
  schema-valid failure record instead of crashing, a keep-alive that
  heartbeats during long task runs, periodic health reporting to the
  operator, and a hard tool denylist that keeps a constrained turn from
  shelling out or spawning subagents.
- Operator UI: top-bar navigation with a fleet-pulse chip and throughput
  sparkline; per-node and per-work-item detail pages; a patchset-browser
  home page; a thread viewer with header stripping and diff highlighting;
  AI-review concerns rendered inline as patch diffs; deferred / unappliable
  badges that re-arm a work item; node hard-delete behind a themed modal.
- Methodology: a review operation guidance with Stage C consolidation
  and JSON-only output enforcement; version-aware export / import; `%NAME%`
  and `%COMPLETION_RECORD_SCHEMA_JSON%` substitution into the compiled
  document; `mdformat` prose canonicalization.
- Core: DB backup / restore scripts (with a `--db-only` option) and
  duplicate node-name rejection.

### Changed

- **Review is no longer auto-enqueued** — it is a per-patchset manual
  trigger; the review-request route was renamed and its 404 path fixed.
- **Operator web UI now requires authentication** (HTTP Basic, the admin
  token as the password); it must never be exposed unauthenticated.
- **Schema / data model**: the completion record is forward-prepped for
  the Tier-2 enrichment quartet, tree-only sub-objects may be null in
  heuristic mode, and the compiled methodology now carries the JSON
  schema at its tail (the hand-maintained shape was removed).
- A deferred work item is re-armed to claimable once its lease elapses,
  or immediately from its badge.
- `usage.input_tokens` now sums the cached-input portions so a
  cache-served prompt is not under-counted.

### Fixed

- Queue pagination broken by the `X-Queue-Version` 204 short-circuit.
- A node now advertises only its implemented task types, so an
  unsupported claim can't crash the claim loop.
- Self-signed-cert and API-transport errors are classified as
  `connection` (transient) so they back off and retry instead of
  surfacing as fatal.
- `get_maintainer` person entries omit the name when none is returned
  rather than emitting an empty field.

## [0.1.0] — 2026-05-25

Initial release of hone — a service that reviews Linux kernel patchsets
against a codified methodology and self-hones the methodology against
maintainer comments on the originating mailing-list threads. A runnable
hone-core + a runnable hone-node skeleton, the operator web UI, the
REST wire contract, the JSON schemas, the SQLite data model, and a
unit-test suite that pins every contract.

### Added

- Design docs under [`docs/`](docs/):
  [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) plus the
  [work-lifecycle](docs/ARCHITECTURE-WORK-LIFECYCLE.md),
  [training](docs/ARCHITECTURE-TRAINING.md), and
  [merge-gate](docs/ARCHITECTURE-MERGE-GATE.md) spin-offs; the REST wire
  contract ([`API.md`](docs/API.md)); the build-and-run model
  ([`DEPLOYMENT.md`](docs/DEPLOYMENT.md)); the operator runbook
  ([`PROCEDURE.md`](docs/PROCEDURE.md)); the gather sources
  ([`SOURCES.md`](docs/SOURCES.md)).
- Methodology and schemas:
  [`core/default-methodology.yaml`](core/default-methodology.yaml) —
  the canonical kernel-patchset review methodology — plus
  [`common/schema/methodology.schema.yaml`](common/schema/methodology.schema.yaml)
  and
  [`common/schema/completion-record.schema.yaml`](common/schema/completion-record.schema.yaml).
- hone-core data layer: [`core/core_db.py`](core/core_db.py) — the
  SQLite schema and helpers for the seven data-model groups (corpus,
  reviews, work queue, methodology, training sessions, eligibility +
  draft tasks, identity), WAL mode, foreign keys on, forward-only
  migrations keyed by `PRAGMA user_version`.
- hone-core application: [`core/main.py`](core/main.py),
  [`core/config.py`](core/config.py),
  [`core/runtime_config.py`](core/runtime_config.py) — the FastAPI
  lifespan (bootstrap methodology, self-provision a TLS CA + server
  cert via [`core/tls.py`](core/tls.py), load the overlaid
  `config.yaml`, start the gather supervisor, serve HTTPS).
- REST v1 ([`core/api.py`](core/api.py)): OAuth 2.0 device-authorization
  enrollment (RFC 8628) gated by the fleet secret, opaque bearer
  tokens for the main API, and one atomic `/v1/claims` that returns a
  self-contained per-task payload (compiled methodology slice, patches,
  prior review / thread context, eligibility flags) with the
  methodology version stamped on the work_items / draft_tasks row at
  claim time.
- Operator web UI ([`core/ui.py`](core/ui.py),
  [`core/templates/`](core/templates/),
  [`core/static/`](core/static/)): server-rendered Jinja2 + Bootstrap 5
  + HTMX with an AdminLTE 4 shell. Pages: work queue (type/state chip
  filters, paginator, auto-polling pane sorted by most-recent
  activity, clickable rows that round-trip the queue state through
  `?back=`), per-patchset detail (corpus + prepare-derived metadata +
  ai_review concerns + work-item history + thread), node fleet +
  device-grant approval, device-grant enrollment landing, and Settings
  (runtime-config form, list-tag filter, lore-clone status panel).
- GATHER supervisor ([`core/gather.py`](core/gather.py)) +
  gather-module framework ([`core/gather-modules/`](core/gather-modules/)):
  one asyncio task per enabled source with a liveness heartbeat, a
  contiguous-watermark advance that never silently skips a ref, and
  the `lore.kernel.org` public-inbox source.
- hone-node worker tier ([`node/`](node/)): a containerised AI worker
  that enrols itself, claims tasks, and submits structured completion
  records. [`runner.py`](node/runner.py) — the claim loop with
  transient-failure backoff and SIGTERM-clean shutdown;
  [`client.py`](node/client.py) — the v1 REST client with OAuth
  device-grant enrollment, bearer-token main API, 401-refresh-retry,
  and persisted CA trust; [`tasks.py`](node/tasks.py) — the four
  task-type handlers (dispatch + payload shape complete; the actual
  Claude-API call is `NotImplementedError` pending AI integration);
  [`refrepo.py`](node/refrepo.py) — the reference-tree manager (one
  local kernel repo per node, base commits fetched serially on demand,
  detached worktrees per task, repo size bounded by
  `git gc --prune=now`).
- Container images and compose deployments for both tiers
  (`core/{Dockerfile, docker-compose.yml}`,
  `node/{Dockerfile, docker-compose.yml, docker-compose.override.yml}`).
- Unit-test suite ([`tests/`](tests/)): 332 tests covering the
  completion-record schema, REST v1 (claims + submissions + OAuth
  endpoints), core_db (every table group), GATHER and the `lore`
  source, hone-node (client + backoff + dispatch), the operator UI
  (queue + patchset detail + settings + enrollment), and the
  infrastructure layer (TLS, runtime config, version). A
  [`conftest.py`](tests/conftest.py) gate fails the run unless the
  test interpreter matches the Python version the Dockerfiles pin.
- Cross-tier version constant
  ([`common/version.py`](common/version.py)).
