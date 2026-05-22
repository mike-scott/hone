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
