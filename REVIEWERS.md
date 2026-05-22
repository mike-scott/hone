# hone — human-reviewer tracking

How hone identifies human reviewers across data sources and
scores them. Backed by the SQLite database `hone.db` (schema and helpers in
`core_db.py`). Harness machinery — **not** part of `~/PATCH-REVIEW-METHODOLOGY.md`.

## Why track reviewers

Human reviewers are one class of data source (see `SOURCES.md`): their
replies on mailing-list threads are review signal we compare our blind review
against. Tracking each reviewer measures list activity and, over time, how
reliable a given reviewer's findings are — eventually a **confidence score**
per reviewer, mirroring the candidate-practice confidence in `SCORING.md`.

## Identity — one person, many emails

A reviewer is a *person*, not an email address. Kernel contributors use
several emails over a career (work, personal, gmail). `core_db.py`:

- **Auto-merges only on exact email.** `reviewer_emails.email` is the primary
  key; an email belongs to exactly one reviewer.
- **Never merges on name.** Distinct people share names ("Wei Wang"); a name
  match is not evidence and is never used to merge.
- **Seeds cross-email merges from the kernel `.mailmap`.** That file is the
  kernel's own authoritative identity-canonicalization map; `seed-mailmap`
  pre-links every multi-email person it lists. (Seeded from mainline
  `.mailmap`: 472 reviewers, 1270 emails.)
- A genuine cross-email merge that `.mailmap` misses is a **manual** action
  (relink the email with `via='manual'`); the harness never guesses an
  identity merge.

## Statistics

Per reviewer, derived from the DB:

- **Activity** — `reviews` rows: total review acts, plus a per-month rate.
- **Accuracy** — of that reviewer's findings we have *verified against the
  code*, the fraction that were real (`verdict` in `match`/`miss`) vs wrong
  (`source-FP`).
- **Confidence score** — the **Wilson 95% lower bound** on that real-rate.
  Sample-size-gated by construction: a reviewer at 2/2 does not outrank one
  at 95/100, and the score sits near 0 until enough findings are verified.
  Same statistical discipline `SCORING.md` applies to candidate practices.

Accuracy and confidence accrue only as the loop runs and verifies findings —
a freshly-seen reviewer has activity but no confidence yet.

## Dedup

Reviewer identity is **cross-source**: the same person reviewing via two data
sources is one `reviewers` row. Review *acts* are keyed on `Message-ID`
(`reviews.message_id` is the primary key), so re-ingesting a thread — on a
loop re-run or a `git fetch` — never double-counts a reviewer's activity.
