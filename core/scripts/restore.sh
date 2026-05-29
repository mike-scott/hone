#!/usr/bin/env bash
# Restore hone-core's DB (and identity files) from a backup directory made by
# backup.sh. DESTRUCTIVE: overwrites the live volume's hone.db. Requires brief
# downtime — the app must not hold the DB open during the swap.
#
#   cd core && ./scripts/restore.sh backups/2026-05-29-101500
#   cd core && ./scripts/restore.sh --db-only backups/2026-05-29-101500
#
# By default the backup's tls/ + config.yaml are restored too (full identity
# restore — what you want when rebuilding the same server). Pass --db-only to
# restore ONLY hone.db, leaving the target's existing TLS cert + config in
# place — for moving corpus data onto an already-provisioned server without
# disturbing its identity (nodes keep their pinned TLS trust).
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICE=hone-core
# docker compose prefixes the volume with the project name (compose `name:`).
VOLUME=hone-core_hone-core-data
# The `hone` user the image runs as (core/Dockerfile) — files must end up
# owned by it after the root-owned cp below.
DB_UID=10001

db_only=0
src=""
for arg in "$@"; do
  case "$arg" in
    --db-only) db_only=1 ;;
    -*) echo "unknown option: $arg" >&2
        echo "usage: $0 [--db-only] backups/<timestamp>" >&2; exit 2 ;;
    *)  src="$arg" ;;
  esac
done
if [ -z "$src" ]; then
  echo "usage: $0 [--db-only] backups/<timestamp>" >&2
  exit 2
fi
if [ ! -f "$src/hone.db" ]; then
  echo "error: '$src/hone.db' not found — point at a backup dir from backup.sh" >&2
  exit 2
fi
abs_src="$(cd "$src" && pwd)"

if [ "$db_only" -eq 1 ]; then
  scope="hone.db ONLY (tls/ + config.yaml left untouched)"
else
  scope="hone.db + tls/ + config.yaml"
fi
echo "This OVERWRITES the live hone-core volume '$VOLUME' from:"
echo "    $src"
echo "    scope: $scope"
read -r -p "Proceed? [y/N] " ans
case "$ans" in
  y|Y) ;;
  *) echo "aborted"; exit 1 ;;
esac

echo "Stopping $SERVICE…"
docker compose stop "$SERVICE"

# Swap the files in via a throwaway container that mounts the volume. Drop the
# stale -wal/-shm (the backup is a complete file; an old WAL would corrupt it)
# and restore ownership to the image's user after the root cp. tls/ + config
# are restored unless --db-only.
restore_cmd="
  set -e
  cp /b/hone.db /data/hone.db
  rm -f /data/hone.db-wal /data/hone.db-shm
  chown $DB_UID:$DB_UID /data/hone.db
"
if [ "$db_only" -eq 0 ]; then
  restore_cmd="$restore_cmd
  if [ -d /b/tls ]; then rm -rf /data/tls; cp -r /b/tls /data/tls; chown -R $DB_UID:$DB_UID /data/tls; fi
  if [ -f /b/config.yaml ]; then cp /b/config.yaml /data/config.yaml; chown $DB_UID:$DB_UID /data/config.yaml; fi
"
fi
docker run --rm -v "$VOLUME:/data" -v "$abs_src:/b:ro" python:3.14-slim \
  sh -c "$restore_cmd"

echo "Starting $SERVICE…"
docker compose start "$SERVICE"
echo "Restore complete from $src ($scope)"
