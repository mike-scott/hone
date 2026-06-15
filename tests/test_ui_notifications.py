"""Tests for the notification web UI: badge, dropdown, page, click-through
(auto-mark-read + redirect to the stored link), and the user-settings
preference section. CSRF enforcement of the POST routes uses the shared
require_csrf dependency (covered generically in test_csrf.py); here it's
bypassed so the route logic is exercised."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth, core_db, ui


def _client(db, user):
    app = FastAPI()
    app.include_router(ui.router)
    app.state.db = db
    app.state.config = SimpleNamespace(fleet_secret="f", admin_token="a",
                                       google_client_id="")
    app.state.runtime_config = SimpleNamespace(heartbeat_seconds=30)
    app.dependency_overrides[auth.require_session] = lambda: user
    app.dependency_overrides[auth.require_csrf] = lambda: None
    return TestClient(app)


@pytest.fixture
def ctx(tmp_path):
    db = core_db.connect(str(tmp_path / "h.db"))
    uid = core_db.create_user(db, "a@x", "Alice", "local")
    core_db.set_user_state(db, uid, "approved")
    user = auth.SessionUser(id=uid, email="a@x", display_name="Alice",
                            is_config_admin=False)
    return SimpleNamespace(db=db, uid=uid, user=user, client=_client(db, user))


def _notify(db, uid, *, title="Review ready: net: fix"):
    core_db.upsert_patchset(db, "<r@x>", subject="net: fix", n_patches=1,
                            origin=core_db.PATCHSET_ORIGIN_UPLOADED,
                            uploaded_by_user_id=uid)
    core_db.notify_patchset_users(db, "<r@x>",
                                  type=core_db.NOTIF_TYPE_REVIEW_READY,
                                  dedup_key="rr:1", title=title)


def test_badge_empty_then_shows_unread(ctx):
    assert "text-bg-danger" not in ctx.client.get("/notifications/badge").text
    _notify(ctx.db, ctx.uid)
    body = ctx.client.get("/notifications/badge").text
    assert "text-bg-danger" in body and "1" in body


def test_page_lists_items_with_click_links(ctx):
    _notify(ctx.db, ctx.uid)
    body = ctx.client.get("/notifications").text
    assert "Review ready: net: fix" in body
    nid = core_db.list_notifications(ctx.db, ctx.uid)[0]["id"]
    assert f"/notifications/{nid}/click" in body


def test_dropdown_lists_unread(ctx):
    _notify(ctx.db, ctx.uid)
    body = ctx.client.get("/notifications/dropdown").text
    assert "Review ready: net: fix" in body
    assert "See all notifications" in body


def test_click_marks_read_and_redirects_to_stored_link(ctx):
    _notify(ctx.db, ctx.uid)
    nid = core_db.list_notifications(ctx.db, ctx.uid)[0]["id"]
    r = ctx.client.get(f"/notifications/{nid}/click", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/patchsets/r%40x#ai-review"  # stored target + anchor
    assert core_db.unread_notification_count(ctx.db, ctx.uid) == 0


def test_click_anothers_notification_is_404(ctx):
    _notify(ctx.db, ctx.uid)
    nid = core_db.list_notifications(ctx.db, ctx.uid)[0]["id"]
    bob = core_db.create_user(ctx.db, "bob@x", "Bob", "local")
    core_db.set_user_state(ctx.db, bob, "approved")
    bob_user = auth.SessionUser(id=bob, email="bob@x", display_name="Bob",
                                is_config_admin=False)
    r = _client(ctx.db, bob_user).get(f"/notifications/{nid}/click",
                                      follow_redirects=False)
    assert r.status_code == 404
    assert core_db.unread_notification_count(ctx.db, ctx.uid) == 1   # untouched


def test_mark_read_and_read_all(ctx):
    _notify(ctx.db, ctx.uid)
    nid = core_db.list_notifications(ctx.db, ctx.uid)[0]["id"]
    ctx.client.post(f"/notifications/{nid}/read", follow_redirects=False)
    assert core_db.unread_notification_count(ctx.db, ctx.uid) == 0
    core_db.insert_notification(ctx.db, ctx.uid,
                                type=core_db.NOTIF_TYPE_NEW_COMMENT,
                                dedup_key="c:2", title="x")
    ctx.client.post("/notifications/read-all", follow_redirects=False)
    assert core_db.unread_notification_count(ctx.db, ctx.uid) == 0


def test_config_admin_badge_is_empty(tmp_path):
    db = core_db.connect(str(tmp_path / "h.db"))
    admin = auth.SessionUser(id=None, email="admin", display_name="admin",
                             is_config_admin=True)
    r = _client(db, admin).get("/notifications/badge")
    assert r.status_code == 200 and "text-bg-danger" not in r.text


def test_user_settings_shows_access_appropriate_toggles(ctx):
    body = ctx.client.get("/user-settings").text
    assert "notif_review_ready" in body and "notif_node_health_alert" in body
    assert "notif_user_access_request" not in body          # non-admin hides it
    # an admin sees the access-request toggle
    admin = auth.SessionUser(id=ctx.uid, email="a@x", display_name="Alice",
                             is_config_admin=True)
    abody = _client(ctx.db, admin).get("/user-settings").text
    assert "notif_user_access_request" in abody


def test_user_settings_save_opts_out_and_suppresses(ctx):
    # POST with no checkboxes → opt out of everything
    ctx.client.post("/user-settings/notifications", data={},
                    follow_redirects=False)
    assert core_db.get_notification_prefs(ctx.db, ctx.uid)["review_ready"] is False
    _notify(ctx.db, ctx.uid)                                 # would-be review_ready
    assert core_db.unread_notification_count(ctx.db, ctx.uid) == 0


def test_register_notifies_admins(ctx):
    """A new signup fans an access-request notification out to admins."""
    admin = core_db.create_user(ctx.db, "admin@x", "Admin", "local")
    core_db.set_user_state(ctx.db, admin, "approved")
    core_db.set_user_admin(ctx.db, admin, True)
    r = ctx.client.post("/register", data={
        "email": "newbie@x.io", "display_name": "Newbie",
        "password": "longenough12"}, follow_redirects=False)
    assert r.status_code == 200
    assert core_db.unread_notification_count(ctx.db, admin) == 1
