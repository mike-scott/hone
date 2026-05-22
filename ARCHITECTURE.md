# hone — architecture

hone is a service that reviews Linux kernel patchsets. How the system is
shaped: operational steps live in `PROCEDURE.md`; this file is the *model*
behind them. Harness machinery — **not** part of
`~/PATCH-REVIEW-METHODOLOGY.md`.

> **Status.** This describes the **target** architecture — a multi-tenant
> containerized service. It supersedes the earlier single-host 3-stage
> description. What runs *today* is a precursor: the loop is driven by hand,
> `core_db.py` / `core/gather-modules/` / `refrepo.py` are single-host libraries,
> and `hone.db` is still single-tier. See "Today vs. target" at the end.

## Governing principle

**All AI runs on worker nodes; hone-core is deterministic.** Anything
that needs judgement — reviewing a patch, judging a candidate practice,
authoring methodology text — happens on a node. Anything mechanical —
scheduling, queuing, counting, routing, auth, storage — is hone-core.
Every design decision below follows from this one line.

## Two components

```
            HONE-CORE                                      WORKER TIER
  ┌────────────────────────────────┐
  │ containerized web service       │        ┌──────────────────────────────┐
  │ FastAPI + SQLite(WAL), 1 instance│        │ AI node (containerized)       │
  │ NO AI · NO kernel repo           │        │  - Claude token               │
  │                                  │        │  - owns its own kernel repo   │
  │  · cron: GATHER from sources     │  REST  │  - scratch storage            │
  │  · serve REST: claims, results,  │◄──────►│                               │
  │    methodology, node enroll      │  /TLS  │  task worker:                 │
  │  · mechanical self-honing        │        │   claim → do AI work → report │
  │  · owns hone.db                   │        │   (review tasks +             │
  └────────────────────────────────┘         │    maintenance tasks)         │
                                              └──────────────────────────────┘
```

### hone-core

A containerized web service — **FastAPI + SQLite (WAL mode), one instance**,
no AI, and **no kernel git repo**. Five jobs:

1. **GATHER** (cron) — pull qualifying patchsets from the data sources
   (`core/gather-modules/`, see `SOURCES.md`) into the corpus, recording each
   patchset's declared `base_commit`. The producer that feeds the pipeline.
   The cron cadence is a hone-core **configuration option**, defaulting to
   **every 10 minutes**.
2. **Serve the REST API** — hand out work claims, accept results, distribute
   the current methodology, authenticate clients.
3. **Mechanical self-honing** — pooled candidate-practice counters,
   graduation-eligibility checks, threshold prunes (per `SCORING.md`). All
   arithmetic, no judgement.
4. **Own `hone.db`** — the corpus, the methodology, the per-client results, the
   queues.
5. **Serve the operator web UI** — the human-facing management surface; see
   *Operator web UI* below.

SQLite's WAL mode gives concurrent readers + a serialized writer — exactly
what the atomic claim protocol needs. One instance; the node tier scales
independently of it.

### AI node

A containerized worker that **starts from scratch**. Given only its start
parameters — a Claude API token, the hone-core URL, and the fleet secret —
plus a mapped storage volume, it bootstraps everything else itself. On first
start it **enrolls into the fleet** (the device-authorization grant; see
*Auth, enrollment & transport*), obtaining its bearer credentials and
hone-core's CA certificate before it does any work. The node image bakes in
**nothing** deployment- or domain-specific (no methodology, no kernel tree):
on start it fetches the methodology from hone-core, and it **builds its own
reference kernel git repo at runtime** in its storage volume — so it can check
out any base commit, with no shared tree to thrash and nothing to
pre-provision.

The review is **pure analysis** — a node reads the patch and the surrounding
code and reasons about it. It does **not** build the kernel or run review
tooling (checkpatch, smatch, sparse, sanitizers, `dtbs_check`). It uses only
lightweight, toolchain-free git against its reference repo — reading code at
the base commit, and `git apply --check` to confirm the patch applies — so the
node container carries no kernel build toolchain, and a review is reproducible
reasoning rather than a tool run.

A node is a **task worker**: it claims tasks over the REST API, does the AI
work, and reports back. Two task types:

- **review task** — review one patchset for one client.
- **maintenance task** — evaluate the candidate set against the methodology
  and propose changes (see "The merge gate").

A node runs a continuous **claim loop** — claim, do the work, submit, then
immediately claim again while the queue yields work. On an empty claim
(`204 No Content`) it waits a **poll interval** before retrying; the poll
interval is a configuration option, default **60 s**. This idle poll is
distinct from the failure backoff below: the poll timer paces a *healthy but
empty* queue, the backoff handles an *unreachable* hone-core.

The Claude token never leaves the node; hone-core never sees it and
never calls Claude.

### Node resilience — retry & backoff

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
- **Working through an outage.** A node mid-review when hone-core goes
  away finishes the review *locally* (it already holds the blob + base tree),
  **persists the claim and the completed result to its scratch storage**, and
  retry-submits until hone-core returns. A node restart resumes that
  submit from scratch storage.
- **Lapsed claims.** If an outage outlasts the lease, hone-core has
  reclaimed the patchset; on reconnect the node may be told its claim lapsed
  and then discards the stale result — the reclaim already covered the work.
  Lease + idempotent submit + scratch persistence ⇒ no work lost, none
  double-counted.

## Operator web UI

hone-core serves a **server-rendered management UI** alongside the REST
API — one FastAPI app: `/v1/*` for nodes (JSON), the rest for operators
(HTML). The stack is **Jinja2 templates + Bootstrap 5 + HTMX** — HTMX gives
live updates and in-page actions with no single-page-app and no JavaScript
build step (Bootstrap and HTMX are vendored static assets, which is why the
hone-core image needs no Node toolchain).

Pages:
- **Overview** — methodology-progress statistics, the patchset queues, node
  status.
- **Queue management** — inspect and act on the gather / stage / dispatch
  queues.
- **Manual submissions** — patchsets an operator uploads directly (a
  compressed `.tar.zst` patch archive), bypassing GATHER: a list of the
  client's submissions and an upload form. A manual submission is **reviewed
  but does not train the methodology** — with no external-source review to
  compare against, it is excluded from the self-honing loop (no source
  comparison, no candidate counter updates, no candidate nominations); the
  operator simply receives the review. The patchset is flagged `manual` so
  hone-core keeps it out of the self-honing machinery.
- **Node management** — the registered tenants, the live node fleet, and the
  **pending-enrollment queue**: an operator enters a node's device-grant
  *user code* here to approve it and bind the new node to a tenant (see
  *Auth, enrollment & transport*).
- **Settings** — the hone-core configuration options.
- **Merge gate** — disposition the `methodology_proposals` queue (see *The
  merge gate*).
- Reporting pages — later.

**Patchset detail view.** Drilling into any patchset — a manual submission or
a queued/gathered one — opens a shared detail view: the patch files with
reviewer comments **spliced inline** at the lines they anchor to (a code-review
reading of the patchset). For a gathered patchset those comments are the
external source's review plus our nodes' findings; for a manual submission,
our nodes' findings alone. Both Queue management and Manual submissions reach
it.

Operators authenticate with a human login (session-based), distinct from the
header credentials a node presents.

## Persistent storage

Each service maps **one local volume** as its data store; everything else in a
container image is ephemeral.

- **hone-core** — its volume holds `hone.db`, the hone-core config, the
  **self-generated TLS CA and server certificate** (created once on first
  start), the methodology import/export files, and the gathered
  patchset-source archives (public-inbox clones). hone-core's entire owned
  state, in one place.
- **Node** — its volume holds the reference kernel clone(s) and review
  scratch. It starts empty; the node self-populates it (see *AI node*), and a
  restarted node reuses it rather than re-cloning.

## Multi-tenancy — three data tiers

One hone-core instance serves many **pre-registered clients**, each with a client
key. The tenancy boundary is confined to exactly one layer — *review results*.
Everything else is shared:

| Tier | Contents | Scope |
| --- | --- | --- |
| **Global corpus** | `patchsets` (the patch metadata + `base_commit`), `patch_files`, `messages`, `patchset_sources`, reviewer identities (`reviewers`, `reviewer_emails`) | gathered once, shared by all clients |
| **Global methodology** | the versioned methodology, the candidate practices + their **pooled** counters | shared; all clients' misses hone the one methodology |
| **Per-client** | `clients` (the registered tenants), `client_reviews` keyed `(client_id, root_message_id)` — the review lifecycle, verdict, token cost — and the classified `findings` hanging off them | isolated per client |

A patchset is gathered once but **reviewed once per client**, independently.
`patchsets` no longer carries review columns — those move to `client_reviews`.

**Dedup.** A patchset's identity is the **root Message-ID** of its thread, so
the same submission gathered via two sources is one `patchsets` row. Every
message and finding key carries a PRIMARY KEY / UNIQUE constraint, so
re-ingestion — a `git fetch`, a list crosspost, a re-run — is an idempotent
no-op; `hone.db` is the sole dedup authority. Revisions (`v1` → `v2` → …) are
distinct submissions, linked by `change_id` for reporting.

## Methodology storage

The review methodology is **DB-resident and versioned** — the canonical copy
lives in hone-core's global-methodology tier, not in a markdown file.
`PATCH-REVIEW-METHODOLOGY.md` and `…-FINDINGS.md` become *exchange formats*,
not the source of truth (the candidate practices are already a table). Three
operations on the store:

- **Import** — load a methodology from its portable representation —
  `core/default-methodology.yaml`, a structured YAML file — into the DB as a new
  version, **after validating it against `core/methodology.schema.yaml`** (a
  JSON Schema; a malformed methodology is rejected, not imported).
  `core/default-methodology.yaml` (the harness's pure-analysis methodology, a
  cleaned derivative of `~/PATCH-REVIEW-METHODOLOGY.md`) ships in the repo and
  bootstraps DB v1; import also brings human-edited revisions back in. The
  host's `~/PATCH-REVIEW-METHODOLOGY.md` is a separate, untouched file.
- **Export** — render the DB methodology back to YAML (the
  `core/default-methodology.yaml` format) — for human reading, offline editing,
  backup, and git-tracking; the operator can export over the default and
  commit it.
- **Distill** — project the canonical methodology + the active candidate
  practices into the **node review spec**, served as **JSON** at
  `GET /v1/methodology` (YAML is the file / import-export format; JSON is the
  wire format — the same structure). The distilled payload is
  `{ version, principles, stages, checks, candidates, severity_scale,
  report_finalization }` — what a node applies, stripped of human scaffolding
  and of hone-core bookkeeping (proposal history, lifecycle metadata).
  **Graduated checks and candidate practices are sent as two separate
  arrays** — `checks` (the permanent methodology) and `candidates` (the
  experimental layer being trialed); a node applies both, and the split lets
  it report per-candidate outcomes (applied / fired) for the Applied/Catches
  counters. Each `candidates` entry keeps its `id` and current `confidence` —
  the one bookkeeping value distillation retains, as useful weighting context
  for the node. A node never sees the raw canonical store.

Import/export keep the methodology portable and human-editable; distillation
keeps what reaches a node lean. The merge gate mutates the canonical store
(a new version); the next distillation propagates it to nodes.

## Auth, enrollment & transport

hone-core is its own **OAuth 2.0 authorization server**. There is no external
identity provider — consistent with hone-core being one self-contained,
deterministic instance.

**Two channels, authenticated differently.**

- The **OAuth / enrollment API** (`/v1/oauth/*`) is the *bootstrap* channel. A
  brand-new node has only three things: the hone-core URL, the **fleet
  secret**, and its Claude API token. The fleet secret — a fleet-wide shared
  secret every node is given — gates the OAuth API and **nothing else**: it is
  what lets a fleet member *begin* enrollment, and it keeps the enrollment
  endpoint (and the operator's approval queue) closed to anyone outside the
  fleet.
- The **main API** (everything else) is reached with an OAuth **bearer
  token**. The token carries the node's identity and its tenant (client); it
  fully replaces the earlier per-request shared-secret + client-key header
  pair.

**Node enrollment — the device authorization grant (RFC 8628).** A node is
*added to the fleet by enrolling itself*, gated by a human:

1. On first start the node calls `POST /v1/oauth/device_authorization`
   (presenting the fleet secret) and is issued a short **user code** and a
   verification URL. It logs them and begins polling.
2. An operator opens hone-core's web UI, enters the user code, reviews the
   node's self-described metadata, **binds it to a tenant (client)**, names
   it, and approves — or denies. This human step is the trust anchor; it
   replaces the earlier admin "pre-register a client key" call.
3. The node's poll to `POST /v1/oauth/token` then returns an **access token**,
   a **refresh token**, and hone-core's **CA certificate** (see *Transport*
   below). The node persists all three to its data volume and is now a fleet
   member.

Access tokens are short-lived; the node refreshes them with the refresh token
over the OAuth channel. An access token is **opaque** — hone-core stores only
its hash and validates it by lookup, so revoking a node is a single database
update. (hone-core is one instance; a per-request database lookup is the
existing pattern, and opaque tokens keep revocation immediate and add no
cryptographic key management.)

**Transport — a self-provisioned TLS CA.** On **first startup** hone-core
generates, once, a private **certificate authority** and a server certificate
signed by it, and stores them on its data volume (reused on every later
start). hone-core serves HTTPS directly with that certificate — there is no
external TLS-terminating proxy to provision.

The two channels bootstrap trust differently, and that is the reason for
keeping them apart:

- The **OAuth channel** is contacted before the node holds hone-core's CA, so
  the node trusts that first connection on first use; the fleet secret is what
  authenticates the exchange.
- The **CA certificate is delivered to the node during enrollment** (in the
  token response). From then on the node validates the TLS of **every
  non-OAuth call** against that CA. "The node trusts hone-core" is therefore
  itself established through the gated, human-approved enrollment — not
  pre-provisioned out of band.

**Admin** endpoints (e.g. registering a tenant) use a separate
`X-HONE-Admin-Token`, an operator credential distinct from any node or fleet
credential.

## Work lifecycle & the claim protocol

hone-core does not push work; a node **claims** it. One atomic SQL
`UPDATE … RETURNING` flips the oldest available item to `claimed` and stamps
`claimed_by` / `claimed_at`. SQLite serializes writers, so two nodes never
claim the same item.

**Per-client review lifecycle** for a patchset:

```
  (corpus: gathered, base_commit recorded)
        │
        ▼  a client's node claims it
   claimable ──► claimed ──┬─► reviewed       (terminal — review produced)
        ▲           │      ├─► unappliable    (terminal — patch won't apply, unfixable)
        │           │      └─► deferred       (base tree unobtainable)
        └───────────┴────── lease expires / deferred ──► re-claimable

  corpus-level terminal: skip  (e.g. an unresolvable Date — no client reviews it)
```

- **claimable** — in the corpus, not yet claimed/reviewed by *this* client.
- **claimed** — a node holds it; `claimed_at` stamps the lease. The **lease
  time** is a configuration option, default **30 min** — it bounds how long a
  silent node's work is held before re-offer, *not* how long a review may
  take. The node **heartbeats** to extend the lease (the heartbeat interval is
  a separate config option, default **5 min** — comfortably shorter, so a
  healthy node never loses its claim; only a node unreachable for the whole
  lease window is treated as dead and re-offered).
- **reviewed** — terminal; the client's review + verdict + token cost recorded.
- **unappliable** — terminal; the node obtained the base tree but the patch
  will **not** apply (`git apply --check` fails) and the node could not
  reconcile it, so no review was produced. Recorded with the node's reason.
  Distinct from `deferred`: `deferred` is a missing *tree* (retry later);
  `unappliable` is an unworkable *patch* (re-claiming the same blob won't
  help). Also a source-quality signal — frequent `unappliable` patchsets from
  a source flag bad patch reconstruction.
- **deferred** — the node could not obtain the base *tree*; retryable — it
  returns to `claimable`.
- **skip** — corpus-level, terminal.

**Crash recovery:** a dead node's claim goes stale; once the lease elapses the
claim protocol re-offers it. No work is lost or stuck. This is what makes the
worker tier a pool of interchangeable nodes rather than one-shot tasks.

## Self-honing across the tenancy boundary

The methodology is global, so the FINDINGS → methodology loop spans both
components. Each part runs where its nature dictates:

| Step | Runs on | Why |
| --- | --- | --- |
| Propose a candidate practice from a verified miss | **review node** | needs AI; the miss is seen during a review |
| Update Applied/Catches counters; graduation-eligibility; threshold prunes | **hone-core** | pure arithmetic over the pooled counters |
| Judge redundancy / coherence; **draft** the change — a bounded, candidate-anchored adaptation | **maintenance node** | needs AI *and* the whole-corpus view a single review never has |
| Ratify a methodology change | **a human** | the merge gate (below) |

## The merge gate

A methodology change is the single highest-blast-radius mutation in the system
— every client's every review is graded against it. So maintenance nodes
**propose**; a **human ratifies**. Two orthogonal axes:

**The node's axis — recommendation** (what a maintenance task found). Each
proposal is tagged one of: `graduate` · `prune-redundant` · `prune-ineffective`
· `consolidate` · `revise`.

**The human's axis — outcome** (what to do about it). The human dispositions
each queued proposal as one of:

1. **Accept** — apply it; hone-core commits the (optionally
   human-edited) payload and **bumps the methodology version**.
2. **Defer** — sound but thin; records a `defer_watermark`; not re-surfaced
   until the candidate's evidence materially grows past it.
3. **Reject** — permanent no, **scoped to the `(recommendation, subject)`
   pair**: the candidate itself stays active and applied; only that pairing is
   suppressed. The rejected row *is* the suppression-log entry.
4. **Return for redraft** — recommendation stands, draft doesn't; carries a
   `feedback_note`; hone-core re-tasks a maintenance node, and the new
   proposal lands linked via `parent_id`. Bounded — see the redraft cap.

**The queue — `methodology_proposals`** (hone-core table; also the
suppression log — rejected rows):

```
id · created_at · created_by_task
recommendation     graduate | prune-redundant | prune-ineffective | consolidate | revise
subject            candidate id(s)
rationale          the node's reasoning
payload            the concrete change (drafted methodology text / prune target)
evidence           pooled stats at proposal time
base_methodology_ver   version it was drafted against (staleness check)
status             pending | accepted | deferred | rejected | redraft
disposed_by · disposed_at · disposition_note
defer_watermark    (deferred) evidence level to exceed before re-surfacing
parent_id · feedback_note   (redraft lineage)
```

**Flow:** hone-core updates a candidate's pooled counters as each review
result lands; when a counter crosses a `SCORING.md` threshold (graduate- or
prune-eligible) it flags **maintenance due** and enqueues a maintenance task —
**debounced**: at most one outstanding task, and the task is *holistic* (it
handles every currently-eligible candidate at once, not one per crossing). A
node claims it, reads `{candidate set + pooled stats + methodology + the
rejected-proposal suppression log}`, posts a batch of proposals → they queue as
`pending` → a human dispositions each in hone-core's web UI → **Accept**
applies the change and writes a `methodology_versions` row.

The trigger is therefore **event-driven off result ingestion**, not a cron
poll — candidate *application* during reviews is the heartbeat that drives it,
but the cleanup itself is never inline in a review (it needs the whole-corpus
view and the merge gate).

hone-core *applies* an accepted change deterministically — the node
already drafted the change, so committing it is a text substitution + a
version bump, no AI. **Staleness:** if the methodology version moved while a
proposal sat in the queue (`base_methodology_ver` ≠ current), hone-core
flags it; the safe disposition is *redraft*.

**Redraft cap — the loop's stopping condition.** The redraft loop is
human-driven: it advances only while the human keeps choosing *Return for
redraft*, and Accept / Defer / Reject each terminate the lineage. To bound a
node and human that fail to converge, a **configurable cap** (hone-core
config, default **3**) limits the `parent_id` chain — once a proposal is the
3rd *quality-feedback* redraft in its lineage, the *Return for redraft* option
is withheld and the human must Accept (hand-editing the payload in the UI if
needed), Defer, or Reject. This guarantees every lineage terminates. Redrafts
forced purely by **staleness** (the methodology version moved underneath the
proposal) do **not** count toward the cap — that is the target moving, not the
node failing to converge.

The methodology is therefore hone-core-managed, **versioned** data.
`GET /v1/methodology` serves the current version; a node records which version
each review used (an in-flight review finishes on the version it started).

## Guarding the methodology from a bad node

The methodology is the highest-value mutable artifact, and a maintenance node
could be malicious or simply buggy. Four layers keep a bad node from
corrupting it — hone-core stays non-AI throughout, validating *shape*,
never *meaning*.

**1 — A node submits a typed proposal, never a methodology.** A maintenance
node cannot upload or replace a methodology file; it submits a typed, scoped
proposal (`graduate` / `prune-…` / `consolidate` / `revise`), and the
hone-core composes the new version itself from `{current version + the
validated delta}`. Whole-file truncation or an empty-file replacement is
therefore not in the threat model. Only `graduate` carries new methodology
prose; `prune`/`consolidate`/`revise` touch only the structured candidate
table. Graduated prose is a **bounded adaptation of the candidate practice** —
itself already evidence-backed — not free authoring: it must stay traceable to
that candidate. A graduated candidate becomes a new **check** — an entry in
the top-level `checks` set, run under Stage 2 — candidate practices are
bug-class checks by nature, so that is the only shape a graduation takes.
Checks are an **unordered set**: each is an independent bug-class analysis
applied in any order, so a graduated check simply joins the set — there is no
position to choose. Anything with a genuine processing-order dependency is a
`stage` (the ordered 0/1/2/3/S macro-structure), never a check. Through the
merge gate a node can therefore *only add a check*; it can never add or alter
a principle, a stage, an existing check, the severity scale, or any other
scaffolding. Those change only via the operator's import path (export → edit
→ re-import).

**2 — Mechanical validation at submission.** Before a proposal enters the
`pending` queue hone-core runs deterministic, judgement-free checks;
any failure rejects it at submission, so it never reaches a human:
- *non-empty* payload, valid UTF-8, no control characters, within length bounds;
- *composes cleanly* — applying the delta yields a methodology that still
  parses, with every existing stage intact;
- *scoped* — a `graduate` adds exactly one **check** (one entry to the
  top-level `checks` set) and removes exactly its candidate, altering no
  principle, stage, existing check, or scaffolding; a `prune` touches only its
  candidate;
- *proportionate* — reject a net shrink, or an addition wildly larger than the
  candidate it graduates;
- *traceable* — the graduated text must recognizably derive from the candidate
  it claims (containment / similarity).

**3 — An informed human gate.** The merge gate already requires a human
*Accept*; the proposal is presented with an **evidence panel** so the decision
is informed, not a rubber stamp — the diff, the magnitude metrics from layer
2, the candidate's pooled stats (Applied / Confidence / unique-catches), the
originating verified misses, and the submitting node's client provenance.

**4 — Node reputation.** Proposals that fail layer-2 validation or are
*Rejected* at the gate are counted per enrolled node (and its tenant); a node
whose proposals are consistently invalid or rejected is flagged for
**enrollment revocation** — its tokens are revoked and it must re-enroll
through the operator gate. A bad-acting node is caught by its track record,
through the existing node-enrollment auth model.

A fifth option — having other nodes **vote** on a proposal — is deliberately
*not* adopted: it would rest trust on an honest-majority assumption about
partially-untrusted nodes, weaker than trusting the human gate. If ever added
it would be an advisory signal into the layer-3 evidence panel, never an
autonomous decision.

## REST API

The hone-core↔node wire contract is specified in **`API.md`** — base path
`/v1`. It has three parts: the **OAuth / enrollment** endpoints
`POST /v1/oauth/device_authorization` and `POST /v1/oauth/token`
(fleet-secret-gated); the **work API** — `POST /v1/claims` (claim a task),
`…/heartbeat`, `…/result` (the completion record), `GET …/blob`,
`GET …/source-review`, `GET /v1/methodology` (bearer-token-authed); and the
admin `POST /v1/clients` (register a tenant). The merge gate and the
node-enrollment approval are **human web UI** on hone-core, not node-facing
APIs.

## Today vs. target

**Exists today** (single-host precursor): `core/gather-modules/` (the
`GatherModule` API), `refrepo.py`, `core_db.py` and a single-tier `hone.db` (44
patchsets reviewed), `core/default-methodology.yaml` (the v1 seed methodology) and
`core/methodology.schema.yaml` (its validation schema); the loop is run by hand. These become *libraries* of the target —
`core/gather-modules/` drives hone-core's GATHER stage, `refrepo.py` goes
node-side (the node owns its reference repo), and `core_db.py` is now
hone-core's data layer — the three-tier schema (corpus, methodology, and
per-client results) with versioned migrations.

**To build:** the FastAPI hone-core service (the REST API, the GATHER cron,
the operator web UI, the methodology store + import/export/distill, the
`methodology_proposals` queue and merge gate); the node's task execution
(review + maintenance). Containerization
has **started** — both component images (`core/Dockerfile`,
`node/Dockerfile`), each with its app skeleton, the `common/` folder, and a
per-component `docker-compose.yml` in `core/` and `node/` (run separately)
all exist.
The bootstrap step imports `core/default-methodology.yaml` as DB v1; the host's
`~/PATCH-REVIEW-METHODOLOGY.md` is left unchanged.

**Open / not yet specified:** the architecture and the REST contract are
specified; remaining gaps are minor (`API.md` → "Open") and the build itself.
