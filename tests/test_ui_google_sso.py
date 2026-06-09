"""Tests for the Google SSO flow — specifically that the OAuth redirect_uri
sent to Google in BOTH the authorization step and the token-exchange step
comes from cfg.public_url, not the incoming request's Host header. Without
that, an attacker who can spoof Host on a misconfigured proxy could send
the authorization `code` to a domain they control."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from core import auth, core_db, ui

_PUBLIC_URL = "https://hone.example.com"
_CALLBACK   = f"{_PUBLIC_URL}/auth/google/callback"


@pytest.fixture
def sso_ctx(tmp_path):
    """A test app with SessionMiddleware (needed for the OAuth state cookie)
       and a config where Google SSO is enabled and cfg.public_url is the
       canonical operator hostname."""
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.add_middleware(SessionMiddleware,
                       secret_key="test-secret-32-bytes-minimum-padding",
                       session_cookie="hone_session")
    app.include_router(ui.router)
    app.state.db = db
    app.state.config = SimpleNamespace(
        admin_token="adm",
        google_client_id="google-client-id",
        google_client_secret="google-client-secret",
        public_url=_PUBLIC_URL)
    return SimpleNamespace(
        client=TestClient(app, follow_redirects=False),
        db=db)


# --- helper -------------------------------------------------------------------

@pytest.mark.parametrize("public_url, expected", [
    ("https://hone.example.com",  _CALLBACK),
    ("https://hone.example.com/", _CALLBACK),                   # trailing slash trimmed
    ("https://hone.example.com:8443",
     "https://hone.example.com:8443/auth/google/callback"),
])
def test_google_redirect_uri_helper_derives_from_public_url(public_url, expected):
    cfg = SimpleNamespace(public_url=public_url)
    assert ui._google_redirect_uri(cfg) == expected


# --- /auth/google: the authorization redirect carries the safe redirect_uri ---

def test_auth_google_start_uses_public_url_not_the_request_host(sso_ctx):
    """The /auth/google redirect to Google must encode the cfg.public_url-
       derived callback in the `redirect_uri` query param — even when the
       request arrives with a hostile Host header. Otherwise the auth code
       Google later sends would land on attacker.evil."""
    r = sso_ctx.client.get("/auth/google",
                           headers={"Host": "attacker.evil"})
    assert r.status_code in (302, 307)
    loc = r.headers["Location"]
    assert loc.startswith("https://accounts.google.com/")
    redirect_uri = parse_qs(urlparse(loc).query).get("redirect_uri", [""])[0]
    assert redirect_uri == _CALLBACK
    assert "attacker" not in redirect_uri


# --- /auth/google/callback: the token exchange uses the same safe URI --------

def test_auth_google_callback_token_exchange_uses_public_url(
        sso_ctx, monkeypatch):
    """Google REQUIRES the redirect_uri in the token exchange to match the one
       in the authorization step, so the callback must derive the URI from
       cfg.public_url too — not from request.base_url, which is built from
       the (untrusted) Host header on the incoming callback request."""
    # 1) Start the flow normally to populate the session state cookie.
    start = sso_ctx.client.get("/auth/google",
                                headers={"Host": "attacker.evil"})
    assert start.status_code in (302, 307)
    state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]

    # 2) Intercept the outbound HTTPS calls so the test doesn't depend on
    #    Google. Capture redirect_uri off the token-exchange call.
    captured = {}

    async def fake_exchange(cfg, code, redirect_uri):
        captured["redirect_uri"] = redirect_uri
        return {"access_token": "fake-access"}

    async def fake_userinfo(token):
        return {"sub": "google-sub-x", "email": "newcomer@example.com",
                "name": "Newcomer"}

    monkeypatch.setattr(auth, "google_exchange_code", fake_exchange)
    monkeypatch.setattr(auth, "google_fetch_userinfo", fake_userinfo)

    # 3) Hit the callback with the SAME attacker Host. The handler must still
    #    pass the public_url-derived redirect_uri to the token exchange.
    sso_ctx.client.get(
        f"/auth/google/callback?code=fake-code&state={state}",
        headers={"Host": "attacker.evil"})

    assert captured["redirect_uri"] == _CALLBACK
    assert "attacker" not in captured["redirect_uri"]
