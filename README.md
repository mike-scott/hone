# hone

A service that reviews Linux kernel patchsets using a codified methodology.
Over time, the service measures itself against external review signal —
coming from the maintainer comments that land on each patchset's
mailing-list thread — and uses those metrics to self-hone the methodology.

Built from two deployable components, split by one governing rule:

> **All AI runs on worker nodes; hone-core is purely deterministic.**

This README is the system at a glance — the two-component split,
the data model, and the UI. The authoritative documents it draws on:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the system model.
  Subsystem detail docs spin off from it:
  [`ARCHITECTURE-WORK-LIFECYCLE.md`](docs/ARCHITECTURE-WORK-LIFECYCLE.md)
  (claim protocol, review output, train task, review-level aggregation,
  node resilience),
  [`ARCHITECTURE-TRAINING.md`](docs/ARCHITECTURE-TRAINING.md)
  (training sessions and the statistical model behind transitions),
  [`ARCHITECTURE-MERGE-GATE.md`](docs/ARCHITECTURE-MERGE-GATE.md)
  (the merge gate and node-guarding), and
  [`ARCHITECTURE-PREPARE.md`](docs/ARCHITECTURE-PREPARE.md)
  (the target tiered prepare design — a deterministic cgit phase and an
  LLM-judgment phase, confining the kernel tree to `review`).
- [`docs/API.md`](docs/API.md) — the hone-core ↔ node REST wire contract
  (`/v1`).
- [`docs/SOURCES.md`](docs/SOURCES.md) — the gather-module framework and
  the canonical `lore` data source (resume cursors, the list-tag filter,
  dedup).
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — the build-and-run model
  (images, compose, volumes, env-var contract).
- [`docs/PROCEDURE.md`](docs/PROCEDURE.md) — the operator runbook.
- [`common/schema/`](common/schema/) — the JSON Schemas shared across
  both tiers (`methodology.schema.yaml`,
  `completion-record.schema.yaml`).
- [`core/default-methodology.yaml`](core/default-methodology.yaml) —
  the canonical kernel-patchset review methodology, imported on
  hone-core's first run as version 1.

## Two components

Split by the governing rule above — see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md):

- **hone-core** — one FastAPI + SQLite(WAL) instance. Runs the
  supervised GATHER loop, serves the REST API, performs the mechanical
  (judgement-free) self-honing arithmetic, orchestrates training
  sessions, serves the operator web UI, and owns `hone.db`. No AI, no
  kernel repo.
- **hone-node** — a containerized worker that claims tasks from
  hone-core, does the AI work, and reports back. Four task types:
  **prepare** (characterise a patchset for the corpus), **review**
  (blind-review one patchset), **train** (measure our review against
  one maintainer comment), and **draft** (author methodology change
  proposals for the merge gate). The Claude token never leaves the
  node; hone-core never sees it.

Two images, two `docker-compose.yml` files, deployed separately —
both build from the **project root** so each image can `COPY common/`.
hone-core serves HTTPS directly with a CA + server certificate it
generates on first start; its mapped volume holds `hone.db`, the TLS
material, the operator-tunable `config.yaml`, and the gathered
public-inbox archive(s). The node has no inbound ports — it only
dials out. Given just three start parameters (Claude API token,
hone-core URL, fleet secret), it enrols via the OAuth 2.0
device-authorization grant, receives access/refresh tokens plus
hone-core's CA cert, persists them to its data volume, builds its own
reference kernel repo, and begins claiming. A node is **owned** by the
user who pairs and approves its enrollment — it serves its owner's
requested work first and joins the shared system pool only by owner
opt-in (see [`docs/ARCHITECTURE-WORK-LIFECYCLE.md`](docs/ARCHITECTURE-WORK-LIFECYCLE.md)
→ *Work lifecycle & the claim protocol*). The Claude token is
injected at runtime, never baked into the image. Both services run
unprivileged.

The node resilience model — indefinite exponential backoff + jitter
on transient faults, fail-fast on revoked enrolment or bad
credentials, idempotent result submission keyed on `claim_id`, and
lease + scratch persistence — guarantees no work is lost or
double-counted across an outage. The wire contract is in
[`docs/API.md`](docs/API.md); the concrete build/run model —
Dockerfiles, compose topology, volume and env-var contracts — is in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Data model

`hone.db` is one SQLite database with a single `_SCHEMA_V1` seed. It
holds seven groups — see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) → *Data model*:

- **Corpus** — `patchsets`, `messages` (covers, patches, comments),
  `ai_reviews` (blind reviews), `patchset_metadata` (structured
  prepare-task output), `review_evaluations` (review-level coverage /
  FP-rate / redundancy aggregated from per-comment trains).
- **List-tag filter** — `list_tags`, `patchset_tags`: the operator's
  gather filter.
- **Methodology** — the versioned methodology, the candidate practices
  with their pooled counters and two parallel `severity_witness`
  histograms (patch-introduced vs. pre-existing), the dispositioned
  `methodology_proposals` queue, and `eligibility_flags` driving
  draft-task triggers.
- **Work queue** — `work_items` (unified prepare / review / train) and
  `draft_tasks` for merge-gate work.
- **Training sessions** — `training_sessions`,
  `training_session_patchsets`, `patchset_session_history`:
  operator-triggered batch overlays providing per-stratum coverage and
  held-out evaluation pools.
- **Users, nodes & auth** — `users` (the operator accounts), `nodes`
  (each optionally owned by a user), `node_enrollments`, `node_tokens`
  (hashed only).
- **Gather state** — per-source resume cursors.

A patchset's identity — and the cross-source dedup key — is its
thread's **root Message-ID**; UNIQUE constraints make re-ingestion an
idempotent no-op. SQLite's WAL mode gives concurrent readers plus a
serialized writer — exactly what the atomic `UPDATE … RETURNING` claim
protocol (lease-stamped, crash-recoverable) needs.

## UI / UX

hone-core serves a **server-rendered operator web UI** alongside the
REST API, from one FastAPI app: `/v1/*` for nodes (JSON), the rest for
operators (HTML). The stack is **AdminLTE 4 + Jinja2 + Bootstrap 5 +
HTMX** — live updates and in-page actions with no single-page-app and
no JavaScript build step; the AdminLTE / Bootstrap / Bootstrap Icons /
HTMX assets are vendored, not loaded from a CDN.

Operator pages: **Queue** (the work queue with type × state chip
filter), **Nodes** (the fleet — every node visible, controls gated to
the node's owner — plus your pending-enrollment pairing queue),
**Enroll** (the verification URL a node prints on startup; the first
user to look a code up pairs the node to themselves),
**Sessions** (training-session list and live session-draft composer),
**Settings** (runtime config + list-tag gather filter), and the
**merge gate** (planned) for dispositioning methodology proposals — the
human ratification step for any methodology change, deliberately kept
out of the node-facing API. See
[`docs/PROCEDURE.md`](docs/PROCEDURE.md) for the operator runbook.

## Status

The design lives in `docs/`, and `core/` and `node/` already carry a
substantial implementation: the SQLite schema (`core/core_db.py`), the
GATHER loop with the `lore` source (`core/gather.py`,
`core/gather-modules/`), the canonical methodology
(`core/default-methodology.yaml`) and shared JSON Schemas
(`common/schema/`), the FastAPI app serving the full `/v1` wire contract
(`core/api.py`) alongside the server-rendered operator UI (`core/ui.py`,
`core/templates/`, vendored assets in `core/static/`), the session-based
operator login with per-user node ownership (`core/auth.py`),
self-provisioned TLS (`core/tls.py`), and — on the node — the claim
runner plus the `prepare` task, both its deterministic Tier-0 phase
(`node/cgit.py`, `node/tier0.py`, `node/maintainers.py`) and its LLM
Tier-1 phase (`node/tasks.py`, `node/ai.py`). Still to build: the
`review`, `train`, and `draft` task handlers (`node/tasks.py`, currently
raising `NotImplementedError`); training sessions and the
statistical-aggregation module; and the merge gate and its operator UI. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) →
*Today vs. target* for the design's own implementation inventory (note
that it lags the code in places — e.g. the operator UI and the prepare
Tier-0 split, both now landed), and [`docs/API.md`](docs/API.md) →
*Open* for unspecified corners of the contract.
