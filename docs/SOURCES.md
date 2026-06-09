# hone — data sources

hone measures our Linux kernel patch-review methodology against external
review signal. That signal comes from kernel **mailing-list review threads**
plus the patches themselves, drawn from a **data source**.
**lore.kernel.org is the canonical source** for patchsets and the comments
on them. The framework is source-agnostic — a second source can be added by
dropping in a new gather module (see *Adding a source* below) — but today
lore is the only one.

## What a data source provides

A source streams **refs** into hone-core's ingest pipeline. Two ref shapes:

- a **`PatchsetRef`** — a new patchset (a `[PATCH … 0/N]` thread): the root
  Message-ID, the subject, the submitter, the patch count, the declared
  `base_commit`, the change-id and series version (`vN`), and the lore list
  tags carried by the thread.
- a **`MessageRef`** — a single message tied to a patchset: a cover, a
  numbered patch, or a comment. The body is the message text (the diff for a
  patch, the inline review for a comment); the `parent_message_id` ties a
  comment to the patch / cover it replies to.

A patchset arrives as one `PatchsetRef` followed by its messages — covers,
patches, and any reviewer comments. Comments may also arrive *later* (a
maintainer reply landing after the initial patches) and attach to a
patchset already in the corpus. hone-core blind-reviews each patchset (a
*review* work item) and then trains the methodology against each comment
that lands on a reviewed patchset (a *train* work item) — see
`ARCHITECTURE-WORK-LIFECYCLE.md` → Work lifecycle.

## The gather-module API

Each data source is implemented by a **gather module** — a self-contained
file `core/gather-modules/<name>.py` whose class subclasses `GatherModule`,
the public API defined and documented in
`core/gather-modules/gather_api.py`. Adding a source = dropping in one such
module; the framework stays source-agnostic.

A `GatherModule` subclass sets two class attributes — `name` (must equal the
file basename) and `since_date` (`YYYY-MM-DD`, the cold-start floor below
which a pass never gathers) — and implements **one** method:

```python
def list(self, state: GatherState | None = None,
         db: sqlite3.Connection | None = None
         ) -> Iterator[PatchsetRef | MessageRef]:
    ...
```

`list()` is a **streaming iterator** — it yields refs as it discovers them,
oldest first, and may yield indefinitely (the framework cancels it between
ticks). `state` carries the per-source **resume cursor** to start from (an
opaque module-defined string; the framework persists it after every
successfully-ingested ref and resumes on the next tick). `db` is an
optional read-only SQLite handle a module may use for in-flight lookups (lore
uses it for thread-root resolution against the corpus).

The framework's `_ingest_ref(db, module, ref)` writes the patchset or
message into `hone.db`, applies the list-tag filter to a `PatchsetRef`, and
fires the prepare auto-enqueue trigger: `maybe_enqueue_prepare` after each
patchset. Review is **not** auto-enqueued — it is an operator action (the
"Request review" button on the patchset detail page, `POST
/review-requests/<root>` → `maybe_enqueue_review`), so a gather pass
enqueues only prepare. Comments are
upserted but trigger no work-items — training is session-driven, so
comments are inert corpus material until an operator launches a session.
The cursor on the ref is persisted as the source's new resume point.

### `PatchsetRef`

```
root_message_id     thread root (dedup key)
subject             "[PATCH N/M] …"
submitter_email     who posted it
sent                unix ts (the root's Date header)
n_patches           patches in the series (cover excluded)
base_commit         declared baseline (the `base-commit:` trailer)
change_id           gerrit-style series identifier (revision linkage)
series_version      1, 2, 3, … (vN)
list_tags           ["linux-arm-msm.vger.kernel.org", …] — see *List-tag filter*
skip_reason         set ⇒ the framework skip-flags it, never ingested
cursor              the opaque per-source resume cursor *as of this ref*
```

### `MessageRef`

```
message_id          this message's Message-ID
root_message_id     the patchset's root
type                cover | patch | comment
body                the message text (a diff for a patch, prose for a comment)
part_index          1..N for a patch; None for cover/comment
parent_message_id   what a comment replies to (cover or a specific patch)
author_name         the message's From: name
author_email        the message's From: address
subject             the Subject header
sent                unix ts (the message's Date header)
cursor              the opaque per-source resume cursor *as of this ref*
```

### `GatherState`

```
cursor: str         opaque module-defined string; "" on cold start
```

The framework stores `cursor` in the `gather_state` table per source; on
resume it passes the latest value back to `list()`. The cursor is *frozen*
at the first ingest failure of a tick — the module keeps emitting from the
last good point, never silently advancing past a patchset that did not land
in the corpus. A successful ingest commits its cursor; a `git fetch`,
re-run, or list crosspost is deduplicated on the message keys, so resume
never double-counts.

**Invocation.** In-process: `gather_api.load("<name>")` returns a ready
instance; `gather_api.available()` lists the installed modules. As a CLI:
`python3 core/gather-modules/<name>.py list` — the shared `run_cli` shim
serialises refs to JSON (one object per line). hone-core's GATHER loop is
the production caller; the CLI is for inspection and module development.

## Registered sources

| Source | Role | Origin | Status |
| --- | --- | --- | --- |
| `lore` | patchset + comment source | lore.kernel.org public-inbox `all/` git archive | **active** |

### `lore` — the canonical patchset source

[lore.kernel.org](https://lore.kernel.org/all/)'s **all-of-lore**
public-inbox archive is the single source of truth: every kernel mailing
list, in one append-only git repo of one-message-per-commit. lore drives
the corpus — covers, patches, and human reviewer comments alike — and is the
list-tag authority (every message commit carries the originating list as
a header).

**Cold-start clone (one-time).** Use the bundled helper — it reads URL +
`since_date` from `core/gather-modules/lore.py` so there's one canonical
place for the cutoff:

```sh
python3 core/gather-modules/lore.py clone
```

That runs:

```sh
git clone --filter=blob:none --shallow-since="<since_date>" \
          --no-tags --single-branch \
          $HONE_LORE_URL  $HONE_ARCHIVE_DIR/lore
# default URL: https://lore.kernel.org/all/0
```

Two trims combine:

- `--filter=blob:none` — a *blobless* partial clone (commits + trees only,
  cheap on disk); message blobs are fetched on demand as the gather walks.
- `--shallow-since=<date>` — a shallow clone bounded by `Lore.since_date`,
  so the initial download is **the window gather will actually look at**,
  not all-of-lore-since-2018. For a recent floor (e.g. 2026-03-08) that's
  on the order of **a few hundred MB to ~1 GB** rather than multi-GB.

The archive is operator-provisioned and regenerable from upstream;
`archive/` is gitignored and never committed. Set `HONE_LORE_URL` to point
at a single list (e.g. `https://lore.kernel.org/linux-arm-msm/0`) or a
private mirror — the helper picks it up.

**Background autoclone (opt-in).** Set `HONE_LORE_AUTOCLONE=1` in
`core/.env` and hone-core runs the same `clone` helper in a background
task on startup. The service comes up clean either way — `lore.list()` is
a no-op while the archive is missing — but with autoclone enabled the
operator doesn't have to run the helper by hand: gather picks the archive
up on the next supervisor tick once the clone completes. Useful for
fully-automated CI / IaC deployments where the operator never logs in.

**Resume cursor — the archive HEAD SHA.** Because the archive is
**append-only**, the cursor is just the last git commit hash gathered. Each
pass runs `git log --reverse <cursor>..HEAD` and walks the new commits
oldest-first, parsing each message and classifying it as cover / patch /
comment. After every successful ingest the framework records that commit's
SHA as the new cursor — so the next pass picks up from exactly the next
commit, with **no date-watermark gotcha**: a comment posted today on a
3-week-old patchset is a new commit at the archive tip and is gathered
*today*, not silently skipped because its parent's date predates the
watermark.

**Per-cycle patchset cap.** A single cycle is capped at
`MAX_PATCHSETS_PER_CYCLE` (currently 200, in `lore.py`) so a cold-start
backlog — potentially a month of all-of-lore on the first cycle after the
clone — doesn't block the source's slot for hours, brush up against the
supervisor's 30-min stall cancel, or dump a huge spike of work-items into
the queue all at once. The cap counts **patchsets**, not commits: the
cycle stops on a clean boundary (just before the (N+1)th patchset's first
message) so every gathered patchset lands whole — cover + every patch —
never split mid-series. The framework persists the cursor as it goes; the
next supervisor tick picks up the next batch. Each capped cycle logs one
`INFO` line so a long catch-up is visible to the operator.

**Cold-start boundary skip.** When the cursor is empty and the
`since_date` floor doesn't happen to land on a cover letter (the cover
may predate the floor, leaving patches 3..7 of a series as the first
commits in the slice), `list()` skips forward to the first real
patchset boundary — a cover (`[PATCH 0/N]`) or a standalone patch
(`[PATCH]` with no `m/n`) — before emitting anything. Without it, those
leading patches would each become a wrong-rooted one-patch ghost
patchset, scattering the real series across the corpus. The skip is
bounded at `_MAX_BOUNDARY_SKIP` commits as a safety net; at the limit we
log a `WARNING` and accept the next commit as a best-effort boundary so
a misaligned start can't spin forever.

**Thread-root resolution.** Each new message references its thread root
via `In-Reply-To` / `References`. lore resolves the root by looking it up
in-cycle (an in-memory thread cache built as the pass walks) and then
falling back to the corpus (`messages` table) for a comment whose root
predates the current walk. WAL mode lets lore open its own read-only DB
handle alongside the framework's writer.

**List tags.** A lore commit's message carries the mailing-list identity in
its headers (`linux-arm-msm.vger.kernel.org`, …); lore extracts every list
the message hit and attaches them as `list_tags` on the `PatchsetRef`. See
*List-tag filter*.

### Adding a source

Drop a new file in `core/gather-modules/`. The minimum:

```python
from .gather_api import GatherModule, GatherState, PatchsetRef, MessageRef

class MySource(GatherModule):
    name = "my-source"
    since_date = "2026-01-01"

    def list(self, state=None, db=None):
        # walk from `state.cursor` (or `since_date` if blank); yield
        # refs oldest-first; set `cursor=...` on every ref you yield.
        ...
```

A module is auto-discovered by filename; no registration call. The
framework runs one supervised asyncio task per *enabled* module
(`runtime_config.gather.sources`); each module independently picks up from
its own cursor.

## List-tag filter

A `PatchsetRef` carries the lore list tags the thread hit
(`linux-arm-msm.vger.kernel.org`, `linux-kernel.vger.kernel.org`, …). The
operator's **list-tag filter** decides which lists are in scope:

- The lore manifest seeds the `list_tags` table with the universe of known
  lists (origin = `manifest`). A list a patchset is observed on but is not
  in the manifest is added (origin = `observed`) — the operator sees it on
  the Site-settings page and can enable it.
- The operator ticks the lists they want gathered (Site settings → *List-tag
  filter*). The enabled set is the filter; a `PatchsetRef` whose tags do
  **not** intersect the enabled set is recorded with state `skipped` and
  `skip_reason=tag-not-enabled` — never reviewed, never re-offered.
- **An empty enabled set means the filter is off** (every patchset is
  in-scope). Useful for a first-run / experiment; an operator running real
  triage ticks the lists they actually care about.

The filter applies at ingest, so a skipped patchset never costs a review
cycle.

## Dedup

The same patchset gathered via two paths (a list crosspost; a re-run after a
`git fetch`) is recorded — and reviewed — once. Dedup is keyed on the
thread's **root Message-ID**; every messages key and every list-tag key
carries a UNIQUE constraint. The model — idempotent re-ingestion,
`change_id`-linked revisions — is specified in `ARCHITECTURE.md`
(*Data model* → *Dedup*).

## Start dates

Every gather module declares its **`since_date`** — the oldest date a cold
start will gather from. Current floors:

| Source | `since_date` |
| --- | --- |
| `lore` | 2026-03-08 |

`since_date` is only the **cold-start floor**. Once a source has gathered
anything, its `gather_state` cursor is authoritative and the pass resumes
from it — forward only. **Lowering `since_date` later has no effect on its
own** — the cursor is already past the new date. To actually backfill the
newly-exposed older range, clear the source's cursor first:

```sql
DELETE FROM gather_state WHERE source = '<module-name>';
```

(or clear the data volume). The pass then restarts from the new
`since_date`; messages already in the corpus are skipped by the dedup keys,
so the re-backfill never double-ingests.

**For lore specifically,** lowering `since_date` also requires
**deepening the shallow clone** — the bounded archive on disk doesn't have
the older commits yet. Either re-run the clone helper after deleting the
archive, or deepen in place:

```sh
git -C $HONE_ARCHIVE_DIR/lore fetch --shallow-since="<earlier-date>"
```

Like the gather cursor, the shallow boundary is forward-only by default;
you have to ask for the older history explicitly.
