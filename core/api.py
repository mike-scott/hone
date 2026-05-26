"""hone-core — the v1 REST API for nodes (see ../API.md).

OAuth endpoints are gated by the fleet shared secret (X-HONE-Fleet-Secret);
the main API uses opaque bearer tokens issued through the device-grant flow.
Handlers are thin: validate, call core_db, shape the response — all state
lives in the database.

The claim endpoint assembles a self-contained payload (patchset, patch
messages, training comments, compiled methodology), so a node makes one
HTTP call per task instead of three.
"""
import copy
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
# common/schema/completion-record.schema.yaml — four branches discriminated by
# the record's `task_type` (prepare / review / train / draft). A malformed
# record is 422'd before it reaches the database.

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "common", "schema", "completion-record.schema.yaml")
with open(_SCHEMA_PATH, encoding="utf-8") as _f:
    _RECORD_SCHEMA = yaml.safe_load(_f)
_RECORD_VALIDATOR = jsonschema.Draft202012Validator(_RECORD_SCHEMA)


def _validate_record(record):
    """422 on a malformed completion record; a no-op when it's valid."""
    errors = sorted(_RECORD_VALIDATOR.iter_errors(record),
                    key=lambda e: str(list(e.absolute_path)))
    if errors:
        e = errors[0]
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"completion record failed schema validation at {loc}: {e.message}")


# --- request bodies --------------------------------------------------------

class DeviceAuthRequest(BaseModel):
    node_name: str | None = None          # the node's self-described label
    task_types: list[str] | None = None   # capabilities (e.g. ["review","train"])


class TokenRequest(BaseModel):
    grant_type: str
    device_code: str | None = None
    refresh_token: str | None = None


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
    """Gate the OAuth endpoints with the fleet shared secret — the one and
       only place the fleet secret is used."""
    if not _secret_ok(fleet_secret, request.app.state.config.fleet_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad fleet secret")


def require_node(request: Request,
                 authorization: str | None = Header(None)):
    """Authenticate a node by its OAuth bearer token. Returns the node row.
       A missing, invalid, expired, or revoked token is a 401."""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "missing bearer token")
    node = core_db.resolve_access_token(request.app.state.db, token)
    if node is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "invalid or expired token")
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
       the user code for an operator and polls /v1/oauth/token (RFC 8628).

       Returns HTTP 409 Conflict if the requested node_name is already
       in use by an ACTIVE node — the registering node can log the
       conflict and exit, no operator approval round-trip needed."""
    cfg = request.app.state.config
    rc = request.app.state.runtime_config
    try:
        enr = core_db.create_enrollment(
            request.app.state.db,
            node_name=body.node_name if body else None,
            task_types=body.task_types if body else None,
            ttl_seconds=rc.device_code_ttl,
            interval_seconds=rc.device_poll_interval)
    except core_db.DuplicateNodeName as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
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
    rc = request.app.state.runtime_config
    db = request.app.state.db

    if body.grant_type == _DEVICE_GRANT:
        if not body.device_code:
            return _oauth_error("invalid_request", "device_code is required")
        enr = core_db.get_enrollment_by_device_code(db, body.device_code)
        if enr is None:
            return _oauth_error("invalid_grant", "unknown device code")
        if enr["state"] == core_db.NODE_ENROLLMENT_STATE_DENIED:
            return _oauth_error("access_denied",
                                "the operator denied this enrollment")
        if enr["state"] == core_db.NODE_ENROLLMENT_STATE_COMPLETED:
            return _oauth_error("invalid_grant",
                                "device code already redeemed")
        if enr["state"] == core_db.NODE_ENROLLMENT_STATE_PENDING:
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
        # state == approved — redeem the device code, once
        tok = core_db.issue_tokens(db, enr["node_id"],
                                   access_ttl=rc.access_token_ttl,
                                   refresh_ttl=rc.refresh_token_ttl or None)
        core_db.complete_enrollment(db, enr["id"])
        return _token_response(request, tok)

    if body.grant_type == "refresh_token":
        if not body.refresh_token:
            return _oauth_error("invalid_request",
                                "refresh_token is required")
        tok = core_db.rotate_refresh_token(
            db, body.refresh_token, access_ttl=rc.access_token_ttl,
            refresh_ttl=rc.refresh_token_ttl or None)
        if tok is None:
            return _oauth_error("invalid_grant",
                                "unknown, expired, or spent refresh token")
        return _token_response(request, tok)

    return _oauth_error("unsupported_grant_type",
                        f"unsupported grant_type {body.grant_type!r}")


# --- claim payload assembly ------------------------------------------------

# Type-name strings the wire uses (the node + this module agree on these);
# they correspond to the small-int WORK_ITEM_TYPE_* in core_db.
_TYPE_NAME = {core_db.WORK_ITEM_TYPE_PREPARE: "prepare",
              core_db.WORK_ITEM_TYPE_REVIEW:  "review",
              core_db.WORK_ITEM_TYPE_TRAIN:   "train"}

# Outcome → terminal work_items.state mapping for work-item submissions.
# The state column is a lifecycle CLASS, not a task-type outcome — every
# success outcome (prepared / reviewed / trained) lands on COMPLETED;
# every structural-failure outcome (uncharacterisable / unappliable) lands
# on UNAPPLIABLE; deferred is its own class. The per-task richness lives
# in the completion record's `outcome` field, which we store on the row
# under work_items.record.
_OUTCOME_STATE = {
    ("prepare", "prepared"):          core_db.WORK_ITEM_STATE_COMPLETED,
    ("prepare", "uncharacterisable"): core_db.WORK_ITEM_STATE_UNAPPLIABLE,
    ("prepare", "deferred"):          core_db.WORK_ITEM_STATE_DEFERRED,
    ("review",  "reviewed"):          core_db.WORK_ITEM_STATE_COMPLETED,
    ("review",  "unappliable"):       core_db.WORK_ITEM_STATE_UNAPPLIABLE,
    ("review",  "deferred"):          core_db.WORK_ITEM_STATE_DEFERRED,
    ("train",   "trained"):           core_db.WORK_ITEM_STATE_COMPLETED,
    ("train",   "unappliable"):       core_db.WORK_ITEM_STATE_UNAPPLIABLE,
    ("train",   "deferred"):          core_db.WORK_ITEM_STATE_DEFERRED,
}


def _compile_methodology(document, task_type_name):
    """The methodology compilation handed to the node: a `core` slice plus the
       operation-specific guidance + return spec for `task_type_name`. The
       `prepare` task receives a narrower `core` (just `principles`) because it
       consults the cross-operation principles but applies no stages, checks,
       documentation_review, or report-finalization rubric — see
       docs/ARCHITECTURE.md → Methodology storage.

       The methodology YAML stores principles / stages / checks /
       documentation_review / report_finalization at the top level (per
       common/schema/methodology.schema.yaml). The compiled slice wraps the
       selection under a synthetic `core` key so the node sees a clean
       core-vs-operations split."""
    _CORE_KEYS = ("principles", "stages", "checks",
                  "documentation_review", "report_finalization")
    if task_type_name == "prepare":
        core_slice = {"principles": document.get("principles", [])}
    else:
        core_slice = {k: document[k] for k in _CORE_KEYS if k in document}
    return {"core": core_slice,
            "operations": {task_type_name:
                          document.get("operations", {}).get(task_type_name,
                                                              {})}}


def _patches_payload(db, root):
    """The patch messages of a patchset (cover + patches, oldest first), as a
       list of dicts suitable for embedding in a claim payload."""
    out = []
    for m in core_db.messages_for_patchset(db, root):
        if m["type"] in (core_db.MSG_TYPE_COVER, core_db.MSG_TYPE_PATCH):
            out.append({"message_id":  m["message_id"],
                         "type":        core_db.MSG_TYPE_NAMES[m["type"]],
                         "part_index":  m["part_index"],
                         "subject":     m["subject"],
                         "author_name": m["author_name"],
                         "author_email": m["author_email"],
                         "sent":        m["sent"],
                         "body":        m["body"]})
    return out


def _thread_messages_payload(db, root):
    """Every non-patch thread message (cover + comments) carrying the
       In-Reply-To linkage prepare's metadata-extraction reads. Used in
       the prepare claim payload alongside `patches` (the diff-carrying
       messages) so the node has the full conversational context."""
    out = []
    for m in core_db.messages_for_patchset(db, root):
        if m["type"] == core_db.MSG_TYPE_COMMENT:
            out.append({"message_id":   m["message_id"],
                         "author_name":  m["author_name"],
                         "author_email": m["author_email"],
                         "in_reply_to":  m["parent_message_id"],
                         "sent":         m["sent"],
                         "subject":      m["subject"],
                         "body":         m["body"]})
    return out


def _cover_letter_body(db, root):
    """The [PATCH 0/N] cover-letter body, or None when the patchset has
       no cover (e.g. a single [PATCH] with no series)."""
    for m in core_db.messages_for_patchset(db, root):
        if m["type"] == core_db.MSG_TYPE_COVER:
            return m["body"]
    return None


def _build_prepare_payload(db, work_item, methodology_version, methodology):
    """The claim payload for a prepare work item — the patchset, every
       patch message, the cover letter, and the full thread (so the node
       can characterise review intensity), plus the compiled methodology
       slice for prepare. See docs/API.md → prepare-task claim."""
    root = work_item["root_message_id"]
    patchset = core_db.get_patchset(db, root) or {}
    return {"claim_id":  work_item["claim_id"],
            "task_type": "prepare",
            "lease_expires_at":    work_item.get("lease_expires"),
            "methodology_version": methodology_version,
            "methodology":         _compile_methodology(methodology,
                                                         "prepare"),
            "patchset": {"root_message_id":       patchset.get("root_message_id"),
                          "subject":               patchset.get("subject"),
                          "declared_base_commit":  patchset.get("base_commit"),
                          "submitter_email":       patchset.get("submitter_email"),
                          "n_patches":             patchset.get("n_patches")},
            "patches":            _patches_payload(db, root),
            "cover_letter_body":  _cover_letter_body(db, root),
            "thread_messages":    _thread_messages_payload(db, root)}


def _build_review_payload(db, work_item, methodology_version, methodology):
    """The claim payload for a review work item — patchset, the structured
       patchset_metadata produced by the gating prepare task, every patch
       message, and the compiled methodology."""
    root = work_item["root_message_id"]
    patchset = core_db.get_patchset(db, root) or {}
    metadata = core_db.get_patchset_metadata(db, root) or {}
    return {"claim_id":  work_item["claim_id"],
            "task_type": "review",
            "lease_expires_at":    work_item.get("lease_expires"),
            "methodology_version": methodology_version,
            "methodology":         _compile_methodology(methodology, "review"),
            "patchset": {"root_message_id": patchset.get("root_message_id"),
                          "subject":         patchset.get("subject"),
                          "base_commit":     patchset.get("base_commit"),
                          "submitter_email": patchset.get("submitter_email"),
                          "n_patches":       patchset.get("n_patches")},
            "patchset_metadata": {
                "subsystem":        metadata.get("subsystem"),
                "patch_size":       metadata.get("patch_size"),
                "maintainer":       metadata.get("maintainer"),
                "patch_type":       metadata.get("patch_type"),
                "review_intensity": metadata.get("review_intensity"),
                "tree_state":       metadata.get("tree_state")},
            "patches": _patches_payload(db, root)}


def _build_train_payload(db, work_item, methodology_version, methodology):
    """The claim payload for a train work item — the target patch, the
       specific reviewer comment the session selected, the prior ai_review
       (concerns), the patchset's structured metadata, the compiled
       methodology, and the (always-present) session metadata. Per
       docs/ARCHITECTURE-WORK-LIFECYCLE.md → The train task."""
    root = work_item["root_message_id"]
    patchset = core_db.get_patchset(db, root) or {}
    metadata = core_db.get_patchset_metadata(db, root) or {}
    patch_msg = next((m for m in core_db.messages_for_patchset(db, root)
                       if m["message_id"] == work_item["message_id"]), None)
    ai = core_db.get_ai_review(db, root) or {}
    # The session orchestrator named the exact comment when materialising
    # this train; no guesswork needed.
    comment_row = db.execute(
        "SELECT message_id, author_name, author_email, body, sent "
        "FROM messages WHERE message_id=?",
        (work_item["comment_message_id"],)).fetchone()
    comment = dict(comment_row) if comment_row else None
    return {"claim_id":  work_item["claim_id"],
            "task_type": "train",
            "lease_expires_at":    work_item.get("lease_expires"),
            "methodology_version": methodology_version,
            "methodology":         _compile_methodology(methodology, "train"),
            "training_session_id": work_item["training_session_id"],
            "session_role":        core_db.SESSION_ROLE_NAMES[
                work_item["session_role"]],
            "stratum_label":       work_item["stratum_label"],
            "patchset": {"root_message_id": patchset.get("root_message_id"),
                          "subject":         patchset.get("subject"),
                          "base_commit":     patchset.get("base_commit"),
                          "submitter_email": patchset.get("submitter_email"),
                          "n_patches":       patchset.get("n_patches")},
            "patchset_metadata": {
                "subsystem":        metadata.get("subsystem"),
                "patch_size":       metadata.get("patch_size"),
                "maintainer":       metadata.get("maintainer"),
                "patch_type":       metadata.get("patch_type"),
                "review_intensity": metadata.get("review_intensity"),
                "tree_state":       metadata.get("tree_state")},
            "patch": {
                "message_id":  patch_msg["message_id"] if patch_msg else None,
                "part_index":  patch_msg["part_index"] if patch_msg else None,
                "subject":     patch_msg["subject"] if patch_msg else None,
                "body":        patch_msg["body"] if patch_msg else None}
                if patch_msg else None,
            "comment": {
                "message_id":   comment["message_id"],
                "author_name":  comment["author_name"],
                "author_email": comment["author_email"],
                "body":         comment["body"]} if comment else None,
            "ai_review": {"concerns": ai.get("concerns", [])}}


def _build_draft_payload(draft_task, methodology_version, methodology,
                         candidates, recent_rejected, redraft_context):
    """The claim payload for a draft task — the eligibility-flag snapshot
       (captured at enqueue), the compiled methodology slice for draft, the
       pooled per-candidate stats the node reasons over, the
       rejected-proposal log, and the optional redraft_context. Per
       docs/API.md → draft-task claim. Some of the doc's optional fields
       (review_evaluations_summary, recent_session_evidence,
       check_pool_stats) are placeholders until the supporting aggregators
       land."""
    return {"claim_id":            draft_task["claim_id"],
            "task_type":           "draft",
            "methodology_version": methodology_version,
            "methodology":         _compile_methodology(methodology, "draft"),
            "eligibility_flags":   draft_task["eligibility_flag_snapshot"],
            "candidate_pool_stats": [
                {"id":             c["id"],
                  "body":           c["body"],
                  "applied":        c["applied"],
                  "catches":        c["catches"],
                  "unique_catches": c["unique_catches"],
                  "severity_witness_introduced":
                      c["severity_witness_introduced"],
                  "severity_witness_preexisting":
                      c["severity_witness_preexisting"],
                  "origin":         c.get("origin")}
                 for c in candidates],
            "check_pool_stats":          [],
            "review_evaluations_summary": None,
            "rejected_proposal_log":     recent_rejected,
            "recent_session_evidence":   [],
            "redraft_context":           redraft_context}


_BUILD_PAYLOAD = {
    core_db.WORK_ITEM_TYPE_PREPARE: _build_prepare_payload,
    core_db.WORK_ITEM_TYPE_REVIEW:  _build_review_payload,
    core_db.WORK_ITEM_TYPE_TRAIN:   _build_train_payload,
}


def _rejected_proposal_log(db, limit=200):
    """Recent Reject-dispositioned proposals — the suppression log the draft
       node consults so it doesn't re-propose a (recommendation, subject)
       pair the operator already rejected."""
    rows = db.execute(
        "SELECT id, type, payload, note, decided_at FROM methodology_proposals "
        "WHERE state=? ORDER BY decided_at DESC LIMIT ?",
        (core_db.METHODOLOGY_PROPOSAL_STATE_REJECTED, limit)).fetchall()
    out = []
    import json as _json
    for r in rows:
        out.append({"proposal_id": r["id"],
                    "kind":        core_db.METHODOLOGY_PROPOSAL_TYPE_NAMES[
                                       r["type"]],
                    "payload":     _json.loads(r["payload"]),
                    "note":        r["note"],
                    "rejected_at": r["decided_at"]})
    return out


def _redraft_context(db, parent_proposal_id):
    """The parent-proposal snapshot a redraft task carries (None for a
       fresh draft). The full feedback-note + payload of the proposal that
       was *Return for redraft*-ed, so the node can produce a re-draft that
       responds to the operator's feedback."""
    if parent_proposal_id is None:
        return None
    row = db.execute(
        "SELECT id, type, payload, note FROM methodology_proposals WHERE id=?",
        (parent_proposal_id,)).fetchone()
    if row is None:
        return None
    import json as _json
    return {"redraft_of":     row["id"],
            "parent_proposal": _json.loads(row["payload"]),
            "feedback_note":  row["note"]}


@router.post("/claims")
def claim_task(request: Request, node: dict = Depends(require_node)):
    """Claim the next task for the node — a work item (prepare/review/train)
       or a draft task, or 204 when both queues are empty. The payload is
       self-contained: patches, comments, the compiled methodology, and any
       per-task evidence travel in the response, so the node makes one HTTP
       call per task."""
    db = request.app.state.db
    # The worker label written into work_items.claimed_by. We use the
    # node's name (the human handle the operator gave it via
    # HONE_NODE_NAME); falling back to the numeric id only for
    # nameless nodes. The hone-node side already self-identifies by
    # name in the completion record's `worker_id` field — this keeps
    # both sides of the wire in agreement and gives the operator a
    # readable Worker column on the queue page instead of bare ids.
    worker_id = node.get("name") or str(node["id"])
    active = core_db.active_methodology(db)
    if active is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "methodology not bootstrapped")
    version, document = active

    # Read node capabilities; default to all work-item types if unspecified.
    try:
        import json as _json
        types_decl = (_json.loads(node["task_types"])
                       if node.get("task_types") else None)
    except Exception:
        types_decl = None
    work_type_ids = None
    wants_draft = True
    if types_decl:
        rev = {v: k for k, v in _TYPE_NAME.items()}
        work_type_ids = [rev[t] for t in types_decl if t in rev]
        if not work_type_ids:
            work_type_ids = None
        wants_draft = "draft" in types_decl

    if work_type_ids is not False:
        wi = core_db.claim_work_item(
            db, worker_id, methodology_version=version, types=work_type_ids)
        if wi is not None:
            builder = _BUILD_PAYLOAD.get(wi["type"])
            if builder is not None:
                return builder(db, wi, version, document)

    if wants_draft:
        dt = core_db.claim_draft_task(db, worker_id)
        if dt is not None:
            candidates = core_db.list_candidates(db)
            return _build_draft_payload(
                dt, version, document, candidates,
                _rejected_proposal_log(db),
                _redraft_context(db, dt.get("parent_proposal_id")))

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/claims/{claim_id}/heartbeat",
             dependencies=[Depends(require_node)])
def heartbeat(claim_id: str, request: Request):
    """Extend the claim's lease. `valid` is false once the claim has lapsed
       (been reclaimed) or completed — the node should then stop and reclaim."""
    return {"valid": core_db.heartbeat(request.app.state.db, claim_id)}


_PREPARE_METADATA_FIELDS = ("tree_state", "subsystem", "patch_size",
                            "maintainer", "patch_type", "review_intensity",
                            "preparation_notes")


@router.post("/claims/{claim_id}/result")
def submit_result(claim_id: str, request: Request, body: dict,
                  node: dict = Depends(require_node)):
    """Submit a completion record. The body IS the completion record (see
       common/schema/completion-record.schema.yaml); the record's `task_type`
       selects the branch (prepare / review / train / draft). Idempotent on
       the claim id. Returns 'ok', or 'lapsed' when the claim was reclaimed
       — on 'lapsed' the node discards the result."""
    db = request.app.state.db
    _validate_record(body)
    task_type = body.get("task_type")
    outcome = body.get("outcome")
    record = copy.deepcopy(body)
    usage = record.get("usage") or {}

    if task_type in ("prepare", "review", "train"):
        state = _OUTCOME_STATE.get((task_type, outcome))
        if state is None:                            # schema guards this too
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                f"bad outcome {outcome!r} for {task_type}")
        try:
            result = core_db.submit_work_result(
                db, claim_id, state=state, record=record)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

        # The methodology_version was stamped on the row at claim time —
        # we read it back here so the downstream rows (patchset_metadata,
        # ai_reviews) record the same version the claim payload pinned.
        wi = db.execute(
            "SELECT root_message_id, methodology_version "
            "FROM work_items WHERE claim_id=?",
            (claim_id,)).fetchone()
        if wi is not None and result == "ok":
            root = wi["root_message_id"]
            mv = wi["methodology_version"]
            # On a successful prepare, write the patchset_metadata row from
            # the record's flat structured fields and arm the review-enqueue
            # gate (maybe_enqueue_review now sees the metadata row).
            if task_type == "prepare" and outcome == "prepared":
                core_db.upsert_patchset_metadata(
                    db, root,
                    mode=(record.get("preparation_notes") or {}).get("mode"),
                    methodology_version=mv,
                    **{f: record.get(f) for f in _PREPARE_METADATA_FIELDS})
                core_db.maybe_enqueue_review(db, root)
            # On a successful review, capture the concerns into ai_reviews.
            # No train enqueue here — trains are session-driven, created
            # only when the operator launches a session that includes this
            # patchset.
            elif task_type == "review" and outcome == "reviewed":
                # node_id is the authenticated node from the bearer
                # token — NOT parsed from record["worker_id"] (which is
                # the node's human-readable name, not a numeric id).
                core_db.upsert_ai_review(
                    db, root,
                    concerns=record.get("concerns", []),
                    model=record.get("model"),
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    methodology_version=mv,
                    node_id=node["id"],
                    meta=record.get("meta"))
            # On a successful train, advance the candidate's pooled
            # counters and severity_witness histograms from the record's
            # candidate_outcomes — but only for `pool` role. A `holdout`
            # train's per-record outcomes persist in work_items.record
            # and feed the statistical gates' pool-vs-holdout
            # computations on demand; they do not move pooled counters.
            elif (task_type == "train" and outcome == "trained"
                    and record.get("session_role") == "pool"):
                # TODO: walk record["candidate_outcomes"] and call
                # core_db.bump_candidate + core_db.bump_severity_witness
                # per candidate per fired concern. Requires looking up
                # each concern_id's severity + is_preexisting from the
                # prior ai_review; deferred until the train task handler
                # is implemented (see ARCHITECTURE.md → Today vs. target).
                pass
        return {"status": result}

    if task_type == "draft":
        try:
            result = core_db.complete_draft_task(db, claim_id, record)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
        if result == "ok" and outcome == "drafted":
            # Enqueue each "propose" disposition as a pending merge-gate
            # proposal. decline/defer don't change eligibility state at the
            # node level — the operator's disposition is what
            # suppresses / defers the flag (see decide_proposal /
            # mark_flag_suppressed). base_methodology_version is read off
            # the draft_tasks row (stamped at enqueue), not echoed from
            # the record.
            dt_row = db.execute(
                "SELECT methodology_version FROM draft_tasks "
                "WHERE claim_id=?", (claim_id,)).fetchone()
            mv = dt_row["methodology_version"] if dt_row is not None else None
            for prop in record.get("proposals", []):
                ptype = core_db.METHODOLOGY_PROPOSAL_TYPE_BY_NAME.get(
                    prop["recommendation"])
                if ptype is None:                    # schema guards this too
                    continue
                payload = dict(prop)
                payload.setdefault("base_methodology_version", mv)
                core_db.add_proposal(db, ptype, payload)
        return {"status": result}

    raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                        f"unknown task_type {task_type!r}")
