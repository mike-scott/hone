"""Tests for the claim/unclaim flow (core/ui.py /patchsets/*/claim) —
   eligibility, the rights a claim grants, and the claim doorways on the
   patchset page and the upload-collision callout."""
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


def test_claim_stamps_the_matching_account(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    r = ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/patchsets/s%40x"
    row = ctx.db.execute("SELECT claimed_by_user_id FROM patchsets") \
                .fetchone()
    assert row["claimed_by_user_id"] == ctx.uid


def test_claim_403_when_submitter_email_differs(tmp_path):
    ctx = _ctx(tmp_path, email="other@x")
    _gathered(ctx.db, submitter="dev@x")
    r = ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    assert r.status_code == 403


def test_claim_matches_email_case_insensitively(tmp_path):
    ctx = _ctx(tmp_path, email="dev@x")
    _gathered(ctx.db, submitter="Dev@X")
    r = ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    assert r.status_code == 303


def test_claim_403_on_an_already_claimed_series(tmp_path):
    """Claim held by someone else: the button never renders for you, and
       a direct POST is refused (the dict-eligibility gate, not the
       first-wins UPDATE, reports it)."""
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    other = core_db.create_user(ctx.db, "dev2@x", "dev2", "local")
    core_db.claim_patchset(ctx.db, _SERIES_ROOT, other)
    r = ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    assert r.status_code == 403


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


def test_claim_grants_review_and_prepare_requests(tmp_path):
    """The point of claiming: the pipeline actions open up, and the
       enqueued work-item carries the claimant as its origin (routing it
       to their own nodes' queue)."""
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


def test_unclaim_by_claimant_withdraws_the_rights(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    r = ctx.client.post("/patchsets/s@x/unclaim", follow_redirects=False)
    assert r.status_code == 303
    assert ctx.db.execute("SELECT claimed_by_user_id FROM patchsets") \
                 .fetchone()["claimed_by_user_id"] is None
    assert ctx.client.post("/prepare-requests/s@x",
                           follow_redirects=False).status_code == 403


def test_unclaim_403_for_a_bystander_303_for_a_maintainer(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    rex = _ctx(tmp_path, email="rex@x", db=ctx.db)
    assert rex.client.post("/patchsets/s@x/unclaim",
                           follow_redirects=False).status_code == 403
    mnt = _ctx(tmp_path, email="mnt@x", maintainer=True, db=ctx.db)
    assert mnt.client.post("/patchsets/s@x/unclaim",
                           follow_redirects=False).status_code == 303


def test_detail_page_offers_claim_only_to_the_matching_account(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    body = ctx.client.get("/patchsets/s@x").text
    assert "This looks like your series" in body
    assert "/patchsets/s%40x/claim" in body
    rex = _ctx(tmp_path, email="rex@x", db=ctx.db)
    body = rex.client.get("/patchsets/s@x").text
    assert "This looks like your series" not in body


def test_detail_page_shows_claimant_and_release_to_the_right_eyes(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    body = ctx.client.get("/patchsets/s@x").text
    assert "Claimed by" in body and "dev@x" in body
    assert "Release claim" in body
    assert "This looks like your series" not in body   # claimed: no offer
    rex = _ctx(tmp_path, email="rex@x", db=ctx.db)
    body = rex.client.get("/patchsets/s@x").text
    assert "Claimed by" in body
    assert "Release claim" not in body


def test_my_patchsets_blends_a_claimed_series(tmp_path):
    """A claimed lore series joins the dashboard table, badged "from
       lore", with the status chip starting at "gathered" (not
       "uploaded")."""
    ctx = _ctx(tmp_path)
    _gathered(ctx.db)
    ctx.client.post("/patchsets/s@x/claim", follow_redirects=False)
    body = ctx.client.get("/my-patchsets").text
    assert "[PATCH v2 0/2] net: fix" in body
    assert "from lore" in body
    assert ">gathered<" in body
    rex = _ctx(tmp_path, email="rex@x", db=ctx.db)
    assert "[PATCH v2 0/2] net: fix" not in rex.client.get(
        "/my-patchsets").text


def test_my_patchsets_suggests_matching_lore_series(tmp_path):
    """The claim strip: an unclaimed gathered series with this account's
       submitter address is offered with a one-click Claim; claiming it
       moves it from the strip into the table."""
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


def test_upload_links_to_a_claimed_lore_series(tmp_path):
    """The post-review loop across the origin seam: v1 was gathered and
       claimed; the developer uploads v2 of the same series title. The
       preview offers the claimed series as the prior, confirm stamps
       the link, and the dashboard collapses the chain to the upload
       (×2, no longer badged as lore — the head is an upload)."""
    ctx = _ctx(tmp_path, email="alice@x")
    _gathered(ctx.db, "<v1@x>", submitter="alice@x",
              subject="[PATCH 0/2] net: fix things")
    ctx.client.post("/patchsets/v1@x/claim", follow_redirects=False)

    from test_ui_upload import _post_files, _series_files
    r = _post_files(ctx.client, _series_files())   # v2, same title
    assert "new iteration of" in r.text
    assert "net: fix things" in r.text
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
        "SELECT claimed_by_user_id, supersedes_root_message_id "
        "FROM patchsets WHERE root_message_id='v2@x'").fetchone()
    assert row["claimed_by_user_id"] == ctx.uid
    assert row["supersedes_root_message_id"] == "v1@x"
    assert ctx.db.execute("SELECT COUNT(*) AS n FROM work_items "
                          "WHERE root_message_id='v1@x'") \
                 .fetchone()["n"] == 0          # queued work retired
    body = ctx.client.get("/my-patchsets").text
    assert "×2" in body


def test_claim_without_the_box_does_not_link(tmp_path):
    """The checkbox is consent — unticking it claims without linking,
       leaving two independent dashboard rows."""
    ctx = _ctx(tmp_path)
    _gathered(ctx.db, "<v1@x>")
    ctx.client.post("/patchsets/v1@x/claim", follow_redirects=False)
    _gathered(ctx.db, "<v2@x>")
    ctx.client.post("/patchsets/v2@x/claim", follow_redirects=False)
    row = ctx.db.execute(
        "SELECT claimed_by_user_id, supersedes_root_message_id "
        "FROM patchsets WHERE root_message_id='v2@x'").fetchone()
    assert row["claimed_by_user_id"] == ctx.uid
    assert row["supersedes_root_message_id"] is None


def test_claim_strip_offers_the_link_checkbox(tmp_path):
    ctx = _ctx(tmp_path)
    _gathered(ctx.db, "<v1@x>")
    ctx.client.post("/patchsets/v1@x/claim", follow_redirects=False)
    _gathered(ctx.db, "<v2@x>")
    body = ctx.client.get("/my-patchsets").text
    assert "link as new iteration" in body


def test_collision_callout_offers_claim_on_email_match(tmp_path):
    """The upload dead end becomes the doorway: colliding with your own
       gathered series renders the Claim button in the callout."""
    ctx = _ctx(tmp_path, email="alice@x")
    core_db.upsert_patchset(ctx.db, "<cover@x>",
                            subject="[PATCH v2 0/2] net: fix things",
                            submitter_email="alice@x", n_patches=2)
    from test_ui_upload import _post_files, _series_files
    r = _post_files(ctx.client, _series_files())
    assert "already in hone" in r.text
    assert "claim it to request" in r.text.lower()
    assert 'action="/patchsets/cover%40x/claim"' in r.text
    assert 'name="token"' not in r.text


def test_collision_callout_offers_no_claim_on_mismatch(tmp_path):
    ctx = _ctx(tmp_path, email="rex@x")
    core_db.upsert_patchset(ctx.db, "<cover@x>",
                            subject="[PATCH v2 0/2] net: fix things",
                            submitter_email="alice@x", n_patches=2)
    from test_ui_upload import _post_files, _series_files
    r = _post_files(ctx.client, _series_files())
    assert "already in hone" in r.text
    assert "/claim" not in r.text
