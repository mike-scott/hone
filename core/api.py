"""hone-core — the v1 REST API for nodes (see ../API.md).

Node endpoints authenticate with X-HONE-Fleet-Secret (the fleet-wide gate)
and X-HONE-Client-Key (the tenant identity); the admin endpoint uses
X-HONE-Admin-Token. Handlers are thin: validate, call core_db, shape the
response — all state lives in the database.
"""
import os
import secrets
import time

import jsonschema
import yaml
from fastapi import (APIRouter, Depends, Header, HTTPException, Request,
                     Response, status)
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core import core_db

router = APIRouter(prefix="/v1", tags=["v1"])


# --- completion-record schema ----------------------------------------------
# Every node result (POST /v1/claims/{claim_id}/result) is validated against
# core/completion-record.schema.yaml before it reaches the database. That
# schema is a oneOf of two shapes; we validate each task type against its own
# branch, so a review claim cannot be closed with a maintenance-shaped record.

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "completion-record.schema.yaml")
with open(_SCHEMA_PATH, encoding="utf-8") as _f:
    _RECORD_SCHEMA = yaml.safe_load(_f)


def _branch_validator(branch):
    """A draft-2020-12 validator for one $defs branch of the completion-record
       schema (review_record / maintenance_record), with $defs in scope so the
       branch's internal $refs resolve."""
    return jsonschema.Draft202012Validator(
        {"$schema": _RECORD_SCHEMA["$schema"],
         "$defs": _RECORD_SCHEMA["$defs"],
         "$ref": f"#/$defs/{branch}"})


_REVIEW_VALIDATOR = _branch_validator("review_record")
_MAINTENANCE_VALIDATOR = _branch_validator("maintenance_record")


def _validate_record(validator, record, what):
    """Validate a completion record against its schema branch. Raises 422 with
       the first error's location and message; a no-op when the record is
       valid. The record's referential integrity is still checked downstream."""
    errors = sorted(validator.iter_errors(record),
                    key=lambda e: str(list(e.absolute_path)))
    if errors:
        e = errors[0]
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{what} failed schema validation at {loc}: {e.message}")


# --- request bodies --------------------------------------------------------

class DeviceAuthRequest(BaseModel):
    node_name: str | None = None          # the node's self-described label
    task_types: list[str] | None = None   # capabilities, shown at approval


class TokenRequest(BaseModel):
    grant_type: str
    device_code: str | None = None        # the device-code grant
    refresh_token: str | None = None      # the refresh grant


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


def _bearer(authorization):
    """The token from an `Authorization: Bearer <token>` header, or None."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


def require_fleet(request: Request,
                  fleet_secret: str | None = Header(
                      None, alias="X-HONE-Fleet-Secret")):
    """Gate the OAuth / enrollment endpoints with the fleet shared secret —
       the one and only place the fleet secret is used."""
    if not _secret_ok(fleet_secret, request.app.state.config.fleet_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad fleet secret")


def require_node(request: Request,
                 authorization: str | None = Header(None)):
    """Authenticate a node by its OAuth bearer token. Returns the node row
       (a dict, including its `client_id` tenant). A missing, invalid, expired,
       or revoked token is a `401`; a token for a disabled tenant is a `403`."""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "missing bearer token")
    db = request.app.state.db
    node = core_db.resolve_access_token(db, token)
    if node is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "invalid or expired token")
    client = core_db.get_client(db, node["client_id"])
    if client is None or client["state"] != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tenant disabled")
    return node


def require_admin(request: Request,
                  admin_token: str | None = Header(
                      None, alias="X-HONE-Admin-Token")):
    """Authenticate an admin request."""
    if not _secret_ok(admin_token, request.app.state.config.admin_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad admin token")


def _oauth_error(code, message, status_code=status.HTTP_400_BAD_REQUEST):
    """An OAuth error body — `error.code` is the RFC 8628 device-flow code a
       node branches on (authorization_pending, slow_down, ...)."""
    return JSONResponse(status_code=status_code,
                        content={"error": {"code": code, "message": message}})


def _token_response(request: Request, tok):
    """The successful /v1/oauth/token body — the bearer-token pair plus
       hone-core's CA certificate, which the node pins for the main API."""
    return {"access_token": tok["access_token"],
            "token_type": "Bearer",
            "expires_in": tok["expires_in"],
            "refresh_token": tok["refresh_token"],
            "ca_cert": request.app.state.ca_cert_pem}


# --- OAuth: node enrollment (RFC 8628 device authorization grant) ----------

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


@router.post("/oauth/device_authorization",
             dependencies=[Depends(require_fleet)])
def device_authorization(request: Request,
                         body: DeviceAuthRequest | None = None):
    """Begin node enrollment — issue a device code + user code. The node logs
       the user code for an operator and polls /v1/oauth/token (RFC 8628)."""
    cfg = request.app.state.config
    enr = core_db.create_enrollment(
        request.app.state.db,
        node_name=body.node_name if body else None,
        task_types=body.task_types if body else None,
        ttl_seconds=cfg.device_code_ttl,
        interval=cfg.device_poll_interval)
    base = cfg.public_url.rstrip("/")
    return {"device_code": enr["device_code"],
            "user_code": enr["user_code"],
            "verification_uri": f"{base}/enroll",
            "verification_uri_complete":
                f"{base}/enroll?code={enr['user_code']}",
            "expires_in": enr["expires_in"],
            "interval": enr["interval"]}


@router.post("/oauth/token", dependencies=[Depends(require_fleet)])
def oauth_token(request: Request, body: TokenRequest):
    """Exchange an approved device code, or a refresh token, for a fresh
       bearer-token pair. Device-flow states use the RFC 8628 error codes."""
    cfg = request.app.state.config
    db = request.app.state.db

    if body.grant_type == _DEVICE_GRANT:
        if not body.device_code:
            return _oauth_error("invalid_request", "device_code is required")
        enr = core_db.get_enrollment_by_device_code(db, body.device_code)
        if enr is None:
            return _oauth_error("invalid_grant", "unknown device code")
        if enr["state"] == "denied":
            return _oauth_error("access_denied",
                                "the operator denied this enrollment")
        if enr["state"] == "completed":
            return _oauth_error("invalid_grant",
                                "device code already redeemed")
        if enr["state"] == "pending":
            now = int(time.time())
            if enr["expires_at"] is not None and enr["expires_at"] <= now:
                return _oauth_error("expired_token",
                                    "the device code has expired")
            too_soon = (enr["last_polled_at"] is not None and
                        now - enr["last_polled_at"] < enr["interval_seconds"])
            core_db.set_enrollment_polled(db, enr["id"], now)
            if too_soon:
                return _oauth_error("slow_down", "polling too fast")
            return _oauth_error("authorization_pending",
                                "awaiting operator approval")
        # state == 'approved' — redeem the device code, once
        tok = core_db.issue_tokens(db, enr["node_id"],
                                   access_ttl=cfg.access_token_ttl,
                                   refresh_ttl=cfg.refresh_token_ttl or None)
        core_db.complete_enrollment(db, enr["id"])
        return _token_response(request, tok)

    if body.grant_type == "refresh_token":
        if not body.refresh_token:
            return _oauth_error("invalid_request",
                                "refresh_token is required")
        tok = core_db.rotate_refresh_token(
            db, body.refresh_token, access_ttl=cfg.access_token_ttl,
            refresh_ttl=cfg.refresh_token_ttl or None)
        if tok is None:
            return _oauth_error("invalid_grant",
                                "unknown, expired, or spent refresh token")
        return _token_response(request, tok)

    return _oauth_error("unsupported_grant_type",
                        f"unsupported grant_type {body.grant_type!r}")


# --- claims ----------------------------------------------------------------

@router.post("/claims")
def claim_task(request: Request, node: dict = Depends(require_node)):
    """Claim the next task for the node's tenant — a review task, else a
       global maintenance task, else 204 when both queues are empty."""
    db = request.app.state.db
    worker_id = str(node["id"])

    review = core_db.claim_review(db, node["client_id"], worker_id)
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
    """Submit a completion record. The record is validated against
       core/completion-record.schema.yaml — a malformed record is rejected 422
       and never reaches the database. Idempotent on the claim id. `status` is
       'ok', or 'lapsed' when the claim was reclaimed — on 'lapsed' the node
       discards the result, the reclaim already covered the work."""
    db = request.app.state.db
    if body.task_type == "review":
        if body.state is None or body.record is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "a review result needs `state` and `record`")
        _validate_record(_REVIEW_VALIDATOR, body.record,
                          "review completion record")
        if body.state != body.record.get("outcome"):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"`state` ({body.state!r}) does not match the record's "
                f"`outcome` ({body.record.get('outcome')!r})")
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
        _validate_record(_MAINTENANCE_VALIDATOR, body.result,
                          "maintenance-task record")
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
    """Admin — register a tenant. No credential is minted: a node is bound to
       this tenant when an operator approves its enrollment."""
    cid = core_db.register_client(request.app.state.db, body.name)
    return {"id": cid, "name": body.name}
