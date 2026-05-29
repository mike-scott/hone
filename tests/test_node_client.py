"""Unit tests for the hone-node HoneCoreClient (node/client.py) — identity
persistence and the auth-failure paths. The full device flow is exercised
server-side by test_oauth_endpoints and end-to-end in the Stage-4 smoke test.
"""
import os

import httpx
import pytest

from core import tls
from node.client import (EnrollmentError, HoneCoreClient,
                          SchemaRejectedError, _err_code)
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


def test_409_from_device_authorization_raises_enrollment_error(cfg, monkeypatch):
    """A duplicate-name conflict at /v1/oauth/device_authorization
       surfaces to main()'s clean-exit path as an EnrollmentError
       carrying the conflict detail — not an httpx traceback whose
       body-buried reason isn't printed."""
    c = HoneCoreClient(cfg)
    monkeypatch.setattr(
        c, "_oauth_request",
        lambda path, body: httpx.Response(
            409, json={"detail":
                       "a node already exists with name 'builder-7'"}))
    with pytest.raises(EnrollmentError, match="builder-7") as ei:
        c._begin_device_flow()
    # Suppressed cause — the body's reason IS the operator-facing
    # message; we don't want an httpx traceback chained underneath.
    assert ei.value.__cause__ is None


def test_409_without_detail_field_falls_back_to_a_generic_message(
        cfg, monkeypatch):
    """If hone-core (or some intermediate proxy) returns a 409 with no
       JSON body or no `detail` field, we still produce a useful
       operator message — pointing them at HONE_NODE_NAME."""
    c = HoneCoreClient(cfg)
    monkeypatch.setattr(
        c, "_oauth_request",
        lambda path, body: httpx.Response(409, text="<html>nope</html>"))
    with pytest.raises(EnrollmentError, match="HONE_NODE_NAME"):
        c._begin_device_flow()


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


# --- TLS modes (adopt = pin self-signed CA, system = OS trust store) -------

@pytest.fixture
def cfg_system(tmp_path, monkeypatch):
    monkeypatch.setenv("HONE_CORE_URL", "https://core.example")
    monkeypatch.setenv("HONE_FLEET_SECRET", "fleet")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("HONE_DATA", str(tmp_path))
    monkeypatch.setenv("HONE_CORE_TLS", "system")
    return Config.from_env()


def test_tls_mode_defaults_to_adopt(cfg):
    assert cfg.tls_mode == "adopt"


def test_bad_tls_mode_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HONE_CORE_URL", "https://core.example")
    monkeypatch.setenv("HONE_FLEET_SECRET", "fleet")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("HONE_CORE_TLS", "bogus")
    with pytest.raises(RuntimeError, match="HONE_CORE_TLS"):
        Config.from_env()


def test_verify_adopt_mode_tofu_then_pinned(cfg):
    """adopt: first contact trusts on first use (no CA yet → verify=False),
       then pins the CA path once it's on disk; the main API always pins."""
    c = HoneCoreClient(cfg)
    assert c._verify(first_contact=True) is False         # no CA yet → TOFU
    assert c._verify() == cfg.ca_cert_path                # main API pins
    with open(cfg.ca_cert_path, "w", encoding="utf-8") as f:
        f.write("ca")                                     # CA now adopted
    assert c._verify(first_contact=True) == cfg.ca_cert_path


def test_verify_system_mode_uses_os_trust_store(cfg_system):
    """system: always the OS trust store (True), even on first contact —
       hone-core is behind a proxy with a publicly trusted cert."""
    c = HoneCoreClient(cfg_system)
    assert c._verify(first_contact=True) is True
    assert c._verify() is True


def test_system_mode_does_not_pin_the_handed_ca(cfg_system):
    """system mode ignores the CA hone-core hands at enrollment — nothing is
       written to disk — but the main client is still built (verify=True)."""
    c = HoneCoreClient(cfg_system)
    c._adopt_tokens({"access_token": "a", "refresh_token": "r",
                     "ca_cert": "-----BEGIN CERTIFICATE-----\nx\n"})
    try:
        assert not os.path.exists(cfg_system.ca_cert_path)
        assert c._http is not None
    finally:
        c.close()


def test_system_mode_rebuilds_client_on_restart_without_a_ca_file(cfg_system):
    """An enrolled node in system mode has no CA file; a restart must still
       rebuild the main client (gated on tls_mode, not the CA's presence)."""
    c = HoneCoreClient(cfg_system)
    c._access, c._refresh = "a", "r"
    c._save_identity()
    restarted = HoneCoreClient(cfg_system)
    try:
        assert restarted._http is not None
    finally:
        restarted.close()


@pytest.mark.parametrize("body, expected", [
    ({"error": {"code": "slow_down"}}, "slow_down"),
    ({"error": {"code": "authorization_pending"}}, "authorization_pending"),
    ({"error": {}}, None),
    ({"not-an-error": 1}, None),
])
def test_err_code(body, expected):
    assert _err_code(httpx.Response(400, json=body)) == expected


# --- main-API HTTP methods (via httpx.MockTransport) -----------------------

def _client_with_transport(cfg, transport, task_types=None):
    """A HoneCoreClient with its main-API http client replaced by one
       wired to an in-memory MockTransport — lets us exercise the real
       method bodies without an actual hone-core."""
    c = HoneCoreClient(cfg, task_types=task_types)
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


def test_claim_declares_live_capabilities_in_the_body(cfg):
    """The claim POST carries the node's injected capabilities so hone-core
       filters the queue to types this node can execute — overriding the
       (one-time) enrolled set if it has gone stale."""
    import json as _json
    bodies = []

    def handler(request):
        bodies.append(_json.loads(request.content) if request.content else None)
        return httpx.Response(204)
    c = _client_with_transport(cfg, httpx.MockTransport(handler),
                               task_types=["prepare"])
    try:
        assert c.claim() is None
        assert bodies == [{"task_types": ["prepare"]}]
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


def test_submit_result_422_raises_schema_rejected_with_detail(cfg):
    """A 422 from /result carries the validator's detail in the JSON
       body. The client lifts that into a SchemaRejectedError so the
       runner can build a fallback failure-outcome record — instead
       of crashing on the bare httpx.HTTPStatusError."""

    def handler(request):
        return httpx.Response(
            422,
            json={"detail": "completion record failed schema validation"
                            " at maintainer/mailing_lists/0: "
                            "Additional properties are not allowed"
                            " ('was_cc\\'d' was unexpected)"})

    c = _client_with_transport(cfg, httpx.MockTransport(handler))
    record = {"task_type": "prepare", "outcome": "prepared"}
    try:
        with pytest.raises(SchemaRejectedError) as ei:
            c.submit_result("c1", record)
        assert "maintainer/mailing_lists/0" in ei.value.detail
        assert ei.value.rejected_record is record    # original carried through
    finally:
        c.close()


def test_submit_result_422_without_detail_still_raises_schema_rejected(cfg):
    """Even without a usable `detail` field (e.g. a 422 from an
       intermediate proxy), the client still raises
       SchemaRejectedError — the runner shouldn't have to special-
       case proxy 422s vs schema-validator 422s."""

    def handler(request):
        return httpx.Response(422, text="<html>nope</html>")

    c = _client_with_transport(cfg, httpx.MockTransport(handler))
    try:
        with pytest.raises(SchemaRejectedError):
            c.submit_result("c1", {"task_type": "prepare"})
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


def test_report_health_posts_the_snapshot(cfg):
    """report_health POSTs the snapshot dict verbatim to
       /v1/nodes/me/health — the operator UI surfaces whatever the
       node puts in the body."""
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.read()))
        return httpx.Response(200, json={"status": "ok"})

    c = _client_with_transport(cfg, httpx.MockTransport(handler))
    snapshot = {"free_disk_mb": 1024, "refrepo_size_mb": 4500,
                 "last_anthropic_error": None}
    try:
        c.report_health(snapshot)
        method, path, body = seen[0]
        import json as _json
        assert method == "POST" and path == "/v1/nodes/me/health"
        assert _json.loads(body) == snapshot
    finally:
        c.close()


def test_release_claim_posts_the_reason(cfg):
    """The runner calls release_claim with a short prose reason when
       it aborts a task — this surfaces server-side in the release_claim
       log line so an operator inspecting hone-core's log can see why a
       node bailed without grepping both halves of the fleet."""
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.read()))
        return httpx.Response(200, json={"status": "ok"})

    c = _client_with_transport(cfg, httpx.MockTransport(handler))
    try:
        c.release_claim("c1", reason="Claude API key rejected")
        method, path, body = seen[0]
        import json as _json
        assert method == "POST" and path == "/v1/claims/c1/release"
        assert _json.loads(body) == {"reason": "Claude API key rejected"}
    finally:
        c.close()
