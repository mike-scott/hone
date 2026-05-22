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

from core import api, gather, ui
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
    # TODO: open and migrate the database (the multi-tenant schema).
    # TODO: bootstrap the methodology — import default-methodology.yaml as
    #       DB version 1 if the methodology store is empty.
    gather_task = asyncio.create_task(gather.gather_loop(cfg.gather_interval))
    try:
        yield
    finally:
        gather_task.cancel()
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
