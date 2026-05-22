"""Tests for the /v1/oauth/* enrollment endpoints and bearer auth on the
main API (core/api.py), driven through FastAPI's TestClient over a real
temporary database."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import api, core_db

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
FLEET = {"X-HONE-Fleet-Secret": "fleet-xyz"}
ADMIN = {"X-HONE-Admin-Token": "admin-xyz"}


@pytest.fixture
def ctx(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.include_router(api.router)
    app.state.db = db
    app.state.ca_cert_pem = "-----BEGIN CERTIFICATE-----\nTEST\n" \
                            "-----END CERTIFICATE-----\n"
    app.state.config = type("Cfg", (), {
        "fleet_secret": "fleet-xyz", "admin_token": "admin-xyz",
        "device_code_ttl": 900, "device_poll_interval": 5,
        "access_token_ttl": 3600, "refresh_token_ttl": 0,
        "public_url": "https://core.example:8000"})()
    return SimpleNamespace(client=TestClient(app), db=db)


def _device_code(ctx):
    return ctx.client.post("/v1/oauth/device_authorization", json={},
                           headers=FLEET).json()


def _approved_device_code(ctx):
    da = _device_code(ctx)
    cid = core_db.register_client(ctx.db, "Acme")
    core_db.approve_enrollment(ctx.db, da["user_code"], cid)
    return da


def _token(ctx, **body):
    return ctx.client.post("/v1/oauth/token", json=body, headers=FLEET)


# --- device authorization --------------------------------------------------

def test_device_authorization_requires_the_fleet_secret(ctx):
    assert ctx.client.post("/v1/oauth/device_authorization",
                           json={}).status_code == 401


def test_device_authorization_issues_codes(ctx):
    r = ctx.client.post("/v1/oauth/device_authorization",
                        json={"node_name": "n1"}, headers=FLEET)
    assert r.status_code == 200
    d = r.json()
    assert d["user_code"] in d["verification_uri_complete"]
    assert d["verification_uri"] == "https://core.example:8000/enroll"


# --- the device-code grant -------------------------------------------------

def test_token_pending_then_slow_down(ctx):
    da = _device_code(ctx)
    r1 = _token(ctx, grant_type=DEVICE_GRANT, device_code=da["device_code"])
    assert r1.status_code == 400
    assert r1.json()["error"]["code"] == "authorization_pending"
    r2 = _token(ctx, grant_type=DEVICE_GRANT, device_code=da["device_code"])
    assert r2.json()["error"]["code"] == "slow_down"


def test_token_unknown_device_code(ctx):
    r = _token(ctx, grant_type=DEVICE_GRANT, device_code="nope")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_grant"


def test_token_denied_enrollment(ctx):
    da = _device_code(ctx)
    core_db.deny_enrollment(ctx.db, da["user_code"])
    r = _token(ctx, grant_type=DEVICE_GRANT, device_code=da["device_code"])
    assert r.json()["error"]["code"] == "access_denied"


def test_token_approved_then_single_use(ctx):
    da = _approved_device_code(ctx)
    r = _token(ctx, grant_type=DEVICE_GRANT, device_code=da["device_code"])
    assert r.status_code == 200
    tok = r.json()
    assert tok["token_type"] == "Bearer"
    assert tok["ca_cert"].startswith("-----BEGIN CERTIFICATE-----")
    # the device code is single-use — a replay is rejected
    r2 = _token(ctx, grant_type=DEVICE_GRANT, device_code=da["device_code"])
    assert r2.json()["error"]["code"] == "invalid_grant"


# --- the refresh grant -----------------------------------------------------

def test_refresh_grant_rotates_the_pair(ctx):
    da = _approved_device_code(ctx)
    tok = _token(ctx, grant_type=DEVICE_GRANT,
                 device_code=da["device_code"]).json()
    r = _token(ctx, grant_type="refresh_token",
               refresh_token=tok["refresh_token"])
    assert r.status_code == 200
    assert r.json()["access_token"] != tok["access_token"]


def test_refresh_unknown_token(ctx):
    r = _token(ctx, grant_type="refresh_token", refresh_token="nope")
    assert r.json()["error"]["code"] == "invalid_grant"


def test_unsupported_grant_type(ctx):
    r = _token(ctx, grant_type="password")
    assert r.json()["error"]["code"] == "unsupported_grant_type"


# --- bearer auth on the main API -------------------------------------------

def test_main_api_requires_a_bearer_token(ctx):
    assert ctx.client.post("/v1/claims").status_code == 401


def test_bearer_token_authenticates_the_main_api(ctx):
    da = _approved_device_code(ctx)
    tok = _token(ctx, grant_type=DEVICE_GRANT,
                 device_code=da["device_code"]).json()
    r = ctx.client.post("/v1/claims", headers={
        "Authorization": f"Bearer {tok['access_token']}"})
    assert r.status_code in (200, 204)        # 204: empty queue


# --- admin: tenant registration --------------------------------------------

def test_create_client_registers_a_tenant_without_a_key(ctx):
    r = ctx.client.post("/v1/clients", json={"name": "Beta"}, headers=ADMIN)
    assert r.status_code == 201
    assert "client_key" not in r.json() and r.json()["name"] == "Beta"


def test_create_client_requires_the_admin_token(ctx):
    assert ctx.client.post("/v1/clients", json={"name": "x"}).status_code == 401
