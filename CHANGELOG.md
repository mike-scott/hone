# Changelog

All notable changes to **hone** are recorded here, newest release first.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/). The single
source of truth for the running version is `common/version.py`; each release
bumps it, adds a section here, and tags the release commit `v<version>`.

## Versioning

While the version is `0.y.z` the v1 REST API (`API.md`) and the on-disk
formats are not yet declared stable. Pick a release's bump from its
highest-impact change since the previous release:

- **PATCH** (`0.1.x`) — backward-compatible bug fixes only.
- **MINOR** (`0.x.0`) — new operator- or node-facing functionality, **or** a
  breaking change to the REST API, the completion-record schema, the
  enrollment flow, or the database schema. (Pre-1.0, breaking changes ride a
  minor bump rather than forcing a major one.)
- **MAJOR** (`1.0.0`) — cut when the REST API and on-disk formats are
  committed to stability; from then on any breaking change forces a major
  bump.

Accumulate entries under `[Unreleased]` as work lands; at release time rename
that heading to the new version with the date.

## [Unreleased]

_Nothing yet._

## [0.1.0] - 2026-05-22

Initial release.

### Added
- **hone-core** control-plane service — a FastAPI app that serves the v1 REST
  API for nodes, runs the GATHER stage in-process, serves the operator web UI,
  and owns the SQLite database.
- **hone-node** AI review worker — claims review and maintenance tasks from
  hone-core over the v1 REST API and reports completion records back, with
  transient-failure backoff and a clean SIGTERM shutdown.
- OAuth 2.0 device-authorization-grant node enrollment with opaque bearer
  tokens; hone-core is its own TLS certificate authority and serves HTTPS
  directly, with no external TLS-terminating proxy.
- GATHER stage — pulls qualifying patchsets from the configured data sources
  into the corpus; the enabled sources are operator-selectable.
- Operator web UI — the AdminLTE 4 theme with a light/dark toggle, a
  review-queue home page, node-enrollment management, and a Settings page.
- A formal JSON Schema for the review completion record, validated on
  submission.
- `config.yaml` operator-tunable runtime configuration, editable from the
  Settings page and applied without a restart.
- Two-tier containerized deployment (Docker / docker-compose) on Python 3.14.
- A unit-test suite pinned to the container's Python version.
- A `common/version.py` release-version constant, surfaced in the hone-core
  UI footer and the hone-node startup banner, and this changelog.
