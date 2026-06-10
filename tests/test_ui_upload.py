"""Tests for the patchset upload path: the series parser (core/upload.py)
   and the upload → preview → confirm flow (core/ui.py /upload*)."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth, core_db, ui, upload

_BASE = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"


def _mail(subject, msgid, *, body="fix it\n---\ndiff --git a/f b/f\n"
                                  "@@ -1 +1 @@\n-old\n+new\n",
          base=None, sender="Alice <alice@x>"):
    if base:
        body += f"\nbase-commit: {base}\n"
    return (f"From: {sender}\n"
            f"Date: Tue, 09 Dec 2025 15:51:00 +0000\n"
            f"Subject: {subject}\n"
            f"Message-ID: {msgid}\n"
            f"\n{body}").encode()


def _series_files():
    """A 2-patch v2 series with cover letter and a declared base —
       deliberately out of order to exercise the sort."""
    return [
        ("0002-b.patch", _mail("[PATCH v2 2/2] net: part two", "<p2@x>")),
        ("0000-cover.patch", _mail("[PATCH v2 0/2] net: fix things",
                                   "<cover@x>", base=_BASE)),
        ("0001-a.patch", _mail("[PATCH v2 1/2] net: part one", "<p1@x>")),
    ]


# --- parser -----------------------------------------------------------------

def test_parse_full_series_with_cover_and_base():
    parsed = upload.parse_upload(_series_files())
    assert parsed["ok"], parsed["errors"]
    assert parsed["root_message_id"] == "<cover@x>"
    assert parsed["subject"] == "[PATCH v2 0/2] net: fix things"
    assert parsed["submitter_email"] == "alice@x"
    assert parsed["n_patches"] == 2
    assert parsed["series_version"] == 2
    assert parsed["base_commit"] == _BASE
    assert [p["part_index"] for p in parsed["patches"]] == [1, 2]
    assert not any("base-commit" in w for w in parsed["warnings"])


def test_parse_incomplete_series_is_an_error():
    files = [("a", _mail("[PATCH 1/3] one", "<p1@x>")),
             ("c", _mail("[PATCH 3/3] three", "<p3@x>"))]
    parsed = upload.parse_upload(files)
    assert not parsed["ok"]
    assert any("missing patch 2/3" in e for e in parsed["errors"])


def test_parse_single_mbox_ignores_replies():
    """A b4-style thread mbox: cover + patch + a reply. The reply is
       ignored with a warning; the series still parses."""
    mbox = (b"From x Mon Jan 1 00:00:00 2026\n"
            + _mail("[PATCH 0/1] one thing", "<c@x>", base=_BASE)
            + b"\nFrom x Mon Jan 1 00:00:01 2026\n"
            + _mail("[PATCH 1/1] the thing", "<p1@x>")
            + b"\nFrom x Mon Jan 1 00:00:02 2026\n"
            + _mail("Re: [PATCH 1/1] the thing", "<r1@x>",
                    sender="Bob <bob@x>"))
    parsed = upload.parse_upload([("thread.mbox", mbox)])
    assert parsed["ok"], parsed["errors"]
    assert parsed["n_patches"] == 1
    assert any("ignored" in w for w in parsed["warnings"])


def test_parse_pasted_bare_diff_is_a_single_synthetic_patch():
    diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n"
    parsed = upload.parse_upload([], pasted=diff)
    assert parsed["ok"], parsed["errors"]
    assert parsed["n_patches"] == 1
    assert parsed["patches"][0]["part_index"] is None
    assert parsed["root_message_id"].startswith("<upload-")
    assert any("base-commit" in w for w in parsed["warnings"])


def test_parse_garbage_paste_is_an_error():
    parsed = upload.parse_upload([], pasted="hello, please review")
    assert not parsed["ok"]
    assert any("neither" in e for e in parsed["errors"])


def test_parse_missing_message_id_synthesises_one_with_warning():
    raw = (b"From: A <a@x>\nSubject: [PATCH] no msgid\n\n"
           b"diff --git a/f b/f\n@@ -1 +1 @@\n-x\n+y\n")
    parsed = upload.parse_upload([("p.patch", raw)])
    assert parsed["ok"], parsed["errors"]
    assert parsed["root_message_id"].startswith("<upload-")
    assert any("Message-ID" in w for w in parsed["warnings"])


def test_parse_mixed_numbered_and_unnumbered_is_an_error():
    files = [("a", _mail("[PATCH 1/2] one", "<p1@x>")),
             ("b", _mail("[PATCH] loner", "<p2@x>"))]
    parsed = upload.parse_upload(files)
    assert not parsed["ok"]
    assert any("mix" in e for e in parsed["errors"])


# --- the upload → preview → confirm flow ------------------------------------

def _ctx(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    uid = core_db.create_user(db, "alice@x", "alice", "local")
    core_db.set_user_state(db, uid, "approved")
    # Maintainer so the corpus-listing assertions below can render "/";
    # uploading itself needs no grant.
    user = auth.SessionUser(id=uid, email="alice@x", display_name="alice",
                            is_config_admin=False, is_maintainer=True)
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: user
    app.dependency_overrides[auth.require_csrf] = lambda: None
    app.state.db = db
    return SimpleNamespace(client=TestClient(app), db=db, uid=uid)


def _post_files(client, files):
    return client.post("/upload", files=[
        ("files", (name, data, "text/plain")) for name, data in files])


def test_upload_preview_then_confirm_ingests_the_series(tmp_path):
    ctx = _ctx(tmp_path)
    r = _post_files(ctx.client, _series_files())
    assert r.status_code == 200
    assert "Series preview" in r.text and "Confirm" in r.text
    token = r.text.split('name="token" value="')[1].split('"')[0]

    r = ctx.client.post("/upload/confirm", data={"token": token},
                        follow_redirects=False)
    assert r.status_code == 303

    ps = ctx.db.execute("SELECT * FROM patchsets").fetchone()
    assert ps["root_message_id"] == "cover@x"
    assert ps["origin"] == core_db.PATCHSET_ORIGIN_UPLOADED
    assert ps["uploaded_by_user_id"] == ctx.uid
    assert ps["n_patches"] == 2 and ps["base_commit"] == _BASE

    msgs = ctx.db.execute(
        "SELECT type, part_index FROM messages "
        "ORDER BY COALESCE(part_index, 99)").fetchall()
    assert [(m["type"], m["part_index"]) for m in msgs] == [
        (core_db.MSG_TYPE_COVER, 0),
        (core_db.MSG_TYPE_PATCH, 1),
        (core_db.MSG_TYPE_PATCH, 2)]

    wi = ctx.db.execute("SELECT type, requested_by_user_id "
                        "FROM work_items").fetchone()
    assert wi["type"] == core_db.WORK_ITEM_TYPE_PREPARE
    assert wi["requested_by_user_id"] == ctx.uid


def test_upload_preview_shows_errors_without_a_confirm(tmp_path):
    ctx = _ctx(tmp_path)
    r = ctx.client.post("/upload", data={"pasted": "hello"})
    assert r.status_code == 200
    assert "neither" in r.text
    assert 'name="token"' not in r.text
    assert ctx.db.execute("SELECT COUNT(*) AS n FROM patchsets") \
                 .fetchone()["n"] == 0


def test_upload_confirm_rejects_unknown_or_foreign_tokens(tmp_path):
    ctx = _ctx(tmp_path)
    r = ctx.client.post("/upload/confirm", data={"token": "nope"})
    assert r.status_code == 410

    # A token stashed by another user is not confirmable by this one.
    r = _post_files(ctx.client, _series_files())
    token = r.text.split('name="token" value="')[1].split('"')[0]
    store = ctx.client.app.state.pending_uploads
    store[token]["user_id"] = 999                 # someone else's preview
    r = ctx.client.post("/upload/confirm", data={"token": token})
    assert r.status_code == 410


def test_upload_preview_warns_when_root_already_in_corpus(tmp_path):
    ctx = _ctx(tmp_path)
    core_db.upsert_patchset(ctx.db, "<cover@x>", subject="already here",
                            n_patches=2)
    r = _post_files(ctx.client, _series_files())
    assert "already in the" in r.text


# --- commit 2: dashboard, corpus exclusion, badges, training guard ---------

def _confirm_upload(ctx, files=None):
    """Drive the full upload → preview → confirm flow; returns the root."""
    r = _post_files(ctx.client, files or _series_files())
    token = r.text.split('name="token" value="')[1].split('"')[0]
    ctx.client.post("/upload/confirm", data={"token": token},
                    follow_redirects=False)
    return "cover@x"


def test_uploaded_patchsets_are_excluded_from_the_corpus_listing(tmp_path):
    """The home page is the CORPUS view — an uploaded series must not
       appear in its rows or its pager total."""
    ctx = _ctx(tmp_path)
    core_db.upsert_patchset(ctx.db, "<lkml@x>", subject="gathered thing",
                            n_patches=1)
    _confirm_upload(ctx)
    assert core_db.count_patchsets(ctx.db) == 1
    page = core_db.list_patchsets_page(ctx.db)
    assert [p["subject"] for p in page] == ["gathered thing"]
    body = ctx.client.get("/").text
    assert "gathered thing" in body
    assert "net: fix things" not in body


def test_my_patchsets_lists_own_uploads_with_status(tmp_path):
    """The uploader's dashboard shows their series with the pipeline
       status chip — 'preparing' right after confirm (the prepare
       work-item exists, nothing has run yet)."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    body = ctx.client.get("/my-patchsets").text
    assert "net: fix things" in body
    assert ">preparing<" in body
    assert ">Owner<" not in body                  # own view: no owner column


def test_my_patchsets_scopes_to_the_viewer(tmp_path):
    """Another regular user sees an empty dashboard; the admin sees the
       upload with an Owner column."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)

    bob_id = core_db.create_user(ctx.db, "bob@x", "bob", "local")
    bob = auth.SessionUser(id=bob_id, email="bob@x", display_name="bob",
                           is_config_admin=False)
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: bob
    app.state.db = ctx.db
    assert "net: fix things" not in TestClient(app).get("/my-patchsets").text

    admin = auth.SessionUser(id=None, email="admin", display_name="Admin",
                             is_config_admin=True)
    app.dependency_overrides[auth.require_session] = lambda: admin
    body = TestClient(app).get("/my-patchsets").text
    assert "net: fix things" in body
    assert ">Owner<" in body and "alice@x" in body


def test_patchset_detail_badges_an_uploaded_series(tmp_path):
    ctx = _ctx(tmp_path)
    root = _confirm_upload(ctx)
    body = ctx.client.get(f"/patchsets/{root}").text
    assert ">uploaded<" in body
    assert "alice@x" in body


def test_enqueue_session_train_refuses_uploaded_patchsets(tmp_path):
    """The structural training guard: every train work-item passes
       through enqueue_session_train, which refuses uploaded-origin
       patchsets regardless of what a future selector picks."""
    ctx = _ctx(tmp_path)
    root = _confirm_upload(ctx)
    core_db.upsert_ai_review(ctx.db, root, concerns=[])
    sid = core_db.create_session_draft(ctx.db, "standard")
    with pytest.raises(ValueError, match="never training data"):
        core_db.enqueue_session_train(
            ctx.db, session_id=sid, root_message_id=root,
            patch_message_id="<p1@x>", comment_message_id="<c1@x>",
            session_role=core_db.SESSION_ROLE_POOL, stratum_label="x")


# --- per-patchset actions: maintainer-gated, uploader-excepted --------------

def _regular_ctx(tmp_path, db=None):
    """A client over the SAME db pinned to a regular (no-grant) user."""
    db = db or core_db.connect(str(tmp_path / "hone.db"))
    uid = core_db.create_user(db, "rex@x", "rex", "local")
    core_db.set_user_state(db, uid, "approved")
    user = auth.SessionUser(id=uid, email="rex@x", display_name="rex",
                            is_config_admin=False)
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: user
    app.dependency_overrides[auth.require_csrf] = lambda: None
    app.state.db = db
    return SimpleNamespace(client=TestClient(app), db=db, uid=uid)


def test_review_request_403_for_regular_user_on_corpus_patchset(tmp_path):
    """Request/delete review — and request prepare — on a gathered
       patchset is maintainer territory: a no-grant user gets a 403
       and no buttons."""
    ctx = _regular_ctx(tmp_path)
    core_db.upsert_patchset(ctx.db, "<lkml@x>", subject="corpus row",
                            n_patches=1)
    r = ctx.client.post("/review-requests/lkml@x", follow_redirects=False)
    assert r.status_code == 403
    r = ctx.client.post("/review-requests/lkml@x/delete",
                        follow_redirects=False)
    assert r.status_code == 403
    r = ctx.client.post("/prepare-requests/lkml@x", follow_redirects=False)
    assert r.status_code == 403
    body = ctx.client.get("/patchsets/lkml@x").text
    assert "Request review" not in body
    assert "Request prepare" not in body


def test_uploader_can_request_review_of_their_own_upload(tmp_path):
    """The uploader exception: review is the upload's whole purpose, so
       the no-grant uploader can re-request (and delete) the review of
       their own series — but a different no-grant user cannot."""
    ctx = _ctx(tmp_path)                     # alice (maintainer fixture)...
    root = _confirm_upload(ctx)
    # ...but the rule must hold for a NO-grant uploader, so re-stamp the
    # upload onto rex and act as rex.
    rex = _regular_ctx(tmp_path, db=ctx.db)
    ctx.db.execute("UPDATE patchsets SET uploaded_by_user_id=? "
                   "WHERE root_message_id=?", (rex.uid, root))
    ctx.db.commit()
    # Prepare hasn't produced metadata, so the enqueue is a no-op — the
    # gate is what's under test, and it must NOT 403.
    r = rex.client.post(f"/review-requests/{root}", follow_redirects=False)
    assert r.status_code == 303
    body = rex.client.get(f"/patchsets/{root}").text
    # The upload's auto-enqueued prepare is in the queue — the uploader
    # sees the pipeline chip; no action is executable for them yet.
    assert ">preparing<" in body

    # A different no-grant user still gets the 403 on the same upload.
    bob_id = core_db.create_user(ctx.db, "bob2@x", "bob2", "local")
    bob = auth.SessionUser(id=bob_id, email="bob2@x", display_name="bob2",
                           is_config_admin=False)
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: bob
    app.dependency_overrides[auth.require_csrf] = lambda: None
    app.state.db = ctx.db
    r = TestClient(app).post(f"/review-requests/{root}",
                             follow_redirects=False)
    assert r.status_code == 403


def test_work_item_re_arms_are_admin_only(tmp_path):
    """release-deferred / retry-unappliable are ADMIN-only: a re-arm
       mutates fleet scheduling on a row that keeps its original origin,
       so even the uploader of their own upload (and maintainers) get a
       403 — only an admin may re-arm."""
    ctx = _regular_ctx(tmp_path)
    core_db.add_methodology_version(ctx.db, {"name": "t", "version": 1})
    # Make it rex's OWN upload — ownership must not open the re-arm gate.
    core_db.upsert_patchset(ctx.db, "<lkml@x>", subject="rex upload",
                            n_patches=1,
                            origin=core_db.PATCHSET_ORIGIN_UPLOADED,
                            uploaded_by_user_id=ctx.uid)
    wid = core_db.enqueue_prepare(ctx.db, "<lkml@x>")
    for action in ("retry-unappliable", "release-deferred"):
        r = ctx.client.post(f"/work-items/{wid}/{action}",
                            follow_redirects=False)
        assert r.status_code == 403

    admin = auth.SessionUser(id=None, email="admin", display_name="Admin",
                             is_config_admin=True)
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: admin
    app.dependency_overrides[auth.require_csrf] = lambda: None
    app.state.db = ctx.db
    r = TestClient(app).post(f"/work-items/{wid}/retry-unappliable",
                             follow_redirects=False)
    assert r.status_code == 303          # no-op on a claimable row, but allowed


# --- re-upload invalidation ---------------------------------------------

def _modified_series():
    """_series_files with patch 1's body edited — the iterate-on-review
       loop: same Message-IDs, new content."""
    return [
        ("0002-b.patch", _mail("[PATCH v2 2/2] net: part two", "<p2@x>")),
        ("0000-cover.patch", _mail("[PATCH v2 0/2] net: fix things",
                                   "<cover@x>", base=_BASE)),
        ("0001-a.patch", _mail("[PATCH v2 1/2] net: part one", "<p1@x>",
                               body="fix it better\n---\n"
                                    "diff --git a/f b/f\n"
                                    "@@ -1 +1 @@\n-old\n+newer\n")),
    ]


def _plant_pipeline_artifacts(db, root="<cover@x>"):
    """Pretend prepare and review ran: their products exist."""
    core_db.upsert_patchset_metadata(
        db, root, mode="heuristic",
        tree_state={}, subsystem={}, patch_size={}, maintainer={},
        patch_type={}, review_intensity={}, preparation_notes={})
    core_db.upsert_ai_review(db, root, concerns=[])


def test_reupload_with_changed_content_resets_the_pipeline(tmp_path):
    """Re-confirming the same root with an edited body drops the stale
       prepare metadata + AI review and queues a fresh prepare — derived
       artifacts never outlive the bodies they were computed from."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    _plant_pipeline_artifacts(ctx.db)
    _confirm_upload(ctx, files=_modified_series())
    assert core_db.get_patchset_metadata(ctx.db, "<cover@x>") is None
    assert core_db.get_ai_review(ctx.db, "<cover@x>") is None
    rows = ctx.db.execute("SELECT type, state FROM work_items").fetchall()
    assert [(r["type"], r["state"]) for r in rows] == [
        (core_db.WORK_ITEM_TYPE_PREPARE, core_db.WORK_ITEM_STATE_CLAIMABLE)]
    body = ctx.db.execute("SELECT body FROM messages "
                          "WHERE message_id='p1@x'").fetchone()["body"]
    assert "+newer" in body


def test_reupload_with_identical_content_keeps_artifacts(tmp_path):
    """Re-confirming byte-identical files is a refresh-only no-op — the
       prepared metadata and review survive."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    _plant_pipeline_artifacts(ctx.db)
    _confirm_upload(ctx)
    assert core_db.get_patchset_metadata(ctx.db, "<cover@x>") is not None
    assert core_db.get_ai_review(ctx.db, "<cover@x>") is not None


def test_reupload_collision_with_a_corpus_patchset_does_not_reset(tmp_path):
    """The invalidation is scoped to the uploader's own uploaded row — a
       collision with a GATHERED patchset must not let an upload wipe the
       corpus's review or metadata."""
    ctx = _ctx(tmp_path)
    core_db.upsert_patchset(ctx.db, "<cover@x>", subject="corpus row",
                            n_patches=2)
    _plant_pipeline_artifacts(ctx.db)
    _confirm_upload(ctx)
    assert core_db.get_patchset_metadata(ctx.db, "<cover@x>") is not None
    assert core_db.get_ai_review(ctx.db, "<cover@x>") is not None


def test_reupload_by_another_user_does_not_reset(tmp_path):
    """A different no-grant user re-uploading someone else's upload with
       changed content refreshes bodies (today's behaviour) but cannot
       drop the owner's pipeline artifacts."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    _plant_pipeline_artifacts(ctx.db)
    rex = _regular_ctx(tmp_path, db=ctx.db)
    r = _post_files(rex.client, _modified_series())
    token = r.text.split('name="token" value="')[1].split('"')[0]
    rex.client.post("/upload/confirm", data={"token": token},
                    follow_redirects=False)
    assert core_db.get_patchset_metadata(ctx.db, "<cover@x>") is not None
    assert core_db.get_ai_review(ctx.db, "<cover@x>") is not None


def test_upload_preview_warns_rerun_for_own_reupload(tmp_path):
    """The duplicate-root warning tells the owner the pipeline re-runs on
       content change (the corpus-collision wording keeps the old text —
       see test_upload_preview_warns_when_root_already_in_corpus)."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    r = _post_files(ctx.client, _modified_series())
    assert "pipeline re-runs" in r.text


# --- iteration linking --------------------------------------------------

def test_parse_extracts_change_id_trailer():
    files = [("p.patch", _mail("[PATCH] x", "<p@x>",
                               body="fix\n---\ndiff --git a/f b/f\n"
                                    "@@ -1 +1 @@\n-a\n+b\n\n"
                                    "Change-Id: I0123abcd\n"))]
    parsed = upload.parse_upload(files)
    assert parsed["ok"], parsed["errors"]
    assert parsed["change_id"] == "I0123abcd"


def test_parse_change_id_absent_is_none():
    parsed = upload.parse_upload(_series_files())
    assert parsed["change_id"] is None


@pytest.mark.parametrize("subject,title", [
    ("[PATCH v2 0/3] net: fix things",      "net: fix things"),
    ("[PATCH v9 0/3] net: fix things",      "net: fix things"),
    ("[PATCH 0/3] NET: Fix Things",         "net: fix things"),
    ("[RFC PATCH v2] [resend] net: fix it", "net: fix it"),
    ("no brackets at all",                  "no brackets at all"),
    ("",                                    ""),
])
def test_series_title(subject, title):
    assert upload.series_title(subject) == title


def test_find_prior_iteration_change_id_beats_title():
    """A Change-Id match wins even when the title differs (the series
       was renamed between iterations); newest candidate first."""
    cands = [
        {"root_message_id": "a", "subject": "net: other", "change_id": "I1"},
        {"root_message_id": "b", "subject": "net: fix",   "change_id": None},
    ]
    got = upload.find_prior_iteration(
        cands, subject="[PATCH v3] renamed entirely", change_id="I1")
    assert got["root_message_id"] == "a"
    # Without a Change-Id the title decides.
    got = upload.find_prior_iteration(cands, subject="[PATCH v2] net: fix")
    assert got["root_message_id"] == "b"
    assert upload.find_prior_iteration(cands, subject="[PATCH] new work") \
        is None


def _series_files_iter2(subject_tag="v2"):
    """A second format-patch run of the same series: fresh Message-IDs,
       same subjects (the uploader didn't bump the marker)."""
    return [
        ("0002-b.patch", _mail(f"[PATCH {subject_tag} 2/2] net: part two",
                               "<p2-iter2@x>")),
        ("0000-cover.patch", _mail(f"[PATCH {subject_tag} 0/2] net: fix things",
                                   "<cover-iter2@x>", base=_BASE)),
        ("0001-a.patch", _mail(f"[PATCH {subject_tag} 1/2] net: part one",
                               "<p1-iter2@x>",
                               body="better\n---\ndiff --git a/f b/f\n"
                                    "@@ -1 +1 @@\n-old\n+better\n")),
    ]


def test_second_upload_offers_and_links_the_iteration(tmp_path):
    """The full loop: iteration 2 (fresh Message-IDs, same subject) is
       offered as a link at preview; confirming stamps supersedes,
       retires the old iteration's queued prepare, and the dashboard
       collapses to one ×2 row."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)                          # iteration 1 + its prepare
    r = _post_files(ctx.client, _series_files_iter2())
    assert "new iteration of" in r.text
    assert 'name="link_iteration"' in r.text
    token = r.text.split('name="token" value="')[1].split('"')[0]
    ctx.client.post("/upload/confirm",
                    data={"token": token, "link_iteration": "1"},
                    follow_redirects=False)
    ps = ctx.db.execute("SELECT supersedes_root_message_id FROM patchsets "
                        "WHERE root_message_id='cover-iter2@x'").fetchone()
    assert ps["supersedes_root_message_id"] == "cover@x"
    # The old iteration's claimable prepare is retired; the new one's is
    # queued — exactly one prepare item remains, on the new root.
    rows = ctx.db.execute(
        "SELECT root_message_id, type, state FROM work_items").fetchall()
    assert [(w["root_message_id"], w["type"], w["state"]) for w in rows] == [
        ("cover-iter2@x", core_db.WORK_ITEM_TYPE_PREPARE,
         core_db.WORK_ITEM_STATE_CLAIMABLE)]
    # Dashboard: one row (the head), iteration badge.
    body = ctx.client.get("/my-patchsets").text
    assert body.count("net: fix things") == 1
    assert "×2" in body


def test_link_opt_out_keeps_two_rows(tmp_path):
    """Unticking the checkbox uploads a genuinely separate patchset —
       no link, both rows on the dashboard, old queue untouched."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    r = _post_files(ctx.client, _series_files_iter2())
    token = r.text.split('name="token" value="')[1].split('"')[0]
    ctx.client.post("/upload/confirm", data={"token": token},
                    follow_redirects=False)      # checkbox NOT posted
    ps = ctx.db.execute("SELECT supersedes_root_message_id FROM patchsets "
                        "WHERE root_message_id='cover-iter2@x'").fetchone()
    assert ps["supersedes_root_message_id"] is None
    n = ctx.db.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
    assert n == 2                                 # both prepares queued
    body = ctx.client.get("/my-patchsets").text
    assert body.count("net: fix things") == 2
    assert "×2" not in body


def test_no_offer_for_a_different_title(tmp_path):
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    files = [("p.patch", _mail("[PATCH] mm: unrelated work", "<other@x>"))]
    r = _post_files(ctx.client, files)
    assert "new iteration of" not in r.text


def test_no_offer_across_users(tmp_path):
    """Candidates are the uploader's OWN chain heads — another user's
       identically-titled upload is not offered."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    rex = _regular_ctx(tmp_path, db=ctx.db)
    r = _post_files(rex.client, _series_files_iter2())
    assert "new iteration of" not in r.text


def test_claimed_work_of_superseded_iteration_survives(tmp_path):
    """Auto-cancel retires only UNHELD items: an in-flight claimed
       prepare keeps its lease and finishes as the old iteration's
       history."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    core_db.add_methodology_version(ctx.db, {"name": "t", "version": 1})
    assert core_db.claim_work_item(
        ctx.db, "node-1", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_PREPARE,)) is not None
    r = _post_files(ctx.client, _series_files_iter2())
    token = r.text.split('name="token" value="')[1].split('"')[0]
    ctx.client.post("/upload/confirm",
                    data={"token": token, "link_iteration": "1"},
                    follow_redirects=False)
    states = {w["root_message_id"]: w["state"] for w in ctx.db.execute(
        "SELECT root_message_id, state FROM work_items "
        "WHERE type=?", (core_db.WORK_ITEM_TYPE_PREPARE,))}
    assert states["cover@x"] == core_db.WORK_ITEM_STATE_CLAIMED
    assert states["cover-iter2@x"] == core_db.WORK_ITEM_STATE_CLAIMABLE


def test_detail_page_walks_the_chain(tmp_path):
    """The old iteration says 'Superseded by' (warning-styled), the new
       one says 'Supersedes' and its iteration number."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    r = _post_files(ctx.client, _series_files_iter2())
    token = r.text.split('name="token" value="')[1].split('"')[0]
    ctx.client.post("/upload/confirm",
                    data={"token": token, "link_iteration": "1"},
                    follow_redirects=False)
    new = ctx.client.get("/patchsets/cover-iter2@x").text
    assert "Supersedes" in new and "iteration 2" in new
    old = ctx.client.get("/patchsets/cover@x").text
    assert "Superseded by" in old


def test_third_iteration_chains_on_the_head(tmp_path):
    """Iteration 3 is offered against the chain HEAD (iteration 2), not
       the original — chains stay linear and the count reads ×3."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    for files in (_series_files_iter2(),):
        r = _post_files(ctx.client, files)
        token = r.text.split('name="token" value="')[1].split('"')[0]
        ctx.client.post("/upload/confirm",
                        data={"token": token, "link_iteration": "1"},
                        follow_redirects=False)
    files3 = [("c.patch", _mail("[PATCH v2 0/1] net: fix things",
                                "<cover-iter3@x>", base=_BASE)),
              ("p.patch", _mail("[PATCH v2 1/1] net: part one",
                                "<p1-iter3@x>"))]
    r = _post_files(ctx.client, files3)
    assert "new iteration of" in r.text
    token = r.text.split('name="token" value="')[1].split('"')[0]
    ctx.client.post("/upload/confirm",
                    data={"token": token, "link_iteration": "1"},
                    follow_redirects=False)
    ps = ctx.db.execute("SELECT supersedes_root_message_id FROM patchsets "
                        "WHERE root_message_id='cover-iter3@x'").fetchone()
    assert ps["supersedes_root_message_id"] == "cover-iter2@x"
    body = ctx.client.get("/my-patchsets").text
    assert body.count("net: fix things") == 1
    assert "×3" in body


# --- uploaded-patchset deletion -------------------------------------------

def test_uploader_can_delete_their_own_upload(tmp_path):
    """The cleanup path: the row, thread, work-items and derived
       artifacts all go; redirect lands on /my-patchsets, now empty."""
    ctx = _ctx(tmp_path)
    root = _confirm_upload(ctx)
    _plant_pipeline_artifacts(ctx.db)
    body = ctx.client.get(f"/patchsets/{root}").text
    assert "Delete patchset" in body
    r = ctx.client.post(f"/patchsets/{root}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/my-patchsets"
    for table in ("patchsets", "messages", "work_items",
                  "patchset_metadata", "ai_reviews"):
        assert ctx.db.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert "net: fix things" not in ctx.client.get("/my-patchsets").text


def test_delete_mid_chain_splices_the_iteration_chain(tmp_path):
    """Deleting iteration 2 of a 3-link chain re-points iteration 3 at
       iteration 1 — the chain stays linear and reads ×2."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    for files, _ in ((_series_files_iter2(), 2),):
        r = _post_files(ctx.client, files)
        token = r.text.split('name="token" value="')[1].split('"')[0]
        ctx.client.post("/upload/confirm",
                        data={"token": token, "link_iteration": "1"},
                        follow_redirects=False)
    files3 = [("c.patch", _mail("[PATCH v2 0/1] net: fix things",
                                "<cover-iter3@x>", base=_BASE)),
              ("p.patch", _mail("[PATCH v2 1/1] net: part one",
                                "<p1-iter3@x>"))]
    r = _post_files(ctx.client, files3)
    token = r.text.split('name="token" value="')[1].split('"')[0]
    ctx.client.post("/upload/confirm",
                    data={"token": token, "link_iteration": "1"},
                    follow_redirects=False)

    r = ctx.client.post("/patchsets/cover-iter2@x/delete",
                        follow_redirects=False)
    assert r.status_code == 303
    ps = ctx.db.execute("SELECT supersedes_root_message_id FROM patchsets "
                        "WHERE root_message_id='cover-iter3@x'").fetchone()
    assert ps["supersedes_root_message_id"] == "cover@x"
    body = ctx.client.get("/my-patchsets").text
    assert body.count("net: fix things") == 1
    assert "×2" in body


def test_delete_head_promotes_the_prior_iteration(tmp_path):
    """Deleting the newest iteration makes the prior one the chain head
       again — it reappears as the dashboard row."""
    ctx = _ctx(tmp_path)
    _confirm_upload(ctx)
    r = _post_files(ctx.client, _series_files_iter2())
    token = r.text.split('name="token" value="')[1].split('"')[0]
    ctx.client.post("/upload/confirm",
                    data={"token": token, "link_iteration": "1"},
                    follow_redirects=False)
    ctx.client.post("/patchsets/cover-iter2@x/delete",
                    follow_redirects=False)
    rows = ctx.db.execute("SELECT root_message_id FROM patchsets").fetchall()
    assert [r["root_message_id"] for r in rows] == ["cover@x"]
    body = ctx.client.get("/my-patchsets").text
    assert body.count("net: fix things") == 1
    assert "×2" not in body


def test_corpus_patchsets_are_never_deletable(tmp_path):
    """Gathered rows are data: no button, and the POST 403s even for the
       maintainer-grade _ctx user (gather's dedup would make the loss
       silently permanent)."""
    ctx = _ctx(tmp_path)
    core_db.upsert_patchset(ctx.db, "<lkml@x>", subject="corpus row",
                            n_patches=1)
    body = ctx.client.get("/patchsets/lkml@x").text
    assert "Delete patchset" not in body
    r = ctx.client.post("/patchsets/lkml@x/delete", follow_redirects=False)
    assert r.status_code == 403
    assert core_db.get_patchset(ctx.db, "<lkml@x>") is not None


def test_delete_403_for_a_different_no_grant_user(tmp_path):
    ctx = _ctx(tmp_path)
    root = _confirm_upload(ctx)
    rex = _regular_ctx(tmp_path, db=ctx.db)
    r = rex.client.post(f"/patchsets/{root}/delete", follow_redirects=False)
    assert r.status_code == 403
    assert core_db.get_patchset(ctx.db, root) is not None


def test_delete_unknown_patchset_404(tmp_path):
    ctx = _ctx(tmp_path)
    r = ctx.client.post("/patchsets/nope@x/delete", follow_redirects=False)
    assert r.status_code == 404
