"""GATHER stage — the in-process scheduled task that pulls patchsets from the
data sources into the corpus (see ../ARCHITECTURE.md and ../SOURCES.md).

Each pass lists every gather module's qualifying patchsets, dedups them against
the corpus, and pulls + ingests the new ones: the patchset row, its `.tar.zst`
patch archive, the external review signal, and a claimable review.

A pass is blocking (HTTP / git), so gather_once runs it in a worker thread on
its own database connection — the event loop and the request handlers keep
running while a pass is in flight.
"""
import asyncio
import io
import logging
import os
import sys
import tarfile
import tempfile

import zstandard

from core import core_db

# The gather modules live in a hyphen-named directory — not an importable
# package — so put it on sys.path and import the gather-module framework by
# name. gather_api.load() then path-loads the individual modules, and they all
# resolve to this one gather_api (so the GatherModule isinstance checks hold).
_MODULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "gather-modules")
if _MODULES_DIR not in sys.path:
    sys.path.insert(0, _MODULES_DIR)
import gather_api  # noqa: E402  — deliberately after the sys.path insert

log = logging.getLogger("hone.gather")


def _tar_zst(patch_dir, files) -> bytes:
    """Pack the pulled patch files into a `.tar.zst` archive — the blob format
       a node fetches and a re-evaluation run re-materializes from (SOURCES.md).
       Empty input -> empty bytes."""
    names = sorted(os.path.basename(f) for f in files)
    if not names:
        return b""
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        for name in names:
            tar.add(os.path.join(patch_dir, name), arcname=name)
    return zstandard.ZstdCompressor().compress(tar_buf.getvalue())


def _ingest(db, module, ref) -> None:
    """Pull one new patchset and ingest it into the corpus: the patchset row,
       its patch archive, the source's review findings, and a claimable
       review. Not atomic — a failure part way leaves a partial row, which the
       next pass treats as already handled (`is_handled`)."""
    root = ref.root_message_id
    base_commit = module.base(ref.id)
    with tempfile.TemporaryDirectory(prefix="hone-gather-") as tmp:
        files = module.pull(ref.id, tmp)
        blob = _tar_zst(tmp, files)
        core_db.upsert_patchset(db, root, subject=ref.subject,
                                source=module.name, sent=ref.sent,
                                n_patches=len(files), base_commit=base_commit)
        core_db.record_patchset_source(db, root, module.name, ref.id)
        if blob:
            core_db.set_patch_blob(db, root, blob)
    for seq, finding in enumerate(module.findings(ref.id)):
        core_db.record_source_finding(
            db, root, module.name,
            ref=finding.message_id or str(seq),
            kind=finding.type, reviewer=finding.reviewer,
            reviewer_email=finding.reviewer_email, text=finding.text,
            severity=finding.severity, preexisting=finding.preexisting,
            sent=finding.date)
    core_db.enqueue_reviews_for_patchset(db, root)


def _gather_source(db, module) -> dict:
    """List one module's qualifying patchsets and ingest the new ones. Returns
       a {seen, gathered, skipped, known} tally. A patchset that fails to
       ingest is logged and skipped — one bad patchset never aborts the pass."""
    stats = {"seen": 0, "gathered": 0, "skipped": 0, "known": 0}
    for ref in module.list():
        stats["seen"] += 1
        root = ref.root_message_id
        if not root:
            continue
        if core_db.is_handled(db, root):
            stats["known"] += 1               # already in the corpus — dedup
            continue
        if ref.skip_reason:
            core_db.mark_skipped(db, root, ref.skip_reason, ref.subject)
            stats["skipped"] += 1
            continue
        try:
            _ingest(db, module, ref)
            stats["gathered"] += 1
        except (Exception, SystemExit):
            log.exception("GATHER: %s/%s — ingest failed", module.name, ref.id)
    return stats


def _gather_pass(cfg) -> dict:
    """One synchronous GATHER pass over every registered gather module, on its
       own database connection. Returns a per-module tally."""
    db = core_db.connect(cfg.db_path)
    tally = {}
    try:
        for name in gather_api.available():
            try:
                tally[name] = _gather_source(db, gather_api.load(name))
            except (Exception, SystemExit) as exc:
                log.warning("GATHER: source %r unavailable — %s", name, exc)
                tally[name] = "failed"
    finally:
        db.close()
    return tally


async def gather_once(cfg) -> None:
    """Run one GATHER pass. Blocking (HTTP / git), so it runs in a worker
       thread; the event loop keeps serving requests meanwhile."""
    tally = await asyncio.to_thread(_gather_pass, cfg)
    log.info("GATHER pass complete — %s",
             "; ".join(f"{name}: {t}" for name, t in tally.items())
             or "no sources")


async def gather_loop(cfg) -> None:
    """Run gather_once() every `cfg.gather_interval` seconds until cancelled.
       A failed pass is logged and the loop continues — one bad pass must not
       stop GATHER."""
    log.info("GATHER loop started — interval %ds", cfg.gather_interval)
    while True:
        try:
            await gather_once(cfg)
        except Exception:
            log.exception("GATHER pass failed")
        await asyncio.sleep(cfg.gather_interval)
