"""The operator web UI is gated by HTTP Basic auth (core.api.require_ui_auth,
wired onto ui.router in core.main.create_app). The password is the admin token
(HONE_ADMIN_TOKEN); /v1/* and /healthz are unaffected."""
import base64
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from core import api, core_db, ui

TOKEN = "s3cret-admin-token"


@pytest.fixture
def client(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    core_db.add_methodology_version(db, {"name": "t", "version": 1})
    app = FastAPI()
    app.state.db = db
    app.state.config = SimpleNamespace(admin_token=TOKEN, fleet_secret="f")
    # Gate the UI exactly as create_app() does.
    app.include_router(ui.router,
                       dependencies=[Depends(api.require_ui_auth)])
    return TestClient(app)


def _basic(user, password):
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def test_ui_requires_auth_and_sends_a_basic_challenge(client):
    r = client.get("/")
    assert r.status_code == 401
    # the Basic challenge makes a browser show its login dialog
    assert r.headers.get("WWW-Authenticate", "").startswith("Basic")


def test_ui_accepts_the_admin_token_as_the_basic_password(client):
    r = client.get("/", headers=_basic("admin", TOKEN))
    assert r.status_code == 200


def test_ui_accepts_any_username(client):
    # single-secret model: the username is ignored, the password is the token
    r = client.get("/", headers=_basic("whoever", TOKEN))
    assert r.status_code == 200


def test_ui_rejects_a_wrong_password(client):
    r = client.get("/", headers=_basic("admin", "wrong"))
    assert r.status_code == 401


def test_ui_rejects_malformed_basic_header(client):
    r = client.get("/", headers={"Authorization": "Basic not@@base64"})
    assert r.status_code == 401


def test_ui_gate_protects_admin_post_endpoints(client):
    # the destructive admin POSTs (gather trigger, enrollment approve, …) are
    # under ui.router, so they're gated too — no token, no action.
    assert client.post("/settings/gather/trigger").status_code == 401


def test_ui_gate_does_not_touch_the_v1_api(tmp_path):
    """require_ui_auth is bound to ui.router only — the /v1 API keeps its own
       fleet/bearer gates and is never subject to the Basic challenge."""
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.state.db = db
    app.state.config = SimpleNamespace(admin_token=TOKEN, fleet_secret="f")
    app.include_router(api.router)                       # no UI dependency
    app.include_router(ui.router,
                       dependencies=[Depends(api.require_ui_auth)])
    c = TestClient(app)
    # a /v1 claim with no creds fails on the NODE bearer gate, not a Basic
    # challenge — i.e. the UI gate isn't in its path.
    r = c.post("/v1/claims")
    assert r.status_code == 401
    assert "WWW-Authenticate" not in r.headers


def test_unset_admin_token_fails_closed(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.state.db = db
    app.state.config = SimpleNamespace(admin_token="", fleet_secret="f")
    app.include_router(ui.router,
                       dependencies=[Depends(api.require_ui_auth)])
    c = TestClient(app)
    # even with a (blank) matching password, an unset token denies all
    assert c.get("/", headers=_basic("admin", "")).status_code == 401