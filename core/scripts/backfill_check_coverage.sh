#!/usr/bin/env bash
# Backfill ai_reviews.check_coverage for reviews recorded before the column
# existed — and re-derive it after a gate-registry change — with a gate
# sanity report. Coverage is deterministic, so this is idempotent: safe to
# re-run.
#
#   cd core && ./scripts/backfill_check_coverage.sh --dry-run   # inspect first
#   cd core && ./scripts/backfill_check_coverage.sh             # then write (NULL rows)
#   cd core && ./scripts/backfill_check_coverage.sh --all       # recompute every row
#
# Runs in the container against /data/hone.db via its Python (the core package
# + stdlib sqlite3 — no sqlite3 CLI needed, same as backup.sh). The Python
# ships in the image under core/scripts/, so deploy the current core build
# first.
set -euo pipefail

# Resolve to the dir holding docker-compose.yml (core/) so `docker compose`
# works regardless of the caller's cwd.
cd "$(dirname "$0")/.."

SERVICE=hone-core
docker compose exec -T "$SERVICE" \
  python core/scripts/backfill_check_coverage.py "$@"
