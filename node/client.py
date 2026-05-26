"""HoneCoreClient — the hone-node's client for the hone-core v1 REST API
(see ../API.md).

The client owns the node's OAuth identity. On first start it enrolls via the
device authorization grant (RFC 8628): it requests a device code, logs a user
code for an operator to approve, and polls until approved — then persists the
bearer tokens and hone-core's CA certificate to the data volume and reuses
them on every later start. The main API is called with the bearer token over
TLS validated against that CA; the OAuth/enrollment endpoints are gated by the
fleet secret. A 401 triggers a token refresh; a permanent auth failure raises
EnrollmentError so the node stops (and re-enrolls on its next start).

Thin request wrappers otherwise: the retry / backoff policy
(../ARCHITECTURE.md → Node resilience) is applied by the runner around these
calls, not here.
"""
import json
import logging
import os
import time

import httpx

from node.config import Config

log = logging.getLogger("hone.node.client")

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_TASK_TYPES = ["prepare", "review", "train", "draft"]   # this node's self-described capability


class EnrollmentError(Exception):
    """A permanent authentication failure — the node cannot proceed and must
       re-enroll (operator denied it, the enrollment expired unrecoverably, or
       a revoked node's token refresh was rejected)."""


def _err_code(response: httpx.Response) -> str | None:
    """The `error.code` from an OAuth error body, or None."""
    try:
        return response.json().get("error", {}).get("code")
    except ValueError:
        return None


class HoneCoreClient:
    """One node's session with hone-core — enrollment, tokens, and the v1 API."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._access: str | None = None
        self._refresh: str | None = None
        self._http: httpx.Client | None = None     # main-API client, once enrolled
        self._load_identity()
        if self._access and os.path.exists(cfg.ca_cert_path):
            self._build_main_client()

    def close(self) -> None:
        if self._http is not None:
            self._http.close()

    # --- identity persistence (survives a restart) --------------------------

    def _load_identity(self) -> None:
        try:
            with open(self._cfg.identity_path, encoding="utf-8") as f:
                d = json.load(f)
            self._access = d.get("access_token")
            self._refresh = d.get("refresh_token")
        except (OSError, ValueError):
            pass                                    # not enrolled yet

    def _save_identity(self) -> None:
        os.makedirs(self._cfg.data_dir, exist_ok=True)
        tmp = self._cfg.identity_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"access_token": self._access,
                       "refresh_token": self._refresh}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._cfg.identity_path)    # atomic

    def _clear_identity(self) -> None:
        self._access = self._refresh = None
        try:
            os.remove(self._cfg.identity_path)
        except OSError:
            pass

    # --- the OAuth / enrollment channel (fleet-secret gated) ----------------

    def _oauth_request(self, path: str, body: dict) -> httpx.Response:
        """POST to an /v1/oauth/* endpoint. TLS is validated against
           hone-core's CA once the node holds it; the very first contact, made
           before the CA is known, is trusted on first use (the fleet secret
           authenticates the exchange)."""
        verify = (self._cfg.ca_cert_path
                  if os.path.exists(self._cfg.ca_cert_path) else False)
        with httpx.Client(base_url=self._cfg.core_url, timeout=30.0,
                          verify=verify) as c:
            return c.post(path, json=body,
                          headers={"X-HONE-Fleet-Secret":
                                   self._cfg.fleet_secret})

    def _begin_device_flow(self) -> dict:
        r = self._oauth_request(
            "/v1/oauth/device_authorization",
            {"node_name": self._cfg.node_name, "task_types": _TASK_TYPES})
        # 409 means hone-core rejected the enrollment because the
        # requested node_name is already held by an active node. The
        # response body's `detail` carries the human-readable reason
        # — lift it into an EnrollmentError so main()'s existing
        # one-line clean-exit path surfaces it cleanly rather than
        # crashing with an httpx traceback whose body isn't printed.
        if r.status_code == 409:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except ValueError:
                pass
            raise EnrollmentError(
                detail or "hone-core rejected this node's enrollment "
                "(HTTP 409). Check HONE_NODE_NAME is unique across "
                "the fleet.") from None
        r.raise_for_status()
        da = r.json()
        log.warning("ENROLL THIS NODE — open %s and enter code:  %s",
                    da["verification_uri"], da["user_code"])
        return da

    def _poll_for_token(self, da: dict) -> str:
        """Poll the token endpoint for a device code. Returns 'approved' (the
           tokens are adopted) or 'expired'; raises EnrollmentError on a
           denial."""
        interval = da.get("interval", 5)
        deadline = time.monotonic() + da.get("expires_in", 900)
        while time.monotonic() < deadline:
            time.sleep(interval)
            r = self._oauth_request(
                "/v1/oauth/token",
                {"grant_type": _DEVICE_GRANT, "device_code": da["device_code"]})
            if r.status_code == 200:
                self._adopt_tokens(r.json())
                log.info("enrollment approved — node is now a fleet member")
                return "approved"
            code = _err_code(r)
            if code == "authorization_pending":
                continue
            if code == "slow_down":
                interval += 5
                continue
            if code == "expired_token":
                return "expired"
            if code == "access_denied":
                raise EnrollmentError("operator denied this node's enrollment")
            raise EnrollmentError(
                f"enrollment failed: {code or r.status_code}")
        return "expired"

    def ensure_enrolled(self) -> None:
        """Make sure the node holds a usable bearer token, enrolling via the
           device-authorization grant if it does not. Blocks until an operator
           approves the node; a fresh device code is requested if one expires
           while waiting."""
        if self._http is not None:
            return
        log.info("not enrolled — beginning device-authorization enrollment "
                 "with %s", self._cfg.core_url)
        while True:
            if self._poll_for_token(self._begin_device_flow()) == "approved":
                return
            log.warning("device code expired before approval — re-issuing")

    def _refresh_token(self) -> None:
        """Exchange the refresh token for a fresh pair. A rejected refresh is
           permanent (the node was likely revoked): the identity is cleared so
           the node re-enrolls on its next start, and EnrollmentError is
           raised."""
        if not self._refresh:
            self._clear_identity()
            raise EnrollmentError("no refresh token — re-enrollment required")
        r = self._oauth_request(
            "/v1/oauth/token",
            {"grant_type": "refresh_token", "refresh_token": self._refresh})
        if r.status_code == 200:
            self._adopt_tokens(r.json())
            log.info("access token refreshed")
            return
        self._clear_identity()
        raise EnrollmentError(
            f"token refresh rejected ({_err_code(r)}) — the node was likely "
            "revoked; identity cleared, it will re-enroll on restart")

    def _adopt_tokens(self, payload: dict) -> None:
        """Persist a fresh token pair (and hone-core's CA, if present) and
           rebuild the main-API client."""
        self._access = payload["access_token"]
        self._refresh = payload["refresh_token"]
        ca = payload.get("ca_cert")
        if ca:
            os.makedirs(self._cfg.data_dir, exist_ok=True)
            with open(self._cfg.ca_cert_path, "w", encoding="utf-8") as f:
                f.write(ca)
        self._save_identity()
        self._build_main_client()

    # --- the main API (bearer token, CA-validated TLS) ----------------------

    def _build_main_client(self) -> None:
        if self._http is not None:
            self._http.close()
        self._http = httpx.Client(
            base_url=self._cfg.core_url, timeout=30.0,
            verify=self._cfg.ca_cert_path,
            headers={"Authorization": f"Bearer {self._access}"})

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        """A main-API request. Enrolls first if needed; on a 401 refreshes the
           token and retries once; a 403 is a permanent stop."""
        self.ensure_enrolled()
        r = self._http.request(method, path, **kw)
        if r.status_code == 401:
            self._refresh_token()
            r = self._http.request(method, path, **kw)
        if r.status_code == 403:
            raise EnrollmentError(
                "hone-core returned 403 — this node's enrollment or tenant "
                "is no longer active")
        return r

    def claim(self) -> dict | None:
        """POST /v1/claims — the next task, or None on 204 (empty queue)."""
        r = self._request("POST", "/v1/claims")
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def heartbeat(self, claim_id: str) -> None:
        """POST /v1/claims/{id}/heartbeat — extend the claim's lease."""
        self._request("POST",
                      f"/v1/claims/{claim_id}/heartbeat").raise_for_status()

    def submit_result(self, claim_id: str, record: dict) -> None:
        """POST /v1/claims/{id}/result — idempotent on claim_id."""
        self._request("POST", f"/v1/claims/{claim_id}/result",
                      json=record).raise_for_status()

    def release_claim(self, claim_id: str, reason: str = "") -> None:
        """POST /v1/claims/{id}/release — return the claim to the
           CLAIMABLE pool immediately. Called by the runner when a
           non-transient error aborted the task, so a correctly-
           configured peer can pick it up without waiting for the
           lease to lapse (default 30 min). `reason` is logged server-
           side; idempotent on a re-call."""
        self._request("POST", f"/v1/claims/{claim_id}/release",
                      json={"reason": reason}).raise_for_status()

    def report_health(self, snapshot: dict) -> None:
        """POST /v1/nodes/me/health — periodic node-initiated health
           report. The snapshot is a free-form dict (today:
           free_disk_mb, refrepo_size_mb, last_anthropic_error);
           hone-core stores the latest one per node and surfaces it
           on the /nodes page. Failures here are non-fatal — the
           runner wraps the call in a best-effort try/except so a
           transient blip doesn't disrupt the claim loop."""
        self._request("POST", "/v1/nodes/me/health",
                      json=snapshot).raise_for_status()

    # The claim payload now carries everything a task needs — the patchset,
    # the patch messages, any training comments, and the compiled methodology
    # — so the previous side-fetch endpoints (get_blob / get_source_review /
    # get_methodology) are gone. See ../API.md for the new claim shape.
