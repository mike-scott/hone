"""Tests for the cooperative claim flow (core/ui.py /patchsets/*/claim)
   — open eligibility, the rights a claim grants (and the curation it
   doesn't), per-viewer visibility, per-user chains, and the claim
   doorways on the patchset page and the upload-collision callout."""
import re
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth, core_db, ui

_SERIES_ROOT = "<s@x>"


def _app(db, user):
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: user
    app.dependency_overrides[auth.require_csrf] = lambda: None
    app.state.db = db
    return TestClient(app)


def _ctx(tmp_path, *, email="dev@x", maintainer=False, db=None):
    db = db or core_db.connect(str(tmp_path / "hone.db"))
    uid = core_db.create_user(db, email, email.split("@")[0], "local")
    core_db.set_user_state(db, uid, "approved")
    user = auth.SessionUser(id=uid, email=email, display_name=email,
                            is_config_admin=False,
                            is_maintainer=maintainer)
    return SimpleNamespace(client=_app(db, user), db=db, uid=uid)


def _gathered(db, root=_SERIES_ROOT, *, submitter="dev@x",
              subject="[PATCH v2 0/2] net: fix"):
    core_db.upsert_patchset(db, root, subject=subject,
                            submitter_email=submitter, n_patches=2)


# --- claiming is open and cooperative ---------------------------------------

def test_claim_lands_for_any_account(tmp_path):
    """No submitter-address gate: an account that never posted the
       series claims it all the same."""
    ctx = _ctx(tmp_path, email="other@x")
    _gathered(ctx.db, submitter="dev@x")
    r = ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/patchsets/s%40x"
    assert core_db.user_has_claim(ctx.db, "s@x", ctx.uid)


def test_two_accounts_claim_the_same_series(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    rex = _ctx(tmp_path, email="rex@x", db=ctx.db)
    r = rex.client.post("/patchsets/s@x/claim", follow_redirects=False)
    assert r.status_code == 303
    assert core_db.user_has_claim(ctx.db, "s@x", ctx.uid)
    assert core_db.user_has_claim(ctx.db, "s@x", rex.uid)


def test_reclaiming_your_own_is_a_noop_redirect(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    r = ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    assert r.status_code == 303
    assert len(core_db.patchset_claimants(ctx.db, "s@x")) == 1


def test_claim_403_on_uploaded_and_404_on_unknown(tmp_path):
    ctx = _ctx(tmp_path)
    core_db.upsert_patchset(ctx.db, "<u@x>", subject="[PATCH] mine",
                            submitter_email="dev@x", n_patches=1,
                            origin=core_db.PATCHSET_ORIGIN_UPLOADED,
                            uploaded_by_user_id=ctx.uid)
    assert ctx.client.post("/patchsets/u@x/claim",
                           follow_redirects=False).status_code == 403
    assert ctx.client.post("/patchsets/nope@x/claim",
                           follow_redirects=False).status_code == 404


# --- what a claim grants (and what it doesn't) ------------------------------

def test_claim_grants_review_and_prepare_requests(tmp_path):
    """The point of claiming: the request actions open up, and the
       enqueued work-item carries the claimant as its origin (routing
       it to their own nodes' queue)."""
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    assert ctx.client.post("/prepare-requests/s@x",
                           follow_redirects=False).status_code == 403
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    r = ctx.client.post("/prepare-requests/s@x", follow_redirects=False)
    assert r.status_code == 303
    wi = ctx.db.execute("SELECT type, requested_by_user_id "
                        "FROM work_items").fetchone()
    assert wi["type"] == core_db.WORK_ITEM_TYPE_PREPARE
    assert wi["requested_by_user_id"] == ctx.uid


def test_claimants_cannot_delete_the_shared_review(tmp_path):
    """Curation stays closed: with open claiming, any account could
       otherwise claim a corpus series and wipe the review others (and
       training) rely on. Maintainers still can."""
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    core_db.upsert_ai_review(ctx.db, "<s@x>", concerns=[])
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    r = ctx.client.post("/review-requests/s@x/delete",
                        follow_redirects=False)
    assert r.status_code == 403
    assert core_db.get_ai_review(ctx.db, "<s@x>") is not None
    body = ctx.client.get("/patchsets/s@x").text
    assert "Delete review" not in body          # button hidden too
    mnt = _ctx(tmp_path, email="mnt@x", maintainer=True, db=ctx.db)
    assert mnt.client.post("/review-requests/s@x/delete",
                           follow_redirects=False).status_code == 303
    assert core_db.get_ai_review(ctx.db, "<s@x>") is None


def test_unclaim_by_claimant_releases_only_their_claim(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    rex = _ctx(tmp_path, email="rex@x", db=ctx.db)
    rex.client.post("/patchsets/s@x/claim", follow_redirects=False)
    r = ctx.client.post("/patchsets/s@x/unclaim", follow_redirects=False)
    assert r.status_code == 303
    assert not core_db.user_has_claim(ctx.db, "s@x", ctx.uid)
    assert core_db.user_has_claim(ctx.db, "s@x", rex.uid)
    assert ctx.client.post("/prepare-requests/s@x",
                           follow_redirects=False).status_code == 403


def test_unclaim_403_for_a_bystander_revoke_all_for_a_maintainer(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    rex = _ctx(tmp_path, email="rex@x", db=ctx.db)
    assert rex.client.post("/patchsets/s@x/unclaim",
                           follow_redirects=False).status_code == 403
    mnt = _ctx(tmp_path, email="mnt@x", maintainer=True, db=ctx.db)
    assert mnt.client.post("/patchsets/s@x/unclaim",
                           follow_redirects=False).status_code == 303
    assert core_db.patchset_claimants(ctx.db, "s@x") == []


# --- per-viewer visibility ---------------------------------------------------

def test_detail_page_offers_claim_to_everyone_with_flavor(tmp_path):
    """The claim callout shows for any signed-in account; the
       submitter-address match only changes the wording."""
    ctx = _ctx(tmp_path)                       # dev@x — matches
    _gathered(ctx.db)
    body = ctx.client.get("/patchsets/s@x").text
    assert "This looks like your series" in body
    assert "/patchsets/s%40x/claim" in body
    rex = _ctx(tmp_path, email="rex@x", db=ctx.db)
    body = rex.client.get("/patchsets/s@x").text
    assert "Working with this series?" in body
    assert "/patchsets/s%40x/claim" in body


def test_detail_page_shows_claims_per_viewer(tmp_path):
    """The claimant sees 'by you' + their release button; another
       regular user sees no claim row (but still the claim offer); a
       maintainer sees the claimant list and the revoke-all control."""
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    body = ctx.client.get("/patchsets/s@x").text
    assert "by you" in body and "Release claim" in body
    assert "Working with this series?" not in body   # already claimed
    rex = _ctx(tmp_path, email="rex@x", db=ctx.db)
    body = rex.client.get("/patchsets/s@x").text
    assert "by you" not in body and "Release claim" not in body
    assert "Claimants" not in body
    assert "Working with this series?" in body
    mnt = _ctx(tmp_path, email="mnt@x", maintainer=True, db=ctx.db)
    body = mnt.client.get("/patchsets/s@x").text
    assert "Claimants" in body and "dev@x" in body
    assert "Revoke all claims" in body


def test_detail_page_nav_highlights_per_viewer(tmp_path):
    """The shared /patchsets/* detail routes light up the nav entry the
       viewer actually has: Corpus for maintainers / admins, My
       patchsets for regular users — whose Corpus entry is hidden, so
       the highlight must not land on a hidden link (which read as no
       highlight at all)."""
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)

    def nav_class(body, href):
        m = re.search(rf'<a href="{href}" class="(nav-link[^"]*)"', body)
        return m.group(1) if m else None

    body = ctx.client.get("/patchsets/s@x").text
    assert "active" in nav_class(body, "/my-patchsets")
    assert nav_class(body, "/") is None         # no Corpus entry at all
    mnt = _ctx(tmp_path, email="mnt@x", maintainer=True, db=ctx.db)
    body = mnt.client.get("/patchsets/s@x").text
    assert "active" in nav_class(body, "/")
    assert "active" not in nav_class(body, "/my-patchsets")


def test_detail_page_banner_names_claimants_for_maintainers(tmp_path):
    """A maintainer / admin viewing a series someone else claimed sees
       WHO holds it in the claim banner itself — their everyone-view
       dashboard lists the row, so the plain claim offer would read as
       a contradiction. Outranks the submitter flavor; regular viewers
       (no claimant visibility) keep the generic wording."""
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    mnt = _ctx(tmp_path, email="dev@x2", maintainer=True, db=ctx.db)
    body = mnt.client.get("/patchsets/s@x").text
    assert "Claimed by" in body and "dev@x" in body
    assert "claim it yourself" in body
    assert "/patchsets/s%40x/claim" in body           # offer still open
    assert "Working with this series?" not in body
    rex = _ctx(tmp_path, email="rex@x", db=ctx.db)
    body = rex.client.get("/patchsets/s@x").text
    assert "Claimed by" not in body
    assert "Working with this series?" in body


# --- the dashboard blend under multi-claim ----------------------------------

def test_my_patchsets_blends_each_users_own_view(tmp_path):
    """Two claimants of the same lore series each see it on their own
       dashboard; each sees only their own uploads next to it."""
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    core_db.upsert_patchset(ctx.db, "<mine@x>", subject="dev upload",
                            n_patches=1,
                            origin=core_db.PATCHSET_ORIGIN_UPLOADED,
                            uploaded_by_user_id=ctx.uid)
    rex = _ctx(tmp_path, email="rex@x", db=ctx.db)
    rex.client.post("/patchsets/s@x/claim", follow_redirects=False)

    body = ctx.client.get("/my-patchsets").text
    assert "[PATCH v2 0/2] net: fix" in body and "from lore" in body
    assert "dev upload" in body
    body = rex.client.get("/my-patchsets").text
    assert "[PATCH v2 0/2] net: fix" in body
    assert "dev upload" not in body


def test_my_patchsets_suggests_matching_lore_series(tmp_path):
    """The claim strip stays email-matched (suggestion heuristic) and
       per-user: claiming moves the row from the strip into the table
       for that user only."""
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    body = ctx.client.get("/my-patchsets").text
    assert "look like yours" in body
    assert 'action="/patchsets/s%40x/claim"' in body
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    body = ctx.client.get("/my-patchsets").text
    assert "look like yours" not in body
    assert "from lore" in body


def test_my_patchsets_strip_is_empty_on_no_match(tmp_path):
    ctx = _ctx(tmp_path, email="rex@x")
    _gathered(ctx.db, submitter="dev@x")
    body = ctx.client.get("/my-patchsets").text
    assert "look like yours" not in body


# --- per-user iteration chains ----------------------------------------------

def test_upload_links_to_a_claimed_lore_series(tmp_path):
    """The post-review loop across the origin seam: v1 was gathered and
       claimed; the developer uploads v2 of the same series title. The
       preview offers the claimed series as the prior, confirm stamps
       the link, and the dashboard collapses the chain to the upload."""
    ctx = _ctx(tmp_path, email="alice@x")
    _gathered(ctx.db, "<v1@x>", submitter="alice@x",
              subject="[PATCH 0/2] net: fix things")
    ctx.client.post("/patchsets/v1@x/claim", follow_redirects=False)

    from test_ui_upload import _post_files, _series_files
    r = _post_files(ctx.client, _series_files())   # v2, same title
    assert "new iteration of" in r.text
    token = r.text.split('name="token" value="')[1].split('"')[0]
    ctx.client.post("/upload/confirm",
                    data={"token": token, "link_iteration": "1"},
                    follow_redirects=False)
    sup = ctx.db.execute(
        "SELECT supersedes_root_message_id FROM patchsets "
        "WHERE root_message_id='cover@x'").fetchone()
    assert sup["supersedes_root_message_id"] == "v1@x"
    body = ctx.client.get("/my-patchsets").text
    assert "×2" in body
    assert "from lore" not in body


def test_two_users_chain_their_own_v2_off_one_lore_series(tmp_path):
    """Per-user linearity: alice linking her upload to the shared lore
       v1 does not consume it — bob's preview still offers v1, his
       confirm still links, and each dashboard shows its own chain."""
    alice = _ctx(tmp_path, email="alice@x")
    _gathered(alice.db, "<v1@x>", submitter="alice@x",
              subject="[PATCH 0/2] net: fix things")
    alice.client.post("/patchsets/v1@x/claim", follow_redirects=False)
    bob = _ctx(tmp_path, email="bob@x", db=alice.db)
    bob.client.post("/patchsets/v1@x/claim", follow_redirects=False)

    from test_ui_upload import _mail, _post_files, _series_files
    r = _post_files(alice.client, _series_files())
    token = r.text.split('name="token" value="')[1].split('"')[0]
    alice.client.post("/upload/confirm",
                      data={"token": token, "link_iteration": "1"},
                      follow_redirects=False)

    bob_files = [
        ("0000-c.patch", _mail("[PATCH v2 0/2] net: fix things",
                               "<bcover@x>")),
        ("0001-a.patch", _mail("[PATCH v2 1/2] net: one", "<bp1@x>")),
        ("0002-b.patch", _mail("[PATCH v2 2/2] net: two", "<bp2@x>")),
    ]
    r = _post_files(bob.client, bob_files)
    assert "new iteration of" in r.text            # v1 still bob's head
    token = r.text.split('name="token" value="')[1].split('"')[0]
    bob.client.post("/upload/confirm",
                    data={"token": token, "link_iteration": "1"},
                    follow_redirects=False)
    sup = {r["root_message_id"]: r["supersedes_root_message_id"]
           for r in alice.db.execute(
               "SELECT root_message_id, supersedes_root_message_id "
               "FROM patchsets WHERE supersedes_root_message_id "
               "IS NOT NULL")}
    assert sup == {"cover@x": "v1@x", "bcover@x": "v1@x"}

    body = alice.client.get("/my-patchsets").text
    assert "×2" in body and "net: fix things" in body
    assert "bcover@x" not in body
    body = bob.client.get("/my-patchsets").text
    assert "×2" in body


def test_claim_links_a_new_lore_iteration(tmp_path):
    """v1 claimed, v2 gathered later with the same title: the detail
       page offers link-on-claim, and the POST stamps claim + link and
       retires v1's queued pipeline work."""
    ctx = _ctx(tmp_path)
    _gathered(ctx.db, "<v1@x>")
    ctx.client.post("/patchsets/v1@x/claim", follow_redirects=False)
    ctx.client.post("/prepare-requests/v1@x", follow_redirects=False)
    _gathered(ctx.db, "<v2@x>")

    body = ctx.client.get("/patchsets/v2@x").text
    assert "new iteration of" in body
    assert 'name="link_iteration"' in body
    r = ctx.client.post("/patchsets/v2@x/claim",
                        data={"link_iteration": "1"},
                        follow_redirects=False)
    assert r.status_code == 303
    row = ctx.db.execute(
        "SELECT supersedes_root_message_id FROM patchsets "
        "WHERE root_message_id='v2@x'").fetchone()
    assert row["supersedes_root_message_id"] == "v1@x"
    assert core_db.user_has_claim(ctx.db, "v2@x", ctx.uid)
    assert ctx.db.execute("SELECT COUNT(*) AS n FROM work_items "
                          "WHERE root_message_id='v1@x'") \
                 .fetchone()["n"] == 0          # queued work retired


def test_claim_without_the_box_does_not_link(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db, "<v1@x>")
    ctx.client.post("/patchsets/v1@x/claim", follow_redirects=False)
    _gathered(ctx.db, "<v2@x>")
    ctx.client.post("/patchsets/v2@x/claim", follow_redirects=False)
    row = ctx.db.execute(
        "SELECT supersedes_root_message_id FROM patchsets "
        "WHERE root_message_id='v2@x'").fetchone()
    assert row["supersedes_root_message_id"] is None
    assert core_db.user_has_claim(ctx.db, "v2@x", ctx.uid)


def test_claim_strip_offers_the_link_checkbox(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db, "<v1@x>")
    ctx.client.post("/patchsets/v1@x/claim", follow_redirects=False)
    _gathered(ctx.db, "<v2@x>")
    body = ctx.client.get("/my-patchsets").text
    assert "link as new iteration" in body


# --- the upload-collision doorway -------------------------------------------

def test_collision_callout_offers_claim_to_any_account(tmp_path):
    """The upload dead end is a doorway for everyone now — no address
       match required."""
    ctx = _ctx(tmp_path, email="rex@x")
    core_db.upsert_patchset(ctx.db, "<cover@x>",
                            subject="[PATCH v2 0/2] net: fix things",
                            submitter_email="alice@x", n_patches=2)
    from test_ui_upload import _post_files, _series_files
    r = _post_files(ctx.client, _series_files())
    assert "already in hone" in r.text
    assert "you can claim it" in r.text
    assert 'action="/patchsets/cover%40x/claim"' in r.text
    assert 'name="token"' not in r.text


def test_collision_callout_offers_no_claim_once_you_hold_one(tmp_path):
    """Already claimed by this account: the callout points at the
       existing patchset instead of re-offering the claim."""
    ctx = _ctx(tmp_path, email="alice@x")
    core_db.upsert_patchset(ctx.db, "<cover@x>",
                            subject="[PATCH v2 0/2] net: fix things",
                            submitter_email="alice@x", n_patches=2)
    ctx.client.post("/patchsets/cover@x/claim", follow_redirects=False)
    from test_ui_upload import _post_files, _series_files
    r = _post_files(ctx.client, _series_files())
    assert "already in hone" in r.text
    assert 'action="/patchsets/cover%40x/claim"' not in r.text
    assert 'name="token"' not in r.text
