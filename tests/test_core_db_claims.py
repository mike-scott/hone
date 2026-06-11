"""Cooperative claims on gathered series (core_db.claim_patchset &
   friends): the v9 junction table, multi-claimant semantics, release,
   the suggestion query, and the per-user chain-head scoping."""
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


def _upload(db, root, user_id, *, subject=None):
    core_db.upsert_patchset(db, root, subject=subject or f"[PATCH] {root}",
                            n_patches=1,
                            origin=core_db.PATCHSET_ORIGIN_UPLOADED,
                            uploaded_by_user_id=user_id)


def test_v9_replaces_the_claim_column_with_the_junction(tmp_path):
    """A fresh connect lands on the v9 head: patchset_claims exists,
       the v8 single-claimant column is gone."""
    db = _db(tmp_path)
    assert (db.execute("PRAGMA user_version").fetchone()[0]
            == len(core_db._MIGRATIONS))
    cols = {r["name"] for r in db.execute(
        "PRAGMA table_info(patchsets)").fetchall()}
    assert "claimed_by_user_id" not in cols
    names = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
    assert {"patchset_claims", "idx_patchset_claims_user"} <= names


def test_many_developers_claim_one_series(tmp_path):
    """Claiming is cooperative: a second claimant adds an association
       instead of losing a race; re-claiming your own is a no-op."""
    db = _db(tmp_path)
    alice, bob = _user(db, "alice@x"), _user(db, "bob@x")
    _gathered(db, "<s@x>")
    assert core_db.claim_patchset(db, "<s@x>", alice) is True
    assert core_db.claim_patchset(db, "<s@x>", bob) is True
    assert core_db.claim_patchset(db, "<s@x>", alice) is False  # held
    assert [c["user_id"] for c in
            core_db.patchset_claimants(db, "<s@x>")] == [alice, bob]
    assert core_db.user_has_claim(db, "<s@x>", alice)
    assert core_db.user_has_claim(db, "<s@x>", bob)


def test_uploaded_rows_are_not_claimable(tmp_path):
    """Uploads already have an owner — a claim on one must not land."""
    db = _db(tmp_path)
    alice = _user(db, "alice@x")
    _upload(db, "<u@x>", alice)
    assert core_db.claim_patchset(db, "<u@x>", alice) is False
    assert core_db.patchset_claimants(db, "<u@x>") == []


def test_unclaim_releases_one_or_all(tmp_path):
    db = _db(tmp_path)
    alice, bob = _user(db, "alice@x"), _user(db, "bob@x")
    _gathered(db, "<s@x>")
    assert core_db.unclaim_patchset(db, "<s@x>", alice) is False  # none
    core_db.claim_patchset(db, "<s@x>", alice)
    core_db.claim_patchset(db, "<s@x>", bob)
    assert core_db.unclaim_patchset(db, "<s@x>", alice) is True
    assert [c["user_id"] for c in
            core_db.patchset_claimants(db, "<s@x>")] == [bob]
    # user_id=None: the maintainer revoke clears everything.
    assert core_db.unclaim_patchset(db, "<s@x>") is True
    assert core_db.patchset_claimants(db, "<s@x>") == []
    # Released means claimable again.
    assert core_db.claim_patchset(db, "<s@x>", alice) is True


def test_claimable_patchsets_suggests_per_user(tmp_path):
    """The suggestion query stays email-matched (a heuristic, not a
       gate) and is scoped per user: my claim hides a row from MY
       suggestions only."""
    db = _db(tmp_path)
    alice = _user(db, "alice@x")
    bob = _user(db, "bob@x")
    _gathered(db, "<mine@x>", submitter="Alice@X")
    _gathered(db, "<theirs@x>", submitter="bob@x")
    got = core_db.claimable_patchsets(db, alice, "alice@x")
    assert [p["root_message_id"] for p in got] == ["mine@x"]
    assert core_db.claimable_patchsets(db, alice, "") == []
    # Bob claiming alice's lookalike does NOT hide it from alice…
    core_db.claim_patchset(db, "<mine@x>", bob)
    assert [p["root_message_id"] for p in
            core_db.claimable_patchsets(db, alice, "alice@x")] \
        == ["mine@x"]
    # …but alice claiming it does.
    core_db.claim_patchset(db, "<mine@x>", alice)
    assert core_db.claimable_patchsets(db, alice, "alice@x") == []


def test_claimable_patchsets_ignores_private_upload_successors(tmp_path):
    """A gathered successor ends the suggestion (a series fact); some
       OTHER developer's private upload superseding the row must not
       hide the lore series from this user."""
    db = _db(tmp_path)
    alice, bob = _user(db, "alice@x"), _user(db, "bob@x")
    _gathered(db, "<v1@x>", submitter="alice@x")
    _upload(db, "<bobv2@x>", bob)
    db.execute("UPDATE patchsets SET supersedes_root_message_id='v1@x' "
               "WHERE root_message_id='bobv2@x'")
    db.commit()
    assert [p["root_message_id"] for p in
            core_db.claimable_patchsets(db, alice, "alice@x")] \
        == ["v1@x"]
    _gathered(db, "<v2@x>", submitter="alice@x", supersedes="<v1@x>")
    assert [p["root_message_id"] for p in
            core_db.claimable_patchsets(db, alice, "alice@x")] \
        == ["v2@x"]


def test_list_user_patchsets_blends_uploads_and_claims(tmp_path):
    """The dashboard query returns the user's uploads AND their claimed
       gathered series — and nobody else's; the admin everyone-view
       carries every upload plus every claimed series, but never an
       unclaimed gathered row."""
    db = _db(tmp_path)
    alice, bob = _user(db, "alice@x"), _user(db, "bob@x")
    _upload(db, "<u@x>", alice, subject="alice upload")
    _upload(db, "<ub@x>", bob, subject="bob upload")
    _gathered(db, "<g@x>", submitter="alice@x")
    _gathered(db, "<loose@x>", submitter="nobody@x")
    core_db.claim_patchset(db, "<g@x>", alice)
    core_db.claim_patchset(db, "<g@x>", bob)        # shared claim

    mine = core_db.list_user_patchsets(db, user_id=alice)
    assert {r["root_message_id"] for r in mine} == {"u@x", "g@x"}
    by_root = {r["root_message_id"]: r for r in mine}
    assert by_root["g@x"]["origin"] == core_db.PATCHSET_ORIGIN_GATHERED
    assert by_root["g@x"]["first_claimant_user_id"] == alice

    bobs = core_db.list_user_patchsets(db, user_id=bob)
    assert {r["root_message_id"] for r in bobs} == {"ub@x", "g@x"}

    everyone = core_db.list_user_patchsets(db)
    assert {r["root_message_id"] for r in everyone} \
        == {"u@x", "ub@x", "g@x"}


def test_unsuperseded_user_series_is_scoped_per_user(tmp_path):
    """Iteration candidates cross the origin seam and are judged per
       user: another developer's upload superseding the shared lore
       series must not consume it for this one."""
    db = _db(tmp_path)
    alice, bob = _user(db, "alice@x"), _user(db, "bob@x")
    _gathered(db, "<v1@x>")
    core_db.claim_patchset(db, "<v1@x>", alice)
    core_db.claim_patchset(db, "<v1@x>", bob)
    _upload(db, "<bobv2@x>", bob)
    db.execute("UPDATE patchsets SET supersedes_root_message_id='v1@x' "
               "WHERE root_message_id='bobv2@x'")
    db.commit()
    # Bob's head moved to his upload; alice still sees v1 as her head.
    assert [r["root_message_id"] for r in
            core_db.unsuperseded_user_series(db, bob)] == ["bobv2@x"]
    assert [r["root_message_id"] for r in
            core_db.unsuperseded_user_series(db, alice)] == ["v1@x"]


def test_claim_with_supersedes_links_once(tmp_path):
    """The claim-time link lands with the first claimant who asks; the
       shared pointer is a series fact and is never overwritten."""
    db = _db(tmp_path)
    alice, bob = _user(db, "alice@x"), _user(db, "bob@x")
    _gathered(db, "<v1@x>")
    _gathered(db, "<v0@x>")
    _gathered(db, "<v2@x>")
    core_db.claim_patchset(db, "<v1@x>", alice)
    assert core_db.claim_patchset(db, "<v2@x>", alice,
                                  supersedes="<v1@x>") is True
    # Bob claims v2 too, trying to point it elsewhere — claim lands,
    # the existing link stays.
    assert core_db.claim_patchset(db, "<v2@x>", bob,
                                  supersedes="<v0@x>") is True
    row = db.execute(
        "SELECT supersedes_root_message_id FROM patchsets "
        "WHERE root_message_id='v2@x'").fetchone()
    assert row["supersedes_root_message_id"] == "v1@x"


def test_deleting_a_claimant_cascades_their_claims(tmp_path):
    """ON DELETE CASCADE — a removed account releases its claims and
       only its claims."""
    db = _db(tmp_path)
    alice, bob = _user(db, "alice@x"), _user(db, "bob@x")
    _gathered(db, "<s@x>")
    core_db.claim_patchset(db, "<s@x>", alice)
    core_db.claim_patchset(db, "<s@x>", bob)
    db.execute("DELETE FROM users WHERE id=?", (alice,))
    db.commit()
    assert [c["user_id"] for c in
            core_db.patchset_claimants(db, "<s@x>")] == [bob]
