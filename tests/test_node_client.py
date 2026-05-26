"""Unit tests for the hone-node HoneCoreClient (node/client.py) — identity
persistence and the auth-failure paths. The full device flow is exercised
server-side by test_oauth_endpoints and end-to-end in the Stage-4 smoke test.
"""
import os

import httpx
import pytest

from core import tls
from node.client import EnrollmentError, HoneCoreClient, _err_code
from node.config import Config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HONE_CORE_URL", "https://core.example")
    monkeypatch.setenv("HONE_FLEET_SECRET", "fleet")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("HONE_DATA", str(tmp_path))
    return Config.from_env()


def test_a_fresh_node_starts_unenrolled(cfg):
    c = HoneCoreClient(cfg)
    assert c._access is None and c._http is None


def test_identity_persists_across_a_restart(cfg):
    c = HoneCoreClient(cfg)
    c._access, c._refresh = "access-tok", "refresh-tok"
    c._save_identity()
    restarted = HoneCoreClient(cfg)            # a new client = a restart
    assert restarted._access == "access-tok"
    assert restarted._refresh == "refresh-tok"


def test_clear_identity_removes_the_stored_file(cfg):
    c = HoneCoreClient(cfg)
    c._access, c._refresh = "a", "r"
    c._save_identity()
    assert os.path.exists(cfg.identity_path)
    c._clear_identity()
    assert not os.path.exists(cfg.identity_path)
    assert c._access is None and c._refresh is None


def test_refresh_without_a_token_is_a_fatal_enrollment_error(cfg):
    c = HoneCoreClient(cfg)
    with pytest.raises(EnrollmentError):
        c._refresh_token()


def test_adopt_tokens_persists_the_ca_and_builds_the_client(cfg, tmp_path):
    src = str(tmp_path / "core-tls")
    tls.ensure_certs(src, ["core.example"])
    ca_pem = tls.ca_cert_pem(src)

    c = HoneCoreClient(cfg)
    c._adopt_tokens({"access_token": "a", "refresh_token": "r",
                     "ca_cert": ca_pem})
    try:
        assert open(cfg.ca_cert_path, encoding="utf-8").read() == ca_pem
        assert c._http is not None             # the main-API client is built
        assert c._access == "a"
    finally:
        c.close()


@pytest.mark.parametrize("body, expected", [
    ({"error": {"code": "slow_down"}}, "slow_down"),
    ({"error": {"code": "authorization_pending"}}, "authorization_pending"),
    ({"error": {}}, None),
    ({"not-an-error": 1}, None),
])
def test_err_code(body, expected):
    assert _err_code(httpx.Response(400, json=body)) == expected


# --- main-API HTTP methods (via httpx.MockTransport) -----------------------

def _client_with_transport(cfg, transport):
    """A HoneCoreClient with its main-API http client replaced by one
       wired to an in-memory MockTransport — lets us exercise the real
       method bodies without an actual hone-core."""
    c = HoneCoreClient(cfg)
    c._access = "access-tok"
    c._refresh = "refresh-tok"
    c._http = httpx.Client(base_url=cfg.core_url,
                            headers={"Authorization": "Bearer access-tok"},
                            transport=transport)
    return c


def test_claim_returns_none_on_204(cfg):
    """The claim wrapper's empty-queue contract: None, not {} or a 204
       response object."""
    def handler(request):
        return httpx.Response(204)
    c = _client_with_transport(cfg, httpx.MockTransport(handler))
    try:
        assert c.claim() is None
    finally:
        c.close()


def test_claim_returns_the_payload_on_200(cfg):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path,
                     request.headers.get("Authorization")))
        return httpx.Response(200, json={"task_type": "review",
                                          "claim_id": "c1"})
    c = _client_with_transport(cfg, httpx.MockTransport(handler))
    try:
        assert c.claim() == {"task_type": "review", "claim_id": "c1"}
        assert seen == [("POST", "/v1/claims", "Bearer access-tok")]
    finally:
        c.close()


def test_claim_refreshes_on_401_then_retries(cfg, monkeypatch):
    """A 401 triggers _refresh_token + a retry — the second call uses the
       fresh access token."""
    calls = []

    def handler(request):
        calls.append(request.headers.get("Authorization"))
        if len(calls) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"task_type": "review"})

    c = _client_with_transport(cfg, httpx.MockTransport(handler))

    def fake_refresh():
        c._access = "fresh-tok"
        # rebuild the http client with the new auth header but keep our
        # MockTransport so the retry routes back through `handler`.
        c._http = httpx.Client(
            base_url=cfg.core_url,
            headers={"Authorization": f"Bearer {c._access}"},
            transport=httpx.MockTransport(handler))
    monkeypatch.setattr(c, "_refresh_token", fake_refresh)
    try:
        assert c.claim() == {"task_type": "review"}
        assert calls == ["Bearer access-tok", "Bearer fresh-tok"]
    finally:
        c.close()


def test_403_raises_enrollment_error(cfg):
    """A 403 means the node's enrollment was revoked — permanent stop, not
       a transient retry."""
    def handler(request):
        return httpx.Response(403)
    c = _client_with_transport(cfg, httpx.MockTransport(handler))
    try:
        with pytest.raises(EnrollmentError):
            c.claim()
    finally:
        c.close()


def test_submit_result_posts_the_record(cfg):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.read()))
        return httpx.Response(200, json={"status": "ok"})

    c = _client_with_transport(cfg, httpx.MockTransport(handler))
    try:
        c.submit_result("c1", {"task_type": "review", "outcome": "reviewed"})
        method, path, body = seen[0]
        import json as _json
        assert method == "POST" and path == "/v1/claims/c1/result"
        assert _json.loads(body) == {"task_type": "review",
                                       "outcome": "reviewed"}
    finally:
        c.close()


def test_heartbeat_posts_to_the_heartbeat_path(cfg):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"valid": True})

    c = _client_with_transport(cfg, httpx.MockTransport(handler))
    try:
        c.heartbeat("c1")
        assert seen == [("POST", "/v1/claims/c1/heartbeat")]
    finally:
        c.close()
