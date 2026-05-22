# hone — data sources

hone measures our Linux kernel patch-review methodology against external
review signal. That signal comes from one or more **data sources**, all
derived from kernel **mailing-list review threads**. sashiko-bot was the
first; it is now one source among several.

This is harness machinery — **not** part of `~/PATCH-REVIEW-METHODOLOGY.md`.

## What a data source provides

For patchsets posted to kernel mailing lists, a source yields:
- the **patches** themselves (reconstructed from the thread), and
- one or more **external reviews** — findings attributed to a reviewer.

Our loop blind-reviews the patches, then compares against each source's
review (see `PROCEDURE.md`).

## Source types

- **`ai`** — an automated review bot (e.g. sashiko-bot). Findings are
  machine-generated, structured, API-accessible. Expect *verbose* output and
  a *notable false-positive rate* (~25–30% observed for sashiko).
- **`human`** — review replies written by people on the lore/lkml thread
  (maintainers, subsystem reviewers). Findings are free-text and must be
  extracted from thread replies, attributed to a named person. Expect
  *sparse, high-precision* signal — but terse, sometimes implicit (a one-line
  "this leaks" or a bare `NAK`).

The trust posture is the same for both — **no source is ground truth; verify
every finding against the code** — but the *prior* differs: an AI finding is
likely noise until verified; a senior maintainer's objection is likely real
but still must be code-verified before it becomes a learning.

## The gather-module API

Each data source is implemented by a **gather module** — a self-contained
file `core/gather-modules/<name>.py` whose class subclasses `GatherModule`, the
public API defined and documented in `core/gather-modules/gather_api.py`. Adding a
source = dropping in one such module; the loop stays source-agnostic.

A `GatherModule` subclass sets two class attributes — `name` (must equal the
file basename) and `kind` (`'ai'` | `'human'`) — and implements four methods:

| Method | Returns |
| --- | --- |
| `list()` | `list[PatchsetRef]` — qualifying patchsets, oldest first |
| `pull(id, dest_dir)` | writes `patch0..N.patch`; returns the file paths |
| `findings(id)` | `list[Finding]` — the source's external review |
| `base(id)` | `str \| None` — the review's baseline commit, if stated |

`inline(id)` is optional (default `None`) — a source's free-text inline
review, if it has one.

Two dataclasses cross the boundary (both in `gather_api.py`):

- **`PatchsetRef`** — `id` (the module's native patchset id, taken by
  `pull`/`findings`/`base`), `root_message_id` (the patchset's root
  Message-ID — the cross-source `hone.db` dedup key), `subject`, `sent`,
  `n_replies`, `skip_reason` (set ⇒ the gather phase skip-flags it), `extra`.
- **`Finding`** — the normalized review shape: `reviewer`, `type`
  (`'ai'` | `'human'`), `text`, `severity`, `preexisting`, plus human-source
  attribution `reviewer_email` / `message_id` / `date` / `date_ok`, and
  `extra` for the source's raw payload.

**Invocation.** In-process: `gather_api.load("<name>")` returns a ready
instance; `gather_api.available()` lists the modules. As a CLI: `python3
core/gather-modules/<name>.py <list|pull|findings|base|inline>` — the shared
`run_cli` shim serialises `PatchsetRef`/`Finding` to JSON (one object per line
for `list`, a JSON array for `findings`).

## Registered sources

| Source | Type | Origin | Status |
| --- | --- | --- | --- |
| `sashiko` | `ai` | sashiko.dev review bot | **active** |
| `linux-arm-msm` | `human` | lore.kernel.org public-inbox git archive | **active** |
| _(other AI bots / lists)_ | — | — | planned |

### `linux-arm-msm` (human source) — access notes

lore.kernel.org's web/search UI is behind an Anubis anti-bot gate and is not
scrapeable. The archive's **public-inbox git repo** is not gated, so the
source clones it: `git clone --filter=blob:none https://lore.kernel.org/linux-arm-msm/0`
— a *blobless partial* clone (commits + trees only, cheap); message blobs are
fetched on demand. The source then walks `git log --since=2026-01-01`, so the
download is bounded to the human-source start date. `git fetch` keeps it
current (append-only — no thrash). A patchset = a `[PATCH …]` thread; the
source's `findings` are the human reviewer reply messages in that thread.

`list` emits patchsets **oldest-first** — chronological by send date, with the
root Message-ID as a stable tiebreaker (a `(date, id)` key). The loop sweeps
forward from the start date; `hone.db` dedup is the implicit cursor (each
iteration advances the frontier), so there is no separate state file. This is
why `PROCEDURE.md` step 2 says "oldest first": it sweeps the backlog
contiguously instead of re-skimming the newest tip every run.

A patchset whose thread root has **no resolvable `Date` header** has an
undeterminable place in that chronological sweep. `list()` emits it last with
`skip_reason='unresolved-date'` on its `PatchsetRef`; the gather phase calls
`core_db.mark_skip()` on it — recording a `patchsets` row with `status='skip'`
— instead of pulling or reviewing it, so it is never picked or re-offered.
(`load_messages` falls a missing/unparseable `Date` back to an epoch-0
sentinel and marks the message `date_ok=False`.)

**Start date:** human data sources begin at **2026-01-01**. (sashiko is
unchanged — it has no start-date bound.)

## Dedup

The same patchset gathered via two sources is recorded — and reviewed — once.
Dedup is keyed on the thread's **root Message-ID** in the `hone.db` ledger; the
gather loop applies it via `core_db.is_handled()`. The model — idempotent
re-ingestion, `change_id`-linked revisions — is specified in `ARCHITECTURE.md`
(Data model → Dedup).

## Stored patchsets — re-evaluation without re-pulling

When the gather phase pulls a patchset it stores the patch files into the
`patchsets.patch_blob` column as a single **`.tar.zst`** archive
(`core_db.store_patch_blob`; `patch_blob_bytes` records the compressed size) —
the blob the process phase later reviews from.

**Format of the blob** — the archive members are `patch0.patch` (the cover
letter, if the series has one), `patch1.patch`, … `patchN.patch`. Every
member is **`.patch`-formatted text**: a `git am`-able patch email — mail
headers, commit message, `---`, diffstat, unified diff. Fidelity is
source-dependent:

- `linux-arm-msm` members are the **pristine original RFC-822 messages** from
  the public-inbox archive — byte-for-byte as posted.
- `sashiko` members are our **hunk-whitespace-repaired reconstruction** (built
  from sashiko's API JSON, which stores damaged copies) — they apply cleanly
  but are not byte-identical to the original posting.

A re-evaluation run re-materializes any stored patchset with
`core_db.extract_patch_blob(root_msgid, dir)` (or `core_db.py extract <root>
<dir>`) — no source access — so a batch can be re-reviewed offline even if a
source's API changes or the git archive is unavailable.
