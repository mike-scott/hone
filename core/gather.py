"""GATHER stage — the in-process scheduled task that pulls patchsets from the
data sources into the corpus (see ../ARCHITECTURE.md and ../SOURCES.md).

Skeleton: the scheduling loop is real; the gather pass itself is a stub.
"""
import asyncio
import logging

log = logging.getLogger("hone.gather")


async def gather_once() -> None:
    """Run one GATHER pass.

    TODO: for each registered gather module — list qualifying patchsets,
    dedup against the ledger, pull + ingest the new ones, mark them pulled.
    """
    log.info("GATHER tick — not yet implemented")


async def gather_loop(interval_seconds: int) -> None:
    """Run gather_once() every `interval_seconds` until cancelled. A failed
    pass is logged and the loop continues — one bad pass must not stop GATHER."""
    log.info("GATHER loop started — interval %ds", interval_seconds)
    while True:
        try:
            await gather_once()
        except Exception:
            log.exception("GATHER pass failed")
        await asyncio.sleep(interval_seconds)
