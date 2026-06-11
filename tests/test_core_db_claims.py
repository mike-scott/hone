"""Claiming gathered series (core_db.claim_patchset & friends): the v8
   column, the first-wins stamp, release, and the submitter-email
   suggestion query."""
from core import core_db


def _db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _user(db, email):
    uid = core_db.create_user(db, email, email.split("@")[0], "local")
    core_db.set_user_state(db, uid, "approved")
    return uid


def _gathered(db, root, *, submitter="dev@x", supersedes=None):
    core_db.upsert_patchset(db, root, subject=f"[PATCH] {root}",
                            submitter_email=submitter, n_patches=1,
                            supersedes_root_message_id=supersedes)


def test_v8_adds_claimed_by_to_an_existing_database(tmp_path):
    """A fresh connect lands on the v8 head with the claim column and
       its index present."""
    db = _db(tmp_path)
    assert (db.execute("PRAGMA user_version").fetchone()[0]
            == len(core_db._MIGRATIONS))
    cols = {r["name"] for r in db.execute(
        "PRAGMA table_info(patchsets)").fetchall()}
    assert "claimed_by_user_id" in cols
    names = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_patchsets_claimed" in names


def test_claim_is_first_wins(tmp_path):
    db = _db(tmp_path)
    alice, bob = _user(db, "alice@x"), _user(db, "bob@x")
    _gathered(db, "<s@x>")
    assert core_db.claim_patchset(db, "<s@x>", alice) is True
    assert core_db.claim_patchset(db, "<s@x>", bob) is False
    row = db.execute("SELECT claimed_by_user_id FROM patchsets").fetchone()
    assert row["claimed_by_user_id"] == alice


def test_uploaded_rows_are_not_claimable(tmp_path):
    """Uploads already have an owner — a claim on one must not land."""
    db = _db(tmp_path)
    alice = _user(db, "alice@x")
    core_db.upsert_patchset(db, "<u@x>", subject="[PATCH] mine",
                            n_patches=1,
                            origin=core_db.PATCHSET_ORIGIN_UPLOADED,
                            uploaded_by_user_id=alice)
    assert core_db.claim_patchset(db, "<u@x>", alice) is False


def test_unclaim_releases_and_reports(tmp_path):
    db = _db(tmp_path)
    alice = _user(db, "alice@x")
    _gathered(db, "<s@x>")
    assert core_db.unclaim_patchset(db, "<s@x>") is False   # nothing held
    core_db.claim_patchset(db, "<s@x>", alice)
    assert core_db.unclaim_patchset(db, "<s@x>") is True
    row = db.execute("SELECT claimed_by_user_id FROM patchsets").fetchone()
    assert row["claimed_by_user_id"] is None
    # Released means claimable again.
    assert core_db.claim_patchset(db, "<s@x>", alice) is True


def test_claimable_patchsets_matches_email_case_insensitively(tmp_path):
    db = _db(tmp_path)
    alice = _user(db, "alice@x")
    _gathered(db, "<mine@x>", submitter="Alice@X")
    _gathered(db, "<theirs@x>", submitter="bob@x")
    got = core_db.claimable_patchsets(db, "alice@x")
    assert [p["root_message_id"] for p in got] == ["mine@x"]
    assert core_db.claimable_patchsets(db, "") == []
    # A claimed row stops being suggested.
    core_db.claim_patchset(db, "<mine@x>", alice)
    assert core_db.claimable_patchsets(db, "alice@x") == []


def test_claimable_patchsets_returns_chain_heads_only(tmp_path):
    """A superseded gathered iteration is history — only the head of the
       chain is offered for claiming."""
    db = _db(tmp_path)
    _gathered(db, "<v1@x>")
    _gathered(db, "<v2@x>", supersedes="<v1@x>")
    got = core_db.claimable_patchsets(db, "dev@x")
    assert [p["root_message_id"] for p in got] == ["v2@x"]


def test_list_user_patchsets_blends_uploads_and_claims(tmp_path):
    """The dashboard query returns the user's uploads AND their claimed
       gathered series — and nobody else's; the admin everyone-view
       carries every upload plus every claimed series, but never an
       unclaimed gathered row."""
    db = _db(tmp_path)
    alice, bob = _user(db, "alice@x"), _user(db, "bob@x")
    core_db.upsert_patchset(db, "<u@x>", subject="alice upload",
                            n_patches=1,
                            origin=core_db.PATCHSET_ORIGIN_UPLOADED,
                            uploaded_by_user_id=alice)
    core_db.upsert_patchset(db, "<ub@x>", subject="bob upload",
                            n_patches=1,
                            origin=core_db.PATCHSET_ORIGIN_UPLOADED,
                            uploaded_by_user_id=bob)
    _gathered(db, "<g@x>", submitter="alice@x")
    _gathered(db, "<loose@x>", submitter="nobody@x")
    core_db.claim_patchset(db, "<g@x>", alice)

    mine = core_db.list_user_patchsets(db, user_id=alice)
    assert {r["root_message_id"] for r in mine} == {"u@x", "g@x"}
    by_root = {r["root_message_id"]: r for r in mine}
    assert by_root["g@x"]["origin"] == core_db.PATCHSET_ORIGIN_GATHERED
    assert by_root["g@x"]["iterations"] == 1

    everyone = core_db.list_user_patchsets(db)
    assert {r["root_message_id"] for r in everyone} \
        == {"u@x", "ub@x", "g@x"}


def test_deleting_the_claimant_clears_the_claim(tmp_path):
    """ON DELETE SET NULL — a removed account releases its claims."""
    db = _db(tmp_path)
    alice = _user(db, "alice@x")
    _gathered(db, "<s@x>")
    core_db.claim_patchset(db, "<s@x>", alice)
    db.execute("DELETE FROM users WHERE id=?", (alice,))
    db.commit()
    row = db.execute("SELECT claimed_by_user_id FROM patchsets").fetchone()
    assert row["claimed_by_user_id"] is None
