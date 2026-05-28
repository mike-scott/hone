"""Tests for common/schema/completion-record.schema.yaml — the JSON Schema
for the body of POST /v1/claims/{id}/result. Validates the four branches
(prepare, review, train, draft), discriminated by `task_type`."""
import os

import jsonschema
import pytest
import yaml

_SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "..",
    "common", "schema", "completion-record.schema.yaml")


@pytest.fixture(scope="module")
def schema():
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        s = yaml.safe_load(f)
    jsonschema.Draft202012Validator.check_schema(s)
    return s


def _validate(schema, record):
    jsonschema.validate(record, schema,
                        cls=jsonschema.Draft202012Validator)


def _reject(schema, record):
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, record)


# --- shared fixtures -------------------------------------------------------

_USAGE = {"input_tokens": 1000, "output_tokens": 200, "duration_ms": 45000}
_MODEL = "claude-opus-4-7"

_SELF_REVIEW = {"summary": "no challenges arose", "challenges": []}

_CONCERN = {
    "concern_id":           "rev-c-001",
    "stage_id":             "2",
    "candidate_or_check_id": "object-lifetime",
    "text":                 "use-after-free at frob()",
    "severity":             "critical",
    "is_preexisting":       False,
    "patch_scope":          {"kind":     "patch",
                             "patches": ["<p1@x>"]},
    "locations":            [{"file":              "drivers/foo/bar.c",
                              "function_symbol":   "foo_handle",
                              "code_snippet":      "kfree(x); x->next"}],
}

# A minimal but schema-valid prepare-task metadata payload.
_PREPARE_METADATA = {
    "patchset_id":       "<r1@x>",
    "tree_state":        {"tree_available": True,
                          "base_commit_source": "trailer",
                          "prerequisite_patch_ids": []},
    "subsystem":         {"primary": "drivers/net", "secondary": [],
                          "cross_cutting": False, "uncertain_paths": [],
                          "source": "tree"},
    "patch_size":        {"lines_added": 10, "lines_removed": 2,
                          "files_modified": 1, "files_added": 0,
                          "files_deleted": 0, "files_renamed": 0,
                          "hunks": 1, "bucket": "small", "series_length": 1,
                          "churn_ratio": {"max": None, "mean": None,
                                          "high_churn_file_count": None},
                          "source": "tree"},
    "maintainer":        {"authoritative_set": [],
                          "authoritative_reviewer_set": [],
                          "mailing_lists": [], "cc_list_size": 0,
                          "source": "thread"},
    "patch_type":        {"primary": "bugfix", "secondary": [],
                          "evidence": {"primary": "Fixes: trailer"},
                          "source": "thread"},
    "review_intensity":  {"bucket_overall": "light",
                          "reply_count": 1, "unique_reviewers": 1,
                          "trailer_only_count": 0, "light_count": 1,
                          "substantive_count": 0, "deep_count": 0,
                          "had_nack": False, "had_v_next": False,
                          "per_reply": [], "source": "thread"},
    "preparation_notes": {"warnings": [], "confidence": "medium",
                          "mode": "heuristic"},
}

# A minimal but schema-valid train-task `trained` record. The train_record's
# new shape: per-point matching against the in-scope concerns, candidate /
# check outcomes, derived summary, optional proposals (pool only), and the
# session-link fields (always non-null — every train belongs to a session).
_TRAIN_TRAINED_FIELDS = {
    "training_session_id": "ses-018",
    "session_role":         "pool",
    "stratum_label":        "driver:net · moderate",
    "concerns_considered": [{"concern_id": "rev-c-001",
                              "in_scope_reason": "patch",
                              "is_preexisting": False}],
    "comment_points":      [{"point_id": "p1",
                              "text":     "release order is wrong",
                              "kind":     "correctness",
                              "severity": "major"}],
    "point_matches":       [{"point_id":             "p1",
                              "match_status":         "caught",
                              "match_confidence":     0.9,
                              "addressing_concerns": [
                                  {"concern_id":        "rev-c-001",
                                   "overlap_kind":      "exact",
                                   "overlap_rationale": "same UAF"}],
                              "addressing_candidates": [],
                              "addressing_checks":     ["object-lifetime"]}],
    "candidate_outcomes":  [],
    "check_outcomes":      [{"check_id":             "object-lifetime",
                              "applied":              True,
                              "fired":                True,
                              "fired_concerns":      ["rev-c-001"],
                              "caught_points":       [{"point_id":     "p1",
                                                       "overlap_kind": "exact"}],
                              "false_positive_concerns": [],
                              "redundancy_with":     []}],
    "summary":             {"comment_points_total": 1,
                            "comment_points_by_kind": {"correctness": 1},
                            "match_distribution":
                                {"caught": 1, "partially_caught": 0,
                                 "missed": 0, "not_applicable": 0},
                            "severity_weighted_catch_rate": 1.0,
                            "had_missed_critical": False,
                            "had_missed_major":    False,
                            "candidates_fired":    0,
                            "checks_fired":        1,
                            "new_candidate_proposals": 0},
    "proposals":           [],
    "self_review_record":  _SELF_REVIEW,
}


def _prepare(**fields):
    base = {"task_type": "prepare", "worker_id": "n",
            "model": _MODEL, "usage": _USAGE}
    base.update(fields)
    return base


def _review(**fields):
    base = {"task_type": "review", "worker_id": "n",
            "model": _MODEL, "usage": _USAGE}
    base.update(fields)
    return base


def _train(**fields):
    base = {"task_type": "train", "worker_id": "n",
            "model": _MODEL, "usage": _USAGE}
    base.update(fields)
    return base


def _draft(**fields):
    base = {"task_type": "draft", "worker_id": "n",
            "model": _MODEL, "usage": _USAGE}
    base.update(fields)
    return base


# --- prepare ---------------------------------------------------------------

def test_prepare_prepared_with_full_metadata(schema):
    _validate(schema, _prepare(outcome="prepared",
                                self_review_record=_SELF_REVIEW,
                                **_PREPARE_METADATA))


def test_prepare_uncharacterisable_requires_reason(schema):
    _validate(schema, _prepare(outcome="uncharacterisable",
                                reason="patchset malformed"))
    _reject(schema, _prepare(outcome="uncharacterisable"))


def test_prepare_uncharacterisable_with_metadata_is_rejected(schema):
    _reject(schema, _prepare(outcome="uncharacterisable",
                              reason="x",
                              **_PREPARE_METADATA,
                              self_review_record=_SELF_REVIEW))


def test_prepare_prepared_without_self_review_record_is_rejected(schema):
    _reject(schema, _prepare(outcome="prepared", **_PREPARE_METADATA))


def test_prepare_accepts_null_for_authoritative_sets(schema):
    """In heuristic mode (no reference tree) the authoritative-* sets
       are the MAINTAINERS-file lookup result and have no honest value
       — the methodology directs Claude to emit `null`, not `[]` and
       not a populated list parsed from the To: header. The schema
       accepts either null or an array.

       Tracks audit finding #3: work-item 2 populated
       authoritative_reviewer_set with names lifted from the cover
       letter's To: header in heuristic mode (which is a methodology
       violation), and would have hit a schema rejection if it had
       done the right thing and emitted null."""
    record = _prepare(outcome="prepared",
                       self_review_record=_SELF_REVIEW,
                       **{**_PREPARE_METADATA,
                          "maintainer": {
                              "authoritative_set":           None,
                              "authoritative_reviewer_set":  None,
                              "mailing_lists":               [],
                              "cc_list_size":                0,
                              "source":                      "thread"}})
    _validate(schema, record)


def test_prepare_authoritative_sets_can_be_omitted(schema):
    """They're no longer in `required` (since heuristic mode lets them
       be absent entirely). Empty maintainer block with just the
       still-required fields validates."""
    record = _prepare(outcome="prepared",
                       self_review_record=_SELF_REVIEW,
                       **{**_PREPARE_METADATA,
                          "maintainer": {"mailing_lists": [],
                                          "cc_list_size":  0,
                                          "source":        "thread"}})
    _validate(schema, record)


def test_prepare_accepts_null_for_tree_only_subobjects(schema):
    """In heuristic mode (no reference kernel tree on the node) the
       tree-only sub-objects `patch_size.churn_ratio` and
       `patch_type.file_activity` are honest as bare `null` — the
       node never ran the git queries that fill them. The schema
       accepts either null or the populated shape; the methodology's
       `preparation_notes.mode` field is the load-bearing signal.

       Tracks audit finding #2: prepare runs in heuristic mode were
       being rejected at submit_result time because Claude emitted
       bare null for file_activity while the schema required an
       object."""
    record = _prepare(outcome="prepared",
                       self_review_record=_SELF_REVIEW,
                       **{**_PREPARE_METADATA,
                          "patch_size": {**_PREPARE_METADATA["patch_size"],
                                          "churn_ratio": None,
                                          "source": "thread"},
                          "patch_type": {**_PREPARE_METADATA["patch_type"],
                                          "file_activity": None,
                                          "source": "thread"}})
    _validate(schema, record)


def test_prepare_accepts_base_resolution_outcomes(schema):
    """tree_state.base_resolution records the Tier-0 outcome that
       disambiguates the three reasons base_in_tree is null. Each enum
       value validates; null validates (records predating the field)."""
    for outcome in ("found", "absent", "unknown", "no_base", None):
        record = _prepare(outcome="prepared", self_review_record=_SELF_REVIEW,
                          **{**_PREPARE_METADATA,
                             "tree_state": {**_PREPARE_METADATA["tree_state"],
                                            "base_resolution": outcome}})
        _validate(schema, record)


def test_prepare_rejects_unknown_base_resolution(schema):
    record = _prepare(outcome="prepared", self_review_record=_SELF_REVIEW,
                      **{**_PREPARE_METADATA,
                         "tree_state": {**_PREPARE_METADATA["tree_state"],
                                        "base_resolution": "maybe"}})
    _reject(schema, record)


# --- review ----------------------------------------------------------------

def test_review_reviewed_with_concerns(schema):
    _validate(schema, _review(outcome="reviewed", concerns=[_CONCERN],
                              self_review_record=_SELF_REVIEW))


def test_review_reviewed_with_empty_concerns_is_valid(schema):
    _validate(schema, _review(outcome="reviewed", concerns=[],
                              self_review_record=_SELF_REVIEW))


def test_review_reviewed_without_concerns_is_rejected(schema):
    _reject(schema, _review(outcome="reviewed",
                            self_review_record=_SELF_REVIEW))


def test_review_reviewed_without_self_review_record_is_rejected(schema):
    _reject(schema, _review(outcome="reviewed", concerns=[]))


def test_review_reviewed_with_reason_is_rejected(schema):
    _reject(schema, _review(outcome="reviewed", concerns=[],
                            self_review_record=_SELF_REVIEW,
                            reason="no"))


def test_review_unappliable_requires_reason(schema):
    _validate(schema, _review(outcome="unappliable",
                              reason="base commit not obtainable"))
    _reject(schema, _review(outcome="unappliable"))


def test_review_unappliable_with_concerns_is_rejected(schema):
    _reject(schema, _review(outcome="unappliable", reason="x",
                            concerns=[_CONCERN]))


def test_review_rejects_extra_top_level_keys(schema):
    _reject(schema, _review(outcome="reviewed", concerns=[],
                            self_review_record=_SELF_REVIEW,
                            unknown_key=True))


# --- review enrichment (Tier-2 fields moved from prepare) ------------------

def test_review_reviewed_accepts_tree_state_and_enrichment(schema):
    """The tree-dependent fields moved from prepare — apply result +
       churn / file_activity / fixes_verified — validate on a reviewed
       record (the review task will populate them once implemented)."""
    _validate(schema, _review(
        outcome="reviewed", concerns=[], self_review_record=_SELF_REVIEW,
        tree_state={"applies_cleanly": True, "apply_failure_reason": None},
        enrichment={
            "churn_ratio": {"max": 0.4, "mean": 0.1,
                             "high_churn_file_count": 0},
            "file_activity": {"mean_commits_last_year": 3.0,
                               "max_commits_last_year": 5,
                               "oldest_last_touched_days": 400,
                               "newest_last_touched_days": 10,
                               "submitter_recently_touched_files_ratio": 0.5},
            "fixes_verified": [{"hash": "abc1234", "exists": True,
                                 "age_days": 90, "file_overlap_ratio": 0.8,
                                 "suspicious_reason": None}]}))


def test_review_enrichment_fields_are_optional(schema):
    """A reviewed record without the tree_state / enrichment blocks is
       still valid — they're forward-prep, not yet produced."""
    _validate(schema, _review(outcome="reviewed", concerns=[],
                              self_review_record=_SELF_REVIEW))


def test_review_unappliable_carries_apply_failure(schema):
    """An unappliable review reports applies_cleanly=false + the reason
       in tree_state — exactly the case the moved fields are for."""
    _validate(schema, _review(
        outcome="unappliable", reason="patch 2/3 fails to apply",
        tree_state={"applies_cleanly": False,
                     "apply_failure_reason": "hunk #3 FAILED"}))


def test_review_enrichment_rejects_unknown_subkey(schema):
    _reject(schema, _review(outcome="reviewed", concerns=[],
                            self_review_record=_SELF_REVIEW,
                            enrichment={"not_a_field": 1}))


def test_review_concern_must_carry_locations(schema):
    bad = {**_CONCERN, "locations": []}
    _reject(schema, _review(outcome="reviewed", concerns=[bad],
                            self_review_record=_SELF_REVIEW))


def test_review_concern_severity_enum_is_enforced(schema):
    bad = {**_CONCERN, "severity": "high"}        # not in the 5-level scale
    _reject(schema, _review(outcome="reviewed", concerns=[bad],
                            self_review_record=_SELF_REVIEW))


def test_review_concern_requires_patch_scope(schema):
    bad = {k: v for k, v in _CONCERN.items() if k != "patch_scope"}
    _reject(schema, _review(outcome="reviewed", concerns=[bad],
                            self_review_record=_SELF_REVIEW))


# --- train -----------------------------------------------------------------

def test_train_trained_full_record(schema):
    _validate(schema, _train(outcome="trained", **_TRAIN_TRAINED_FIELDS))


def test_train_trained_requires_session_fields(schema):
    """training_session_id / session_role / stratum_label are required on a
       `trained` record — every train belongs to a session, no NULL-session
       trains."""
    no_session = {k: v for k, v in _TRAIN_TRAINED_FIELDS.items()
                  if k != "training_session_id"}
    _reject(schema, _train(outcome="trained", **no_session))


def test_train_session_role_enum_is_enforced(schema):
    bad = {**_TRAIN_TRAINED_FIELDS, "session_role": "natural"}  # invalid
    _reject(schema, _train(outcome="trained", **bad))


def test_train_trained_without_summary_is_rejected(schema):
    no_summary = {k: v for k, v in _TRAIN_TRAINED_FIELDS.items()
                  if k != "summary"}
    _reject(schema, _train(outcome="trained", **no_summary))


def test_train_unappliable_requires_reason(schema):
    _validate(schema, _train(outcome="unappliable",
                              reason="no prior review"))
    _reject(schema, _train(outcome="unappliable"))


def test_train_point_match_status_enum(schema):
    bad_match = dict(_TRAIN_TRAINED_FIELDS["point_matches"][0],
                      match_status="kind-of-caught")
    fields = {**_TRAIN_TRAINED_FIELDS, "point_matches": [bad_match]}
    _reject(schema, _train(outcome="trained", **fields))


# --- draft -----------------------------------------------------------------

_DRAFT_PROPOSE = {
    "flag_id":     "elig-1",
    "disposition": "propose",
    "proposal_id": "prop-a",
}

_DRAFT_DECLINE = {
    "flag_id":           "elig-2",
    "disposition":       "decline",
    "decline_rationale": "evidence not strong enough yet",
}

_GRADUATE_PROPOSAL = {
    "proposal_id":   "prop-a",
    "recommendation": "graduate",
    "subject_kind":   "candidate",
    "subject_ids":   ["c-test"],
    "payload": {"candidate_id":        "c-test",
                "graduated_check_id":   "c-test",
                "graduated_text":       "graduated body"},
    "rationale": {"summary": "the catches mature",
                  "evidence_cited": {"from_eligibility_flag": "elig-1"},
                  "considered_alternatives": []},
    "predicted_impact": {"expected_fire_rate": 0.4,
                          "expected_unique_catch_rate": 0.3},
}


def test_draft_drafted_full_record(schema):
    _validate(schema, _draft(
        outcome="drafted",
        eligibility_dispositions=[_DRAFT_PROPOSE, _DRAFT_DECLINE],
        proposals=[_GRADUATE_PROPOSAL],
        cross_proposal_dependencies=[],
        node_notes={"warnings": [], "confidence": "high",
                    "confidence_reason": "strong evidence",
                    "overflow_flags_deferred": []},
        self_review_record=_SELF_REVIEW))


def test_draft_drafted_with_no_proposals_is_valid(schema):
    """A "drafted" record with zero proposals is valid — every eligibility
       flag may be dispositioned as decline or defer."""
    _validate(schema, _draft(
        outcome="drafted",
        eligibility_dispositions=[_DRAFT_DECLINE],
        proposals=[],
        cross_proposal_dependencies=[],
        node_notes={"warnings": [], "confidence": "high",
                    "confidence_reason": "all evidence stale",
                    "overflow_flags_deferred": []},
        self_review_record=_SELF_REVIEW))


def test_draft_disposition_propose_requires_proposal_id(schema):
    bad = {"flag_id": "elig-1", "disposition": "propose"}    # no proposal_id
    _reject(schema, _draft(
        outcome="drafted",
        eligibility_dispositions=[bad],
        proposals=[],
        cross_proposal_dependencies=[],
        node_notes={"warnings": [], "confidence": "high",
                    "confidence_reason": "x",
                    "overflow_flags_deferred": []},
        self_review_record=_SELF_REVIEW))


def test_draft_disposition_decline_requires_rationale(schema):
    bad = {"flag_id": "elig-1", "disposition": "decline"}    # no rationale
    _reject(schema, _draft(
        outcome="drafted",
        eligibility_dispositions=[bad],
        proposals=[],
        cross_proposal_dependencies=[],
        node_notes={"warnings": [], "confidence": "high",
                    "confidence_reason": "x",
                    "overflow_flags_deferred": []},
        self_review_record=_SELF_REVIEW))


def test_draft_failed_requires_reason(schema):
    _validate(schema, _draft(outcome="failed", reason="ran out of budget"))
    _reject(schema, _draft(outcome="failed"))


def test_draft_recommendation_enum_is_enforced(schema):
    bad = {**_GRADUATE_PROPOSAL, "recommendation": "delete-everything"}
    _reject(schema, _draft(
        outcome="drafted",
        eligibility_dispositions=[_DRAFT_PROPOSE],
        proposals=[bad],
        cross_proposal_dependencies=[],
        node_notes={"warnings": [], "confidence": "high",
                    "confidence_reason": "x",
                    "overflow_flags_deferred": []},
        self_review_record=_SELF_REVIEW))


# --- discriminator ---------------------------------------------------------

def test_task_type_selects_the_branch(schema):
    """task_type discriminates; the four record kinds validate cleanly."""
    _validate(schema, _prepare(outcome="uncharacterisable", reason="x"))
    _validate(schema, _review(outcome="unappliable", reason="x"))
    _validate(schema, _train(outcome="unappliable", reason="x"))
    _validate(schema, _draft(outcome="failed", reason="x"))


def test_task_type_review_with_train_outcome_is_rejected(schema):
    """task_type=review constrains outcome to reviewed/unappliable/deferred;
       `trained` belongs to the train branch."""
    _reject(schema, _review(outcome="trained", concerns=[],
                            self_review_record=_SELF_REVIEW))


def test_unknown_task_type_is_rejected(schema):
    _reject(schema, {"task_type": "maintenance", "worker_id": "n",
                     "outcome": "ok",
                     "model": _MODEL, "usage": _USAGE})


def test_methodology_version_on_record_is_rejected(schema):
    """The methodology version is stamped on the work_items / draft_tasks
       row at claim time and is not part of the completion record. A node
       that sends it back is faulting the contract — `additionalProperties:
       false` rejects it for every task_type branch."""
    _reject(schema, _prepare(outcome="uncharacterisable",
                              reason="couldn't classify",
                              methodology_version=1))
    _reject(schema, _review(outcome="reviewed", concerns=[],
                             self_review_record=_SELF_REVIEW,
                             methodology_version=1))
