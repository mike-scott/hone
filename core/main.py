"""hone-core — application entry point.

Builds the FastAPI app, wires the v1 REST API and the operator web UI, and
runs the GATHER task in-process. See ../ARCHITECTURE.md.

Run:  uvicorn core.main:app   (the container does this; see core/Dockerfile)
"""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from core import api, core_db, gather, runtime_config, tls, ui
from core.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("hone.core")


class _QuietIdlePollFilter(logging.Filter):
    """Drop uvicorn access-log records for the queue page's idle-poll
       204s — they fire every 5 seconds per open operator session and
       contribute nothing to ops awareness. Real GETs (200) and any
       other endpoint's access logs are unaffected.

       uvicorn.access formats with `record.args` as
       `(client, method, full_path, http_version, status)` — we match
       on the structured args, not the formatted string, so a future
       log-format change can't silently break the filter."""

    def filter(self, record):
        args = record.args
        if not (isinstance(args, tuple) and len(args) >= 5):
            return True
        method, path, status_code = args[1], args[2], args[4]
        if (method == "GET" and status_code == 204
                and (path == "/" or path.startswith("/?"))):
            return False
        return True


logging.getLogger("uvicorn.access").addFilter(_QuietIdlePollFilter())

_HERE = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: Config = app.state.config
    log.info("hone-core starting — db=%s", cfg.db_path)
    db = core_db.connect(cfg.db_path)
    _project_root = os.path.dirname(_HERE)
    version = core_db.bootstrap_methodology(
        db,
        os.path.join(_HERE, "default-methodology.yaml"),
        os.path.join(_project_root, "common", "schema",
                     "methodology.schema.yaml"))
    app.state.db = db
    log.info("database ready — methodology active at version %d", version)
    # TLS: generate the CA + server certificate on first start, and hold the
    # CA in memory — every enrollment hands it to the node (API.md → token).
    tls.ensure_certs(cfg.cert_dir, [cfg.hostname])
    app.state.ca_cert_pem = tls.ca_cert_pem(cfg.cert_dir)
    log.info("TLS material ready — cert_dir=%s", cfg.cert_dir)
    # Operator-tunable runtime config (config.yaml on the data volume) — held
    # live in app.state so a Settings change applies without a restart.
    app.state.runtime_config = runtime_config.load(
        cfg.config_path, all_sources=gather.gather_api.available())
    log.info("runtime config ready — %s", cfg.config_path)
    # Opt-in background autoclone for the lore archive — for unattended /
    # IaC deployments that want hone-core to self-bootstrap. The service
    # comes up clean either way (lore.list() is a no-op while the archive
    # is missing — see core/gather-modules/lore.py); autoclone just runs
    # the same `clone` helper in the background on startup, so gather
    # picks the archive up on the next supervisor tick.
    # Lore-clone status — published by the autoclone task, polled by the
    # Settings page partial (`core.ui /settings/lore-clone-status`). The
    # initial snapshot reflects what's on disk; the autoclone path mutates
    # it as the clone runs. In-memory: a restart starts fresh (and re-runs
    # autoclone if the env var is still set).
    app.state.lore_clone = _initial_lore_status()
    autoclone_task = None
    if os.environ.get("HONE_LORE_AUTOCLONE"):
        autoclone_task = asyncio.create_task(
            _autoclone_lore(app), name="lore-autoclone")
    gather_task = asyncio.create_task(gather.gather_supervisor(app))
    try:
        yield
    finally:
        if autoclone_task and not autoclone_task.done():
            autoclone_task.cancel()
        gather_task.cancel()
        try:
            await gather_task          # let the supervisor stop its tasks
        except asyncio.CancelledError:
            pass
        if autoclone_task:
            try:
                await autoclone_task
            except (asyncio.CancelledError, Exception):
                pass
        db.close()
        log.info("hone-core stopping")


# --- lore-clone status (UI-readable) ---------------------------------------

_HEARTBEAT_SECONDS = 30        # cadence of the "still cloning" log line


def _initial_lore_status() -> dict:
    """The snapshot of lore-archive state at startup, before any autoclone
       work runs. `phase` is the single source of truth for which Settings
       panel branch renders; the rest is informational."""
    Lore = gather.gather_api.load("lore").__class__
    present = os.path.isdir(os.path.join(Lore.archive_dir, ".git"))
    return {"phase":         "ready" if present else "absent",
            "percent":       100 if present else 0,
            "git_phase":     None,
            "last_line":     None,
            "started_at":    None,
            "completed_at":  None,
            "error":         None,
            "archive_present": present,
            "archive_path":  Lore.archive_dir,
            "autoclone_enabled": bool(os.environ.get("HONE_LORE_AUTOCLONE"))}


async def _autoclone_lore(app: FastAPI):
    """Run the lore `clone` helper in a worker thread; publish progress to
       `app.state.lore_clone` for the Settings page, and log a periodic
       heartbeat so the operator tailing `docker logs` knows it's alive
       (git's own dynamic progress lines flow through too, via lore's
       Popen reader + `--progress`). Log + swallow any failure so a
       transient clone error never crashes hone-core; the service stays
       up; the gather supervisor finds the archive on a later tick once
       the operator (or a retry) fixes whatever failed."""
    Lore = gather.gather_api.load("lore").__class__
    state = app.state.lore_clone

    # Idempotent: skip if the archive is already present.
    if state["archive_present"]:
        log.info("HONE_LORE_AUTOCLONE: archive already present, skipping")
        return

    state.update(phase="cloning", percent=0, git_phase=None,
                 last_line=None, started_at=time.time(),
                 completed_at=None, error=None)
    log.info("HONE_LORE_AUTOCLONE: starting background lore clone")

    def on_progress(phase, percent, line):
        # Called from the worker thread; writes to `state` are single dict
        # ops (atomic in CPython) and the UI reads a snapshot.
        state["git_phase"] = phase
        state["percent"]   = percent
        state["last_line"] = line

    async def heartbeat():
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            elapsed = int(time.time() - (state["started_at"] or time.time()))
            log.info("HONE_LORE_AUTOCLONE: clone still running "
                     "(%ds elapsed, phase=%s percent=%d)",
                     elapsed, state["git_phase"] or "?", state["percent"])

    hb = asyncio.create_task(heartbeat(), name="lore-autoclone-heartbeat")
    try:
        cloned = await asyncio.to_thread(Lore.clone, progress=on_progress)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("HONE_LORE_AUTOCLONE: clone failed; lore gather "
                      "stays paused until the archive is provisioned")
        state.update(phase="error", error=str(e),
                     completed_at=time.time())
        return
    finally:
        hb.cancel()
    state.update(phase="ready", percent=100, completed_at=time.time(),
                 archive_present=True)
    log.info("HONE_LORE_AUTOCLONE: %s — lore gather will resume on the "
             "next supervisor tick",
             "clone complete" if cloned else "archive already present")


def create_app() -> FastAPI:
    app = FastAPI(title="hone-core", lifespan=lifespan)
    app.state.config = Config.from_env()

    app.include_router(api.router)   # /v1/* — the node-facing REST API
    app.include_router(ui.router)    # the operator web UI
    app.mount("/static",
              StaticFiles(directory=os.path.join(_HERE, "static")),
              name="static")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        """Liveness probe — see core/Dockerfile's HEALTHCHECK."""
        return {"status": "ok"}

    return app


app = create_app()


def run():
    """Container entry point — ensure the TLS material exists, then serve
       HTTPS directly with the self-generated server certificate (no external
       TLS-terminating proxy; see ARCHITECTURE.md → Auth, enrollment &
       transport).

       uvicorn installs its own SIGTERM / SIGINT handlers and shuts down
       gracefully on `docker stop`; `timeout_graceful_shutdown` bounds the
       connection drain so the container always exits well within docker's
       stop grace period (10 s by default) instead of being SIGKILLed
       (exit 137)."""
    import uvicorn

    cfg: Config = app.state.config
    _, cert_file, key_file = tls.ensure_certs(cfg.cert_dir, [cfg.hostname])
    log.info("hone-core serving HTTPS on :%d", cfg.http_port)
    # log_config=None: skip uvicorn's own logging dictConfig so its
    # `uvicorn`, `uvicorn.access`, and `uvicorn.error` loggers use
    # the root logger we configured at module top — same
    # `%(asctime)s %(name)s %(levelname)s %(message)s` format as
    # `hone.core` lines, instead of uvicorn's default that omits
    # the timestamp entirely. The `_QuietIdlePollFilter` attached
    # to `uvicorn.access` keeps working because the filter is on
    # the logger, not the handler.
    uvicorn.run(app, host="0.0.0.0", port=cfg.http_port,
                ssl_certfile=cert_file, ssl_keyfile=key_file,
                timeout_graceful_shutdown=8, log_config=None)


if __name__ == "__main__":
    run()
