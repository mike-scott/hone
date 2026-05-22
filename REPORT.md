# hone — running report

Measures our Linux kernel patch-review methodology against external review
signal from kernel mailing-list review threads. Procedure: `PROCEDURE.md`. Data sources:
`SOURCES.md`. Candidate scoring: `SCORING.md`. Candidate practices:
`~/PATCH-REVIEW-METHODOLOGY-FINDINGS.md`.

**No source is ground truth** — every external finding (AI or human) is
verified against the code; wrong ones are logged as *source-FP* and excluded
from our learnings.

**Data sources used so far:** `sashiko` (type `ai`, sashiko.dev) — iterations
1–8; `linux-arm-msm` (type `human`, lore public-inbox git archive) —
iteration 9. Each source keeps its own scorecard below — our methodology
compares differently against AI vs human review. Candidate practices are
project-wide.

## Running totals — source: sashiko (ai)

| Metric | Count |
| --- | --- |
| Iterations run | 9 |
| Patchsets reviewed | 36 |
| external findings — matched | 20 |
| external findings — missed (verified real) | 45 |
| source-FP (verified wrong) | 32 |
| issues we caught, source missed (we-win) | 18 |
| candidate practices — active (project-wide) | 14 |
| candidate practices — graduated (project-wide) | 1 (C5 → Stage 2i) |
| candidate practices — pruned (project-wide) | 1 (C6) |

## Iterations (source: sashiko)

- **Iter 1** — 8 patchsets — N10: M5/K4/FP1, we-win 7. Candidates C1,C2,C3.
- **Iter 2** — 23037,23043,23046 — N6: M0/K3/FP3. C4,C5.
- **Iter 3** — 23051,23055,23059 — N8: M2/K5/FP1. C6,C7,C8.
- **Iter 4** — 23060,23064,23066 — N27: M6/K19/FP2, we-win 6. C9,C10.
- **Iter 5** — 23068,23070,23079 — N7: M2/K2/FP3, we-win 2. C2,C7 broadened.
- **Iter 6** — 23081 — N1: M0/K0/FP1, we-win 1.
- **Iter 7** — 23086,23088,23089 — N16: M1/K6/FP9. C11.
- **Iter 8** — 23185,23192,23194,23195 — N9: M2/K2/FP5, we-win 1. C12 added;
  **C5 graduated** → methodology Stage 2i; **C6 pruned** (first prune).
- **Iter 10** — **Phase B** — sashiko-23297/23298/23306/23308/23313/23314/
  23320/23329 — N13: M2/K4/FP7 (~54% FP), we-win 1. First run of the
  two-phase loop's Process phase — each patchset reviewed from its stored
  `.tar.zst` blob, no source re-pull. Verdicts: clean ×3 (23297, 23314,
  23320), issues ×5. Candidates **C15**, **C16** added (from the ovpn and
  leds misses); **C2** broadened — caught sashiko-23313. 526,874 review
  tokens. Misses: sashiko-23298 (BTF member width vs C struct pad — real,
  one-off, BPF-specific) and sashiko-23313 (fd leak on a new early return —
  one-off, already covered by Stage 2e) did not mint candidates.

(Per-iteration detail for iterations 1–8 is preserved in git/history of this
file; condensed here after the project refactor to hone.)

## Observations (9 iterations, sashiko source)

- **Scorecard:** 97 external findings — matched 20, missed 45, **32
  false-positives (~33%)**; we-win 18.
- **The self-honing loop works both ways:** C5 graduated into the methodology
  (Stage 2i); C6 pruned as a redundant red herring. FINDINGS.md is
  converging, not just growing.
- **The sashiko (ai) source has a ~30% false-positive rate** and a recurring
  failure mode — reviewing one patch of a multi-patch series in isolation and
  asserting a definitive breakage the sibling patches resolve. This is the
  motivation for the multi-source design: an AI source is noisy; human-
  reviewer signal (sparse, high-precision) will be a useful complement.
- **Our methodology's gap is the long tail** — it catches headline criticals
  and reachability regressions but under-explores pre-existing 2h-class code
  and framework-callback contracts. The active candidates target that.

## Running totals — source: linux-arm-msm (human)

| Metric | Count |
| --- | --- |
| Iterations run | 1 |
| Patchsets reviewed | 5 |
| external findings — matched | 2 |
| external findings — missed (verified real) | 7 |
| source-FP (verified wrong) | 1 |
| issues we caught, source missed (we-win) | 4 |
| review token cost | 411,496 tokens / 5 patchsets (avg ~82k) |

> Missed counts *unique methodology misses*. `hone.db` holds 8 `miss`
> finding-rows — Bryan O'Donoghue and Val Packett each raised the same
> patch-4 UDMABUF point (two reviewer findings, one methodology miss); the
> reviewer-accuracy view credits both, the methodology scorecard counts one.

## Iterations (source: linux-arm-msm)

- **Iter 9** — 5 patchsets, posted 2026-05-20/21 — N10: M2/K7/FP1, we-win 4.
  Candidates **C13**, **C14** added; **C2** broadened (counter-guard underflow
  trigger). Per patchset:
  - `hawi-crypto` `[PATCH 0/2]` dt-bindings — clean; 1 reply (Konrad Dybcio,
    bare `Reviewed-by:`). N0, we-win 0. 49.3k tok.
  - `pdev-fwnode-ref` `[PATCH 00/23]` driver core — N0 (2 replies: Wolfram
    Sang, Robin Murphy — both bare `Acked-by:`). **we-win 1**: a `critical`
    OF-node refcount leak in patch 11 (i2c-pxa-pci) — a non-scoped
    `for_each_child_of_node()` loop reference left unbalanced after the
    `platform_device_set_of_node()` conversion; raised by no reviewer, in the
    exact patch Wolfram `Acked`. 114.4k tok.
  - `qmp-combo v2` `[PATCH v2 0/4]` phy: qcom — N5: M2/K2/FP1, we-win 2.
    Matched: missing `Fixes:` tag; the unlocked-then-relocked `usb_init_count`
    read (our P1-1 data race). Missed: `usb_init_count` signedness/underflow
    audit (→ C2 broadened); patch-4 DT memory bump may belong in userspace
    (UDMABUF/libcamera). source-FP: a workqueue+completion redesign — a design
    preference, not a defect. we-win: the guard relies on the `qmp` struct
    outliving the freed mappings (safe-by-no-contract residual); guard
    redundant for all three callers. 95.9k tok.
  - `icc-rpmh` `[PATCH]` interconnect: qcom — N1: M0/K1. **Missed** (Dmitry
    Baryshkov): the EPROBE_DEFER path register-then-removes nodes already
    published to the global provider list — a teardown-time race a concurrent
    consumer can hit. We classified the path "consistent with the sibling
    error `goto`s, clean" — consistency is not correctness. → candidate C13.
    49.5k tok.
  - `surface-sp9-5g` `[PATCH 00/11]` Surface Pro 9 5G — N4: M0/K4, we-win 1.
    Missed: patch 2 backlight gpio references `pmc8280c` but the commit
    message's own debug dump points to `pmc8280_2` (→ C14); three nit-class
    commit-message-hygiene points (strip ACPI dump, overstated wording,
    `Fixes:`-tag ordering). **we-win 1**: patch 9 BT node uses the deprecated
    `vddrfa1p7-supply` and omits the binding-required `vddrfa1p8-supply` (a
    `dtbs_check` failure) — caught by Stage 2i, raised by no reviewer incl. the
    patch-9 `Reviewed-by`. 102.3k tok.

## Observations (1 iteration, linux-arm-msm source)

- **Scorecard:** 10 human findings — matched 2, missed 7, **1 source-FP
  (~10%)**; we-win 4. As predicted in `SOURCES.md`, the human source is
  *sparse and high-precision*: ~10% FP vs sashiko's ~30%, but only 0–1
  substantive points per thread and many replies are bare `Reviewed-by:` /
  `Acked-by:` endorsements with no signal to compare against.
- **Two of the five patchsets drew zero substantive human review**, yet one
  (`pdev-fwnode-ref`) carried a `critical` refcount leak we caught blind — in
  the exact patch a reviewer had already `Acked`. Corroborates Principle 6:
  trailers are not evidence.
- **Our recurring miss is "consistent ≠ correct" reasoning** — in `icc-rpmh`
  we accepted a teardown because it matched its sibling error paths. New
  candidate C13 targets exactly this.
- **Patch-vs-own-commit-message self-consistency** (`surface` patch 2) is a
  new gap — C14. Stage 2i says the commit message is not authority *upward*
  (vs the documented contract); C14 turns it inward (vs the message's own
  cited evidence).
- **Human-source FP is rare and benign:** the lone source-FP was not a wrong
  defect claim but an alternative-design preference — a different failure mode
  from sashiko's confident-but-wrong breakage assertions.
- **Watch item:** C2/C7/C8 are accruing Applied (10/10/11) at Confidence 1.
  Still small-sample (<50) and each has ≥1 catch, so retained; revisit under
  `SCORING.md`'s rate-based rule as Applied climbs toward 50.
