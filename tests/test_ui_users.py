"""Tests for the Users screen's admin-grant controls
   (core/ui.py /users/{id}/grant-admin + /users/{id}/revoke-admin,
   core/templates/users.html)."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth, core_db, ui


@pytest.fixture
def ctx(tmp_path, fake_admin_session):
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.include_router(ui.router)
    fake_admin_session(app)
    app.state.db = db
    return SimpleNamespace(client=TestClient(app), db=db)


def _approved_user(db, email="alice@x"):
    uid = core_db.create_user(db, email, email.split("@")[0], "local")
    core_db.set_user_state(db, uid, "approved")
    return uid


def test_grant_admin_endpoint_sets_the_flag(ctx):
    uid = _approved_user(ctx.db)
    r = ctx.client.post(f"/users/{uid}/grant-admin", follow_redirects=False)
    assert r.status_code == 303
    assert core_db.get_user_by_id(ctx.db, uid)["is_admin"] == 1


def test_revoke_admin_endpoint_clears_the_flag(ctx):
    uid = _approved_user(ctx.db)
    core_db.set_user_admin(ctx.db, uid, True)
    r = ctx.client.post(f"/users/{uid}/revoke-admin", follow_redirects=False)
    assert r.status_code == 303
    assert core_db.get_user_by_id(ctx.db, uid)["is_admin"] == 0


def test_users_page_shows_admin_badge_and_toggle_controls(ctx):
    """An admin user's row carries the badge + a Revoke-admin control; a
       regular user's row offers Make admin."""
    admin_uid = _approved_user(ctx.db, "boss@x")
    core_db.set_user_admin(ctx.db, admin_uid, True)
    _approved_user(ctx.db, "pleb@x")
    body = ctx.client.get("/users").text
    assert "Revoke admin" in body
    assert "Make admin" in body
    assert 'class="badge text-bg-primary">admin<' in body


def test_admin_grant_routes_are_403_for_non_admin(tmp_path):
    """The grant/revoke endpoints sit behind the REAL require_config_admin
       gate — a regular session user gets a 403 and the flag never flips."""
    db = core_db.connect(str(tmp_path / "hone.db"))
    target = _approved_user(db)
    app = FastAPI()
    app.include_router(ui.router)
    user = auth.SessionUser(id=99, email="user@x", display_name="user",
                            is_config_admin=False)
    app.dependency_overrides[auth.require_session] = lambda: user
    app.dependency_overrides[auth.require_csrf] = lambda: None
    app.state.db = db
    client = TestClient(app)
    assert client.post(f"/users/{target}/grant-admin").status_code == 403
    assert client.post(f"/users/{target}/revoke-admin").status_code == 403
    assert core_db.get_user_by_id(db, target)["is_admin"] == 0
