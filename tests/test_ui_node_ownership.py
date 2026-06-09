"""Tests for the node-ownership UI scoping rules
   (core/ui.py + core/templates/{nodes,node_detail,_nodes_fleet_table,
   enroll}.html).

   The rules under test:
   - Approved nodes are *visible* to every logged-in user, but mutation
     controls (handles_system toggle, owner reassignment, delete) only
     render — and only respond — for the node's owner or the config-token
     admin.
   - Pending enrollments are private: the user who first looks up the
     user_code via /enroll claims it (tag_pending_enrollment); nobody
     else sees it.
   - Approving a tagged enrollment creates a node owned by the tagger,
     with handles_system=0 (strict user-only until the owner opts in).
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth, core_db, runtime_config, ui


def _build_ctx(tmp_path, *, current_user):
    """A TestClient over the UI router with a real DB, with the auth
       dependencies pinned to `current_user`. Returns SimpleNamespace
       carrying client + db + the user."""
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: current_user
    app.dependency_overrides[auth.require_csrf] = lambda: None
    if current_user.is_config_admin:
        app.dependency_overrides[auth.require_config_admin] = lambda: current_user
    else:
        # Real require_config_admin raises 403 when not admin — let it.
        pass
    app.state.db = db
    app.state.runtime_config = runtime_config.load(
        str(tmp_path / "config.yaml"))
    return SimpleNamespace(client=TestClient(app), db=db, user=current_user)


def _make_user(db, email):
    """Create a user, approve it, and return (user_id, SessionUser)."""
    uid = core_db.create_user(db, email, email.split("@")[0], "local")
    core_db.set_user_state(db, uid, "approved")
    return uid, auth.SessionUser(
        id=uid, email=email, display_name=email,
        is_config_admin=False)


def _seed_node(db, *, name, owner_user_id, handles_system=0):
    """Insert a node row directly — short-circuit the enrollment dance
       for tests that only care about the post-approval state."""
    import time as _t
    cur = db.execute(
        "INSERT INTO nodes (name, task_types, state, enrolled_at, "
        "owner_user_id, handles_system) VALUES (?, ?, ?, ?, ?, ?)",
        (name, '["prepare"]', core_db.NODE_STATE_ACTIVE,
         int(_t.time()), owner_user_id, handles_system))
    db.commit()
    return cur.lastrowid


# --- /nodes list: every approved node is visible -------------------------

def test_nodes_list_shows_all_approved_nodes_to_any_user(tmp_path):
    """Bob can see Alice's node on /nodes — read access is global, only
       writes are gated. The Owner column carries Alice's email."""
    # Bootstrap two users + an approved node owned by Alice.
    tmp = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin@x", display_name="admin",
        is_config_admin=True))                                # admin to seed
    alice_id, _ = _make_user(tmp.db, "alice@x")
    bob_id, bob_user = _make_user(tmp.db, "bob@x")
    _seed_node(tmp.db, name="alice-node-1", owner_user_id=alice_id)

    # Re-bind the app to Bob and re-issue the GET.
    bob_ctx = _build_ctx(tmp_path, current_user=bob_user)
    body = bob_ctx.client.get("/nodes").text
    assert "alice-node-1" in body
    assert "alice@x" in body                                  # owner column


def test_nodes_list_marks_my_own_nodes_with_a_yours_badge(tmp_path):
    """Alice's row carries a "Yours" affordance so her own nodes stand
       out among the visible-to-everyone listing."""
    ctx = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin@x", display_name="admin",
        is_config_admin=True))
    alice_id, alice_user = _make_user(ctx.db, "alice@x")
    _seed_node(ctx.db, name="alice-node", owner_user_id=alice_id)

    alice_ctx = _build_ctx(tmp_path, current_user=alice_user)
    body = alice_ctx.client.get("/nodes").text
    assert "alice-node" in body and "Yours" in body


# --- node mutation handlers: 403 for non-owners --------------------------

def test_configure_node_403s_for_non_owner(tmp_path):
    """Bob cannot toggle handles_system on Alice's node."""
    seed = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin@x", display_name="admin",
        is_config_admin=True))
    alice_id, _ = _make_user(seed.db, "alice@x")
    bob_id, bob_user = _make_user(seed.db, "bob@x")
    nid = _seed_node(seed.db, name="alice-node",
                     owner_user_id=alice_id, handles_system=0)

    bob = _build_ctx(tmp_path, current_user=bob_user)
    r = bob.client.post(f"/nodes/{nid}/configure",
                        data={"handles_system": "on"})
    assert r.status_code == 403
    # And the flag did not flip.
    row = bob.db.execute("SELECT handles_system FROM nodes WHERE id=?",
                          (nid,)).fetchone()
    assert row["handles_system"] == 0


def test_configure_node_owner_flips_handles_system(tmp_path):
    """Alice CAN flip handles_system on her own node. The form posts
       `handles_system=on` (checkbox semantics) → 303 redirect + the
       DB row updated to 1."""
    seed = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin@x", display_name="admin",
        is_config_admin=True))
    alice_id, alice_user = _make_user(seed.db, "alice@x")
    nid = _seed_node(seed.db, name="alice-node",
                     owner_user_id=alice_id, handles_system=0)

    alice = _build_ctx(tmp_path, current_user=alice_user)
    r = alice.client.post(f"/nodes/{nid}/configure",
                          data={"handles_system": "on"},
                          follow_redirects=False)
    assert r.status_code == 303
    row = alice.db.execute("SELECT handles_system FROM nodes WHERE id=?",
                            (nid,)).fetchone()
    assert row["handles_system"] == 1

    # And a follow-up POST with an absent field flips it back off.
    r = alice.client.post(f"/nodes/{nid}/configure", data={},
                          follow_redirects=False)
    assert r.status_code == 303
    row = alice.db.execute("SELECT handles_system FROM nodes WHERE id=?",
                            (nid,)).fetchone()
    assert row["handles_system"] == 0


def test_delete_node_403s_for_non_owner(tmp_path):
    """Bob cannot delete Alice's node."""
    seed = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin@x", display_name="admin",
        is_config_admin=True))
    alice_id, _ = _make_user(seed.db, "alice@x")
    bob_id, bob_user = _make_user(seed.db, "bob@x")
    nid = _seed_node(seed.db, name="alice-node", owner_user_id=alice_id)

    bob = _build_ctx(tmp_path, current_user=bob_user)
    r = bob.client.post(f"/nodes/{nid}/delete")
    assert r.status_code == 403
    assert core_db.get_node(bob.db, nid) is not None         # still there


def test_owner_form_is_admin_only(tmp_path):
    """The change-owner endpoint is admin-gated via require_config_admin;
       a non-admin posting it gets a 403."""
    seed = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin@x", display_name="admin",
        is_config_admin=True))
    alice_id, alice_user = _make_user(seed.db, "alice@x")
    nid = _seed_node(seed.db, name="alice-node", owner_user_id=alice_id)

    # Even owning the node doesn't let Alice change its owner.
    alice = _build_ctx(tmp_path, current_user=alice_user)
    r = alice.client.post(f"/nodes/{nid}/owner",
                          data={"owner_email": ""})
    assert r.status_code == 403


def test_owner_form_admin_can_make_node_ownerless(tmp_path):
    """Admin posts an empty owner_email → the node becomes ownerless
       (system-only)."""
    admin = auth.SessionUser(id=None, email="admin@x",
                              display_name="admin", is_config_admin=True)
    ctx = _build_ctx(tmp_path, current_user=admin)
    alice_id, _ = _make_user(ctx.db, "alice@x")
    nid = _seed_node(ctx.db, name="alice-node", owner_user_id=alice_id)
    r = ctx.client.post(f"/nodes/{nid}/owner",
                        data={"owner_email": ""},
                        follow_redirects=False)
    assert r.status_code == 303
    row = ctx.db.execute("SELECT owner_user_id FROM nodes WHERE id=?",
                          (nid,)).fetchone()
    assert row["owner_user_id"] is None


# --- pending-enrollment pairing: scope-on-lookup, first-wins -------------

def test_enroll_lookup_tags_the_enrollment_for_the_lookup_er(tmp_path):
    """The first user to look up a user_code via /enroll claims the
       pending row. The page renders the Approve button, and the row
       picks up requested_by_user_id pointing at that user."""
    seed = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin@x", display_name="admin",
        is_config_admin=True))
    alice_id, alice_user = _make_user(seed.db, "alice@x")
    enr = core_db.create_enrollment(seed.db, node_name="alice-pending")

    alice = _build_ctx(tmp_path, current_user=alice_user)
    r = alice.client.get("/enroll", params={"code": enr["user_code"]})
    assert r.status_code == 200 and "Approve" in r.text

    row = alice.db.execute(
        "SELECT requested_by_user_id FROM node_enrollments "
        "WHERE user_code=?", (enr["user_code"],)).fetchone()
    assert row["requested_by_user_id"] == alice_id


def test_enroll_lookup_refuses_when_another_user_already_paired(tmp_path):
    """Once Alice has tagged a pending enrollment, Bob looking up the
       same code sees an "already paired" notice and does NOT see the
       approve form — first-lookup-wins."""
    seed = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin@x", display_name="admin",
        is_config_admin=True))
    alice_id, alice_user = _make_user(seed.db, "alice@x")
    bob_id, bob_user = _make_user(seed.db, "bob@x")
    enr = core_db.create_enrollment(seed.db, node_name="alice-pending")

    alice = _build_ctx(tmp_path, current_user=alice_user)
    alice.client.get("/enroll", params={"code": enr["user_code"]})

    bob = _build_ctx(tmp_path, current_user=bob_user)
    r = bob.client.get("/enroll", params={"code": enr["user_code"]})
    assert "already been paired" in r.text
    assert "Approve" not in r.text


def test_pending_enrollment_visible_only_to_the_lookup_er(tmp_path):
    """After Alice tags a pending enrollment, /nodes lists it for her —
       but Bob's /nodes does not show it. Admins (config-token) see
       every pending row regardless of tagging."""
    seed = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin@x", display_name="admin",
        is_config_admin=True))
    alice_id, alice_user = _make_user(seed.db, "alice@x")
    bob_id, bob_user = _make_user(seed.db, "bob@x")
    enr = core_db.create_enrollment(seed.db, node_name="alice-pending")

    # Alice pairs it.
    alice = _build_ctx(tmp_path, current_user=alice_user)
    alice.client.get("/enroll", params={"code": enr["user_code"]})

    # Alice's /nodes shows the pending row; Bob's does not.
    assert enr["user_code"] in alice.client.get("/nodes").text
    bob = _build_ctx(tmp_path, current_user=bob_user)
    assert enr["user_code"] not in bob.client.get("/nodes").text

    # Admin sees every pending row.
    admin = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin2@x", display_name="admin",
        is_config_admin=True))
    assert enr["user_code"] in admin.client.get("/nodes").text


def test_approve_enrollment_creates_node_owned_by_tagger_with_handles_system_zero(tmp_path):
    """Alice approves the pending enrollment she paired → the new
       `nodes` row carries her id as owner and handles_system=0
       (strict user-only until she opts in)."""
    seed = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin@x", display_name="admin",
        is_config_admin=True))
    alice_id, alice_user = _make_user(seed.db, "alice@x")
    enr = core_db.create_enrollment(seed.db, node_name="alice-pending")

    alice = _build_ctx(tmp_path, current_user=alice_user)
    alice.client.get("/enroll", params={"code": enr["user_code"]})
    r = alice.client.post(
        f"/nodes/enrollments/{enr['user_code']}/approve")
    assert r.status_code == 200                               # follow redirect

    row = alice.db.execute(
        "SELECT owner_user_id, handles_system FROM nodes "
        "WHERE name='alice-pending'").fetchone()
    assert row["owner_user_id"] == alice_id
    assert row["handles_system"] == 0


def test_admin_approve_creates_ownerless_node_that_handles_system(tmp_path):
    """The config-token admin approves an untagged enrollment → the new
       node is ownerless AND handles_system=1. The pair matters: an
       ownerless node with handles_system=0 would be configured for
       nothing (no owner queue, no system fallback) and 204 forever."""
    admin = auth.SessionUser(id=None, email="admin@x",
                              display_name="admin", is_config_admin=True)
    ctx = _build_ctx(tmp_path, current_user=admin)
    enr = core_db.create_enrollment(ctx.db, node_name="fleet-node")

    ctx.client.get("/enroll", params={"code": enr["user_code"]})
    r = ctx.client.post(
        f"/nodes/enrollments/{enr['user_code']}/approve")
    assert r.status_code == 200                               # follow redirect

    row = ctx.db.execute(
        "SELECT owner_user_id, handles_system FROM nodes "
        "WHERE name='fleet-node'").fetchone()
    assert row["owner_user_id"] is None
    assert row["handles_system"] == 1


def test_admin_approve_of_tagged_enrollment_preserves_the_pairing(tmp_path):
    """The admin approving an enrollment Alice paired creates the node
       owned by Alice (handles_system=0) — admin approval is a shortcut,
       not an ownership grab. Only an untagged enrollment yields an
       ownerless node."""
    admin = auth.SessionUser(id=None, email="admin@x",
                              display_name="admin", is_config_admin=True)
    seed = _build_ctx(tmp_path, current_user=admin)
    alice_id, alice_user = _make_user(seed.db, "alice@x")
    enr = core_db.create_enrollment(seed.db, node_name="alice-pending")

    # Alice pairs it; the admin approves it.
    alice = _build_ctx(tmp_path, current_user=alice_user)
    alice.client.get("/enroll", params={"code": enr["user_code"]})
    r = seed.client.post(
        f"/nodes/enrollments/{enr['user_code']}/approve")
    assert r.status_code == 200                               # follow redirect

    row = seed.db.execute(
        "SELECT owner_user_id, handles_system FROM nodes "
        "WHERE name='alice-pending'").fetchone()
    assert row["owner_user_id"] == alice_id
    assert row["handles_system"] == 0


def test_approve_enrollment_403s_if_caller_did_not_pair(tmp_path):
    """Bob cannot approve a pending enrollment Alice paired — the
       handler silent-redirects without creating a node. (No 403
       because the row is invisible to Bob; the response is the same
       as a no-op approve.)"""
    seed = _build_ctx(tmp_path, current_user=auth.SessionUser(
        id=None, email="admin@x", display_name="admin",
        is_config_admin=True))
    alice_id, alice_user = _make_user(seed.db, "alice@x")
    bob_id, bob_user = _make_user(seed.db, "bob@x")
    enr = core_db.create_enrollment(seed.db, node_name="alice-pending")

    alice = _build_ctx(tmp_path, current_user=alice_user)
    alice.client.get("/enroll", params={"code": enr["user_code"]})

    bob = _build_ctx(tmp_path, current_user=bob_user)
    bob.client.post(f"/nodes/enrollments/{enr['user_code']}/approve")
    assert core_db.list_nodes(bob.db) == []                   # not approved
