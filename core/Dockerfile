# hone — hone-core service image.
#
# hone-core is the non-AI control plane (see ARCHITECTURE.md): a
# FastAPI service that runs the GATHER cron in-process, serves the v1 REST
# API (API.md) and the operator web UI, and owns hone.db. One instance.
#
# Build from the PROJECT ROOT — the build context must include common/,
# which is shared with the node tier:
#
#   docker build -f core/Dockerfile -t hone-core .
#
# TLS is terminated upstream (an ingress / reverse proxy); this container
# serves plain HTTP on port 8000.

FROM python:3.13-slim

# Container-friendly Python: flushed logs, no .pyc clutter, no pip cache.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# git — the GATHER stage walks public-inbox archives (e.g. linux-arm-msm).
# The only system package beyond the slim base; no compilers, no toolchain.
RUN apt-get update \
 && apt-get install --no-install-recommends -y git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, in their own layer — editing app code below does not
# bust the (slow) pip-install layer.
COPY core/requirements.txt core/requirements.txt
RUN pip install --no-cache-dir -r core/requirements.txt

# Application code. common/ is shared with the node tier; core/ is
# the service itself (its GATHER modules and the methodology seed live here).
COPY common/       common/
COPY core/ core/

# Run unprivileged; hone.db lives on a persistent volume.
RUN useradd --create-home --uid 10001 hone \
 && mkdir -p /data/archive /data/methodology \
 && chown -R hone:hone /app /data
USER hone

# Persistent state — one mapped volume holds everything hone-core
# owns: the SQLite db, its config, methodology import/export files, and the
# gathered patchset-source archives (public-inbox clones).
ENV HONE_DATA=/data \
    HONE_DB=/data/hone.db \
    HONE_CONFIG=/data/config.yaml \
    HONE_METHODOLOGY_DIR=/data/methodology \
    HONE_ARCHIVE_DIR=/data/archive
VOLUME ["/data"]
EXPOSE 8000

# Liveness probe — the app must serve GET /healthz.
# (python, not curl: the slim image ships no curl.)
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

# uvicorn runs the FastAPI app; GATHER runs as an in-process scheduled task.
# exec form → uvicorn is PID 1 and receives SIGTERM directly for a clean stop.
CMD ["uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]
