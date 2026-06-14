"""Tests for node/tasks.py — the claim → handler dispatch table and the
handlers themselves. The AI call is monkeypatched (`node.ai.call_claude`)
so the tests stay hermetic; the completion records the handlers emit
are validated against common/schema/completion-record.schema.yaml so a
shape regression is caught here, before submission to hone-core."""
import json
import os
import subprocess
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


def test_supported_task_types_all_have_handlers():
    """Every advertised capability must have a registered handler — so the
       node never tells hone-core it can do work that has no handler at all.
       (The remaining HANDLERS are NotImplementedError stubs, deliberately
       NOT in SUPPORTED_TASK_TYPES.)"""
    assert set(tasks.SUPPORTED_TASK_TYPES) <= set(tasks.HANDLERS)
    assert "prepare" in tasks.SUPPORTED_TASK_TYPES


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
              tools=None, cwd=None):
        calls.append({"system": system, "user_text": user_text,
                       "model": model, "max_tokens": max_tokens,
                       "tools": tools, "cwd": cwd})
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


_PATCH_WITH_DIFF = (
    "From: alice@example.com\n"
    "Subject: [PATCH v2 03/16] drm/msm: add PERFCNTR_CONFIG ioctl\n"
    "\n"
    "This adds the PERFCNTR_CONFIG ioctl so userspace can program counters.\n"
    "\n"
    "Signed-off-by: Alice <alice@example.com>\n"
    "---\n"
    " drivers/gpu/drm/msm/msm_drv.c | 42 +++++++++++\n"
    " 1 file changed, 42 insertions(+)\n"
    "\n"
    "diff --git a/drivers/gpu/drm/msm/msm_drv.c b/drivers/gpu/drm/msm/msm_drv.c\n"
    "index abc..def 100644\n"
    "--- a/drivers/gpu/drm/msm/msm_drv.c\n"
    "+++ b/drivers/gpu/drm/msm/msm_drv.c\n"
    "@@ -1,3 +1,45 @@\n"
    + "+HUNK_LINE_SHOULD_BE_DROPPED\n" * 2000)


def test_slim_patch_body_drops_hunks_keeps_message_and_diffstat():
    """_slim_patch_body cuts at the first `diff --git`, keeping the email
       headers, commit message and the diffstat git format-patch puts before
       it, and shedding the unbounded hunks."""
    slim = tasks._slim_patch_body(_PATCH_WITH_DIFF)
    assert "This adds the PERFCNTR_CONFIG ioctl" in slim        # message kept
    assert "1 file changed, 42 insertions(+)" in slim           # diffstat kept
    assert "drivers/gpu/drm/msm/msm_drv.c | 42" in slim         # diffstat line
    assert "diff --git" not in slim                             # hunks gone
    assert "HUNK_LINE_SHOULD_BE_DROPPED" not in slim
    assert "[diff hunks omitted" in slim
    assert len(slim) < len(_PATCH_WITH_DIFF)                    # actually smaller


def test_slim_patch_body_passes_through_when_no_diff():
    """A message-only body (cover letter, or a patch with no hunks) has no
       `diff --git`, so it's returned unchanged."""
    body = "From: a@x\nSubject: [PATCH 0/3] cover\n\nSeries overview, no diff.\n"
    assert tasks._slim_patch_body(body) == body
    assert tasks._slim_patch_body(None) is None


def test_prepare_user_text_drops_diff_hunks(monkeypatch):
    """End to end: the prepare prompt carries the message + diffstat but not
       the raw hunks — the fix for the 'Prompt is too long' overflow on large
       series (e.g. the 16-patch drm/msm PERFCNTR_CONFIG set)."""
    claim = dict(_PREPARE_CLAIM)
    claim["patches"] = [{"message_id": "p1@x", "type": "patch",
                         "part_index": 1, "subject": "x",
                         "body": _PATCH_WITH_DIFF}]
    user_text = tasks._build_prepare_user_text(claim)
    assert "1 file changed, 42 insertions(+)" in user_text      # diffstat kept
    assert "HUNK_LINE_SHOULD_BE_DROPPED" not in user_text       # hunks dropped
    assert "[diff hunks omitted" in user_text
    assert "=== RETURN CONTRACT ===" in user_text               # contract intact


def test_prepare_user_text_backstop_cap_preserves_contract():
    """A pathological payload (huge commit message, no diff to shed) is
       truncated by the backstop cap — and the truncation happens before the
       return contract is appended, so the contract always survives."""
    claim = dict(_PREPARE_CLAIM)
    claim["patches"] = [{"message_id": "p1@x", "type": "patch",
                         "part_index": 1, "subject": "x",
                         "body": "Subject: x\n\n" + "A" * 700000}]  # > 600K cap
    user_text = tasks._build_prepare_user_text(claim)
    assert "[payload truncated to fit the model context]" in user_text
    assert "=== RETURN CONTRACT ===" in user_text
    assert "Return raw JSON only" in user_text                  # the contract body


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


# --- review handler -------------------------------------------------------

# A review claim as core/api.py:_build_review_payload compiles it: the full
# `core` slice + operations.review.{guidance, return}, the patchset with a
# resolved base_commit, the prepare metadata (carrying the base_tree hint),
# and the patch messages (raw lore emails).
_REVIEW_CLAIM = {
    "claim_id":            "c-2",
    "task_type":           "review",
    "lease_expires_at":    1779360000,
    "methodology_version": 7,
    "methodology": {
        "core": {
            "principles": [_PRINCIPLE],
            "stages": [{"id": "2", "title": "Manual semantic review",
                         "applies": "Any patch touching C code.",
                         "body": "Build the call graph; apply the checks."}],
            "checks": [{"id": "concurrency", "title": "Concurrency / locking",
                         "body": "Search the whole driver for the field."}],
            "report_finalization": {
                "body": "Severity is impact x reachability.",
                "severity_scale": {"levels": [
                    {"tag": "major", "blocks_merge": True,
                     "meaning": "A genuine functional defect."}]}}},
        "operations": {"review": {
            "guidance": "Drive the methodology over the applied tree.",
            "return":   "Return raw JSON only — no prose, no fences."}},
    },
    "patchset": {
        "root_message_id": "r2@x",
        "subject":         "[PATCH] drm/msm: fix locking",
        "base_commit":     "abc123def456",
        "n_patches":       1},
    "patchset_metadata": {"tree_state": {"base_tree": "mainline"}},
    "patches": [{"message_id": "p2@x",
                  "type":       "patch",
                  "part_index": 1,
                  "subject":    "[PATCH] drm/msm: fix locking",
                  "body":       "From: a@x\nSubject: [PATCH] drm/msm: fix\n\n"
                                "diff --git a/x.c b/x.c\n@@ -1 +1 @@\n-a\n+b\n"}],
}


# A schema-valid `reviewed` body as Claude would return it (one concern +
# the required self_review_record).
_STUB_REVIEW_BODY = {
    "concerns": [{
        "concern_id":            "rev-c-1",
        "stage_id":              "2",
        "candidate_or_check_id": "concurrency",
        "text":                  "Field foo is read without holding the lock.",
        "severity":              "major",
        "is_preexisting":        False,
        "patch_scope":           {"kind": "patch", "patches": ["p2@x"],
                                   "spans_lines_in_diff": [1, 1]},
        "locations":             [{"file": "x.c",
                                    "function_symbol": "foo_probe"}]}],
    "self_review_record": {"summary": "checked 1 concern; held up",
                            "challenges": []},
}


def _review_cfg(tmp_path, backend="cli"):
    return SimpleNamespace(node_name="fake-node",
                            anthropic_api_key="sk-test",
                            anthropic_model="claude-sonnet-4-6",
                            claude_backend=backend,
                            scratch_dir=str(tmp_path),
                            cgit_trees=cgit.DEFAULT_TREES)


def _stub_refrepo(monkeypatch):
    """Stub refrepo so review tests stay hermetic (no real git). Returns the
       list cleanup() is called with, so a test can assert teardown."""
    cleaned = []
    monkeypatch.setattr("node.refrepo.prepare",
                        lambda base, wt, base_tree=None: (wt, "fetched"))
    monkeypatch.setattr("node.refrepo.cleanup",
                        lambda *wts: cleaned.extend(wts))
    return cleaned


def test_review_handler_emits_schema_valid_reviewed_record(monkeypatch,
                                                            tmp_path):
    cleaned = _stub_refrepo(monkeypatch)
    monkeypatch.setattr("node.tasks._apply_series",
                        lambda wt, patches: (True, None))
    stub, calls = _fake_call_claude(json.dumps(_STUB_REVIEW_BODY))
    monkeypatch.setattr("node.ai.call_claude", stub)

    record = tasks.handle_review_task(_review_cfg(tmp_path), None,
                                      _REVIEW_CLAIM)

    _validate_record(record)
    assert record["outcome"] == "reviewed"
    assert record["concerns"][0]["candidate_or_check_id"] == "concurrency"
    assert record["self_review_record"]["summary"]
    # agentic: read-only tools, rooted in the prepared worktree
    assert calls[0]["tools"] == tasks._REVIEW_TOOLS
    assert calls[0]["cwd"] and calls[0]["cwd"].endswith("review-r2_x")
    # the methodology rode in the system prompt; the diffs in the user msg
    assert "Concurrency / locking" in calls[0]["system"]
    assert "diff --git" in calls[0]["user_text"]
    # each diff header carries the patch's Message-Id — the value the
    # model must cite in patch_scope.patches (it appears nowhere else)
    assert "Message-Id: p2@x" in calls[0]["user_text"]
    assert cleaned == [calls[0]["cwd"]]          # worktree torn down


# The off-contract shapes from the 2026-06-11 schema rejection: the model
# paraphrased the challenge fields (challenge/response/confirmed instead
# of challenge_kind/challenge_text/outcome_note/upheld) and dropped the
# required patch_scope.patches on a series-scoped concern.
_OFF_CONTRACT_REVIEW_BODY = {
    "concerns": [{
        "concern_id":            "rev-c-1",
        "stage_id":              "2",
        "candidate_or_check_id": "concurrency",
        "text":                  "Field foo is read without holding the lock.",
        "severity":              "major",
        "is_preexisting":        False,
        "patch_scope":           {"kind": "series"},
        "locations":             [{"file": "x.c",
                                    "function_symbol": "foo_probe"}]}],
    "self_review_record": {"summary": "checked 1 concern; held up",
                            "challenges": [
                                {"target_kind": "stage3",
                                 "challenge":   "would this survive?",
                                 "response":    "yes — verified by reading.",
                                 "outcome":     "confirmed"}]},
}

_REPAIRED_REVIEW_BODY = {
    "concerns": [{**_OFF_CONTRACT_REVIEW_BODY["concerns"][0],
                  "patch_scope": {"kind": "series", "patches": ["p2@x"]}}],
    "self_review_record": {"summary": "checked 1 concern; held up",
                            "challenges": [
                                {"target_kind":    "stage3",
                                 "target_id":      "stage3-1",
                                 "challenge_kind": "evidence_check",
                                 "challenge_text": "would this survive?",
                                 "outcome":        "upheld",
                                 "outcome_note":   "yes — verified by "
                                                   "reading."}]},
}


def _sequenced_call_claude(texts):
    """A call_claude stub returning each of `texts` in turn — the review
       turn, then the repair turn. Captures every call's kwargs."""
    calls = []
    def _stub(cfg, system, user_text, *, model=None, max_tokens=None,
              tools=None, cwd=None):
        calls.append({"system": system, "user_text": user_text,
                       "tools": tools, "cwd": cwd})
        return {"text":  texts[min(len(calls), len(texts)) - 1],
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 1000, "output_tokens": 200,
                          "duration_ms": 5000}}
    return _stub, calls


def test_review_handler_repairs_off_contract_record(monkeypatch, tmp_path):
    """A record that would 422 at hone-core is caught node-side and fixed
       by one no-tools repair turn: the submitted record is schema-valid,
       carries the summed usage of both turns, and notes the repair in
       meta. The review content survives verbatim."""
    _stub_refrepo(monkeypatch)
    monkeypatch.setattr("node.tasks._apply_series",
                        lambda wt, patches: (True, None))
    stub, calls = _sequenced_call_claude(
        [json.dumps(_OFF_CONTRACT_REVIEW_BODY),
         json.dumps(_REPAIRED_REVIEW_BODY)])
    monkeypatch.setattr("node.ai.call_claude", stub)

    record = tasks.handle_review_task(_review_cfg(tmp_path), None,
                                      _REVIEW_CLAIM)

    _validate_record(record)
    assert record["outcome"] == "reviewed"
    assert record["concerns"][0]["text"] == \
        "Field foo is read without holding the lock."
    assert record["usage"]["input_tokens"] == 2000          # both turns
    assert record["meta"]["schema_repair"]["errors"]        # what was wrong
    # The repair turn: no tools, and it carried the validator's errors,
    # the model's own JSON, and the binding schema.
    assert len(calls) == 2
    assert calls[1]["tools"] == []
    assert "VALIDATOR ERRORS" in calls[1]["user_text"]
    assert "'patches' is a required property" in calls[1]["user_text"]
    assert "self_review_challenge" not in calls[1]["user_text"]  # resolved
    assert '"upheld"' in calls[1]["user_text"]               # schema inlined
    # the authoritative citation table rode along, so the repair turn can
    # fill patch_scope.patches with real Message-Ids
    assert "SERIES PATCHES" in calls[1]["user_text"]
    assert "part 1: p2@x" in calls[1]["user_text"]


def test_review_handler_repairs_placeholder_patch_citations(monkeypatch,
                                                             tmp_path):
    """The 2026-06-12 rejection's other half: a reviewer that never saw
       the series' Message-Ids cites `<patch-...>` placeholders. Those
       PASS the schema (any non-empty string) but hone-core's UI anchors
       concerns to patches by Message-Id, so they silently dump every
       per-patch finding into the series-wide bucket. The node must
       treat an off-claim citation as repairable and only submit once
       the citation resolves to a real patch."""
    _stub_refrepo(monkeypatch)
    monkeypatch.setattr("node.tasks._apply_series",
                        lambda wt, patches: (True, None))
    placeholder_body = {
        **_STUB_REVIEW_BODY,
        "concerns": [{**_STUB_REVIEW_BODY["concerns"][0],
                      "patch_scope": {"kind": "patch",
                                       "patches": ["<patch-drm-locking>"],
                                       "spans_lines_in_diff": [1, 1]}}]}
    stub, calls = _sequenced_call_claude(
        [json.dumps(placeholder_body), json.dumps(_STUB_REVIEW_BODY)])
    monkeypatch.setattr("node.ai.call_claude", stub)

    record = tasks.handle_review_task(_review_cfg(tmp_path), None,
                                      _REVIEW_CLAIM)

    _validate_record(record)
    assert record["outcome"] == "reviewed"
    assert record["concerns"][0]["patch_scope"]["patches"] == ["p2@x"]
    assert len(calls) == 2                                   # repair ran
    assert "not the Message-Id of any patch" in \
        record["meta"]["schema_repair"]["errors"][0]
    # the repair prompt named the offending value and the valid table
    assert "<patch-drm-locking>" in calls[1]["user_text"]
    assert "part 1: p2@x" in calls[1]["user_text"]


def test_review_handler_strips_null_optional_fields(monkeypatch, tmp_path):
    """The 2026-06-13 rejection: the model wrote `"contributing_check_ids":
       null` for "absent" on an optional array field — a type violation
       hone-core 422s. Null-valued optional keys are dropped
       deterministically before validation (no repair turn spent);
       schema-nullable fields like spans_lines_in_diff keep their null."""
    _stub_refrepo(monkeypatch)
    monkeypatch.setattr("node.tasks._apply_series",
                        lambda wt, patches: (True, None))
    null_body = {
        **_STUB_REVIEW_BODY,
        "concerns": [{**_STUB_REVIEW_BODY["concerns"][0],
                      "contributing_check_ids": None,
                      "patch_scope": {"kind": "patch",
                                       "patches": ["p2@x"],
                                       "spans_lines_in_diff": None}}]}
    stub, calls = _fake_call_claude(json.dumps(null_body))
    monkeypatch.setattr("node.ai.call_claude", stub)

    record = tasks.handle_review_task(_review_cfg(tmp_path), None,
                                      _REVIEW_CLAIM)

    _validate_record(record)
    assert record["outcome"] == "reviewed"
    assert len(calls) == 1                                   # no repair turn
    concern = record["concerns"][0]
    assert "contributing_check_ids" not in concern           # null → absent
    assert concern["patch_scope"]["spans_lines_in_diff"] is None  # kept


def test_review_handler_accepts_bracketed_citations(monkeypatch, tmp_path):
    """Lore renders Message-Ids both bare and <wrapped>; hone-core's UI
       normalises before anchoring (core_db.norm_msgid). A citation that
       differs only by wrapping <> or case is the SAME patch — no repair
       turn, submit as-is."""
    _stub_refrepo(monkeypatch)
    monkeypatch.setattr("node.tasks._apply_series",
                        lambda wt, patches: (True, None))
    bracketed_body = {
        **_STUB_REVIEW_BODY,
        "concerns": [{**_STUB_REVIEW_BODY["concerns"][0],
                      "patch_scope": {"kind": "patch",
                                       "patches": ["<P2@x>"],
                                       "spans_lines_in_diff": [1, 1]}}]}
    stub, calls = _fake_call_claude(json.dumps(bracketed_body))
    monkeypatch.setattr("node.ai.call_claude", stub)

    record = tasks.handle_review_task(_review_cfg(tmp_path), None,
                                      _REVIEW_CLAIM)

    _validate_record(record)
    assert record["outcome"] == "reviewed"
    assert len(calls) == 1                                   # no repair


def test_review_handler_defers_when_repair_does_not_converge(monkeypatch,
                                                              tmp_path):
    """A repair turn that returns a still-invalid body must not be
       submitted (hone-core would 422 → the runner's TERMINAL unappliable
       fallback would bury the review). Defer instead — re-arm — with the
       validator's errors as evidence."""
    _stub_refrepo(monkeypatch)
    monkeypatch.setattr("node.tasks._apply_series",
                        lambda wt, patches: (True, None))
    stub, calls = _sequenced_call_claude(
        [json.dumps(_OFF_CONTRACT_REVIEW_BODY),
         json.dumps(_OFF_CONTRACT_REVIEW_BODY)])    # repair changes nothing
    monkeypatch.setattr("node.ai.call_claude", stub)

    record = tasks.handle_review_task(_review_cfg(tmp_path), None,
                                      _REVIEW_CLAIM)

    _validate_record(record)
    assert record["outcome"] == "deferred"
    assert "contract" in record["reason"]
    assert record["meta"]["schema_errors"]
    assert len(calls) == 2
    assert record["usage"]["input_tokens"] == 2000          # both turns


def test_review_handler_unappliable_when_series_wont_apply(monkeypatch,
                                                            tmp_path):
    _stub_refrepo(monkeypatch)
    monkeypatch.setattr("node.tasks._apply_series",
                        lambda wt, patches: (False, "git am failed: conflict"))
    monkeypatch.setattr(
        "node.ai.call_claude",
        lambda *a, **k: pytest.fail("claude must not run on an unappliable "
                                    "series"))

    record = tasks.handle_review_task(_review_cfg(tmp_path), None,
                                      _REVIEW_CLAIM)

    _validate_record(record)
    assert record["outcome"] == "unappliable"
    assert "conflict" in record["reason"]
    assert "concerns" not in record


def test_review_handler_deferred_when_no_base(tmp_path):
    """No declared base, no recorded fallback, and no submission time to
       anchor the linux-next default → defer (nothing to stage against).
       _REVIEW_CLAIM's patchset carries no `sent`."""
    claim = dict(_REVIEW_CLAIM)
    claim["patchset"] = dict(claim["patchset"])
    claim["patchset"]["base_commit"] = None        # no fallback, no sent

    record = tasks.handle_review_task(_review_cfg(tmp_path), None, claim)

    _validate_record(record)
    assert record["outcome"] == "deferred"
    assert "base_commit" in record["reason"]


def test_review_handler_defaults_to_linux_next_tip_when_no_fallback(
        monkeypatch, tmp_path):
    """No declared base and no recorded base_fallback, but the patchset has
       a submission time: the handler resolves linux-next's tip-at-submission
       as a last resort and reviews against it."""
    seen = {}
    monkeypatch.setattr(
        "node.refrepo.resolve_tip",
        lambda tree, as_of: seen.update(tree=tree, as_of=as_of)
        or "beef00ddcafe")
    prepared = {}
    monkeypatch.setattr(
        "node.refrepo.prepare",
        lambda base, wt, base_tree=None: prepared.update(
            base=base, base_tree=base_tree) or (wt, "fetched"))
    monkeypatch.setattr("node.refrepo.cleanup", lambda *wts: None)
    monkeypatch.setattr("node.tasks._apply_series",
                        lambda wt, patches: (True, None))
    stub, _ = _fake_call_claude(json.dumps(_STUB_REVIEW_BODY))
    monkeypatch.setattr("node.ai.call_claude", stub)

    claim = dict(_REVIEW_CLAIM)
    claim["patchset"] = {**claim["patchset"], "base_commit": None,
                          "sent": 1_700_000_000}
    claim["patchset_metadata"] = {"tree_state": {"base_resolution": "no_base"}}

    record = tasks.handle_review_task(_review_cfg(tmp_path), None, claim)

    _validate_record(record)
    assert record["outcome"] == "reviewed"
    assert seen == {"tree": "linux-next", "as_of": 1_700_000_000}
    assert prepared == {"base": "beef00ddcafe", "base_tree": "linux-next"}


def _no_base_with_fallback_claim(tree="net-next", as_of=1_700_000_000):
    """A review claim with no declared base but a recorded tip-at-submission
       fallback — the state the resolve_tip path acts on."""
    claim = dict(_REVIEW_CLAIM)
    claim["patchset"] = {**claim["patchset"], "base_commit": None}
    claim["patchset_metadata"] = {"tree_state": {
        "base_resolution": "no_base",
        "base_fallback": {"tree": tree, "strategy": "tip-at-submission",
                          "as_of": as_of}}}
    return claim


def test_review_handler_resolves_tip_at_submission_fallback(monkeypatch,
                                                            tmp_path):
    """No declared base, but the fallback resolves: the handler turns the
       tip-at-submission hint into a concrete commit, stages it (passing the
       fallback tree as the fetch hint), and reviews normally."""
    prepared = {}
    monkeypatch.setattr("node.refrepo.resolve_tip",
                        lambda tree, as_of: "fa11bacc0de0")
    monkeypatch.setattr(
        "node.refrepo.prepare",
        lambda base, wt, base_tree=None: prepared.update(
            base=base, base_tree=base_tree) or (wt, "fetched"))
    monkeypatch.setattr("node.refrepo.cleanup", lambda *wts: None)
    monkeypatch.setattr("node.tasks._apply_series",
                        lambda wt, patches: (True, None))
    stub, calls = _fake_call_claude(json.dumps(_STUB_REVIEW_BODY))
    monkeypatch.setattr("node.ai.call_claude", stub)

    record = tasks.handle_review_task(_review_cfg(tmp_path), None,
                                      _no_base_with_fallback_claim())

    _validate_record(record)
    assert record["outcome"] == "reviewed"
    # staged the resolved tip, hinting the fallback tree to the fetcher
    assert prepared == {"base": "fa11bacc0de0", "base_tree": "net-next"}


def test_review_handler_defers_when_fallback_unresolvable(monkeypatch,
                                                          tmp_path):
    """No declared base and the fallback can't be resolved (tree unreachable
       / no commit predates submission) → defer, never call Claude."""
    monkeypatch.setattr("node.refrepo.resolve_tip", lambda tree, as_of: None)
    monkeypatch.setattr(
        "node.ai.call_claude",
        lambda *a, **k: pytest.fail("claude must not run without a base"))

    record = tasks.handle_review_task(_review_cfg(tmp_path), None,
                                      _no_base_with_fallback_claim())

    _validate_record(record)
    assert record["outcome"] == "deferred"
    assert "fallback" in record["reason"]


def test_review_handler_deferred_when_base_unobtainable(monkeypatch,
                                                         tmp_path):
    def _boom(base, wt, base_tree=None):
        raise RuntimeError(f"base {base} not found in any remote")
    monkeypatch.setattr("node.refrepo.prepare", _boom)

    record = tasks.handle_review_task(_review_cfg(tmp_path), None,
                                      _REVIEW_CLAIM)

    _validate_record(record)
    assert record["outcome"] == "deferred"
    assert "unobtainable" in record["reason"]


def test_review_failure_record_has_a_valid_model_when_unset(tmp_path):
    """A node with no ANTHROPIC_MODEL (cfg.anthropic_model == "") must still
       emit a schema-valid failure record — `model` has minLength: 1, so an
       empty model would 422 and wedge the node on its own failure report
       (the sim hit exactly this). _review_failure falls back to
       ai.DEFAULT_MODEL."""
    cfg = _review_cfg(tmp_path)
    cfg.anthropic_model = ""                      # unset, as in the sim
    claim = dict(_REVIEW_CLAIM)
    claim["patchset"] = dict(claim["patchset"])
    claim["patchset"]["base_commit"] = None       # defer before any AI call

    record = tasks.handle_review_task(cfg, None, claim)

    _validate_record(record)                      # would raise on model: ""
    assert record["outcome"] == "deferred"
    assert record["model"] == ai.DEFAULT_MODEL and record["model"]


def test_review_handler_requires_cli_backend(tmp_path):
    # Agentic review needs the CLI tool surface; an sdk-backend node that
    # somehow claims review work must fail loudly, not produce a tree-blind
    # "review".
    with pytest.raises(RuntimeError, match="cli"):
        tasks.handle_review_task(_review_cfg(tmp_path, backend="sdk"),
                                 None, _REVIEW_CLAIM)


def test_review_handler_defers_on_offcontract_response(monkeypatch, tmp_path):
    _stub_refrepo(monkeypatch)
    monkeypatch.setattr("node.tasks._apply_series",
                        lambda wt, patches: (True, None))
    # concerns present but self_review_record missing → off-contract; the
    # handler defers (re-arm) rather than emit a record hone-core would 422.
    stub, _calls = _fake_call_claude(json.dumps({"concerns": []}))
    monkeypatch.setattr("node.ai.call_claude", stub)

    record = tasks.handle_review_task(_review_cfg(tmp_path), None,
                                      _REVIEW_CLAIM)

    _validate_record(record)
    assert record["outcome"] == "deferred"
    assert "self_review_record" in record["reason"]


# --- _apply_series: the real git am framing (no monkeypatch) ---------------

def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "-C", cwd, *args], check=True, capture_output=True,
                   text=True)


def _authored_series(src, n):
    """Author `n` sequential commits on top of a base in `src`, then return
       their raw RFC 5322 email bodies — exactly the shape lore stores and
       the claim payload carries (no mbox 'From ' separator line)."""
    _git("init", "-q", cwd=src)
    open(os.path.join(src, "f.txt"), "w").write("L0\n")
    _git("add", "f.txt", cwd=src)
    _git("commit", "-qm", "base", cwd=src)
    for i in range(1, n + 1):
        open(os.path.join(src, "f.txt"), "w").write(
            "L0\n" + "".join(f"ADD{j}\n" for j in range(1, i + 1)))
        _git("commit", "-qam", f"patch {i}", cwd=src)
    out = subprocess.run(
        ["git", "-C", src, "format-patch", f"-{n}", "--stdout"],
        check=True, capture_output=True, text=True).stdout
    # Split format-patch's mbox stream back into per-message raw bodies,
    # dropping the leading mbox "From <sha> …" line so each looks like the
    # raw email the node receives in its claim payload.
    bodies = []
    for chunk in out.split("\nFrom "):
        chunk = chunk if chunk.startswith("From ") else "From " + chunk
        bodies.append(chunk.split("\n", 1)[1])
    return bodies


def test_apply_series_applies_every_patch_in_a_multi_patch_series(tmp_path):
    """Regression: the bodies are raw emails with no mbox separators, so a
       naive newline-join is one message to `git am` — only patch 1 applies
       and the rest are silently dropped. _apply_series must frame them as
       an mbox so all N patches land."""
    src = str(tmp_path / "src")
    os.mkdir(src)
    bodies = _authored_series(src, 3)
    patches = [{"type": "patch", "part_index": i + 1, "body": b}
               for i, b in enumerate(bodies)]

    wt = str(tmp_path / "wt")
    os.mkdir(wt)
    _git("init", "-q", cwd=wt)
    open(os.path.join(wt, "f.txt"), "w").write("L0\n")
    _git("add", "f.txt", cwd=wt)
    _git("commit", "-qm", "base", cwd=wt)

    ok, reason = tasks._apply_series(wt, patches)
    assert ok, reason
    log = subprocess.run(["git", "-C", wt, "log", "--oneline"],
                         capture_output=True, text=True).stdout
    for i in (1, 2, 3):
        assert f"patch {i}" in log, f"patch {i} missing — only some applied"
    # the cumulative effect of all three patches is on disk
    assert open(os.path.join(wt, "f.txt")).read() == "L0\nADD1\nADD2\nADD3\n"
