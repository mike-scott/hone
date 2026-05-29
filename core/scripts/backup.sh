#!/usr/bin/env bash
# Hot backup of hone-core's SQLite DB plus the small static identity files,
# into core/backups/<timestamp>/ — no downtime.
#
# The DB runs in WAL mode (hone.db + -wal/-shm), so a plain `cp` of the live
# file can capture a torn or stale state. We use SQLite's online backup API
# (via the container's Python — the image has no sqlite3 CLI) from a
# read-only source connection: zero risk to the live DB, and the result is a
# single consistent file with the WAL already folded in.
#
#   cd core && ./scripts/backup.sh
#
# Restore a backup dir with ./scripts/restore.sh backups/<timestamp>
set -euo pipefail

# Resolve to the dir holding docker-compose.yml (core/) so `docker compose`
# and the relative backups/ path work regardless of the caller's cwd.
cd "$(dirname "$0")/.."

SERVICE=hone-core
ts="$(date +%Y-%m-%d-%H%M%S)"
dest="backups/$ts"
mkdir -p "$dest"

echo "Backing up $SERVICE → $dest/ (online, no downtime)…"

# Online backup inside the container, then copy the consistent file out.
docker compose exec -T "$SERVICE" python -c \
  "import sqlite3; s=sqlite3.connect('file:/data/hone.db?mode=ro', uri=True); d=sqlite3.connect('/tmp/hb.db'); s.backup(d); d.close(); s.close()"
docker compose cp "$SERVICE:/tmp/hb.db" "$dest/hone.db"
docker compose exec -T "$SERVICE" rm -f /tmp/hb.db

# Small, static identity state — restoring these keeps the fleet's pinned TLS
# trust intact on a fresh host. archive/ is intentionally skipped: large and
# re-gatherable.
docker compose cp "$SERVICE:/data/tls"         "$dest/tls"
docker compose cp "$SERVICE:/data/config.yaml" "$dest/config.yaml"

echo "Backup complete:"
ls -la "$dest"
