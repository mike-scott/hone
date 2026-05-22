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
# hone-core generates its own TLS CA + server certificate on first start
# (stored on the data volume) and serves HTTPS directly on port 8000 — there
# is no external TLS-terminating proxy.

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
 && mkdir -p /data/archive /data/methodology /data/tls \
 && chown -R hone:hone /app /data
USER hone

# Persistent state — one mapped volume holds everything hone-core
# owns: the SQLite db, its config, methodology import/export files, and the
# gathered patchset-source archives (public-inbox clones).
ENV HONE_DATA=/data \
    HONE_DB=/data/hone.db \
    HONE_CONFIG=/data/config.yaml \
    HONE_METHODOLOGY_DIR=/data/methodology \
    HONE_ARCHIVE_DIR=/data/archive \
    HONE_CERT_DIR=/data/tls
VOLUME ["/data"]
EXPOSE 8000

# Liveness probe — the app must serve GET /healthz over HTTPS. The cert is
# self-generated, so the probe does not verify it. (python, not curl: the
# slim image ships no curl.)
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:8000/healthz', context=ssl._create_unverified_context())"

# `python -m core.main` runs main.run(): it generates the TLS material on
# first start, then serves the FastAPI app over HTTPS (GATHER runs in-process).
# exec form → the process is PID 1 and gets SIGTERM directly for a clean stop.
CMD ["python", "-m", "core.main"]
