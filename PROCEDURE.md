# hone — loop procedure

A **testing procedure** — NOT part of `~/PATCH-REVIEW-METHODOLOGY.md`. Each
run measures our Linux kernel patch-review methodology against external review
signal drawn from kernel **mailing-list review threads**, and hones
`~/PATCH-REVIEW-METHODOLOGY-FINDINGS.md` into genuine methodology
improvements.

> This describes the **current single-host loop**, run by hand. The
> containerized core/node design it is evolving toward is in
> `ARCHITECTURE.md`; this procedure will be rewritten when that is built.

Files:
- `core/gather-modules/<name>.py` — gather modules; each subclasses `GatherModule`
- `core/gather-modules/gather_api.py` — the gather-module public API (`GatherModule`, `PatchsetRef`, `Finding`)
- `SOURCES.md` — the data-source registry, source types (`ai` / `human`), and the gather-module API
- `SCORING.md` — candidate scoring & lifecycle (the single authority)
- `hone.db` / `core_db.py` — the dedup ledger & human-reviewer tracker (see `REVIEWERS.md`)
- `refrepo.py` — reference-tree manager: locates base commits, stages worktrees (the STAGE stage)
- `ARCHITECTURE.md` — the 3-stage pipeline + worker-tier model behind this procedure
- `REPORT.md` — running human-readable report
- `~/PATCH-REVIEW-METHODOLOGY-FINDINGS.md` — candidate practices (self-honing)

## No source is ground truth

Review signal comes from data sources — AI review bots **and** human
reviewers. None is ground truth. AI sources are verbose with a high
false-positive rate; human reviewers are sparse and high-precision but terse.
**Every external finding — AI or human — must be verified against the code
before it counts.** A finding verified wrong is a *source-FP*: recorded in
`REPORT.md`, never added to `FINDINGS.md`. We do not inject any source's
mistakes into our learnings.

## The hone.db ledger

`hone.db` (SQLite; schema and helpers in `core_db.py`, model in `REVIEWERS.md`)
is the cross-source **dedup ledger** and the **human-reviewer tracker**. It is
keyed on **Message-IDs**: a patchset's identity is its thread's root
Message-ID, so the same submission seen via two sources is recorded — and
reviewed — once. Every message/finding key carries a PRIMARY KEY / UNIQUE
constraint, so re-ingestion (re-runs, `git fetch`, list crossposts) is an
idempotent no-op. It also records, per patchset, our review's measured
**token cost**. SQLite commits are durable on commit — no separate `sync`
needed for the DB.

## The three-stage pipeline

The loop is a 3-stage pipeline — **GATHER → STAGE → DISPATCH** — decoupled by
the `patchsets.status` flag in `hone.db`. `ARCHITECTURE.md` is the model; this
is the operational procedure. Each stage consumes one status and produces the
next, and they can run independently / in separate sessions:

`gather` → **`pulled`** → `stage` → **`staged`** → `dispatch` → **`reviewed`**
(plus **`deferred`** = staging found no base tree, retryable; **`skip`** =
never to process). Process at most 3 patchsets per stage run (oldest first)
unless told otherwise.

## GATHER  (stage 1 — produces `pulled`)

1. **Pick a data source.** `python3 ~/hone/core/gather-modules/<name>.py list`
   → qualifying patchsets, oldest-first (JSON, one `PatchsetRef` per line).

2. **Dedup.** For each candidate take its **root Message-ID** (the `id` /
   `root_message_id` in the `PatchsetRef`). Skip it if `core_db.is_handled(db,
   root_msgid)` — it already carries a status, possibly gathered via the
   *other* source. `hone.db` is the sole dedup authority; a submission seen via
   both sources is gathered once.
   If a `PatchsetRef` has `skip_reason` set (e.g. linux-arm-msm flags a
   patchset whose root has no resolvable `Date`), do **not** pull it: call
   `core_db.mark_skip(db, root_msgid, reason, subject)` — status `skip`.

3. **Pull & ingest** each remaining candidate (oldest first):
   a. `core/gather-modules/<name>.py pull <id> /tmp/hone-<id>` → patch files.
   b. `upsert_patchset(root_msgid, subject, …, sent=<root Date>)`;
      `record_patchset_source(root_msgid, <source>, <source-ref>)`;
      `record_patch_files(root_msgid, [(filename, lines_changed) …])`;
      `store_patch_blob(root_msgid, /tmp/hone-<id>)`.
   c. For a **human source**, ingest the thread: `record_message(...)` per
      message — with each message's **real `Date`-header UTC timestamp**
      (never a placeholder) — and per reviewer reply `resolve_reviewer(name,
      email)` + `record_review(reply_msgid, reviewer_id, root_msgid, …)`.
   d. **`mark_pulled(root_msgid, subject)`** — status `pulled`; the patchset
      enters the STAGE queue. No review, no base resolution here.

## STAGE  (stage 2 — `pulled` / `deferred` → `staged` or `deferred`)

1. **Take the queue.** `core_db.stage_queue(db)` → root Message-IDs with status
   `pulled` (or `deferred`, to retry), oldest first.

2. **Stage a base tree** for each: `core_db.extract_patch_blob(root_msgid,
   /tmp/hone-<id>)`; resolve the base commit — the gather module's `base <id>`,
   falling back to the patch's `base-commit:` trailer (`refrepo.py base
   /tmp/hone-<id>/patch1.patch`); then `refrepo.py prepare <base>
   ~/hone/staged/<id>`. `refrepo.py` locates a linux-tree with
   that commit, fetching it **once, serially**, from a bounded remote set if
   missing, and stages a detached worktree.
   - base staged → **`mark_staged(root_msgid, base_commit, staged_worktree)`**
     — status `staged`; the patchset enters the DISPATCH queue.
   - base not obtainable → **`mark_deferred(root_msgid, reason)`** — status
     `deferred`; a later STAGE run retries it.
   Run `refrepo.py gc` periodically — the reference repo accumulates
   daily-rebased linux-next churn and `gc` is what bounds it.

## DISPATCH  (stage 3 — `staged` → `reviewed`)

1. **Claim work.** A worker calls `core_db.claim_patchset(db, worker_id)` — one
   atomic claim returning `{root_message_id, base_commit, staged_worktree,
   subject}` (or `None` if the queue is empty); status → `claimed` with a
   lease. SQLite serialises writers, so two workers never claim the same
   patchset; a crashed worker's claim is re-offered once its lease expires.

2. **Review** — one worker per claim; workers run in parallel (the fetch cost
   was already paid in STAGE):
   a. The patches are at `/tmp/hone-<id>` (or re-extract via
      `extract_patch_blob`); the base tree is the handed `staged_worktree`.
      Review **in that worktree** — never `git fetch` / `git worktree`.
   b. **BLIND review** per `~/PATCH-REVIEW-METHODOLOGY.md` (Stage 0 + Stage 2
      + Stage S), applying every active candidate in `FINDINGS.md` as an extra
      check. **Keep the worker's returned `usage` block** (`total_tokens`,
      `tool_uses`, `duration_ms`). Record our findings before step c.
   c. **Reveal the source** (`core/gather-modules/<name>.py findings <id>`). Verify
      every external finding against the code, then classify `match` / `miss`
      / `source-FP`; note any real issue we caught the source missed
      (`we-win`). For a **human source**, write each classified finding with
      `record_finding(reply_msgid, seq, severity, text, verdict)` — that
      verdict feeds the reviewer's accuracy and confidence score. (sashiko is
      `ai`: its comparison goes to `REPORT.md` only; not the reviewer DB.)
   d. **`mark_reviewed(root_msgid, verdict, total_tokens, tool_uses,
      duration_ms)`** — status `reviewed`, with the measured token cost.

3. **Tear down.** `refrepo.py cleanup <staged_worktree> …` removes the staged
   worktrees once their reviews are recorded.

4. Update `FINDINGS.md` (counters + prune / graduate per `SCORING.md`) and
   append to `REPORT.md`; run `sync` after writing each. (`hone.db` is already
   durable per commit.)

## FINDINGS.md — the self-honing rule

- Each verified `miss` that points to a *generalisable* methodology gap →
  a candidate practice. A one-off, non-generalisable miss → `REPORT.md` only.
  A `source-FP` never becomes a candidate practice.
- For every candidate applied in DISPATCH step 2b, update its counters
  (Applied, Catches, unique-catches) and apply the prune / graduate decisions
  per **`SCORING.md`** — the single authority. Do not restate those thresholds.
- Converge: `FINDINGS.md` should shrink toward a few practices that
  repeatably catch real things our methodology misses.

## REPORT.md — append each DISPATCH run

Date; data source(s) used; patchsets reviewed; per-patchset scorecard
(external findings N / of which P preexisting; matched M; missed K;
source-FP F; we-win W; review token cost); candidates added / pruned /
graduated; updated running totals. Track results **per source** — our
methodology may compare differently against AI review than human review.

## Honesty rules

- Review BLIND — never read a source's review before recording our findings.
- A finding we "sort of" had is a `miss`, not a `match`.
- Verify before you believe — no source, AI or human, is ground truth; the
  code is.
