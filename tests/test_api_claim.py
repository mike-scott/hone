"""Tests for the /v1/claims endpoint and the api.py payload builders.

`test_api_submit_result.py` covers the result-submission path against a
heavily-stubbed core_db. This file exercises the dispatch + payload
assembly against a real in-memory database so the four payload shapes are
constructed end-to-end."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import api, core_db

HEADERS = {"Authorization": "Bearer good-token"}


def _plant_metadata(db, root):
    core_db.upsert_patchset_metadata(
        db, root, mode="heuristic",
        tree_state={"tree_available": True},
        subsystem={"primary": "drivers/net"},
        patch_size={"bucket": "small"},
        maintainer={"primary": "alice@k.org"},
        patch_type={"primary": "bugfix"},
        review_intensity={"bucket_overall": "light", "per_reply": []},
        preparation_notes={"mode": "heuristic"})


@pytest.fixture
def ctx(tmp_path):
    """A FastAPI test client over the real v1 router with a real db
       (in-memory-but-on-disk so the migrations run), a stub node
       resolver, and a bootstrapped methodology."""
    db = core_db.connect(str(tmp_path / "hone.db"))
    core_db.add_methodology_version(
        db, {"name": "test", "version": 1,
             "principles": [{"id": "p1", "title": "P", "body": "..."}],
             "stages": [{"id": "0", "title": "S", "applies": "x",
                          "body": "..."}],
             "checks": [],
             "operations": {"prepare":  {"guidance": "g-p", "return": "r-p"},
                            "review":   {"guidance": "g-r", "return": "r-r"},
                            "train":    {"guidance": "g-t", "return": "r-t"},
                            "draft":    {"guidance": "g-d", "return": "r-d"}}})
    app = FastAPI()
    app.include_router(api.router)
    app.state.db = db
    app.state.config = SimpleNamespace(fleet_secret="f", admin_token="a")
    # the bearer-token authenticator resolves to a fake node
    import json as _json
    node = {"id": 1, "task_types":
            _json.dumps(["prepare", "review", "train", "draft"])}

    def fake_resolve(_db, tok):
        return node if tok == "good-token" else None

    app.state.config = SimpleNamespace(fleet_secret="f", admin_token="a")
    app.dependency_overrides = {}
    # We can't monkeypatch on a session-scope fixture cheaply; do it via
    # the require_node dependency override.
    app.dependency_overrides[api.require_node] = lambda: node
    return SimpleNamespace(http=TestClient(app), db=db, node=node)


# --- _compile_methodology (pure function — narrow slice for prepare) ------

def test_compile_methodology_prepare_narrows_to_principles_only():
    """The prepare task gets `core: { principles }` only — no stages,
       checks, documentation_review, or report_finalization. See
       docs/ARCHITECTURE.md → Methodology storage. The methodology
       document stores these fields at the top level (per
       common/schema/methodology.schema.yaml)."""
    doc = {"principles":          [{"id": "p1"}],
            "stages":              [{"id": "0"}],
            "checks":              [{"id": "c1"}],
            "documentation_review": {"body": "..."},
            "report_finalization": {"body": "..."},
            "operations": {"prepare": {"guidance": "g", "return": "r"},
                            "review":  {"guidance": "g2", "return": "r2"}}}
    out = api._compile_methodology(doc, "prepare")
    assert out["core"] == {"principles": [{"id": "p1"}]}
    assert out["operations"] == {"prepare": {"guidance": "g", "return": "r"}}


def test_compile_methodology_review_gets_the_full_core():
    """Review / train / draft get the full `core` block — every governing
       field present in the document, lifted under the synthetic `core`
       wrapper."""
    doc = {"principles": [{"id": "p1"}],
            "stages":     [{"id": "0"}],
            "checks":     [{"id": "c1"}],
            "operations": {"review": {"guidance": "g", "return": "r"}}}
    out = api._compile_methodology(doc, "review")
    assert "stages" in out["core"] and "checks" in out["core"]


def test_compile_methodology_omits_other_operations():
    """Only the asked-for operation's prompt rides along in the slice."""
    doc = {"principles": [],
            "operations": {"review": {"guidance": "gr", "return": "rr"},
                            "train":  {"guidance": "gt", "return": "rt"}}}
    out = api._compile_methodology(doc, "review")
    assert out["operations"] == {"review": {"guidance": "gr",
                                              "return": "rr"}}


# --- variable substitution ------------------------------------------------

def test_substitute_replaces_known_tokens_in_strings():
    """%NAME% tokens get replaced from the variables dict; everything
       else passes through verbatim."""
    out = api._substitute("today is %DATE_LONG%, run %DATE_SHORT%",
                           {"%DATE_LONG%": "Tuesday, 26 May 2026",
                            "%DATE_SHORT%": "2026-05-26"})
    assert out == "today is Tuesday, 26 May 2026, run 2026-05-26"


def test_substitute_leaves_unknown_tokens_in_place():
    """Methodology text may want to discuss the syntax itself; an
       unknown token like %FUTURE_VAR% is left literal, not stripped
       or errored on."""
    out = api._substitute("hello %FUTURE_VAR% world",
                           {"%DATE_LONG%": "x"})
    assert out == "hello %FUTURE_VAR% world"


def test_substitute_recurses_through_lists_and_dicts():
    """The methodology slice is a nested dict/list of strings —
       _substitute walks the structure and replaces tokens inside
       every string regardless of depth."""
    doc = {
        "principles": [
            {"body": "the date is %DATE_LONG%"},
            {"body": "iso: %DATE_SHORT%"},
        ],
        "operations": {"prepare": {"guidance": "now: %DATE_LONG%"}},
    }
    out = api._substitute(doc, {"%DATE_LONG%": "L", "%DATE_SHORT%": "S"})
    assert out["principles"][0]["body"] == "the date is L"
    assert out["principles"][1]["body"] == "iso: S"
    assert out["operations"]["prepare"]["guidance"] == "now: L"


def test_substitute_passes_non_string_values_through():
    """Non-string leaves (int, bool, None) are not touched; only
       string nodes are scanned."""
    doc = {"count": 42, "flag": True, "nada": None, "msg": "hi %X%"}
    out = api._substitute(doc, {"%X%": "ok"})
    assert out == {"count": 42, "flag": True, "nada": None, "msg": "hi ok"}


def test_substitute_reindents_multiline_values_to_line_indent():
    """A multi-line replacement value adopts the leading whitespace of
       the line where the token sits, prefixed onto every newline.
       This is what keeps a JSON block dropped inside an indented
       bullet from breaking out of its container."""
    src = "preamble\n    %BLOB%\ntail"
    out = api._substitute(src, {"%BLOB%": '{\n  "k": "v"\n}'})
    # The opening brace lands where the token was (column 4 — handled
    # by the token's own position). Each continuation line picks up
    # the 4-space indent from the line.
    assert out == 'preamble\n    {\n      "k": "v"\n    }\ntail'


def test_substitute_does_not_reindent_when_line_has_no_indent():
    """A token at column 0 leaves continuation lines at column 0 —
       no leading whitespace means no re-indent. Idempotent for
       single-line values too."""
    out_multiline = api._substitute(
        "%BLOB%", {"%BLOB%": '{\n  "k": "v"\n}'})
    assert out_multiline == '{\n  "k": "v"\n}'
    out_single = api._substitute(
        "x %TOK% y", {"%TOK%": "hi"})
    assert out_single == "x hi y"


def test_substitute_uses_only_leading_whitespace_not_full_prefix():
    """If non-whitespace precedes the token on the line, the re-indent
       still uses just the LEADING whitespace of the line — not the
       column where the token starts. (Otherwise the alignment would
       jut out into prose space.)"""
    src = "    text before %BLOB% after"
    out = api._substitute(src, {"%BLOB%": "L1\nL2"})
    # Continuation lines get the 4-space indent from the line, NOT
    # alignment with the column where %BLOB% started.
    assert out == "    text before L1\n    L2 after"


def test_methodology_variables_returns_iso_and_long_dates():
    """The variable registry exposes %DATE_SHORT% (ISO-8601, fixed
       length 10) and %DATE_LONG% (natural-language form). Both
       come off the same UTC `now` snapshot."""
    vars_ = api._methodology_variables()
    assert set(vars_) == {"%DATE_LONG%", "%DATE_SHORT%"}
    # ISO date: YYYY-MM-DD, ten chars, three dashes.
    assert len(vars_["%DATE_SHORT%"]) == 10
    assert vars_["%DATE_SHORT%"].count("-") == 2
    # Long form is at least 12 chars (e.g. "1 May 2026" = 10, with
    # weekday prefix ≥ 16) and contains the same year as ISO.
    assert vars_["%DATE_SHORT%"][:4] in vars_["%DATE_LONG%"]


def test_methodology_variables_includes_completion_record_schema_per_task(
        ):
    """%COMPLETION_RECORD_SCHEMA_JSON% is per-task-type: the prepare
       branch's $ref-resolved schema gets injected for a prepare
       claim, the review branch for a review claim, etc. Lets the
       methodology drop a single token where it wants the
       authoritative output contract."""
    prepare_vars = api._methodology_variables("prepare")
    review_vars  = api._methodology_variables("review")
    assert "%COMPLETION_RECORD_SCHEMA_JSON%" in prepare_vars
    assert "%COMPLETION_RECORD_SCHEMA_JSON%" in review_vars
    # Schemas are distinct — prepare's task_type const is "prepare",
    # review's is "review".
    assert '"const": "prepare"' in prepare_vars[
        "%COMPLETION_RECORD_SCHEMA_JSON%"]
    assert '"const": "review"' in review_vars[
        "%COMPLETION_RECORD_SCHEMA_JSON%"]
    # No task_type → variable is omitted (existing callers don't get
    # surprised).
    assert "%COMPLETION_RECORD_SCHEMA_JSON%" not in api._methodology_variables()


def test_completion_record_schema_has_refs_resolved():
    """The injected schema is self-contained — no $ref nodes left to
       chase. The model gets the full shape inline rather than
       references it can't follow."""
    schema_json = api._methodology_variables("prepare")[
        "%COMPLETION_RECORD_SCHEMA_JSON%"]
    # The original schema has $refs to e.g. prepare_tree_state; after
    # resolution they're inlined and the literal "$ref" string is
    # gone from the prepare branch.
    assert "$ref" not in schema_json
    # And specific inlined field shapes are present (was_cc_d is
    # the often-missed mailing_list field name — the whole point of
    # injecting the schema is so Claude sees it spelled correctly).
    assert "was_cc_d" in schema_json


def test_compile_methodology_injects_resolved_schema_into_return(monkeypatch):
    """End-to-end: a prepare-task return field carrying the new
       %COMPLETION_RECORD_SCHEMA_JSON% token comes back with the
       inlined JSON Schema in place, at the right indent."""
    doc = {"principles": [],
            "operations": {"prepare": {
                "guidance": "g",
                # Indented placement to exercise the indent re-apply.
                "return": "  schema:\n    %COMPLETION_RECORD_SCHEMA_JSON%"}}}
    out = api._compile_methodology(doc, "prepare")
    ret = out["operations"]["prepare"]["return"]
    assert "%COMPLETION_RECORD_SCHEMA_JSON%" not in ret
    # Schema is in the output, indented 4 spaces — same indent as
    # the placeholder's line in the source.
    assert "\n    {" in ret           # opening brace on a 4-indented line
    assert '"const": "prepare"' in ret


def test_compile_methodology_substitutes_date_into_principles(monkeypatch):
    """End-to-end: a principle body carrying %DATE_LONG% comes back
       with the live date string, not the literal token. Variables
       are monkeypatched to a fixed value so the test is hermetic."""
    monkeypatch.setattr(api, "_methodology_variables",
                         lambda *_a, **_kw: {
                             "%DATE_LONG%": "Tuesday, 26 May 2026",
                             "%DATE_SHORT%": "2026-05-26"})
    doc = {"principles": [
        {"id": "set-current-date",
         "title": "Establish the current date",
         "body": "the current date is %DATE_LONG% (%DATE_SHORT%)"}],
            "operations": {"prepare": {"guidance": "g", "return": "r"}}}
    out = api._compile_methodology(doc, "prepare")
    body = out["core"]["principles"][0]["body"]
    assert "%DATE_LONG%" not in body
    assert "Tuesday, 26 May 2026" in body
    assert "2026-05-26" in body


# --- /v1/claims endpoint --------------------------------------------------

def test_claim_returns_204_when_queue_empty(ctx):
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 204


def test_claim_serves_a_prepare_task_with_the_narrow_methodology(ctx):
    """A claimable prepare task surfaces as a 200 with the prepare-shaped
       payload + a `core` slice that carries only `principles`."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_prepare(ctx.db, "<r1@x>")
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    payload = r.json()
    assert payload["task_type"] == "prepare"
    assert payload["claim_id"]
    assert set(payload["methodology"]["core"].keys()) == {"principles"}
    assert payload["patchset"]["root_message_id"] == "r1@x"
    assert payload["patchset"]["declared_base_commit"] is None
    assert any(p["message_id"] == "p1@x" for p in payload["patches"])


def test_claim_stamps_methodology_version_on_the_work_items_row(ctx):
    """The active methodology version is frozen on the work_items row at
       claim time — not at result-submission time. So a row that's still
       in the CLAIMED state already carries its methodology_version, and
       the column matches what the payload advertised."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_prepare(ctx.db, "<r1@x>")
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    payload = r.json()
    row = ctx.db.execute(
        "SELECT state, methodology_version FROM work_items WHERE claim_id=?",
        (payload["claim_id"],)).fetchone()
    assert row["state"] == core_db.WORK_ITEM_STATE_CLAIMED
    assert row["methodology_version"] == payload["methodology_version"]
    assert row["methodology_version"] == 1            # the only active version


def test_claim_writes_the_node_name_into_claimed_by(ctx):
    """work_items.claimed_by stores the node's human-readable name
       (the operator-facing label shown in the Worker column on the
       queue page) — not the numeric nodes.id. A node without a
       self-identified name falls back to str(node.id)."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_prepare(ctx.db, "<r1@x>")
    # Give the fixture's authenticated node a name and re-issue a claim.
    ctx.node["name"] = "builder-7"
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    row = ctx.db.execute(
        "SELECT claimed_by FROM work_items WHERE claim_id=?",
        (r.json()["claim_id"],)).fetchone()
    assert row["claimed_by"] == "builder-7"


def test_claim_falls_back_to_node_id_when_unnamed(ctx):
    """If a node hasn't self-identified (no HONE_NODE_NAME), the
       numeric id is the only stable handle — claimed_by gets
       str(node.id) so the row still has SOMETHING in the Worker
       column."""
    core_db.upsert_patchset(ctx.db, "<r2@x>", subject="[PATCH 1/1] y",
                             n_patches=1)
    core_db.upsert_message(ctx.db, "<p2@x>", root_message_id="<r2@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_prepare(ctx.db, "<r2@x>")
    # Fixture node has no "name" key — exercises the fallback.
    ctx.node.pop("name", None)
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    row = ctx.db.execute(
        "SELECT claimed_by FROM work_items WHERE claim_id=?",
        (r.json()["claim_id"],)).fetchone()
    assert row["claimed_by"] == "1"           # the fixture's node id


def test_claim_serves_a_review_task_with_full_core_and_patchset_metadata(ctx):
    """A review claim payload carries the patchset_metadata produced by
       prepare and gets the full `core` block (not narrowed)."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1, sent=1_700_000_000)
    _plant_metadata(ctx.db, "<r1@x>")
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_review(ctx.db, "<r1@x>")
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    payload = r.json()
    assert payload["task_type"] == "review"
    assert "stages" in payload["methodology"]["core"]
    assert payload["patchset_metadata"]["subsystem"] == {
        "primary": "drivers/net"}
    assert payload["patchset_metadata"]["patch_size"] == {"bucket": "small"}
    # submission time rides along for the node's tip-at-submission fallback
    assert payload["patchset"]["sent"] == 1_700_000_000


def test_claim_serves_a_train_task_with_session_fields_and_named_comment(ctx):
    """A train claim payload echoes the session metadata and names the
       specific comment via comment_message_id, not a "latest comment"
       guess."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    _plant_metadata(ctx.db, "<r1@x>")
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.upsert_message(ctx.db, "<c1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_COMMENT, body="LGTM",
                           parent_message_id="<p1@x>",
                           author_name="A", author_email="a@k.org")
    core_db.upsert_ai_review(ctx.db, "<r1@x>", concerns=[])
    sid = core_db.create_session_draft(ctx.db, "standard")
    core_db.add_session_patchset(ctx.db, sid, "<r1@x>",
                                  role=core_db.SESSION_ROLE_POOL,
                                  stratum_label="net:light")
    core_db.enqueue_session_train(
        ctx.db, session_id=sid, root_message_id="<r1@x>",
        patch_message_id="<p1@x>", comment_message_id="<c1@x>",
        session_role=core_db.SESSION_ROLE_POOL, stratum_label="net:light")
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    payload = r.json()
    assert payload["task_type"] == "train"
    assert payload["training_session_id"] == sid
    assert payload["session_role"] == "pool"
    assert payload["stratum_label"] == "net:light"
    assert payload["patch"]["message_id"] == "p1@x"
    assert payload["comment"]["message_id"] == "c1@x"
    assert payload["comment"]["body"] == "LGTM"
    assert payload["ai_review"] == {"concerns": []}


def test_claim_serves_a_draft_task_when_no_work_items_available(ctx):
    """When the work_items queue is empty, the dispatcher falls through to
       the draft queue and serves a draft claim."""
    core_db.enqueue_draft_task(
        ctx.db, [{"flag_id": "elig-1", "kind": "graduate"}],
        methodology_version=1)
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    payload = r.json()
    assert payload["task_type"] == "draft"
    assert payload["eligibility_flags"] == [
        {"flag_id": "elig-1", "kind": "graduate"}]
    # draft task gets the full core slice, draft-only operation prompt.
    assert payload["methodology"]["operations"].keys() == {"draft"}


def test_claim_503s_when_methodology_not_bootstrapped(tmp_path):
    """The claim dispatcher requires an active methodology to compile the
       slice; without one it returns 503."""
    db = core_db.connect(str(tmp_path / "hone.db"))                # no bootstrap
    app = FastAPI()
    app.include_router(api.router)
    app.state.db = db
    app.state.config = SimpleNamespace(fleet_secret="f", admin_token="a")
    app.dependency_overrides[api.require_node] = lambda: {
        "id": 1, "task_types": None}
    r = TestClient(app).post("/v1/claims", headers=HEADERS)
    assert r.status_code == 503


def test_claim_respects_node_task_types_filter(ctx):
    """A node declaring only `prepare` in its task_types must NOT be
       offered a review/train claim, and must NOT receive a draft task."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    _plant_metadata(ctx.db, "<r1@x>")
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_review(ctx.db, "<r1@x>")
    core_db.enqueue_draft_task(ctx.db, [{"flag_id": 1}])

    # Tighten the node's declared task_types to prepare-only.
    import json as _json
    ctx.node["task_types"] = _json.dumps(["prepare"])
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    # No prepare task is queued; the review and draft are out of scope; 204.
    assert r.status_code == 204


def test_claim_body_task_types_override_stale_enrolled_capabilities(ctx):
    """A node enrolled (one-time) with all four types but now declaring only
       `prepare` per-claim must NOT be offered the queued review task — the
       per-claim declaration overrides the stale enrolled set, so an
       already-enrolled node self-heals without re-enrolling."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    _plant_metadata(ctx.db, "<r1@x>")
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_review(ctx.db, "<r1@x>")
    # ctx.node is still enrolled with all four types (the stale superset).
    r = ctx.http.post("/v1/claims", headers=HEADERS,
                      json={"task_types": ["prepare"]})
    assert r.status_code == 204


def test_claim_without_body_falls_back_to_enrolled_capabilities(ctx):
    """Backward-compatible: a node that sends no claim body (an older
       hone-node) is still filtered by its enrolled task_types — here the
       enrolled superset includes `review`, so the queued review is served."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH 1/1] x",
                             n_patches=1)
    _plant_metadata(ctx.db, "<r1@x>")
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_review(ctx.db, "<r1@x>")
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["task_type"] == "review"


# --- ownership-aware claim semantics --------------------------------------

def _seed_prepare_for_user(db, root, *, user_id):
    """Enqueue a prepare work item attributed to `user_id`. Helper for
       the ownership-aware claim tests below — they only care that an
       item exists, not what's inside it."""
    core_db.upsert_patchset(db, root, subject="[PATCH 1/1] x", n_patches=1)
    core_db.upsert_message(db, root.replace("r", "p"), root_message_id=root,
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    core_db.maybe_enqueue_prepare(db, root, requested_by_user_id=user_id)


def test_claim_serves_owner_user_item_before_system(ctx):
    """A node owned by user A serves a user-A item before a system item
       even when the system item was enqueued first — owner items have
       priority by design (the whole point of per-user queues)."""
    # Create user A.
    uid = core_db.create_user(ctx.db, "a@x", "A", "local")
    # System item first (older enqueued_at).
    _seed_prepare_for_user(ctx.db, "<r1@x>", user_id=None)
    # Owner-A item second.
    _seed_prepare_for_user(ctx.db, "<r2@x>", user_id=uid)
    # Configure the test node as owned by A, with system fallback on.
    ctx.node["owner_user_id"] = uid
    ctx.node["handles_system"] = 1

    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    payload = r.json()
    assert payload["patchset"]["root_message_id"] == "r2@x"     # owner's item


def test_claim_owner_only_node_skips_system_pool(ctx):
    """A node with handles_system=0 must not claim a system item, even
       when its owner's queue is empty — 204 is the back-pressure
       signal."""
    uid = core_db.create_user(ctx.db, "a@x", "A", "local")
    _seed_prepare_for_user(ctx.db, "<r1@x>", user_id=None)        # system
    ctx.node["owner_user_id"] = uid
    ctx.node["handles_system"] = 0
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 204


def test_claim_owner_falls_back_to_system_when_owner_queue_drains(ctx):
    """With handles_system=1 and no owner-A items queued, the node falls
       back to the system pool. Mirrors the legacy-node behaviour."""
    uid = core_db.create_user(ctx.db, "a@x", "A", "local")
    _seed_prepare_for_user(ctx.db, "<r1@x>", user_id=None)        # system only
    ctx.node["owner_user_id"] = uid
    ctx.node["handles_system"] = 1
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["patchset"]["root_message_id"] == "r1@x"


def test_claim_legacy_ownerless_node_still_claims_system_items(ctx):
    """Migration default: a pre-existing node with owner_user_id=NULL
       and handles_system=1 keeps claiming the system pool — the
       migration must not freeze the existing fleet."""
    _seed_prepare_for_user(ctx.db, "<r1@x>", user_id=None)
    ctx.node["owner_user_id"] = None                              # legacy
    ctx.node["handles_system"] = 1
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["patchset"]["root_message_id"] == "r1@x"


def test_claim_owned_node_does_not_claim_other_users_items(ctx):
    """A node owned by A must not pull B's items — the per-user queue
       is the whole isolation guarantee."""
    a = core_db.create_user(ctx.db, "a@x", "A", "local")
    b = core_db.create_user(ctx.db, "b@x", "B", "local")
    _seed_prepare_for_user(ctx.db, "<r1@x>", user_id=b)           # B's item
    ctx.node["owner_user_id"] = a
    ctx.node["handles_system"] = 0                                # no fallback
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 204                                   # 0 wins


# --- orphan rescue: the system pool absorbs items of node-less users ------

def _insert_node_row(db, *, name, owner_user_id,
                     state=core_db.NODE_STATE_ACTIVE):
    """Plant a real `nodes` row — the rescue clause in claim_work_item
       decides 'does this user own an active node?' from the table, not
       from the claiming node's identity."""
    import json, time
    db.execute(
        "INSERT INTO nodes (name, task_types, state, enrolled_at, "
        "owner_user_id, handles_system) VALUES (?, ?, ?, ?, ?, 0)",
        (name, json.dumps(["prepare"]), state, int(time.time()),
         owner_user_id))
    db.commit()


def test_claim_system_node_rescues_item_of_node_less_user(ctx):
    """A user item whose requester owns no node has no dedicated server;
       the system pool absorbs it so a node-less user's request is still
       served (the pre-ownership behaviour)."""
    b = core_db.create_user(ctx.db, "b@x", "B", "local")          # no nodes
    _seed_prepare_for_user(ctx.db, "<r1@x>", user_id=b)
    ctx.node["owner_user_id"] = None                              # system node
    ctx.node["handles_system"] = 1
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["patchset"]["root_message_id"] == "r1@x"


def test_claim_system_node_leaves_items_of_user_with_an_active_node(ctx):
    """No rescue while the requester owns an active node — the item
       waits for the owner's own fleet (even if it's currently offline;
       owning a node means owning the routing)."""
    b = core_db.create_user(ctx.db, "b@x", "B", "local")
    _insert_node_row(ctx.db, name="b-node", owner_user_id=b)
    _seed_prepare_for_user(ctx.db, "<r1@x>", user_id=b)
    ctx.node["owner_user_id"] = None
    ctx.node["handles_system"] = 1
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 204


def test_claim_rescue_resumes_once_users_last_node_is_revoked(ctx):
    """A revoked node doesn't count as a dedicated server: once the
       user's last active node is gone, their pending items flow back
       to the system pool instead of starving."""
    b = core_db.create_user(ctx.db, "b@x", "B", "local")
    _insert_node_row(ctx.db, name="b-node", owner_user_id=b,
                     state=core_db.NODE_STATE_REVOKED)
    _seed_prepare_for_user(ctx.db, "<r1@x>", user_id=b)
    ctx.node["owner_user_id"] = None
    ctx.node["handles_system"] = 1
    r = ctx.http.post("/v1/claims", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["patchset"]["root_message_id"] == "r1@x"
