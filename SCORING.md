# hone — candidate scoring & lifecycle

The single authority for how `~/PATCH-REVIEW-METHODOLOGY-FINDINGS.md`
candidate practices are counted, pruned, and graduated. `PROCEDURE.md` and
`FINDINGS.md` **point here** — these rules are not restated elsewhere, to
avoid drift.

This is hone harness machinery. It is **not** part of
`~/PATCH-REVIEW-METHODOLOGY.md`.

## Counters

Each candidate records, as raw data:

- **Applied** — reviews where the candidate's pattern was *present* and the
  check was actually exercised. Reviews where the pattern was absent (N/A) do
  not count.
- **Catches** — applications where the check caught a real, code-verified
  issue. (In FINDINGS.md entries written before 2026-05-21 this field is
  labelled "Confidence"; it was always a plain tally, never a 0–5 scale.)
- **Unique catches** — the subset of Catches that the blind baseline review
  would have *missed* without the candidate. Tracked from 2026-05-21 forward.
  This is the counter that matters: a catch baseline Stage 2 makes anyway is
  not value the candidate added.

Derived: **hit rate = Catches ÷ Applied**; **unique rate = unique-catches ÷
Applied**. The raw counts are the *sample*; the rate is the *quality*. A bare
count is meaningless at scale — Catches 50 / Applied 5000 is far worse than
5 / 10.

## Lifecycle

`proposed` → `candidate` → `graduated` (into the methodology) or `pruned`
(red herring). A candidate is born from a verified miss and earns its verdict
over many reviews.

## Sample-size gate — judge by phase

An observed rate from a few trials is noise. Rule of three: 0 catches in N
trials bounds the true rate only below ≈ 3/N — so 0/5 is consistent with a
rate as high as 60%, and even 0/30 only rules out rates above ~10%.

- **Small sample — Applied < 50.** Too noisy for a rate. Keep any candidate
  with ≥ 1 catch. Prune only an obvious dud: **Applied ≥ 30 with 0 catches**
  (0/30 ⇒ rate confidently under ~10%). This replaces the old
  `Applied ≥ 5 / Confidence 0` rule, which pruned far too aggressively
  (0/5 rules out almost nothing).
- **Large sample — Applied ≥ 50.** Switch to the rate-based prune below.

## Prune rules (large sample) — any one fires a prune

1. **Rate floor, severity-weighted.** Prune if **unique rate < 1%** (< 1
   unique catch per 100 applicable patches) **and** no catch was ever rated
   `major` or `critical`.
2. **Rare-but-serious exemption.** A candidate that has caught a
   `major`/`critical` at least once is *kept* despite a low rate — a check
   that surfaces a critical even 1-in-1000 earns its slot. Revisit such a
   candidate only at Applied ≥ 1000.
3. **Redundancy prune.** Prune — regardless of rate or catch count — if its
   *unique* catches are ≈ zero: everything it caught, baseline Stage 2 would
   have caught anyway. This is what actually killed C6 (recorded as a
   Confidence-0 prune, but redundancy was the real reason).

Rule-of-three reference points for a 0-catch candidate: Applied 30 ⇒ rate
< ~10%; Applied 60 ⇒ < ~5%; Applied 300 ⇒ < ~1%.

## Graduation

Graduate a candidate into `~/PATCH-REVIEW-METHODOLOGY.md` when its **unique
rate is solidly high over a real sample** — as a guide, unique rate ≥ ~5%
sustained across Applied ≥ 50, with at least one `moderate`+ catch. (C5
graduated at Catches 5 / Applied 11.) On graduation: move it to the Graduated
section of `FINDINGS.md`, and add it to the methodology as a numbered check.

## Why a rate, not a count

At thousands of patches/day a raw counter is meaningless and a fixed
`Applied ≥ 5` trigger fires within minutes. Scale forces the shift to a
sample-size-gated, rate-based, severity-weighted, redundancy-aware rule. A
cheap checklist item with even a 1-in-50 *unique* hit rate is worth keeping;
the checks that must die are the redundant ones and the ones whose rate is,
with statistical confidence, near zero.
