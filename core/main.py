"""hone-core — application entry point.

Builds the FastAPI app, wires the v1 REST API and the operator web UI, and
runs the GATHER task in-process. See ../ARCHITECTURE.md.

Run:  python -m core.main   (the container does this; see core/Dockerfile)
"""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

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
    # Migrate + bootstrap on a dedicated startup connection, then serve the
    # routes through ThreadLocalDB — one connection per thread, because
    # FastAPI spreads sync dependencies/handlers across threadpool workers
    # and a sqlite3 connection must never be used by two threads at once.
    setup_db = core_db.connect(cfg.db_path)
    _project_root = os.path.dirname(_HERE)
    version = core_db.bootstrap_methodology(
        setup_db,
        os.path.join(_HERE, "default-methodology.yaml"),
        os.path.join(_project_root, "common", "schema",
                     "methodology.schema.yaml"))
    setup_db.close()
    db = core_db.ThreadLocalDB(cfg.db_path)
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
    # Site-settings page partial (`core.ui /site-settings/lore-clone-status`). The
    # initial snapshot reflects what's on disk; the autoclone path mutates
    # it as the clone runs. In-memory: a restart starts fresh (and re-runs
    # autoclone if the env var is still set).
    app.state.lore_clone = _initial_lore_status()
    # The Settings "Provision now" button calls this to kick off a clone
    # without a restart (ui.py POST /site-settings/lore-clone). Stored on
    # app.state so the router needn't import main (avoids a cycle).
    app.state.lore_clone_task = None
    app.state.trigger_lore_clone = lambda: trigger_lore_clone(app)
    autoclone_task = None
    if os.environ.get("HONE_LORE_AUTOCLONE"):
        autoclone_task = asyncio.create_task(
            _autoclone_lore(app), name="lore-autoclone")
    gather_task = asyncio.create_task(gather.gather_supervisor(app))
    # Crash-recovery sweep: flip lease-expired CLAIMED rows back to
    # claimable on a timer. The claim protocol already re-offers expired
    # rows lazily inside the next claim request, but on a quiet fleet
    # (the dead node was the only poller) nothing ever claims — without
    # the sweep the row sits CLAIMED forever and every status surface
    # (node row, queue, fleet pulse) reads stale state.
    reclaim_task = asyncio.create_task(
        _reclaim_sweep(app), name="reclaim-sweep")
    try:
        yield
    finally:
        reclaim_task.cancel()
        if autoclone_task and not autoclone_task.done():
            autoclone_task.cancel()
        manual_task = getattr(app.state, "lore_clone_task", None)
        if manual_task and not manual_task.done():
            manual_task.cancel()
        gather_task.cancel()
        try:
            await asyncio.wait_for(gather_task, timeout=7)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        if autoclone_task:
            try:
                await autoclone_task
            except (asyncio.CancelledError, Exception):
                pass
        db.close()
        log.info("hone-core stopping")


# --- reclaim sweep ----------------------------------------------------------

_RECLAIM_SWEEP_SECONDS = 60     # cheap: one indexed UPDATE per queue


def _run_reclaim(app):
    """One sweep: return lease-expired claims to their queues. Failures
       are logged and swallowed — a transient DB hiccup must not kill
       the loop. Factored out of the timer task so tests can drive a
       single pass synchronously."""
    try:
        w, d = core_db.reclaim_expired(app.state.db)
        if w or d:
            log.info("reclaim sweep: re-offered %d work item(s), "
                     "%d draft task(s)", w, d)
    except Exception:
        log.exception("reclaim sweep failed")


async def _reclaim_sweep(app):
    """The periodic reclaim timer — runs for the app's lifetime,
       cancelled at shutdown."""
    while True:
        await asyncio.sleep(_RECLAIM_SWEEP_SECONDS)
        _run_reclaim(app)


# --- lore-clone status (UI-readable) ---------------------------------------

_HEARTBEAT_SECONDS = 30        # cadence of the "still cloning" log line


def _initial_lore_status() -> dict:
    """The snapshot of lore-archive state at startup, before any autoclone
       work runs. `phase` is the single source of truth for which Settings
       panel branch renders; the rest is informational."""
    Lore = gather.gather_api.load("lore").__class__
    present = Lore.is_provisioned()
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


async def _run_lore_clone(app: FastAPI):
    """Run `clone_all` in a worker thread, publishing progress to
       `app.state.lore_clone` for the Site-settings page and a periodic heartbeat
       to the log (git's own progress lines flow through via lore's Popen
       reader + `--progress`). Shared by the startup autoclone and the
       Settings 'Provision now' button. On completion the phase is decided
       by what's actually on disk (`is_provisioned`): a no-op run — e.g.
       nothing configured — surfaces as an error, not a false 'ready'.
       Failures are logged + swallowed so a transient clone error never
       crashes hone-core."""
    Lore = gather.gather_api.load("lore").__class__
    state = app.state.lore_clone
    state.update(phase="cloning", percent=0, git_phase=None, last_line=None,
                 started_at=time.time(), completed_at=None, error=None)
    log.info("lore provision: starting clone")

    def on_progress(phase, percent, line):
        # Called from the worker thread; single dict writes are atomic in
        # CPython and the UI reads a snapshot.
        state["git_phase"] = phase
        state["percent"]   = percent
        state["last_line"] = line

    async def heartbeat():
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            elapsed = int(time.time() - (state["started_at"] or time.time()))
            log.info("lore provision: still running (%ds elapsed, "
                     "phase=%s percent=%d)",
                     elapsed, state["git_phase"] or "?", state["percent"])

    hb = asyncio.create_task(heartbeat(), name="lore-clone-heartbeat")
    try:
        await asyncio.to_thread(Lore.clone_all, progress=on_progress)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("lore provision: clone failed")
        state.update(phase="error", error=str(e), completed_at=time.time())
        return
    finally:
        hb.cancel()
    if Lore.is_provisioned():
        state.update(phase="ready", percent=100, completed_at=time.time(),
                     archive_present=True)
        log.info("lore provision: complete — gather resumes next tick")
    else:
        state.update(phase="error", completed_at=time.time(),
                     error="no archive provisioned — set HONE_LORE_LISTS or "
                           "HONE_LORE_URL (see core/.env.example)")
        log.warning("lore provision: nothing was provisioned (unconfigured?)")


async def _autoclone_lore(app: FastAPI):
    """Startup autoclone (HONE_LORE_AUTOCLONE): provision unless the archive
       is already present."""
    Lore = gather.gather_api.load("lore").__class__
    if Lore.is_provisioned():
        log.info("HONE_LORE_AUTOCLONE: archive already present, skipping")
        return
    await _run_lore_clone(app)


def trigger_lore_clone(app: FastAPI) -> bool:
    """Start a background lore provision unless one is already in flight —
       backs the Settings 'Provision now' button. Returns True if it started
       a run, False if a clone was already running (phase=cloning covers both
       the autoclone and a prior button press). Sets phase synchronously so
       the panel returned to the operator shows in-flight + starts polling."""
    if app.state.lore_clone.get("phase") == "cloning":
        return False
    app.state.lore_clone.update(phase="cloning", percent=0, error=None,
                                started_at=time.time(), completed_at=None)
    app.state.lore_clone_task = asyncio.create_task(
        _run_lore_clone(app), name="lore-clone-manual")
    return True


def create_app() -> FastAPI:
    app = FastAPI(title="hone-core", lifespan=lifespan)
    app.state.config = Config.from_env()
    cfg: Config = app.state.config

    # Fail-closed on the session signing key. With no real secret the
    # SessionMiddleware would otherwise fall back to a known-constant key and
    # anyone reading the source could forge an admin session cookie. There is
    # no safe "default" here — the operator must supply one.
    if not cfg.session_secret:
        raise RuntimeError(
            "HONE_SESSION_SECRET is required — generate one with "
            "`openssl rand -hex 32` and set it in core/.env. "
            "Without it the session-cookie signing key would fall back to a "
            "well-known constant and any reader of the source could forge an "
            "admin session.")

    # Session middleware must be added before routers so that route handlers
    # can read/write request.session.  https_only=True keeps the cookie off
    # plain-HTTP connections; same_site="lax" is safe for the redirect flow.
    app.add_middleware(SessionMiddleware,
                       secret_key=cfg.session_secret,
                       https_only=cfg.session_cookie_secure,
                       same_site="lax",
                       session_cookie="hone_session")

    # Per-IP failed-login limiter — caps the wasted CPU / brute-force ceiling
    # against the operator login. Lives on app.state so tests can swap in a
    # tight-limit instance without touching production constants.
    from core import auth
    app.state.login_limiter = auth.FailedAttemptLimiter(
        max_failures=auth.LOGIN_MAX_FAILURES,
        window_seconds=auth.LOGIN_FAILURE_WINDOW_SECONDS)

    app.include_router(api.router)   # /v1/* — the node-facing REST API
    # The operator web UI now uses session-based auth (login page + signed
    # cookie) with an admin-approval flow for self-registered users.  Auth
    # is enforced per-route inside ui.py via auth.require_session /
    # auth.require_config_admin; the old HTTP Basic gate is removed here.
    app.include_router(ui.router)
    app.mount("/static",
              StaticFiles(directory=os.path.join(_HERE, "static")),
              name="static")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        """Liveness probe — see core/Dockerfile's HEALTHCHECK."""
        return {"status": "ok"}

    return app


def run():
    """Container entry point — ensure the TLS material exists, then serve
       HTTPS directly with the self-generated server certificate (no external
       TLS-terminating proxy; see ARCHITECTURE.md → Auth, enrollment &
       transport).

       The FastAPI app is constructed here rather than at module top so a
       plain `import core.main` (in tests or by tooling) doesn't run the
       config-validation gates (e.g. HONE_SESSION_SECRET) before the
       container is actually starting.

       uvicorn installs its own SIGTERM / SIGINT handlers and shuts down
       gracefully on `docker stop`; `timeout_graceful_shutdown` bounds the
       connection drain so the container always exits well within docker's
       stop grace period (10 s by default) instead of being SIGKILLed
       (exit 137)."""
    import uvicorn

    app = create_app()
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
