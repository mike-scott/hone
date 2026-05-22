# hone

A multi-tenant service that blind-reviews Linux kernel patchsets against a
versioned methodology, then measures itself against external review signal
(AI bots **and** human maintainers) to self-hone that methodology. It is
built from two deployable components, split by one governing rule:

> **All AI runs on worker nodes; hone-core is purely deterministic.**

This README is the system read across four lenses — multi-tier architecture,
database design, containerized service engineering, and UI/UX. The
authoritative documents it draws on:

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the system model.
- [`API.md`](API.md) — the hone-core ↔ node REST wire contract (`/v1`).
- [`common/README.md`](common/README.md) — code shared by the two tiers.
- [`core/static/README.md`](core/static/README.md) — the vendored UI assets.

## Multi-tier architecture

Two components, split by the governing rule above —
see [`ARCHITECTURE.md`](ARCHITECTURE.md):

- **hone-core** — one FastAPI + SQLite(WAL) instance. Runs the GATHER cron,
  serves the REST API, performs the mechanical (judgement-free) self-honing
  arithmetic, serves the operator web UI, and owns `hone.db`. No AI, no
  kernel repo.
- **AI node** — a containerized worker that starts from scratch: given only
  four start parameters it bootstraps everything else, builds its own kernel
  reference repo, and runs a claim loop over review and maintenance tasks.
  The Claude token never leaves the node.
- **`common/`** — the typed models for the REST payloads, shared by both
  tiers; see [`common/README.md`](common/README.md).

## Database design

`hone.db` is one SQLite database holding three data tiers — see
[`ARCHITECTURE.md`](ARCHITECTURE.md) → *Multi-tenancy*:

- **Global corpus** — patchsets, messages, reviewer identities; gathered
  once, shared by all clients.
- **Global methodology** — the versioned methodology and the pooled
  candidate-practice counters.
- **Per-client** — `client_reviews` keyed `(client_id, root_message_id)`
  and the findings hanging off them; isolated per client.

A patchset's identity — and the cross-source dedup key — is its thread's
**root Message-ID**; every message and finding key carries a PRIMARY KEY /
UNIQUE constraint, so re-ingestion is an idempotent no-op. SQLite's WAL mode
gives concurrent readers plus a serialized writer — exactly what the atomic
`UPDATE … RETURNING` claim protocol (lease-stamped, crash-recoverable)
needs.

## Containerized service engineering

Two images, two `docker-compose.yml` files, deployed separately. Both build
from the **project root** so each image can `COPY common/`.

- **hone-core** runs unprivileged on plain HTTP:8000 (TLS terminated
  upstream) with a liveness probe; one mapped volume holds all of its state.
- **AI node** has no inbound ports and no healthcheck — it only dials out;
  the Claude token is injected at runtime, never baked into the image.

The node resilience model — indefinite exponential backoff + jitter on
transient faults, fail-fast on `4xx`, idempotent result submission keyed on
`claim_id`, and lease + scratch persistence — guarantees no work is lost or
double-counted across an outage. The wire contract is in
[`API.md`](API.md).

## UI / UX

hone-core serves a **server-rendered operator web UI** alongside the REST
API, from one FastAPI app: `/v1/*` for nodes (JSON), the rest for operators
(HTML). The stack is **Jinja2 + Bootstrap 5 + HTMX** — live updates and
in-page actions with no single-page-app and no JavaScript build step. The
Bootstrap and HTMX assets are vendored, not loaded from a CDN; see
[`core/static/README.md`](core/static/README.md). The merge gate — the
human ratification step for any methodology change — is a UI-only surface,
deliberately kept out of the node-facing API.

## Status

A precursor runs today (a hand-driven single-host loop); the target is the
multi-tenant containerized service described here. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) → *Today vs. target* for what exists
and what is still to build, and [`API.md`](API.md) → *Open* for the
unspecified corners of the contract.
