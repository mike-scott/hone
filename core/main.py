"""hone-core — application entry point.

Builds the FastAPI app, wires the v1 REST API and the operator web UI, and
runs the GATHER task in-process. See ../ARCHITECTURE.md.

Run:  uvicorn core.main:app   (the container does this; see core/Dockerfile)
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from core import api, core_db, gather, tls, ui
from core.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("hone.core")

_HERE = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: Config = app.state.config
    log.info("hone-core starting — db=%s", cfg.db_path)
    db = core_db.connect(cfg.db_path)
    version = core_db.bootstrap_methodology(
        db,
        os.path.join(_HERE, "default-methodology.yaml"),
        os.path.join(_HERE, "methodology.schema.yaml"))
    app.state.db = db
    log.info("database ready — methodology active at version %d", version)
    # TLS: generate the CA + server certificate on first start, and hold the
    # CA in memory — every enrollment hands it to the node (API.md → token).
    tls.ensure_certs(cfg.cert_dir, [cfg.hostname])
    app.state.ca_cert_pem = tls.ca_cert_pem(cfg.cert_dir)
    log.info("TLS material ready — cert_dir=%s", cfg.cert_dir)
    gather_task = asyncio.create_task(gather.gather_loop(cfg.gather_interval))
    try:
        yield
    finally:
        gather_task.cancel()
        db.close()
        log.info("hone-core stopping")


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
       transport)."""
    import uvicorn

    cfg: Config = app.state.config
    _, cert_file, key_file = tls.ensure_certs(cfg.cert_dir, [cfg.hostname])
    log.info("hone-core serving HTTPS on :%d", cfg.http_port)
    uvicorn.run(app, host="0.0.0.0", port=cfg.http_port,
                ssl_certfile=cert_file, ssl_keyfile=key_file)


if __name__ == "__main__":
    run()
