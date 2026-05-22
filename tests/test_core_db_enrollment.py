"""Unit tests for the OAuth enrollment / token data layer in core/core_db.py
(schema migration 2)."""
import pytest

from core import core_db


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _approved_node(db, tenant="Acme"):
    cid = core_db.register_client(db, tenant)
    enr = core_db.create_enrollment(db)
    return core_db.approve_enrollment(db, enr["user_code"], cid), cid


def test_schema_migrated_to_v2(db):
    assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"nodes", "node_enrollments", "node_tokens"} <= tables


def test_clients_no_longer_has_client_key(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(clients)")}
    assert "client_key" not in cols


def test_create_and_look_up_enrollment(db):
    enr = core_db.create_enrollment(db, node_name="n1", task_types=["review"])
    assert enr["device_code"] and enr["user_code"]
    by_dc = core_db.get_enrollment_by_device_code(db, enr["device_code"])
    assert by_dc["state"] == "pending" and by_dc["node_name"] == "n1"


def test_user_code_lookup_is_normalized(db):
    enr = core_db.create_enrollment(db)
    typed = enr["user_code"].lower().replace("-", "")     # operator sloppiness
    assert core_db.get_enrollment_by_user_code(db, typed) is not None


def test_device_code_is_stored_hashed(db):
    enr = core_db.create_enrollment(db)
    row = db.execute("SELECT device_code_hash FROM node_enrollments").fetchone()
    assert row["device_code_hash"] != enr["device_code"]


def test_approve_creates_a_node_bound_to_the_tenant(db):
    node_id, cid = _approved_node(db)
    node = core_db.get_node(db, node_id)
    assert node["client_id"] == cid and node["state"] == "active"


def test_approve_rejects_unknown_or_decided_enrollments(db):
    cid = core_db.register_client(db, "Acme")
    with pytest.raises(KeyError):
        core_db.approve_enrollment(db, "ZZZZ-ZZZZ", cid)
    enr = core_db.create_enrollment(db)
    core_db.deny_enrollment(db, enr["user_code"])
    with pytest.raises(ValueError):
        core_db.approve_enrollment(db, enr["user_code"], cid)


def test_issue_and_resolve_access_token(db):
    node_id, cid = _approved_node(db)
    tok = core_db.issue_tokens(db, node_id)
    node = core_db.resolve_access_token(db, tok["access_token"])
    assert node and node["id"] == node_id and node["client_id"] == cid
    assert core_db.resolve_access_token(db, "bogus-token") is None


def test_expired_access_token_is_rejected(db):
    node_id, _ = _approved_node(db)
    tok = core_db.issue_tokens(db, node_id, access_ttl=-1)   # already expired
    assert core_db.resolve_access_token(db, tok["access_token"]) is None


def test_refresh_rotates_and_is_single_use(db):
    node_id, _ = _approved_node(db)
    tok = core_db.issue_tokens(db, node_id)
    fresh = core_db.rotate_refresh_token(db, tok["refresh_token"])
    assert fresh and core_db.resolve_access_token(db, fresh["access_token"])
    # the rotated-away access token is dead; the refresh token is spent
    assert core_db.resolve_access_token(db, tok["access_token"]) is None
    assert core_db.rotate_refresh_token(db, tok["refresh_token"]) is None


def test_revoke_node_kills_its_tokens(db):
    node_id, _ = _approved_node(db)
    tok = core_db.issue_tokens(db, node_id)
    core_db.revoke_node(db, node_id)
    assert core_db.resolve_access_token(db, tok["access_token"]) is None
    assert core_db.rotate_refresh_token(db, tok["refresh_token"]) is None


def test_complete_enrollment_makes_the_device_code_single_use(db):
    enr = core_db.create_enrollment(db)
    eid = core_db.get_enrollment_by_device_code(db, enr["device_code"])["id"]
    core_db.complete_enrollment(db, eid)
    assert core_db.get_enrollment_by_device_code(
        db, enr["device_code"])["state"] == "completed"


def test_list_pending_enrollments(db):
    core_db.create_enrollment(db, node_name="n1")
    denied = core_db.create_enrollment(db, node_name="n2")
    core_db.deny_enrollment(db, denied["user_code"])
    pending = core_db.list_pending_enrollments(db)
    assert [e["node_name"] for e in pending] == ["n1"]
