"""Data-fix migration tests (core_db._MIGRATIONS callables). The v4
users-table rebuild has its own test in test_auth.py; fresh-database
head-version checks live in test_core_db_enrollment.py."""
import sqlite3

from core import core_db


def _v4_db(path):
    """A database stopped at schema v4 — the last version before the
       series_version backfill."""
    raw = sqlite3.connect(path)
    for ddl in (core_db._SCHEMA_V1, core_db._SCHEMA_V2,
                core_db._SCHEMA_V3, core_db._SCHEMA_V4):
        raw.executescript(ddl)
    raw.execute("PRAGMA user_version=4")
    return raw


def test_v5_backfills_series_version_from_the_subject(tmp_path):
    """Pre-v5 gathers never parsed `[PATCH vN]` — every row sits at the
       column default of 1 even though the subject carries the version.
       The migration re-derives it from the stored subject; untagged and
       NULL-subject rows stay at 1."""
    path = str(tmp_path / "v4.db")
    raw = _v4_db(path)
    raw.executemany(
        "INSERT INTO patchsets (root_message_id, subject, n_patches, "
        "gathered_at) VALUES (?, ?, 1, 100)",
        [("<a@x>", "[PATCH v3 0/2] foo: series", ),
         ("<b@x>", "[RFC PATCH v12 1/1] bar: x", ),
         ("<c@x>", "[PATCH] baz: first posting", )])
    raw.execute("INSERT INTO patchsets (root_message_id, n_patches, "
                "gathered_at) VALUES ('<d@x>', 1, 100)")   # NULL subject
    raw.commit()
    raw.close()

    db = core_db.connect(path)                # applies v5+
    assert (db.execute("PRAGMA user_version").fetchone()[0]
            == len(core_db._MIGRATIONS))
    got = {r["root_message_id"]: r["series_version"] for r in db.execute(
        "SELECT root_message_id, series_version FROM patchsets")}
    assert got == {"<a@x>": 3, "<b@x>": 12, "<c@x>": 1, "<d@x>": 1}


def test_v7_adds_daily_stats_to_an_existing_database(tmp_path):
    """A pre-v7 database picks up the rollup table and the bare
       timestamp indexes on upgrade."""
    path = str(tmp_path / "v4.db")
    _v4_db(path).close()
    db = core_db.connect(path)
    assert (db.execute("PRAGMA user_version").fetchone()[0]
            == len(core_db._MIGRATIONS))
    assert db.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0] == 0
    names = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_work_items_completed", "idx_work_items_enqueued"} <= names


def test_v9_carries_v8_claims_into_the_junction(tmp_path):
    """A database stopped at v8 (the single-claimant column) upgrades
       with its claims preserved in patchset_claims and the column
       dropped."""
    path = str(tmp_path / "v8.db")
    raw = _v4_db(path)
    for ddl in (core_db._SCHEMA_V6, core_db._SCHEMA_V7,
                core_db._SCHEMA_V8):
        raw.executescript(ddl)
    raw.execute("PRAGMA user_version=8")
    raw.execute("INSERT INTO users (email, created_at) "
                "VALUES ('dev@x', 100)")
    uid = raw.execute("SELECT id FROM users").fetchone()[0]
    raw.execute("INSERT INTO patchsets (root_message_id, n_patches, "
                "gathered_at, claimed_by_user_id) "
                "VALUES ('<s@x>', 1, 100, ?)", (uid,))
    raw.execute("INSERT INTO patchsets (root_message_id, n_patches, "
                "gathered_at) VALUES ('<free@x>', 1, 100)")
    raw.commit()
    raw.close()

    db = core_db.connect(path)                # applies v9
    assert (db.execute("PRAGMA user_version").fetchone()[0]
            == len(core_db._MIGRATIONS))
    claims = db.execute("SELECT root_message_id, user_id "
                        "FROM patchset_claims").fetchall()
    assert [(c["root_message_id"], c["user_id"]) for c in claims] \
        == [("<s@x>", uid)]
    cols = {r["name"] for r in db.execute(
        "PRAGMA table_info(patchsets)").fetchall()}
    assert "claimed_by_user_id" not in cols
