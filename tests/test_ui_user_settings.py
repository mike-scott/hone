"""Tests for the per-account User-settings page (core/ui.py /user-settings
   + core/templates/user_settings.html): display-name update for any
   DB-backed account, password change for local-provider accounts, and the
   config-token-admin notice."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from core import auth, core_db, ui

_OLD_PASSWORD = "old-password-123"
_NEW_PASSWORD = "new-password-456"


def _build(tmp_path, current_user):
    """A TestClient over the UI router pinned to `current_user`. Real
       SessionMiddleware — the profile handler refreshes the session
       cookie in place, which needs request.session to exist."""
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t" * 32,
                       session_cookie="hone_session")
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: current_user
    app.dependency_overrides[auth.require_csrf] = lambda: None
    app.state.db = db
    return SimpleNamespace(client=TestClient(app), db=db)


def _local_user(db, email="alice@x"):
    uid = core_db.create_user(
        db, email, "alice", "local",
        password_hash=auth.hash_password(_OLD_PASSWORD))
    core_db.set_user_state(db, uid, "approved")
    return uid


def _session(uid, email="alice@x"):
    return auth.SessionUser(id=uid, email=email, display_name="alice",
                            is_config_admin=False)


def _ctx_with_local_user(tmp_path):
    seed_db = core_db.connect(str(tmp_path / "hone.db"))
    uid = _local_user(seed_db)
    ctx = _build(tmp_path, _session(uid))
    ctx.uid = uid
    return ctx


def test_page_shows_profile_and_password_forms_for_local_user(tmp_path):
    ctx = _ctx_with_local_user(tmp_path)
    body = ctx.client.get("/user-settings").text
    assert "Display name" in body
    assert "Change password" in body
    assert "alice@x" in body


def test_page_hides_password_form_for_google_user(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    uid = core_db.create_user(db, "g@x", "g", "google", google_sub="g-1")
    core_db.set_user_state(db, uid, "approved")
    ctx = _build(tmp_path, _session(uid, email="g@x"))
    body = ctx.client.get("/user-settings").text
    assert "Change password" not in body
    assert "signs in via Google" in body


def test_page_shows_notice_for_config_token_admin(tmp_path):
    admin = auth.SessionUser(id=None, email="admin", display_name="Admin",
                             is_config_admin=True)
    ctx = _build(tmp_path, admin)
    body = ctx.client.get("/user-settings").text
    assert "has no user account" in body
    assert "Change password" not in body


def test_profile_update_changes_display_name(tmp_path):
    ctx = _ctx_with_local_user(tmp_path)
    r = ctx.client.post("/user-settings/profile",
                        data={"display_name": "Alice the Great"},
                        follow_redirects=False)
    assert r.status_code == 303
    row = core_db.get_user_by_id(ctx.db, ctx.uid)
    assert row["display_name"] == "Alice the Great"


def test_profile_update_rejects_an_empty_name(tmp_path):
    ctx = _ctx_with_local_user(tmp_path)
    r = ctx.client.post("/user-settings/profile",
                        data={"display_name": "   "})
    assert r.status_code == 422
    assert core_db.get_user_by_id(ctx.db, ctx.uid)["display_name"] == "alice"


def test_password_change_happy_path(tmp_path):
    ctx = _ctx_with_local_user(tmp_path)
    r = ctx.client.post("/user-settings/password",
                        data={"current_password": _OLD_PASSWORD,
                              "new_password": _NEW_PASSWORD,
                              "confirm_password": _NEW_PASSWORD},
                        follow_redirects=False)
    assert r.status_code == 303
    row = core_db.get_user_by_id(ctx.db, ctx.uid)
    assert auth.verify_password(_NEW_PASSWORD, row["password_hash"])
    assert not auth.verify_password(_OLD_PASSWORD, row["password_hash"])


def test_password_change_rejects_wrong_current_password(tmp_path):
    ctx = _ctx_with_local_user(tmp_path)
    r = ctx.client.post("/user-settings/password",
                        data={"current_password": "not-the-password",
                              "new_password": _NEW_PASSWORD,
                              "confirm_password": _NEW_PASSWORD})
    assert r.status_code == 422
    assert "Current password is incorrect" in r.text
    row = core_db.get_user_by_id(ctx.db, ctx.uid)
    assert auth.verify_password(_OLD_PASSWORD, row["password_hash"])


def test_password_change_rejects_short_or_mismatched_new(tmp_path):
    ctx = _ctx_with_local_user(tmp_path)
    r = ctx.client.post("/user-settings/password",
                        data={"current_password": _OLD_PASSWORD,
                              "new_password": "short",
                              "confirm_password": "short"})
    assert r.status_code == 422 and "at least 10" in r.text
    r = ctx.client.post("/user-settings/password",
                        data={"current_password": _OLD_PASSWORD,
                              "new_password": _NEW_PASSWORD,
                              "confirm_password": "something-else-1"})
    assert r.status_code == 422 and "do not match" in r.text
    row = core_db.get_user_by_id(ctx.db, ctx.uid)
    assert auth.verify_password(_OLD_PASSWORD, row["password_hash"])


def test_password_change_403_for_google_account(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    uid = core_db.create_user(db, "g@x", "g", "google", google_sub="g-1")
    core_db.set_user_state(db, uid, "approved")
    ctx = _build(tmp_path, _session(uid, email="g@x"))
    r = ctx.client.post("/user-settings/password",
                        data={"current_password": "x",
                              "new_password": _NEW_PASSWORD,
                              "confirm_password": _NEW_PASSWORD})
    assert r.status_code == 403
