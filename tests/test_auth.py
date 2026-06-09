"""Tests for core/auth.py — focused on the session-validation dependencies.

require_session re-checks the DB on every request, so admin revocation /
deletion / un-approval take effect immediately rather than waiting for the
session cookie to expire. The config-token admin (id=None) has no DB row
and bypasses the check."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core import auth, core_db


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _fake_request(*, user, db, url="http://test/x"):
    """A minimal Request-stand-in: the attributes auth.require_session reads
       (a dict-typed `session`, `app.state.db`, and `url`). Keeps the unit
       tests independent of Starlette's SessionMiddleware wiring."""
    session: dict = {}
    if user is not None:
        auth.set_session_user(SimpleNamespace(session=session), user)
    return SimpleNamespace(
        session=session,
        app=SimpleNamespace(state=SimpleNamespace(db=db)),
        url=url)


def _approved_user(db, *, email="alice@example.com", display_name="Alice"):
    uid = core_db.create_user(db, email, display_name, "local",
                              password_hash="x")
    core_db.set_user_state(db, uid, "approved")
    return auth.SessionUser(id=uid, email=email, display_name=display_name,
                            is_config_admin=False)


def test_require_session_passes_an_approved_user(db):
    user = _approved_user(db)
    assert auth.require_session(_fake_request(user=user, db=db)) == user


def test_require_session_kicks_a_revoked_user(db):
    """The key behaviour: revoking a user in the DB takes effect on the
       user's NEXT request, not when their cookie expires."""
    user = _approved_user(db)
    core_db.set_user_state(db, user.id, "revoked")
    req = _fake_request(user=user, db=db)
    with pytest.raises(HTTPException) as ei:
        auth.require_session(req)
    assert ei.value.status_code == 302
    assert ei.value.headers["Location"].startswith("/login?next=")
    # the stale session is cleared so the next visit prompts a fresh login
    # rather than looping back through the same revoked identity
    assert req.session == {}


def test_require_session_kicks_a_deleted_user(db):
    """A user row that's been DELETE'd is treated the same as a revoked one
       — the cookie's id no longer resolves; kick them to /login."""
    user = _approved_user(db)
    core_db.delete_user(db, user.id)
    req = _fake_request(user=user, db=db)
    with pytest.raises(HTTPException) as ei:
        auth.require_session(req)
    assert ei.value.status_code == 302
    assert req.session == {}


def test_require_session_kicks_a_user_un_approved_back_to_pending(db):
    """state ∈ {pending, revoked} both block — only `approved` carries an
       active session. Guards against an admin who flips an account back
       to pending pending review."""
    user = _approved_user(db)
    core_db.set_user_state(db, user.id, "pending")
    req = _fake_request(user=user, db=db)
    with pytest.raises(HTTPException) as ei:
        auth.require_session(req)
    assert ei.value.status_code == 302


def test_require_session_skips_db_lookup_for_the_config_admin(db, monkeypatch):
    """The config-token admin (id=None) is controlled by HONE_ADMIN_TOKEN, not
       by a DB row — revocation doesn't apply, and the dependency must not
       hit the DB on every config-admin request."""
    admin = auth.SessionUser(id=None, email="admin", display_name="Admin",
                             is_config_admin=True)
    calls = []
    monkeypatch.setattr(core_db, "get_user_by_id",
                        lambda *a, **kw: calls.append(1))
    assert auth.require_session(_fake_request(user=admin, db=db)) == admin
    assert calls == []                       # no DB hit


def test_require_session_redirects_when_no_session_is_present(db):
    """Sanity: the existing 'no session' path still 302s to /login with the
       request URL preserved in ?next= (the redirect's pre-existing
       behaviour, unchanged by the DB re-check)."""
    req = _fake_request(user=None, db=db, url="http://test/settings")
    with pytest.raises(HTTPException) as ei:
        auth.require_session(req)
    assert ei.value.status_code == 302
    loc = ei.value.headers["Location"]
    assert loc.startswith("/login?next=")
    assert "settings" in loc
