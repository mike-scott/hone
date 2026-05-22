"""HoneCoreClient — the hone-node's client for the hone-core v1 REST API
(see ../API.md).

Thin request wrappers: the URLs, verbs and auth headers are the v1 contract;
the retry / backoff policy (../ARCHITECTURE.md → Node resilience) is applied
by the runner around these calls, not here.
"""
import httpx

from node.config import Config


class HoneCoreClient:
    """One HTTP session to hone-core, carrying the node's auth headers."""

    def __init__(self, cfg: Config):
        self._http = httpx.Client(
            base_url=cfg.core_url,
            timeout=30.0,
            headers={
                "X-HONE-Fleet-Secret": cfg.fleet_secret,
                "X-HONE-Client-Key": cfg.client_key,
            },
        )

    def close(self) -> None:
        self._http.close()

    def claim(self) -> dict | None:
        """POST /v1/claims — the next task, or None on 204 (empty queue)."""
        r = self._http.post("/v1/claims")
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def heartbeat(self, claim_id: str) -> None:
        """POST /v1/claims/{id}/heartbeat — extend the claim's lease."""
        self._http.post(f"/v1/claims/{claim_id}/heartbeat").raise_for_status()

    def submit_result(self, claim_id: str, record: dict) -> None:
        """POST /v1/claims/{id}/result — idempotent on claim_id."""
        self._http.post(f"/v1/claims/{claim_id}/result",
                        json=record).raise_for_status()

    def get_blob(self, root_message_id: str) -> bytes:
        """GET /v1/patchsets/{root}/blob — the .tar.zst patch archive."""
        r = self._http.get(f"/v1/patchsets/{root_message_id}/blob")
        r.raise_for_status()
        return r.content

    def get_source_review(self, root_message_id: str) -> dict:
        """GET /v1/patchsets/{root}/source-review — the gathered source's
        review, for comparison."""
        r = self._http.get(f"/v1/patchsets/{root_message_id}/source-review")
        r.raise_for_status()
        return r.json()

    def get_methodology(self) -> dict:
        """GET /v1/methodology — the methodology to review against."""
        r = self._http.get("/v1/methodology")
        r.raise_for_status()
        return r.json()
