"""Tests for node/tasks.py — the claim → handler dispatch table and the
handlers themselves. The AI call is monkeypatched (`node.ai.call_claude`)
so the tests stay hermetic; the completion records the handlers emit
are validated against common/schema/completion-record.schema.yaml so a
shape regression is caught here, before submission to hone-core."""
import json
import os
from types import SimpleNamespace

import jsonschema
import pytest
import yaml

from node import ai, cgit, tasks, tier0


# --- schema-based record validator ----------------------------------------

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "common", "schema", "completion-record.schema.yaml")
with open(_SCHEMA_PATH, encoding="utf-8") as _f:
    _RECORD_SCHEMA = yaml.safe_load(_f)
_RECORD_VALIDATOR = jsonschema.Draft202012Validator(_RECORD_SCHEMA)


def _validate_record(record):
    """Pass the record through the completion-record schema. Any schema
       violation raises with the failing path + message — the same check
       hone-core's submit_result does on receipt."""
    errors = sorted(_RECORD_VALIDATOR.iter_errors(record),
                    key=lambda e: str(list(e.absolute_path)))
    if errors:
        e = errors[0]
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        raise AssertionError(f"record failed schema at {loc}: {e.message}")


# --- dispatch -------------------------------------------------------------

def test_dispatch_routes_by_task_type(monkeypatch):
    calls = []

    def fake_handler(cfg, client, claim):
        calls.append((cfg, client, claim))
        return {"outcome": "reviewed", "concerns": []}

    monkeypatch.setitem(tasks.HANDLERS, "review", fake_handler)
    out = tasks.dispatch("cfg", "client", {"task_type": "review", "x": 1})
    assert calls == [("cfg", "client", {"task_type": "review", "x": 1})]
    assert out == {"outcome": "reviewed", "concerns": []}


def test_dispatch_raises_on_unknown_task_type():
    with pytest.raises(ValueError, match="unknown task_type"):
        tasks.dispatch("cfg", "client", {"task_type": "bogus"})


def test_dispatch_raises_on_missing_task_type():
    with pytest.raises(ValueError, match="unknown task_type"):
        tasks.dispatch("cfg", "client", {})


def test_handlers_registry_covers_the_four_task_types():
    assert set(tasks.HANDLERS) == {"prepare", "review", "train", "draft"}


def test_review_handler_raises_until_ai_integration_lands(monkeypatch):
    # the dispatch + claim shape are wired; the AI call is explicitly missing
    with pytest.raises(NotImplementedError):
        tasks.handle_review_task(None, None,
                                 {"task_type": "review",
                                   "methodology_version": 1})


def test_train_handler_raises_until_ai_integration_lands():
    with pytest.raises(NotImplementedError):
        tasks.handle_train_task(None, None,
                                {"task_type": "train",
                                 "methodology_version": 1})


def test_draft_handler_raises_until_ai_integration_lands():
    with pytest.raises(NotImplementedError):
        tasks.handle_draft_task(None, None,
                                {"task_type": "draft",
                                 "methodology_version": 1})


# --- prepare handler ------------------------------------------------------

# A realistic claim payload for the prepare handler. Methodology slice
# is what /v1/claims compiles for a prepare task — `core: { principles }`
# only, plus operations.prepare.{guidance, return}. Patchset shape
# mirrors what core/api.py:_build_prepare_payload emits.
_PRINCIPLE = {"id": "trailers-are-not-evidence",
               "title": "Trailers are not evidence",
               "body": "External endorsements are not verification."}

_PREPARE_CLAIM = {
    "claim_id":            "c-1",
    "task_type":           "prepare",
    "lease_expires_at":    1779360000,
    "methodology_version": 7,
    "methodology": {
        "core": {"principles": [_PRINCIPLE]},
        "operations": {"prepare": {
            "guidance": "Characterise the patchset.",
            "return":   "Return raw JSON only — no prose, no fences."}},
    },
    "patchset": {
        "root_message_id":     "r1@x",
        "subject":             "[PATCH v2] drm/msm/dpu: fix mismatch",
        "declared_base_commit": None,
        "submitter_email":     "alice@example.com",
        "n_patches":           1},
    "patches": [{"message_id": "p1@x",
                 "type":       "patch",
                 "part_index": None,
                 "subject":    "[PATCH v2] drm/msm/dpu: fix mismatch",
                 "body":       "<the .patch text>"}],
    "cover_letter_body": None,
    "thread_messages":   [],
}


# A stub `prepared`-outcome JSON response from Claude, with the
# structured metadata fields the prepare-record schema requires. We
# emit JSON-as-text since that's what call_claude returns.
_STUB_PREPARE_BODY = {
    "patchset_id":        "r1@x",
    "tree_state":         {"tree_available": False,
                            "base_commit_source": "none",
                            "prerequisite_patch_ids": []},
    "subsystem":          {"primary": "drivers/gpu/drm/msm",
                            "secondary": [],
                            "cross_cutting": False,
                            "uncertain_paths": [],
                            "source": "thread"},
    "patch_size":         {"lines_added": 8, "lines_removed": 3,
                            "files_modified": 1, "files_added": 0,
                            "files_deleted": 0, "files_renamed": 0,
                            "hunks": 1, "bucket": "small",
                            "series_length": 1,
                            "churn_ratio": {"max": None, "mean": None,
                                             "high_churn_file_count": None},
                            "source": "thread"},
    "maintainer":         {"authoritative_set": [],
                            "authoritative_reviewer_set": [],
                            "mailing_lists": [],
                            "cc_list_size": 0,
                            "source": "thread"},
    "patch_type":         {"primary": "bugfix",
                            "secondary": [],
                            "evidence": {"primary": "Subject contains 'fix'"},
                            "source": "thread"},
    "review_intensity":   {"bucket_overall": "none",
                            "reply_count": 0, "unique_reviewers": 0,
                            "trailer_only_count": 0, "light_count": 0,
                            "substantive_count": 0, "deep_count": 0,
                            "had_nack": False, "had_v_next": False,
                            "per_reply": [],
                            "source": "thread"},
    "preparation_notes":  {"warnings": [],
                            "confidence": "medium",
                            "mode": "heuristic"},
    "self_review_record": {"summary": "no challenges raised",
                            "challenges": []},
}


def _fake_call_claude(response_text, *, trace=None):
    """A node.ai.call_claude replacement that captures the (system, user)
       prompts the handler built and returns a canned response. `trace`,
       when given, is included in the response the way the streaming CLI
       path returns it."""
    calls = []
    def _stub(cfg, system, user_text, *, model=None, max_tokens=None,
              tools=None):
        calls.append({"system": system, "user_text": user_text,
                       "model": model, "max_tokens": max_tokens,
                       "tools": tools})
        out = {"text":  response_text,
               "model": "claude-opus-4-7",
               "usage": {"input_tokens": 1000,
                         "output_tokens": 200,
                         "duration_ms": 5000}}
        if trace is not None:
            out["trace"] = trace
        return out
    return _stub, calls


def _cfg():
    return SimpleNamespace(node_name="fake-node",
                            anthropic_api_key="sk-test",
                            cgit_trees=cgit.DEFAULT_TREES)


def test_prepare_handler_emits_a_schema_valid_prepared_record(monkeypatch):
    """The happy path: Claude returns a well-formed JSON body, the
       handler wraps it in the header (task_type, worker_id, model,
       usage) plus outcome=prepared + self_review_record, and the
       result passes the completion-record schema."""
    stub, _calls = _fake_call_claude(json.dumps(_STUB_PREPARE_BODY))
    monkeypatch.setattr("node.ai.call_claude", stub)
    record = tasks.handle_prepare_task(_cfg(), None, _PREPARE_CLAIM)
    assert record["task_type"] == "prepare"
    assert record["outcome"]   == "prepared"
    assert record["worker_id"] == "fake-node"
    assert record["model"]     == "claude-opus-4-7"
    assert record["usage"]["input_tokens"] == 1000
    assert record["subsystem"] == {"primary": "drivers/gpu/drm/msm",
                                    "secondary": [], "cross_cutting": False,
                                    "uncertain_paths": [], "source": "thread"}
    _validate_record(record)         # hone-core's gate, run here too


def test_prepare_handler_threads_principles_and_guidance_into_system(monkeypatch):
    """The system prompt carries the cross-operation principles +
       the prepare operation guidance. The user prompt carries the
       patchset JSON + the return contract. Verifying both means a
       future refactor that drops one block from the prompt is caught
       here."""
    stub, calls = _fake_call_claude(json.dumps(_STUB_PREPARE_BODY))
    monkeypatch.setattr("node.ai.call_claude", stub)
    tasks.handle_prepare_task(_cfg(), None, _PREPARE_CLAIM)
    assert len(calls) == 1
    system = calls[0]["system"]
    user_text = calls[0]["user_text"]
    # Principles + guidance both reached the model in the system prompt.
    assert "GOVERNING PRINCIPLES" in system
    assert "Trailers are not evidence" in system
    assert "PREPARE OPERATION GUIDANCE" in system
    assert "Characterise the patchset." in system
    # Payload + return contract both reached the model in the user msg.
    assert "drm/msm/dpu: fix mismatch" in user_text
    assert "RETURN CONTRACT" in user_text
    assert "Return raw JSON only" in user_text


def test_prepare_handler_omits_thread_messages_from_the_user_payload(
        monkeypatch):
    """The thread_messages list (review comments + replies) is shipped
       in the claim payload by hone-core but NOT forwarded to Claude
       — keeps the prepare prompt compact and avoids burning thousands
       of tokens on review history the current node revision doesn't
       use authoritatively. Re-add when prepare's review-intensity
       classification is wired against real thread data."""
    stub, calls = _fake_call_claude(json.dumps(_STUB_PREPARE_BODY))
    monkeypatch.setattr("node.ai.call_claude", stub)
    claim = dict(_PREPARE_CLAIM)
    claim["thread_messages"] = [
        {"message_id": "c-1@x", "body": "REVIEWER-COMMENT-SHOULD-NOT-LEAK",
         "author_email": "rev@x", "in_reply_to": "p1@x"}]
    tasks.handle_prepare_task(_cfg(), None, claim)
    user_text = calls[0]["user_text"]
    assert "REVIEWER-COMMENT-SHOULD-NOT-LEAK" not in user_text
    assert "thread_messages" not in user_text


def test_prepare_handler_falls_back_to_uncharacterisable_on_bad_json(monkeypatch):
    """Claude is asked for raw JSON only. If it returns prose, a
       markdown-fenced incomplete blob, or otherwise un-parseable
       output, the handler returns an `uncharacterisable` record
       carrying the parser's reason AND Claude's raw response on
       `meta` so the next debugging pass can inspect WHAT the model
       actually produced rather than guessing from just the reason."""
    raw = "Sorry, I couldn't characterise this."
    stub, _calls = _fake_call_claude(raw)
    monkeypatch.setattr("node.ai.call_claude", stub)
    record = tasks.handle_prepare_task(_cfg(), None, _PREPARE_CLAIM)
    assert record["task_type"] == "prepare"
    assert record["outcome"]   == "uncharacterisable"
    assert "JSON" in record["reason"]
    # The uncharacterisable shape must not carry the success-path keys.
    assert "subsystem" not in record
    assert "self_review_record" not in record
    # Raw response stashed on meta so the next failure is debuggable
    # directly from work_items.record in hone-core's DB.
    assert record["meta"]["raw_response"] == raw
    assert record["meta"]["raw_response_length"] == len(raw)
    assert record["meta"]["raw_response_truncated"] is False
    # the trace rides on the uncharacterisable record too (here empty —
    # the stub returned no trace)
    assert record["meta"]["trace"] == []
    _validate_record(record)


def test_prepare_handler_truncates_runaway_raw_responses(monkeypatch):
    """A pathologically long Claude response (model spat out a novel)
       still gets captured on the record, but truncated so
       work_items.record doesn't bloat. The original length is
       preserved alongside the truncated bytes so the truncation
       point is obvious."""
    raw = "x" * 50000          # well past the 20 KiB cap
    stub, _calls = _fake_call_claude(raw)
    monkeypatch.setattr("node.ai.call_claude", stub)
    record = tasks.handle_prepare_task(_cfg(), None, _PREPARE_CLAIM)
    assert record["outcome"] == "uncharacterisable"
    assert record["meta"]["raw_response_length"] == 50000
    assert record["meta"]["raw_response_truncated"] is True
    assert len(record["meta"]["raw_response"]) < 50000
    _validate_record(record)


def test_prepare_handler_submits_record_when_claude_call_fails(monkeypatch):
    """A CallClaudeError (the CLI ran but produced no usable answer —
       a non-auth exit / timeout / a stream with no result event) does NOT
       crash the claim loop. The handler returns a schema-valid
       `uncharacterisable` record carrying the partial agent trace + the
       CLI's failure context, so the attempt lands in the corpus (and the
       Agent-messages UI) as debuggable data."""
    partial_trace = [
        {"step": "assistant_text", "text": "Looking at the patch."},
        {"step": "tool_use", "name": "Bash",
         "input": {"command": "echo $KERNEL_TREE_PATH"}},
        {"step": "tool_result", "chars": 12}]

    def _boom(cfg, system, user_text, *, model=None, max_tokens=None,
              tools=None):
        raise ai.CallClaudeError(
            "`claude` CLI failed (1): kaboom",
            category="other", returncode=1, stderr="kaboom",
            trace=partial_trace, duration_ms=4200,
            model="claude-opus-4-7")

    monkeypatch.setattr("node.ai.call_claude", _boom)
    record = tasks.handle_prepare_task(_cfg(), None, _PREPARE_CLAIM)
    assert record["task_type"] == "prepare"
    assert record["outcome"]   == "uncharacterisable"
    assert "kaboom" in record["reason"]
    assert record["model"] == "claude-opus-4-7"
    assert record["usage"]["duration_ms"] == 4200
    # The success-path fields must be absent on the failure shape.
    assert "subsystem" not in record
    assert "self_review_record" not in record
    # The partial trace + the CLI failure context ride on meta — the trace
    # for the Agent-messages timeline, claude_error for debugging.
    assert [s["step"] for s in record["meta"]["trace"]] == \
        ["assistant_text", "tool_use", "tool_result"]
    assert record["meta"]["claude_error"]["category"] == "other"
    assert record["meta"]["claude_error"]["returncode"] == 1
    assert "kaboom" in record["meta"]["claude_error"]["stderr"]
    _validate_record(record)


def test_prepare_handler_strips_markdown_fences_around_json(monkeypatch):
    """When Claude wraps a valid JSON object in ```json ... ``` fences
       despite the contract, the AI module strips them so the handler
       still sees a parseable body. This is `node.ai`'s job — the
       handler test just confirms the end-to-end shape stays
       prepared-record-valid in that case."""
    # call_claude returns the post-fence-strip text, so we simulate
    # what _strip_fences would yield — JUST the JSON body. This
    # cross-test belongs in test_node_ai.py for the fence stripping
    # itself; here we just confirm a happy outcome reaches the record.
    stub, _calls = _fake_call_claude(json.dumps(_STUB_PREPARE_BODY))
    monkeypatch.setattr("node.ai.call_claude", stub)
    record = tasks.handle_prepare_task(_cfg(), None, _PREPARE_CLAIM)
    assert record["outcome"] == "prepared"


# --- Tier-0 deterministic phase --------------------------------------------

def test_prepare_runs_deterministic_phase_with_no_trailer_offline(monkeypatch):
    """The default claim's patch carries no base-commit trailer, so the
       deterministic phase resolves entirely offline (no cgit probe) and
       stays heuristic — but it still runs: patch_size is code-counted
       (overriding the LLM's), and the resolver version is stamped."""
    stub, _calls = _fake_call_claude(json.dumps(_STUB_PREPARE_BODY))
    monkeypatch.setattr("node.ai.call_claude", stub)
    record = tasks.handle_prepare_task(_cfg(), None, _PREPARE_CLAIM)
    assert record["outcome"] == "prepared"
    # base trailer absent → base fields null / "none"; base_tree present.
    assert record["tree_state"]["base_commit_source"] == "none"
    assert record["tree_state"]["base_in_tree"] is None
    assert record["tree_state"]["base_resolution"] == "no_base"
    assert record["tree_state"]["base_tree"] is None
    # patch_size is code-counted, replacing the LLM's stub values. The
    # claim's patch body has no diff, so everything is zero/trivial.
    assert record["patch_size"]["lines_added"] == 0
    assert record["patch_size"]["bucket"] == "trivial"
    # heuristic subsystem (det source thread) → the LLM's block is kept.
    assert record["subsystem"]["source"] == "thread"
    assert record["meta"]["deterministic_resolver_version"] == \
        tier0.RESOLVER_VERSION
    _validate_record(record)


def test_prepare_calls_claude_with_no_tools(monkeypatch):
    """prepare is tree-free, so it must gate the CLI to no tools (tools=[])
       — the model can't run Bash/git to probe for a kernel tree."""
    stub, calls = _fake_call_claude(json.dumps(_STUB_PREPARE_BODY))
    monkeypatch.setattr("node.ai.call_claude", stub)
    tasks.handle_prepare_task(_cfg(), None, _PREPARE_CLAIM)
    assert calls[0]["tools"] == []


def test_prepare_record_carries_a_capped_claude_trace(monkeypatch):
    """The streamed assistant/tool trace is lifted into meta.trace, capped:
       each assistant_text truncated to the cap with its original length
       kept in `chars`. The record (with the trace) is schema-valid."""
    trace = [{"step": "assistant_text", "text": "x" * 5000},
             {"step": "tool_use", "id": "t1", "name": "Read",
              "input": {"file_path": "drivers/net/foo.c"}},
             {"step": "tool_result", "id": "t1", "chars": 3210}]
    stub, _calls = _fake_call_claude(json.dumps(_STUB_PREPARE_BODY),
                                     trace=trace)
    monkeypatch.setattr("node.ai.call_claude", stub)
    record = tasks.handle_prepare_task(_cfg(), None, _PREPARE_CLAIM)
    t = record["meta"]["trace"]
    assert [s["step"] for s in t] == ["assistant_text", "tool_use",
                                      "tool_result"]
    assert len(t[0]["text"]) == tasks._TRACE_TEXT_CAP   # truncated
    assert t[0]["chars"] == 5000                        # original length kept
    assert t[1]["name"] == "Read"
    assert t[1]["input"]["file_path"] == "drivers/net/foo.c"
    _validate_record(record)


def test_prepare_overlays_authoritative_deterministic_fields(monkeypatch):
    """When the resolver returns an authoritative (source 'tree') result,
       the merge replaces the LLM's subsystem + maintainer blocks and
       overlays the base_* tree_state fields — code wins for the fields
       it owns, the LLM keeps patch_type / review_intensity / notes."""
    stub, _calls = _fake_call_claude(json.dumps(_STUB_PREPARE_BODY))
    monkeypatch.setattr("node.ai.call_claude", stub)
    det = {
        "base_in_tree": True, "base_resolution": "found", "base_tree": "mainline",
        "base_fallback": None,
        "base_commit_declared": "abc123", "base_commit_source": "trailer",
        "subsystem": {"primary": "EXT4 FILE SYSTEM", "primary_status": None,
                      "primary_tree": None, "secondary": [],
                      "cross_cutting": False, "uncertain_paths": [],
                      "source": "tree"},
        "maintainer": {"primary": "tytso@mit.edu", "primary_role": "maintainer",
                       "authoritative_set": [{"email": "tytso@mit.edu",
                                               "name": "Ted", "role": "maintainer"}],
                       "authoritative_reviewer_set": [], "mailing_lists": [],
                       "cc_coverage": None, "list_coverage": None,
                       "engagement_rate": None, "out_of_scope_engaged": [],
                       "all_engaged": [], "cc_list_size": None,
                       "primary_uncertain_reason": None, "source": "tree"},
        "patch_size": _STUB_PREPARE_BODY["patch_size"],
        "resolver_version": tier0.RESOLVER_VERSION,
    }
    monkeypatch.setattr(tier0, "resolve_deterministic",
                         lambda *a, **k: det)
    record = tasks.handle_prepare_task(_cfg(), None, _PREPARE_CLAIM)
    # code-owned fields come from the resolver, not the LLM stub
    assert record["subsystem"]["primary"] == "EXT4 FILE SYSTEM"
    assert record["subsystem"]["source"] == "tree"
    assert record["maintainer"]["authoritative_set"][0]["email"] == \
        "tytso@mit.edu"
    assert record["tree_state"]["base_in_tree"] is True
    assert record["tree_state"]["base_resolution"] == "found"
    assert record["tree_state"]["base_tree"] == "mainline"
    assert record["tree_state"]["base_commit_declared"] == "abc123"
    # LLM-owned judgment fields are untouched
    assert record["patch_type"]["primary"] == "bugfix"
    assert record["review_intensity"]["bucket_overall"] == "none"
    _validate_record(record)


def test_prepare_records_no_base_fallback_from_subject(monkeypatch):
    """No base trailer + a net-next subject prefix + a sent time → the
       prepare record carries a tip-at-submission fallback hint the review
       task can resolve. Threads patchset.subject/sent through to Tier-0;
       no cgit probe happens (no declared base)."""
    stub, _calls = _fake_call_claude(json.dumps(_STUB_PREPARE_BODY))
    monkeypatch.setattr("node.ai.call_claude", stub)
    claim = dict(_PREPARE_CLAIM)
    claim["patchset"] = {**_PREPARE_CLAIM["patchset"],
                         "subject": "[PATCH net-next v2] net: stmmac: shrink",
                         "sent": 1773000000}
    record = tasks.handle_prepare_task(_cfg(), None, claim)
    assert record["tree_state"]["base_resolution"] == "no_base"
    assert record["tree_state"]["base_fallback"] == {
        "tree": "net-next", "strategy": "tip-at-submission",
        "as_of": 1773000000}
    _validate_record(record)
