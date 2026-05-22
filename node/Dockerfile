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

FROM python:3.13-slim

# Container-friendly Python: flushed logs, no .pyc clutter, no pip cache.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# git — the node builds its own reference kernel repo and checks out base
# commits to review against. The only system package beyond the slim base:
# the review is pure analysis, so there is NO kernel build toolchain.
RUN apt-get update \
 && apt-get install --no-install-recommends -y git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, in their own layer — editing app code below does not
# bust the (slow) pip-install layer.
COPY node/requirements.txt node/requirements.txt
RUN pip install --no-cache-dir -r node/requirements.txt

# Application code. common/ is shared with hone-core; node/ is the worker.
COPY common/ common/
COPY node/   node/

# Run unprivileged; the reference repo + scratch live on a persistent volume.
RUN useradd --create-home --uid 10001 hone \
 && mkdir -p /data/linux /data/scratch \
 && chown -R hone:hone /app /data
USER hone

# Persistent state — one mapped volume holds the node's reference kernel git
# repo and its scratch storage (in-flight claims/results across outages).
ENV HONE_DATA=/data \
    HONE_REPO_DIR=/data/linux \
    HONE_SCRATCH_DIR=/data/scratch
VOLUME ["/data"]

# No EXPOSE and no HEALTHCHECK: the node serves nothing and only dials out.
# Liveness is "the process runs"; docker-compose restarts it (unless-stopped).

# exec form → the worker is PID 1 and receives SIGTERM directly for a clean
# stop. GATHER, the web UI and the database all live in hone-core, not here.
CMD ["python", "-m", "node"]
