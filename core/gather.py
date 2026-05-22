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


def _select_sources(wanted, available):
    """The gather modules to run this pass — the operator's enabled sources
       (`gather.sources`) intersected with what is installed. An empty set
       gathers nothing (GATHER paused); it does NOT mean "everything". A
       configured source that is not installed is dropped with a warning."""
    unknown = [s for s in wanted if s not in available]
    if unknown:
        log.warning("GATHER: enabled source(s) %s are not installed — ignored "
                    "(installed: %s)",
                    ", ".join(unknown), ", ".join(available) or "none")
    return [s for s in wanted if s in available]


def _gather_pass(db_path, gather_sources) -> dict:
    """One synchronous GATHER pass over the selected gather modules, on its
       own database connection. Returns a per-module tally."""
    db = core_db.connect(db_path)
    tally = {}
    try:
        for name in _select_sources(gather_sources, gather_api.available()):
            try:
                tally[name] = _gather_source(db, gather_api.load(name))
            except (Exception, SystemExit) as exc:
                log.warning("GATHER: source %r unavailable — %s", name, exc)
                tally[name] = "failed"
    finally:
        db.close()
    return tally


async def gather_once(app) -> None:
    """Run one GATHER pass. Blocking (HTTP / git), so it runs in a worker
       thread; the event loop keeps serving requests meanwhile. The live
       runtime config is read here, so a Settings change applies next pass."""
    tally = await asyncio.to_thread(
        _gather_pass, app.state.config.db_path,
        app.state.runtime_config.gather_sources)
    log.info("GATHER pass complete — %s",
             "; ".join(f"{name}: {t}" for name, t in tally.items())
             or "no sources")


async def gather_loop(app) -> None:
    """Run gather_once() every `gather.interval_seconds` until cancelled — the
       cadence is re-read from the live runtime config each cycle, so an
       operator's Settings change applies on the next pass. A failed pass is
       logged and the loop continues — one bad pass must not stop GATHER."""
    log.info("GATHER loop started")
    while True:
        try:
            await gather_once(app)
        except Exception:
            log.exception("GATHER pass failed")
        await asyncio.sleep(app.state.runtime_config.gather_interval)
