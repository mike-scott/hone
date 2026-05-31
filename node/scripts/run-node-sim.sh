#!/usr/bin/env bash
# Run hone-node natively (no container) so the `claude` CLI backend uses the
# host's own OAuth session under ~/.claude instead of a bind-mounted copy.
#
# This is a developer simulator: it points the node at a throwaway data dir so
# it never touches the containerized node's enrollment/identity, but otherwise
# starts the real `python -m node` loop and claims work from hone-core exactly
# as a production node would.
#
#   node/scripts/run-node-sim.sh            # uses ./.sim as the data dir
#   SIM_DIR=/tmp/foo node/scripts/run-node-sim.sh
#
# Settings come from node/.env (HONE_CORE_URL, HONE_FLEET_SECRET, model, ...);
# the overrides below force the CLI backend, a distinct node name, and the
# isolated data dir on top of it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

VENV_PY="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
    echo "error: $VENV_PY not found — create the venv first" >&2
    exit 1
fi

ENV_FILE="$REPO_ROOT/node/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    echo "warning: $ENV_FILE not found — relying on the ambient environment" >&2
fi

SIM_DIR="${SIM_DIR:-$REPO_ROOT/.sim}"
mkdir -p "$SIM_DIR/data"

# Force the CLI backend (host OAuth), an isolated data dir so we don't disturb
# the container node's identity/enrollment, and a recognisable node name.
export HONE_CLAUDE_BACKEND=cli
export HONE_DATA="$SIM_DIR/data"
export HONE_NODE_NAME="${HONE_NODE_NAME:-$(hostname)-sim}"

echo "hone-node simulator"
echo "  repo:    $REPO_ROOT"
echo "  data:    $HONE_DATA"
echo "  core:    ${HONE_CORE_URL:-<unset>}"
echo "  node:    $HONE_NODE_NAME"
echo "  backend: $HONE_CLAUDE_BACKEND"
echo

exec "$VENV_PY" -m node
