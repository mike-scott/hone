#!/usr/bin/env python3
"""backfill_check_coverage.py — derive ai_reviews.check_coverage for reviews
that predate the column, and print a gate sanity-check.

Pure Python: it uses the container's Python (the `core` package + the stdlib
`sqlite3` module, like scripts/backup.sh) — no `sqlite3` CLI, no Dockerfile
change. Normally invoked from the host via the wrapper:

    cd core && ./scripts/backfill_check_coverage.sh [--all] [--dry-run]

which is just:

    docker compose exec -T hone-core \
        python core/scripts/backfill_check_coverage.py [--all] [--dry-run]

A host checkout works too — the DB is $HONE_DB if set, else the repo-root
hone.db (core.core_db.DB):

    HONE_DB=/path/to/hone.db .venv/bin/python core/scripts/backfill_check_coverage.py

  --all      recompute EVERY review (use after changing the gate registry),
             not just rows where check_coverage IS NULL (the default).
  --dry-run  compute and report, write nothing.

Writes are a narrow `UPDATE ai_reviews SET check_coverage=?` per row, so the
rest of each review (model / tokens / node_id / concerns) is untouched. Coverage
is deterministic, so the script is idempotent — re-running yields the same
values.
"""
import json
import os
import sys
from pathlib import Path

# Runnable as `python core/scripts/backfill_check_coverage.py` from anywhere
# (incl. the container, WORKDIR /app): put the repo root on the path so
# `from core import ...` resolves. core/scripts/<this> → parents[2] is the root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import check_gates, core_db


def main():
    args = set(sys.argv[1:])
    dry = "--dry-run" in args
    do_all = "--all" in args

    db_path = core_db.DB                  # $HONE_DB in the container, else repo
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path!r} (set HONE_DB)")
    db = core_db.connect(db_path)         # applies migrations (adds the column)

    where = "" if do_all else "WHERE check_coverage IS NULL"
    rows = db.execute(
        f"SELECT id, root_message_id, methodology_version "
        f"FROM ai_reviews {where}").fetchall()
    print(f"DB {db_path}: {len(rows)} review(s) to process "
          f"({'all' if do_all else 'check_coverage IS NULL'})"
          f"{' — DRY RUN' if dry else ''}\n")

    written = skipped = 0
    default_gate = {}        # check_id -> # reviews where it had no registry gate
    applicable_tot = {}      # check_id -> # reviews it was applicable in
    fired_tot = {}           # check_id -> # reviews it fired in
    mismatches = []          # (root, check_id): fired but gate said not applicable

    for row in rows:
        root = row["root_message_id"]
        rev = core_db.get_ai_review(db, root)
        doc = core_db.methodology_document(db, row["methodology_version"])
        cov = check_gates.coverage_for_review(db, root, doc, rev["concerns"])
        if cov is None:
            skipped += 1
            print(f"  SKIP {root[:48]:<48} (no checks in methodology "
                  f"v{row['methodology_version']})")
            continue
        for c in cov:
            if c["gate"] == "default":
                default_gate[c["id"]] = default_gate.get(c["id"], 0) + 1
            if c["applicable"]:
                applicable_tot[c["id"]] = applicable_tot.get(c["id"], 0) + 1
            if c["fired"]:
                fired_tot[c["id"]] = fired_tot.get(c["id"], 0) + 1
            if c["fired"] and not c["applicable"]:
                mismatches.append((root, c["id"]))
        n_app = sum(1 for c in cov if c["applicable"])
        n_fired = sum(1 for c in cov if c["fired"])
        print(f"  {root[:48]:<48} {n_app:2d} applicable, {n_fired:2d} fired")
        if not dry:
            db.execute("UPDATE ai_reviews SET check_coverage=? WHERE id=?",
                       (json.dumps(cov), row["id"]))
            written += 1
    if not dry:
        db.commit()

    print(f"\n{'(dry run) ' if dry else ''}wrote {written}, skipped {skipped}")

    # --- gate sanity check (the reason to run this at small N) ---------------
    print("\n=== gate sanity ===")
    print("applicable (reviews per check):",
          dict(sorted(applicable_tot.items(), key=lambda kv: -kv[1])) or "none")
    print("fired      (reviews per check):",
          dict(sorted(fired_tot.items(), key=lambda kv: -kv[1])) or "none")
    if default_gate:
        print("\n[!] checks on the DEFAULT gate (no registry entry — add one to "
              "core/check_gates._GATES):", dict(default_gate))
    else:
        print("\nDEFAULT-gate checks: none (every check id is registered)")
    if mismatches:
        print(f"\n[!] fired but gate said NOT applicable ({len(mismatches)} — "
              f"gate likely too narrow):")
        for root, cid in mismatches:
            print(f"    {cid:<24} in {root[:48]}")
    else:
        print("fired-but-not-applicable: none (gates look consistent)")


if __name__ == "__main__":
    main()
