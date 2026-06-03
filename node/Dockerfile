# hone — hone-node service image.
#
# hone-node is an AI review worker (see ARCHITECTURE.md): a from-scratch
# container that claims tasks from hone-core over the v1 REST API (API.md),
# does the AI work, and reports back. It has no inbound ports — it only
# dials out to hone-core.
#
# Build from the PROJECT ROOT — the build context must include common/,
# which is shared with hone-core:
#
#   docker build -f node/Dockerfile -t hone-node .
#
# The Claude API token is supplied at runtime and never baked into the image.

FROM python:3.14-slim

# Container-friendly Python: flushed logs, no .pyc clutter, no pip cache.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# git    — the node builds its own reference kernel repo and checks out
#          base commits to review against.
# perl    — runs the kernel's get_maintainer.pl during prepare's Tier-0
#          deterministic phase (docs/ARCHITECTURE-PREPARE.md); the script
#          needs only base perl, no CPAN modules.
# No kernel build toolchain: the review is pure analysis.
RUN apt-get update \
 && apt-get install --no-install-recommends -y git curl ca-certificates perl \
 && rm -rf /var/lib/apt/lists/*

# Optional: install the `claude` CLI for HONE_CLAUDE_BACKEND=cli. Defaults
# on so the image is one-size-fits-both; set `--build-arg WITH_CLAUDE_CLI=0`
# to skip the download when you're using the SDK backend only.
#
# Pulls the per-arch release binary from
# https://github.com/anthropics/claude-code/releases — a single
# ~230 MiB static binary that replaces the entire Node + npm stack the
# upstream install method would otherwise bake into the image.
# TARGETARCH is set automatically by `docker buildx`; for a plain
# `docker build` we fall back to dpkg / uname so the layer still works.
# Pin CLAUDE_VERSION so a rebuild is reproducible — bump it deliberately
# when an upstream release adds something we depend on.
ARG WITH_CLAUDE_CLI=1
ARG CLAUDE_VERSION=v2.1.161
ARG TARGETARCH
RUN if [ "$WITH_CLAUDE_CLI" = "1" ]; then \
        ARCH="${TARGETARCH:-$(dpkg --print-architecture 2>/dev/null || uname -m)}" \
     && case "$ARCH" in \
            amd64|x86_64)   ASSET=claude-linux-x64.tar.gz ;; \
            arm64|aarch64)  ASSET=claude-linux-arm64.tar.gz ;; \
            *) echo "claude CLI: unsupported arch $ARCH" >&2; exit 1 ;; \
        esac \
     && BASE="https://github.com/anthropics/claude-code/releases/download/${CLAUDE_VERSION}" \
     && curl -fsSL "${BASE}/${ASSET}"           -o "/tmp/${ASSET}" \
     && curl -fsSL "${BASE}/SHASUMS256.txt"     -o /tmp/SHASUMS256.txt \
     && grep "  ${ASSET}$" /tmp/SHASUMS256.txt > /tmp/claude.sha256 \
     && (cd /tmp && sha256sum -c claude.sha256) \
     && tar -xzf "/tmp/${ASSET}" -C /usr/local/bin claude \
     && chmod +x /usr/local/bin/claude \
     && rm -f "/tmp/${ASSET}" /tmp/SHASUMS256.txt /tmp/claude.sha256 \
     && claude --version; \
    fi

WORKDIR /app

# Dependencies first, in their own layer — editing app code below does not
# bust the (slow) pip-install layer.
COPY node/requirements.txt node/requirements.txt
RUN pip install --no-cache-dir -r node/requirements.txt

# Application code. common/ is shared with hone-core; node/ is the worker.
COPY common/ common/
COPY node/   node/

# Run unprivileged; the reference repo + scratch live on a persistent volume.
#
# The container user's UID/GID are build-args so an operator can match
# them to the host's user — important for the HONE_CLAUDE_BACKEND=cli
# path, where the container bind-mounts the host's ~/.claude and writes
# refreshed OAuth tokens to it. Default 1000/1000 matches the first
# interactive user on most Linux distros; set the env vars in .env
# (docker-compose.yml threads them to these args) when the host user
# is at a different UID. The getent guard skips groupadd when the GID
# already names a group in the base image (rare under python:slim,
# but harmless if it happens).
ARG HONE_CLAUDE_CLI_USER=1000
ARG HONE_CLAUDE_CLI_GROUP=1000
RUN (getent group ${HONE_CLAUDE_CLI_GROUP} \
     || groupadd --gid ${HONE_CLAUDE_CLI_GROUP} hone) \
 && useradd --create-home \
            --uid ${HONE_CLAUDE_CLI_USER} \
            --gid ${HONE_CLAUDE_CLI_GROUP} hone \
 && mkdir -p /data/linux /data/scratch \
 && chown -R ${HONE_CLAUDE_CLI_USER}:${HONE_CLAUDE_CLI_GROUP} /app /data
USER hone

# Persistent state — one mapped volume holds the node's reference kernel git
# repo and its scratch storage (in-flight claims/results across outages).
# HOME is set so the `claude` CLI finds its OAuth credentials at
# $HOME/.claude (the operator bind-mounts ${HOME}/.claude from the host
# in docker-compose when running HONE_CLAUDE_BACKEND=cli).
#
# CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC disables Claude Code's non-essential
# outbound calls — telemetry (Statsig), error reporting (Sentry), the
# auto-updater, and the bug-report command (umbrella flag for DISABLE_TELEMETRY
# / DISABLE_ERROR_REPORTING / DISABLE_AUTOUPDATER / DISABLE_BUG_COMMAND). An
# unattended review worker has no use for any of it, and crucially this traffic
# does NOT honour ANTHROPIC_BASE_URL — it dials Anthropic's own domains, so
# behind a corporate SSL-inspecting proxy it fails the TLS handshake with a
# "self-signed certificate" error. Baked in; override via compose `environment`
# if a deployment ever needs it on.
ENV HOME=/home/hone \
    HONE_DATA=/data \
    HONE_REPO_DIR=/data/linux \
    HONE_SCRATCH_DIR=/data/scratch \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1
VOLUME ["/data"]

# No EXPOSE and no HEALTHCHECK: the node serves nothing and only dials out.
# Liveness is "the process runs"; docker-compose restarts it (unless-stopped).

# exec form → the worker is PID 1 and receives SIGTERM directly for a clean
# stop. GATHER, the web UI and the database all live in hone-core, not here.
CMD ["python", "-m", "node"]
