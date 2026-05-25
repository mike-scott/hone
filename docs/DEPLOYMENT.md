# hone — deployment

The build-and-run model: container images, compose topologies, volume
and env-var contracts. The architectural model behind it lives in
[`ARCHITECTURE.md`](ARCHITECTURE.md); the operator runbook in
[`PROCEDURE.md`](PROCEDURE.md); the wire contract in [`API.md`](API.md).

## Build model

Both images **build from the project root** — neither service is
self-contained inside its own directory, because both `COPY common/`
(the methodology data + JSON Schemas shared across tiers):

```
docker build -f core/Dockerfile -t hone-core .
docker build -f node/Dockerfile -t hone-node .
```

The compose files set `build.context: ..` accordingly. `.dockerignore`
at the project root keeps the context lean — `.git`, the gathered
`archive/` clone, the `staged/` directory, and Python cache files are
excluded; the `archive/` exclusion alone is what keeps the build
context from ballooning past ~1 GB.

## hone-core image

`python:3.14-slim` plus one system package: `git`, for walking
public-inbox archives during GATHER. No compilers, no kernel toolchain.
Runs unprivileged as uid 10001.

Exposes **8000**. Serves HTTPS directly — there is no external
TLS-terminating proxy. The TLS CA + server certificate are generated
on first start and stored on the data volume (see *First start*
below).

A liveness probe `GET /healthz` over HTTPS, fired every 30 s by the
runtime. The probe does not verify the certificate (it is the
service's own self-generated cert) — the probe is a `python -c "…"`
one-liner since the slim image ships no `curl`.

`CMD ["python", "-m", "core.main"]` — exec form so the process is PID 1
and receives SIGTERM directly for a clean stop. GATHER runs in-process
alongside the FastAPI app; there is no second container.

## hone-node image

`python:3.14-slim` plus `git` (for building the reference kernel repo
and checking out base commits). No kernel build toolchain — the node's
work is pure analysis. Runs unprivileged as uid 10001.

**No `EXPOSE`, no `HEALTHCHECK`.** The node has no inbound ports — it
only dials out to hone-core. Liveness is "the process runs"; compose's
`restart: unless-stopped` policy bounces it if it crashes.

`CMD ["python", "-m", "node"]` — exec form for clean SIGTERM. GATHER,
the web UI, and the database all live in hone-core, not here.

## Compose topology

Two separate deployments, one compose file each — typically on
different hosts. There is no compose-level link between them: the node
reaches hone-core at `$HONE_CORE_URL` and retries with backoff if
hone-core is unreachable.

- `core/docker-compose.yml` — the hone-core service, one instance,
  HTTPS-published on `$HONE_PUBLISH_PORT` (host-side; the container
  always listens on 8000).
- `node/docker-compose.yml` — the hone-node service. Run several nodes
  from one compose file with `docker compose up --scale hone-node=N`;
  each gets its own enrolment and approval.
- `node/docker-compose.override.yml` — **local-testing override,
  gitignored, not for production.** Sets `network_mode: host` so the
  node can reach a hone-core running on the same host at
  `https://localhost:8000` — required because (a) `localhost` inside a
  bridged container is the container itself, and (b) hone-core's
  self-generated cert has `localhost` as its SAN. Compose auto-merges
  this override when present.

Both services use `restart: unless-stopped`.

## Volume contract

One mapped volume per service; the container is otherwise ephemeral.

**hone-core** — `/data` (`HONE_DATA`) holds:

- `hone.db` (`HONE_DB`) — SQLite, WAL mode; the corpus, the
  methodology, the work queue, the training-session state, the node
  fleet, the gather cursors.
- `config.yaml` (`HONE_CONFIG`) — the operator-tunable runtime config
  (see ARCHITECTURE.md → *Configuration & the Settings page*).
- `tls/` (`HONE_CERT_DIR`) — the self-generated CA and server
  certificate, reused on every later start.
- `methodology/` (`HONE_METHODOLOGY_DIR`) — methodology import/export
  YAML files.
- `archive/` (`HONE_ARCHIVE_DIR`) — the gathered public-inbox clones;
  the largest item, can approach ~1 GB on a mature deployment.

**hone-node** — `/data` (`HONE_DATA`) holds:

- `linux/` (`HONE_REPO_DIR`) — the node's reference kernel clone(s).
  Self-populated; starts empty and a restarted node reuses what's
  there.
- `scratch/` (`HONE_SCRATCH_DIR`) — in-flight claim payloads and
  completed results persisted to disk so a node restart can resume
  submission across a hone-core outage (see ARCHITECTURE-WORK-LIFECYCLE.md
  → *Node resilience*).

## Env-var contract

Required at container start — compose asserts each with `${VAR:?set it
in .env}` so missing values fail fast:

**hone-core**:

- `HONE_FLEET_SECRET` — the fleet-wide shared secret; gates node
  enrolment (see ARCHITECTURE.md → *Auth, enrollment & transport*).
- `HONE_ADMIN_TOKEN` — the operator's admin credential.
- `HONE_HOSTNAME` — the public hostname the TLS cert is issued for.
- `HONE_PUBLISH_PORT` — optional, defaults to 8000; host-side port
  only — the container always listens on 8000.

**hone-node**:

- `HONE_CORE_URL` — `https://<hone-core-host>:<port>`, the URL the node
  dials.
- `HONE_FLEET_SECRET` — must match hone-core's.
- `ANTHROPIC_API_KEY` — the node's Claude API token; never baked into
  the image.

Optional knobs (e.g. `HONE_NODE_NAME`, the poll / backoff intervals,
`HONE_LORE_AUTOCLONE`) live in the same `.env` file and reach the
container via the `env_file` directive.

Every other tunable lives in `config.yaml` on hone-core's data volume
and is editable via the Settings page at runtime, no restart needed
(see ARCHITECTURE.md → *Configuration & the Settings page*).

## First start

**hone-core**:

1. Reads its env vars; refuses to start if `HONE_FLEET_SECRET`,
   `HONE_ADMIN_TOKEN`, or `HONE_HOSTNAME` is missing.
2. Generates its TLS CA + server certificate (issued for
   `HONE_HOSTNAME`, with `localhost` as an additional SAN for local
   testing) under `$HONE_CERT_DIR`; reused on every later start.
3. Creates `hone.db` with the fresh `_SCHEMA_V1` seed.
4. Writes `config.yaml` from built-in defaults; env vars for tunable
   keys seed the file if set.
5. If `HONE_LORE_AUTOCLONE=1`, kicks off a background clone of the
   lore archive (see PROCEDURE.md → *First-run setup*).
6. Serves HTTPS on 8000.

**hone-node**:

1. Reads its three required env vars; refuses to start if any is
   missing.
2. Calls `POST /v1/oauth/device_authorization` against hone-core
   (with the fleet secret), logs the user code and verification URL,
   then polls.
3. Waits for the operator to approve the enrolment in hone-core's web
   UI (see PROCEDURE.md → *Enrolling a node*).
4. On approval, receives access/refresh tokens plus hone-core's CA
   cert; persists all three to `/data`; begins claiming.

## Local-testing topology

For development on a single host: bring up hone-core first
(`cd core && docker compose up --build`); then bring up the node from
the same host (`cd node && docker compose up --build`). The
`docker-compose.override.yml` puts the node on host networking so it
can reach `https://localhost:8000` and validate the cert's `localhost`
SAN.

For multi-node testing: `docker compose up --scale hone-node=3` — each
node enrols separately and waits for its own approval.

## Out of scope

Not yet specified, documented when they land: production CI/CD for the
images; image versioning and the wire-compat contract between core and
node image versions; centralised observability (logs / metrics);
backup / restore procedures for the data volumes.
