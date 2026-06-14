"""GATHER stage — pulls patchsets, patch messages, and review comments from
the data sources into the corpus (see ../ARCHITECTURE.md and ../SOURCES.md).

A per-source gather cycle consumes the module's stream of refs and ingests
each into the corpus: a `PatchsetRef` upserts a patchset (and its list
tags); a `MessageRef` upserts a thread email — cover, patch, or comment.
The opaque per-source cursor is persisted after each ref is accounted for,
so a cycle cut short by a rate limit, a stall or a crash leaves an accurate
resume point.

Execution model: `gather_supervisor` runs one `asyncio` task per enabled
source. Each task is a single gather cycle and may run long (a throttled
backfill). The supervisor re-spawns a source every `gather.interval_seconds`,
but never while that source's previous task is still running (an overrunning
cycle keeps its slot), and it cancels a task whose liveness heartbeat has
gone stale. The blocking ingest work runs in a worker thread on its own
database connection, so the event loop and request handlers keep running.
"""
import asyncio
import logging
import os
import sys
import time

from core import core_db

# The gather modules live in a hyphen-named directory — not an importable
# package — so put it on sys.path and import the gather-module framework by
# name. gather_api.load() then path-loads the individual modules.
_MODULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "gather-modules")
if _MODULES_DIR not in sys.path:
    sys.path.insert(0, _MODULES_DIR)
import gather_api  # noqa: E402  — deliberately after the sys.path insert

log = logging.getLogger("hone.gather")


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


def _describe_ref(ref):
    """A compact one-line identifier for a ref — what the framework writes
       to its logs in place of `%r ref`. Drops `body` (a comment's review
       prose or a patch's diff; verbose, irrelevant to the log line) and
       keeps just the fields a reader needs to find the ref in the corpus
       or in the source."""
    if isinstance(ref, gather_api.PatchsetRef):
        return (f"PatchsetRef(root={ref.root_message_id!r}, "
                f"subject={ref.subject!r}, n_patches={ref.n_patches})")
    if isinstance(ref, gather_api.MessageRef):
        type_name = core_db.MSG_TYPE_NAMES.get(ref.type, ref.type)
        return (f"MessageRef(type={type_name}, "
                f"message_id={ref.message_id!r}, "
                f"parent={ref.parent_message_id!r}, "
                f"root={ref.root_message_id!r})")
    return repr(ref)


def _ingest_ref(db, module, ref):
    """Apply one ref to the corpus. Returns the stat key the ref counts
       under ('patchsets' | 'messages' | 'skipped'). Raises on ingest
       failure.

       After upserting, fire the auto-enqueue trigger:
       - a new patchset enqueues a `prepare` work-item (one per root
         Message-ID).
       Review is NOT auto-enqueued — an operator requests it per patchset
       from the detail page (core/ui.py → request_review), so a gather
       run (potentially 100k patchsets) never floods the queue with
       reviews. The gate still lives in core_db.maybe_enqueue_review
       (prepare's patchset_metadata row + every patch present), which the
       manual trigger reuses.

       Comments are upserted into `messages` but trigger no work-items;
       training is session-driven and creates train work-items at session
       materialisation, not at gather time. See
       docs/ARCHITECTURE-TRAINING.md."""
    if isinstance(ref, gather_api.PatchsetRef):
        if ref.skip_reason:
            core_db.mark_skipped(db, ref.root_message_id, ref.skip_reason,
                                 subject=ref.subject)
            return "skipped"
        # Operator's list-tag filter (Site-settings page): when the operator has
        # enabled any tags, patchsets whose tags don't intersect the set are
        # recorded as skipped instead of fully ingested. An empty enabled
        # set means no filter (gather everything the modules surface).
        enabled = set(core_db.enabled_tags(db))
        if enabled and not (set(ref.list_tags or ()) & enabled):
            core_db.mark_skipped(db, ref.root_message_id, "tag-not-enabled",
                                 subject=ref.subject)
            return "skipped"
        core_db.upsert_patchset(
            db, ref.root_message_id,
            subject=ref.subject or None,
            submitter_email=ref.submitter_email or None,
            sent=ref.sent, n_patches=ref.n_patches,
            base_commit=ref.base_commit, change_id=ref.change_id,
            series_version=ref.series_version)
        if ref.list_tags:
            core_db.set_patchset_tags(db, ref.root_message_id, ref.list_tags)
        # Every non-skipped patchset gets a prepare. Review is operator-
        # triggered per patchset (not auto-enqueued), so nothing else
        # fires here.
        core_db.maybe_enqueue_prepare(db, ref.root_message_id)
        return "patchsets"
    if isinstance(ref, gather_api.MessageRef):
        # Detect a genuinely-new comment BEFORE the upsert: upsert_message is
        # ON CONFLICT DO UPDATE, so it can't tell new from re-seen, and gather
        # re-runs the whole stream. Only new comments fan out a notification.
        is_new_comment = (
            ref.type == core_db.MSG_TYPE_COMMENT
            and db.execute("SELECT 1 FROM messages WHERE message_id=?",
                           (core_db.norm_msgid(ref.message_id),)).fetchone()
                is None)
        core_db.upsert_message(
            db, ref.message_id,
            root_message_id=ref.root_message_id,
            type=ref.type, body=ref.body,
            part_index=ref.part_index,
            parent_message_id=ref.parent_message_id,
            author_name=ref.author_name or None,
            author_email=ref.author_email or None,
            subject=ref.subject or None, sent=ref.sent)
        # A landing patch message may complete the patchset, but review is
        # operator-triggered, not auto-enqueued — nothing fires here. A new
        # comment notifies anyone tracking the series (no-op when unowned —
        # the common case). The (user, dedup_key) UNIQUE is the real re-run
        # guard; the pre-check just skips the work for re-seen comments.
        if is_new_comment:
            who = ref.author_name or ref.author_email or "someone"
            core_db.notify_patchset_users(
                db, ref.root_message_id,
                type=core_db.NOTIF_TYPE_NEW_COMMENT,
                dedup_key=f"comment:{core_db.norm_msgid(ref.message_id)}",
                title=f"New comment from {who}")
        return "messages"
    raise TypeError(f"{module.name}: unknown ref type {type(ref).__name__}")


def _gather_source(db, module, beat=None) -> dict:
    """Consume one module's stream of refs into the corpus. The watermark is
       advanced and persisted *after each ref is accounted for*, so a cycle
       cut short by a rate limit, a stall, or a crash leaves an accurate
       resume point. The watermark only advances across a contiguous run of
       accounted-for refs: it freezes at the first ingest failure, so the
       affected ref (and everything after it) is retried next cycle rather
       than skipped.

       `beat`, if given, is called once per ref — the supervisor's liveness
       heartbeat. Returns a {patchsets, messages, skipped, failed} tally;
       an ingest failure is logged and continues the cycle."""
    stats = {"patchsets": 0, "messages": 0, "skipped": 0, "failed": 0}
    mark = core_db.get_gather_state(db, module.name)
    state = gather_api.GatherState(cursor=mark)
    advancing = True               # cleared at the first ingest failure
    stream = module.list(state, db)
    try:
        for ref in stream:
            if beat:
                beat()
            try:
                outcome = _ingest_ref(db, module, ref)
            except (Exception, SystemExit):
                log.exception("GATHER: %s — ingest failed for %s",
                              module.name, _describe_ref(ref))
                stats["failed"] += 1
                advancing = False
                continue
            stats[outcome] += 1
            # advance the watermark only across the contiguous run of
            # accounted refs, persisting each step.
            if advancing and ref.cursor and ref.cursor != mark:
                mark = ref.cursor
                core_db.set_gather_state(db, module.name, mark)
    finally:
        stream.close() if hasattr(stream, "close") else None
    return stats


# --- the GATHER supervisor -------------------------------------------------
# One asyncio task per enabled source. A task is a single gather cycle and
# may run long (a throttled backfill); the supervisor re-spawns a source
# every gather.interval_seconds but never while its previous task is still
# running, and cancels a task whose heartbeat has gone stale.
_TICK_SECONDS = 30        # supervisor housekeeping cadence
_STALL_SECONDS = 1800     # no heartbeat for this long => the task is wedged
                          # (generous: a throttled gather task runs slow)


def _gather_source_cycle(db_path, name, beat):
    """Worker-thread entry point: one source's gather cycle on its own
       database connection."""
    db = core_db.connect(db_path)
    try:
        return _gather_source(db, gather_api.load(name), beat)
    finally:
        db.close()


async def _run_source(app, name, beats):
    """One gather cycle for `name`, as an asyncio task. The blocking ingest
       work runs in a worker thread; `beats[name]` is bumped per ref so the
       supervisor can tell a wedged task from a slow but healthy one. Never
       raises (bar cancellation) — a failed cycle is logged and the task
       ends, freeing the source's slot for the next period."""
    started = time.monotonic()
    log.info("GATHER: %s — cycle started", name)

    def beat():
        beats[name] = time.monotonic()

    try:
        tally = await asyncio.to_thread(
            _gather_source_cycle, app.state.config.db_path, name, beat)
    except (Exception, SystemExit):
        # SystemExit is a BaseException, not Exception, so the bare
        # `except Exception` above doesn't catch it - and a module that
        # raises one (e.g. an opening-guard `raise SystemExit(...)`) would
        # propagate up through asyncio.to_thread and abort the event loop,
        # crashing hone-core. Mirror _gather_source's per-ref catch:
        # contain it here, log it, end the cycle so the slot is freed for
        # the next period. CancelledError still propagates (the supervisor
        # uses it to shut tasks down).
        log.exception("GATHER: %s — cycle failed", name)
        return
    log.info("GATHER: %s — cycle done in %.0fs: %s",
             name, time.monotonic() - started, tally or "nothing")


class _Registry:
    """The supervisor's live state: each source's task, last heartbeat, and
       last spawn time."""

    def __init__(self):
        self.tasks = {}        # source -> asyncio.Task
        self.beats = {}        # source -> last heartbeat (time.monotonic)
        self.last_spawn = {}   # source -> when the source was last spawned


def _reap(name, task):
    """Log how a finished gather task ended (a clean finish is already logged
       by _run_source)."""
    if task.cancelled():
        log.warning("GATHER: %s — task cancelled (stalled, or shutting down)",
                    name)
    elif task.exception() is not None:
        log.error("GATHER: %s — task crashed: %r", name, task.exception())


def _plan_tick(reg, now, enabled, interval, stall_after, *,
               trigger_now=False):
    """One supervisor housekeeping step — the pure scheduling decision.
       Drops finished tasks from `reg` (logging how each ended) and returns
       (to_spawn, to_cancel): the sources to spawn a task for, and the
       stalled tasks to cancel.

       `trigger_now=True` bypasses the per-source interval check — the
       operator's "Gather now" button (Site-settings page) sets it to fire every
       idle source on the next tick regardless of when each was last
       spawned. Sources mid-cycle are unaffected (the supervisor never
       preempts a running cycle); they'll re-spawn on their normal cadence
       once the running cycle finishes."""
    for name in [n for n, t in reg.tasks.items() if t.done()]:
        _reap(name, reg.tasks.pop(name))
        reg.beats.pop(name, None)
    to_cancel = [name for name, task in reg.tasks.items()
                 if now - reg.beats.get(name, now) > stall_after]
    # spawn a source when it has no live task (an overrunning cycle keeps
    # its slot) and EITHER the trigger fired this tick OR its spawn period
    # has elapsed.
    to_spawn = [name for name in enabled
                if name not in reg.tasks
                and (trigger_now or
                     now - reg.last_spawn.get(name, float("-inf")) >= interval)]
    return to_spawn, to_cancel


async def gather_supervisor(app):
    """Spawn and supervise one gather task per enabled source until
       cancelled. The enabled set and the spawn interval are re-read from
       the live runtime config, so a Settings change applies without a
       restart. The operator's "Gather now" button (Site-settings page)
       `set()`s `app.state.gather_trigger` to wake the supervisor early
       and fire every idle source on the next tick regardless of cadence."""
    reg = _Registry()
    trigger = getattr(app.state, "gather_trigger", None) or asyncio.Event()
    app.state.gather_trigger = trigger      # so the UI handler can set() it
    prev_sources, enabled = None, []
    log.info("GATHER supervisor started")
    try:
        while True:
            now = time.monotonic()
            rc = app.state.runtime_config
            if rc.gather_sources != prev_sources:
                enabled = _select_sources(rc.gather_sources,
                                          gather_api.available())
                prev_sources = rc.gather_sources
            triggered = trigger.is_set()
            if triggered:
                trigger.clear()
                log.info("GATHER: operator-triggered tick")
            to_spawn, to_cancel = _plan_tick(
                reg, now, enabled, rc.gather_interval, _STALL_SECONDS,
                trigger_now=triggered)
            for name in to_cancel:
                log.warning("GATHER: %s — no heartbeat for %ds, cancelling",
                            name, _STALL_SECONDS)
                reg.tasks[name].cancel()
            for name in to_spawn:
                reg.last_spawn[name] = now
                reg.beats[name] = now
                reg.tasks[name] = asyncio.create_task(
                    _run_source(app, name, reg.beats), name=f"gather:{name}")
            # Sleep for one housekeeping tick OR until the trigger fires —
            # whichever first. set() wakes the supervisor in ~one event-loop
            # turn, so the button feels instant.
            try:
                await asyncio.wait_for(trigger.wait(),
                                       timeout=_TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        for task in reg.tasks.values():
            task.cancel()
        if reg.tasks:
            await asyncio.wait(list(reg.tasks.values()), timeout=5)
        log.info("GATHER supervisor stopped")
