# hone — patchset characterization & the prepare tiers

How a gathered patchset is characterized before it can be reviewed,
and why that work is split into tiers so that **exactly one node task
type clones the kernel**. The system model is in
[`ARCHITECTURE.md`](ARCHITECTURE.md); the work-item lifecycle these
tasks ride on is in
[`ARCHITECTURE-WORK-LIFECYCLE.md`](ARCHITECTURE-WORK-LIFECYCLE.md);
the stratified selection that consumes this metadata is in
[`ARCHITECTURE-TRAINING.md`](ARCHITECTURE-TRAINING.md); the record
shapes are in [`API.md`](API.md).

> Status: **target architecture.** This describes where prepare is
> headed, not how it works today. Today a single `prepare` node task
> produces all the metadata below *through the LLM* and pulls a kernel
> tree to do it; `review` pulls a tree too. This document is the plan
> to (a) split prepare into a deterministic code phase and an
> LLM-judgment phase, (b) confine the kernel tree to `review` alone.
> The migration sequence is at the end.

## Motivation

Most of what prepare produces is either **deterministic** (no LLM
needed — base existence, MAINTAINERS resolution) or **tree-free**
(reads the diff and the mail thread, not kernel source content —
patch type, review intensity). Only the actual code review genuinely
needs the kernel tree. Yet today both `prepare` and `review` clone
~5 GB of linux-next, and prepare runs the deterministic work through
the LLM (which is both wasteful and a source of fabrication bugs —
the model has invented maintainer/reviewer role splits from `To:`
headers).

The split lets us:

- run the deterministic work as a **non-LLM phase of the prepare
  node task** — no tokens, no hallucination, and a kernel clone
  replaced by two small HTTP fetches;
- keep the LLM-judgment metadata corpus-wide and tree-free;
- confine the kernel tree to `review`, the one operation that reads
  source to reason about correctness;
- preserve **characterize-then-select**: every stratification axis is
  produced by prepare, before review selection happens.

Crucially, the deterministic phase runs **on the node, not in
hone-core**. Each node hits cgit from its own egress IP, so a
100k-patchset backfill is spread across the fleet instead of
hammering kernel.org from one address — far below any per-IP rate
limit. And nodes already have transient-failure backoff
(`node/runner.py`), so a cgit 429/timeout just retries one prepare
task rather than stalling a central pipeline.

## The tiers

A "tier" is a stage of characterization distinguished by what it
needs. Tiers 0 and 1 are **two phases of the one `prepare` node
task**; Tier 2 is the `review` task.

| Tier | Where | LLM? | Tree? | Scope | Produces |
|---|---|---|---|---|---|
| **0 — Deterministic** | prepare task, code phase | no | no (2 cgit fetches) | whole corpus | `base_in_tree`, base commit metadata, `subsystem`, `maintainer` authoritative sets, `mailing_lists`, coverage ratios, `patch_size` base counts |
| **1 — Judgment** | prepare task, LLM phase | yes | **no** | whole corpus | `patch_type`, `review_intensity`, `preparation_notes.confidence`, `self_review_record` |
| **2 — Review + enrichment** | `review` task | yes | **yes** | selected stratified subset | the code review, plus `applies_cleanly`, `churn_ratio`, `file_activity`, `fixes_verified` file-overlap |

The pipeline:

```
gather → prepare task [ Tier 0 code phase → Tier 1 LLM phase ] → stratify + select → review task (Tier 2)
```

All four stratification axes — subsystem, patch_size, patch_type,
review_intensity ([`ARCHITECTURE-TRAINING.md`](ARCHITECTURE-TRAINING.md)
§ selection) — are produced by the prepare task (Tiers 0 + 1), *before*
selection. None needs the kernel tree. That is the load-bearing fact:
the tree is a `review` concern, never a characterization concern.

## Field assignment

Relative to today's single prepare completion record (see
[`API.md`](API.md) → the prepare record). The record shape is
unchanged — the node still submits one complete prepare record; what
changes is that the Tier-0 fields are filled by code, not the LLM.

**Tier 0 — prepare task, deterministic code phase (no LLM, no tree):**

- `tree_state.base_in_tree` — cgit `HEAD /commit/?id=<base>` → 200/404,
  probed across the named-trees registry (below)
- `tree_state.base_tree` — canonical name of the tree the base resolved
  in (`linux-next` / `mainline` / `stable` / …), or null. Persisted for
  the review phase to use as a git-fetch hint (see Tier 2).
- `tree_state.base_commit_{timestamp,subject}` — scraped from the cgit
  `/commit/` page, or null
- `subsystem.*` — `get_maintainer.pl` against `MAINTAINERS` at the base
  commit
- `maintainer.authoritative_set` / `authoritative_reviewer_set` /
  `mailing_lists` / `cc_coverage` / `list_coverage` — same resolution.
  Running this deterministically **eliminates the LLM-fabrication class
  of bug** — code cannot invent role splits.
- `patch_size` line counts + bucket — counted from the diff

**Tier 1 — prepare task, LLM judgment phase (tree-free):**

- `patch_type` base classification + secondary tags — reads the diff
  and commit message
- `review_intensity` buckets, per-reply substance, and the `in_scope_*`
  variants (using Tier-0's authoritative maintainer set) — reads the
  thread
- `preparation_notes.confidence` and `self_review_record`

**Tier 2 — the `review` task (the tree is already present):**

- `tree_state.applies_cleanly` / `apply_failure_reason` —
  `git apply --check`
- `patch_size.churn_ratio` — file length at base
- `patch_type.file_activity` and `fixes_verified` file-overlap — git log
  / show over the touched paths
- `kernel_version_at_base` — `git describe` (needs tags; cleaner where
  the tree lives)

These four enrichment fields are **not** stratification axes, so
computing them only at review time costs nothing in selection power.

## Mechanisms

### The named-trees registry

A declared base often isn't in linux-next — release/-rc-based patches
sit in mainline, backports in stable, some subsystem-targeted ones in
net-next or tip. So Tier 0 resolves the base against an **ordered set
of named trees**, and review later fetches the base over git. These
are the *same conceptual trees accessed two ways* (cgit web vs. git
protocol), so they live in **one registry** — each entry is
`(name, cgit_url, git_url)` — shared by Tier-0 probing and review's
`refrepo` remote list, so the two can never drift. Probe order (and
default set):

| Order | Tree | cgit (HTTP probe) | git (review fetch) |
|---|---|---|---|
| 1 | `linux-next` | …/next/linux-next.git | git://…/next/linux-next.git |
| 2 | `mainline` | …/torvalds/linux.git | git://…/torvalds/linux.git |
| 3 | `stable` | …/stable/linux.git | git://…/stable/linux.git |
| 4 | `net-next` | …/netdev/net-next.git | git://…/netdev/net-next.git |
| 5 | `tip` | …/tip/tip.git | git://…/tip/tip.git |

linux-next leads deliberately — it merges the subsystem trees daily,
so a base that's really in net-next is usually also in linux-next,
hitting on probe #1. The lower-priority trees still get probed so a
base that's *only* in (say) net-next, not yet merged to next, resolves
correctly rather than reporting absent. Override via `HONE_CGIT_TREES`
(`name=cgit_url` comma-list; the git URL is derived by the
https→git:// scheme swap).

### Tier 0 — cgit access from the node

kernel.org runs **cgit**, an HTML git viewer, not a REST API — but its
HTTP status semantics give a clean existence check and its `/plain/`
endpoint serves files at a commit:

- **Base existence + resolving tree:** `HEAD .../commit/?id=<base>`
  per registry tree in priority order, **short-circuiting at the first
  200** — that tree's canonical name becomes `base_tree`. 404 means
  that tree definitively lacks the commit; the probe falls through to
  the next. The set-level verdict is tri-state: **found** (some tree
  200), **absent** (`base_in_tree=false` — *every* tree 404'd), or
  **unknown** (`base_in_tree=null` — no hit and at least one tree was
  unreachable, so the base may yet exist somewhere we couldn't probe;
  do not report it absent). `HEAD` short-circuits cgit's diff render;
  a `GET` would download the full rendered diff, unbounded for large
  commits.
- **MAINTAINERS is fetched from the resolving tree.** A commit SHA is
  content-addressed, so `MAINTAINERS@<sha>` is byte-identical in any
  tree containing the commit — the resolver uses the same tree the
  base resolved in.
- **MAINTAINERS resolution:** fetch `/plain/MAINTAINERS?id=<base>` and
  `/plain/scripts/get_maintainer.pl?id=<base>`, then run
  `perl get_maintainer.pl --no-git --no-tree …` in a directory holding
  just those two files plus the patch. Two flags are load-bearing:
  - `--no-git` — without it the script walks git history for recent
    contributors, which needs a tree and defeats the lightweight path.
  - `--no-tree` — without it the script's `top_of_kernel_tree` guard
    aborts ("does not appear to be a linux kernel source tree") unless
    a full set of marker files/dirs (COPYING, CREDITS, Makefile,
    `arch/`, `drivers/`, `fs/`, …) is present. `--no-tree` skips the
    guard so the two-file layout suffices.

  Verified by spike (2026-05-27): with both flags, `get_maintainer.pl`
  runs against only `MAINTAINERS` + itself + the patch, on base perl,
  with **empty stderr** — it reaches for no `.get_maintainer.conf`,
  `.mailmap`, or other tree files. So the Tier-0 fetch list is exactly
  two blobs per base SHA. A lower-fidelity alternative is a Python
  F:/X: glob matcher over `MAINTAINERS` at base (the methodology
  already sanctions F:/X:-only matching as "mixed mode"); we keep the
  real script for full fidelity — see Decisions below.
- **`/log/?id=<sha>` is unusable** for existence checks — cgit returns
  200 with an empty log for an unknown id, so there is no status-code
  signal.

Operational properties of running this on the node:

1. **Load is distributed by construction.** Each node fetches from its
   own egress IP, so corpus-scale backfill spreads across the fleet
   rather than concentrating on one address. This is the primary
   reason Tier 0 lives on the node and not in hone-core — a single
   central client is exactly the profile kernel.org throttles.
2. **Backoff is already there.** A cgit 429 / timeout is just another
   transient failure for `node/runner.py`'s existing backoff to
   retry; it extends one prepare task, never a central pipeline. A
   long backoff wait on one node doesn't lengthen anyone else's
   prepare.
3. **Per-node cache by base SHA.** A node caches MAINTAINERS@base for
   the duration it processes a series sharing that base. The cache is
   not shared across the fleet, so a hot base may be fetched up to
   once per node — MAINTAINERS@base is ~900 KB, so even 30 redundant
   fetches is trivial, and spreading them is the point.
4. **Degrade, don't block.** If cgit is unreachable, the Tier-0 fields
   are null and the prepare proceeds in heuristic mode for them; the
   node retries on the next claim per its backoff. Ingestion (gather)
   is untouched — it never talks to cgit.

### Tier 2 — the kernel tree on review nodes

A **blobless partial clone** (`git clone --filter=blob:none`) of
linux-next: blobs fault in on demand, the working set is far smaller
than a full clone, and it satisfies `apply --check`, churn, and
file-history queries. Only `review`-capable nodes carry it.

`refrepo` already fetches a base commit by trying its remotes
serially until one has it. The prepare record's `base_tree` turns
that blind scan into a **hint**: fetch from the named remote first
(via the registry's `git_url`), falling back to the serial scan only
if that misses. Treat it strictly as a hint — linux-next force-pushes
daily, so a `base_tree=linux-next` recorded at prepare time may be
stale by review time (days later, post-selection); mainline / stable
/ tip hints are durable. The fallback keeps correctness; the hint
just saves the usual case a few failed fetches.

## Schema implications

- **`patchset_metadata` stays single-writer.** The node submits one
  complete prepare completion record exactly as today; hone-core
  decomposes it into `patchset_metadata` unchanged. The
  deterministic-vs-judgment split is an *internal phase distinction on
  the node*, invisible to the schema — so there is no two-writer merge
  contract to design. (This is why Decision 1 below is withdrawn.)
- **The enrichment quartet moves to the review record.**
  `applies_cleanly`, `churn_ratio`, `file_activity`, and
  `fixes_verified` file-overlap need a home on the `ai_reviews` /
  review completion-record schema (a `tree_state` + `enrichment`
  block), plus a "computed at review, not prepare" note in the
  methodology's prepare section.
- **Resolver versioning** travels in the prepare record. Stamp the
  node's deterministic-resolver version into the completion record's
  `meta` (e.g. `meta.deterministic_resolver_version`) so a
  MAINTAINERS-matcher change is auditable, tracked separately from
  `methodology_version` (the LLM-phase prompt version).

## Mode model

`authoritative` / `heuristic` / `mixed` degrades per field:

- Tier-0 phase succeeds (cgit reachable, base resolves, MAINTAINERS
  parsed) → subsystem and maintainer are `source: tree`, authoritative.
- Tier-0 phase degrades (cgit unreachable, no `base-commit:` trailer,
  base not in tree) → those fields are `source: thread` / null,
  heuristic; the LLM judgment phase still runs.
- Tier-2 enrichment exists only once a review has run against a tree.

So a patchset can be "authoritative for subsystem + maintainer,
heuristic for churn until reviewed" — the existing `mode: mixed`
already expresses exactly this.

## What this preserves, and what changes

**Preserved:** characterize-then-select (all stratification axes exist
before review selection); the node `task_types` capability model; the
work-queue and claim lifecycle (prepare and review remain work-item
types); the prepare completion-record shape and its single writer.

**Changes:** prepare's deterministic fields are computed by code, not
the LLM — cheaper, and the maintainer-fabrication bug class is gone.
Prepare no longer needs a kernel tree (two cgit fetches replace the
clone), so only `review`-capable nodes clone. cgit load is spread
across node egress IPs rather than concentrated in hone-core, and
cgit failures ride the node's existing backoff. hone-core gains no
cgit/perl/HTTP dependency in the gather path.

## Decisions

**1 — `patchset_metadata` two-writer split.** *(withdrawn)* The
gather-time variant of Tier 0 would have made hone-core a second
writer to `patchset_metadata`, needing a merge contract. Moving the
deterministic phase onto the node (this revision) makes the node the
sole writer again — it submits one complete prepare record. No schema
split, no merge contract. Withdrawn as moot.

**2 — maintainer resolution: real `get_maintainer.pl`.** *(resolved,
spike-confirmed)* Run the actual script with `--no-git --no-tree`
against the two cgit-fetched blobs; base perl only, no other files
(see Tier-0 mechanism). Chosen over a Python F:/X: matcher because the
authoritative claim is the whole point — a hand-rolled matcher
silently drops `K:`/`N:` matches and nested-section nuance, producing
subtly-wrong sets that *look* authoritative. Cost is one small,
stable dependency: **perl in the hone-node image** (note: node, not
core). The Python matcher remains the documented degraded path if
perl ever becomes unwelcome.

**4 — resolver versioning.** *(resolved)* Stamp a
`deterministic_resolver_version` into the prepare record's `meta`,
bumped when the Tier-0 resolution logic changes. It is node code,
versioned with the node image — distinct from `methodology_version`
(the LLM prompt). (Revised from "a `patchset_metadata` column" now
that there's no gather-time writer.)

**3 — Tier-1 scope: corpus-wide (default), deferred review.**
*(deferred, with a default)* The prepare task runs one LLM call per
patchset across the whole corpus — unchanged from today. **Keep it
corpus-wide.** The only reason to revisit is LLM cost at 100k scale,
a data-informed call best made once real gather volume is observable.
The trap: selection-gating the LLM phase would mean `patch_type` +
`review_intensity` are no longer computed corpus-wide, re-introducing
the exact characterize-then-select tension this design removes (you
can't stratify on an axis you only compute for the patchsets you
already chose). So do **not** gate the LLM phase without consciously
accepting weaker stratification.

## Migration sequence

1. Build the node-side Tier-0 pieces: a cgit client + the named-trees
   registry + a multi-tree resolver (`node/cgit.py`), a
   `get_maintainer.pl` runner, and a deterministic resolver that
   assembles the Tier-0 fields. Add **perl** to the hone-node image.
2. Add the deterministic phase to the node prepare task: run the
   resolver first, fill the Tier-0 fields (incl. `base_tree`) from
   code, then strip those fields from the LLM prompt's responsibility
   so the judgment phase only produces Tier-1. The completion-record
   shape is unchanged apart from the new `base_tree` field.
3. Point `refrepo` at the shared registry and have it consume
   `base_tree` as a fetch hint (named remote first, serial-scan
   fallback). Add `base_tree` to `tree_state` in the methodology +
   completion-record schema.
4. Move the enrichment quartet to the review record (Tier 2) and drop
   `apply --check` from prepare.
5. Stop provisioning the kernel tree on prepare-only nodes — they need
   only perl + cgit reachability now.
