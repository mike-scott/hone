"""hone-core — the v1 REST API for nodes (see ../API.md).

Skeleton: the routes and the contract are fixed; the handlers are stubs that
return 501. Auth (the X-HONE-Fleet-Secret + X-HONE-Client-Key check) and the
request/response models (from common/) are TODO.
"""
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/v1", tags=["v1"])


def _todo():
    raise HTTPException(status_code=501, detail="not implemented")


# TODO: a FastAPI dependency that verifies X-HONE-Fleet-Secret (fleet gate)
# and X-HONE-Client-Key (tenant identity), and an admin-token dependency.


@router.post("/claims")
async def claim_task():
    """Claim the next task for the client — a review or maintenance task,
    or 204 if the queue is empty."""
    _todo()


@router.post("/claims/{claim_id}/heartbeat")
async def heartbeat(claim_id: str):
    """Extend the claim's lease."""
    _todo()


@router.post("/claims/{claim_id}/result")
async def submit_result(claim_id: str):
    """Submit the completion record — a review record or a maintenance
    record. Idempotent on claim_id."""
    _todo()


@router.get("/patchsets/{root_message_id}/blob")
async def patchset_blob(root_message_id: str):
    """The patchset's .tar.zst patch archive."""
    _todo()


@router.get("/patchsets/{root_message_id}/source-review")
async def source_review(root_message_id: str):
    """The gathered source's review, for the node's comparison."""
    _todo()


@router.get("/methodology")
async def methodology():
    """The distilled methodology a node reviews against."""
    _todo()


@router.post("/clients")
async def register_client():
    """Admin — pre-authorize a client key."""
    _todo()
