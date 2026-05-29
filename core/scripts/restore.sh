#!/usr/bin/env bash
# Restore hone-core's DB (and identity files) from a backup directory made by
# backup.sh. DESTRUCTIVE: overwrites the live volume's hone.db. Requires brief
# downtime — the app must not hold the DB open during the swap.
#
#   cd core && ./scripts/restore.sh backups/2026-05-29-101500
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICE=hone-core
# docker compose prefixes the volume with the project name (compose `name:`).
VOLUME=hone-core_hone-core-data
# The `hone` user the image runs as (core/Dockerfile) — files must end up
# owned by it after the root-owned cp below.
DB_UID=10001

src="${1:-}"
if [ -z "$src" ]; then
  echo "usage: $0 backups/<timestamp>" >&2
  exit 2
fi
if [ ! -f "$src/hone.db" ]; then
  echo "error: '$src/hone.db' not found — point at a backup dir from backup.sh" >&2
  exit 2
fi
abs_src="$(cd "$src" && pwd)"

echo "This OVERWRITES the live hone-core DB in volume '$VOLUME' with:"
echo "    $src/hone.db"
read -r -p "Proceed? [y/N] " ans
case "$ans" in
  y|Y) ;;
  *) echo "aborted"; exit 1 ;;
esac

echo "Stopping $SERVICE…"
docker compose stop "$SERVICE"

# Swap the files in via a throwaway container that mounts the volume. Drop the
# stale -wal/-shm (the backup is a complete file; an old WAL would corrupt it)
# and restore ownership to the image's user after the root cp.
docker run --rm -v "$VOLUME:/data" -v "$abs_src:/b:ro" python:3.14-slim sh -c "
  set -e
  cp /b/hone.db /data/hone.db
  rm -f /data/hone.db-wal /data/hone.db-shm
  chown $DB_UID:$DB_UID /data/hone.db
  if [ -d /b/tls ]; then rm -rf /data/tls; cp -r /b/tls /data/tls; chown -R $DB_UID:$DB_UID /data/tls; fi
  if [ -f /b/config.yaml ]; then cp /b/config.yaml /data/config.yaml; chown $DB_UID:$DB_UID /data/config.yaml; fi
"

echo "Starting $SERVICE…"
docker compose start "$SERVICE"
echo "Restore complete from $src"
