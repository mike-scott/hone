"""hone-core — the v1 REST API for nodes (see ../API.md).

Node endpoints authenticate with X-HONE-Fleet-Secret (the fleet-wide gate)
and X-HONE-Client-Key (the tenant identity); the admin endpoint uses
X-HONE-Admin-Token. Handlers are thin: validate, call core_db, shape the
response — all state lives in the database.
"""
import secrets

from fastapi import (APIRouter, Depends, Header, HTTPException, Request,
                     Response, status)
from pydantic import BaseModel

from core import core_db

router = APIRouter(prefix="/v1", tags=["v1"])


# --- request bodies --------------------------------------------------------

class ClaimRequest(BaseModel):
    worker_id: str | None = None          # the node's id, recorded as claimed_by


class ResultRequest(BaseModel):
    task_type: str                        # 'review' | 'maintenance'
    # review results:
    state: str | None = None              # reviewed | unappliable | deferred
    record: dict | None = None            # the review completion record
    methodology_version: int | None = None
    # maintenance results:
    result: dict | None = None


class ClientRequest(BaseModel):
    name: str | None = None


# --- authentication --------------------------------------------------------

def _secret_ok(provided, expected):
    """Constant-time secret comparison; False if either side is empty."""
    return bool(expected) and provided is not None and \
        secrets.compare_digest(provided, expected)


def require_node(request: Request,
                 fleet_secret: str | None = Header(
                     None, alias="X-HONE-Fleet-Secret"),
                 client_key: str | None = Header(
                     None, alias="X-HONE-Client-Key")):
    """Authenticate a node. The fleet secret gates the whole fleet; the client
       key identifies the tenant. Returns the client row (a dict). A bad
       fleet secret or client key is a hard 401/403 — not retryable."""
    cfg = request.app.state.config
    if not _secret_ok(fleet_secret, cfg.fleet_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad fleet secret")
    if not client_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing client key")
    client = core_db.get_client(request.app.state.db, client_key)
    if client is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "unknown client key")
    if client["state"] != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "client disabled")
    return client


def require_admin(request: Request,
                  admin_token: str | None = Header(
                      None, alias="X-HONE-Admin-Token")):
    """Authenticate an admin request."""
    if not _secret_ok(admin_token, request.app.state.config.admin_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad admin token")


# --- claims ----------------------------------------------------------------

@router.post("/claims")
def claim_task(request: Request, body: ClaimRequest | None = None,
               client: dict = Depends(require_node)):
    """Claim the next task for the client — a review task, else a global
       maintenance task, else 204 when both queues are empty."""
    db = request.app.state.db
    worker_id = (body.worker_id if body and body.worker_id else "unidentified")

    review = core_db.claim_review(db, client["id"], worker_id)
    if review is not None:
        patchset = core_db.get_patchset(db, review["root_message_id"]) or {}
        return {"task_type": "review",
                "claim_id": review["claim_id"],
                "root_message_id": review["root_message_id"],
                "subject": patchset.get("subject"),
                "base_commit": patchset.get("base_commit")}

    maint = core_db.claim_maintenance_task(db, worker_id)
    if maint is not None:
        return {"task_type": "maintenance",
                "claim_id": maint["claim_id"],
                "task_id": maint["id"],
                "kind": maint["kind"],
                "payload": maint["payload"]}

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/claims/{claim_id}/heartbeat",
             dependencies=[Depends(require_node)])
def heartbeat(claim_id: str, request: Request):
    """Extend the claim's lease. `valid` is false once the claim has lapsed
       (been reclaimed) or completed — the node should then stop and reclaim."""
    return {"valid": core_db.heartbeat(request.app.state.db, claim_id)}


@router.post("/claims/{claim_id}/result", dependencies=[Depends(require_node)])
def submit_result(claim_id: str, body: ResultRequest, request: Request):
    """Submit a completion record. Idempotent on the claim id. `status` is
       'ok', or 'lapsed' when the claim was reclaimed — on 'lapsed' the node
       discards the result, the reclaim already covered the work."""
    db = request.app.state.db
    if body.task_type == "review":
        if body.state is None or body.record is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "a review result needs `state` and `record`")
        try:
            outcome = core_db.complete_review(db, claim_id, body.state,
                                              body.record,
                                              body.methodology_version)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    elif body.task_type == "maintenance":
        if body.result is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "a maintenance result needs `result`")
        outcome = core_db.complete_maintenance_task(db, claim_id, body.result)
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"unknown task_type {body.task_type!r}")
    return {"status": outcome}


# --- patchsets -------------------------------------------------------------

@router.get("/patchsets/{root_message_id}/blob",
            dependencies=[Depends(require_node)])
def patchset_blob(root_message_id: str, request: Request):
    """The patchset's .tar.zst patch archive."""
    blob = core_db.get_patch_blob(request.app.state.db, root_message_id)
    if blob is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "no patch archive for that patchset")
    return Response(content=blob, media_type="application/zstd")


@router.get("/patchsets/{root_message_id}/source-review",
            dependencies=[Depends(require_node)])
def source_review(root_message_id: str, request: Request):
    """The external review signal on the patchset — for the node's comparison
       AFTER its own blind review."""
    db = request.app.state.db
    if core_db.get_patchset(db, root_message_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown patchset")
    return {"root_message_id": root_message_id,
            "findings": core_db.source_findings(db, root_message_id)}


# --- methodology -----------------------------------------------------------

@router.get("/methodology", dependencies=[Depends(require_node)])
def methodology(request: Request):
    """The active methodology a node reviews against — the versioned document
       plus the candidate practices currently on trial."""
    active = core_db.active_methodology(request.app.state.db)
    if active is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "methodology not bootstrapped")
    version, document = active
    return {"version": version,
            "methodology": document,
            "candidates": core_db.list_candidates(request.app.state.db,
                                                  state="trial")}


# --- admin -----------------------------------------------------------------

@router.post("/clients", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_admin)])
def create_client(body: ClientRequest, request: Request):
    """Admin — register a client and return its generated key. The operator
       hands that key to the client's node(s)."""
    key = "ck_" + secrets.token_urlsafe(24)
    cid = core_db.register_client(request.app.state.db, key, body.name)
    return {"id": cid, "client_key": key, "name": body.name}
