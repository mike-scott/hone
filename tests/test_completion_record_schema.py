"""Unit tests for core/completion-record.schema.yaml — the JSON Schema for the
POST /v1/claims/{claim_id}/result body (see API.md).

Covers: the schema is itself valid draft-2020-12; well-formed review and
maintenance records validate; malformed ones are rejected; and the two
record shapes are isolated (a record of one shape fails the other's branch).
"""
import os

import pytest
import yaml
from jsonschema import Draft202012Validator

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "core", "completion-record.schema.yaml")

with open(_SCHEMA_PATH, encoding="utf-8") as _f:
    SCHEMA = yaml.safe_load(_f)


def _branch(name):
    """A validator for one $defs branch, with $defs in scope — the same
       construction core/api.py uses to validate per task type."""
    return Draft202012Validator(
        {"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"],
         "$ref": f"#/$defs/{name}"})


WHOLE = Draft202012Validator(SCHEMA)
REVIEW = _branch("review_record")
MAINTENANCE = _branch("maintenance_record")

USAGE = {"tokens": 100, "tool_uses": 3, "duration_ms": 9000}
FINDING = {
    "severity": "major",
    "anchor": {"patch": "2/3", "file": "drivers/x.c", "line": 88},
    "text": "object leaked on the error path",
    "produced_by": "object-lifetime",
    "preexisting": False,
}


def reviewed(**over):
    """A complete, valid `reviewed` review record; `over` patches fields."""
    rec = {
        "worker_id": "node-1", "methodology_version": 1, "outcome": "reviewed",
        "verdict": "issues", "findings": [FINDING],
        "coverage": [{"id": "2", "status": "applied"}],
        "candidate_outcomes": [], "source_comparison": [],
        "residual_risk": "None identified.", "usage": USAGE,
    }
    rec.update(over)
    return rec


def completed(**over):
    """A complete, valid `completed` maintenance record; `over` patches fields."""
    rec = {"worker_id": "node-2", "methodology_version": 1,
           "outcome": "completed", "proposals": [], "usage": USAGE}
    rec.update(over)
    return rec


def without(rec, *keys):
    return {k: v for k, v in rec.items() if k not in keys}


def test_schema_is_valid_draft202012():
    Draft202012Validator.check_schema(SCHEMA)


# --- valid records ---------------------------------------------------------

VALID = {
    "review/reviewed with findings": reviewed(),
    "review/reviewed clean": reviewed(verdict="clean", findings=[]),
    "review/unappliable": {"worker_id": "n", "methodology_version": 1,
                           "outcome": "unappliable",
                           "reason": "patch will not apply"},
    "review/deferred": {"worker_id": "n", "methodology_version": 1,
                        "outcome": "deferred", "reason": "base unobtainable"},
    "review/candidate fired with ref": reviewed(candidate_outcomes=[{
        "candidate_id": "guard-underflow", "applied": True, "fired": True,
        "finding_ref": "f1"}]),
    "review/candidate not fired": reviewed(candidate_outcomes=[{
        "candidate_id": "guard-underflow", "applied": True, "fired": False}]),
    "maintenance/completed empty": completed(),
    "maintenance/graduate": completed(proposals=[{
        "recommendation": "graduate", "subject": "guard-underflow",
        "rationale": "5/11 unique catches over Applied 50",
        "payload": {"id": "guard-underflow", "stage": "2",
                    "title": "Counter-guard underflow",
                    "body": "Audit counter guards for signed underflow."}}]),
    "maintenance/prune-redundant": completed(proposals=[{
        "recommendation": "prune-redundant", "subject": "c6",
        "rationale": "subsumed by an existing check",
        "payload": {"subsumed_by": "object-lifetime"}}]),
    "maintenance/prune-ineffective": completed(proposals=[{
        "recommendation": "prune-ineffective", "subject": "c9",
        "rationale": "0 catches over Applied 40", "payload": {}}]),
    "maintenance/consolidate": completed(proposals=[{
        "recommendation": "consolidate", "subject": ["c2", "c7"],
        "rationale": "the two overlap",
        "payload": {"merged": {"id": "c2", "title": "Merged check",
                               "body": "Merged body.", "stage": "2"}}}]),
    "maintenance/revise": completed(proposals=[{
        "recommendation": "revise", "subject": "c8",
        "rationale": "clarify the wording",
        "payload": {"new_body": "Revised candidate body."}}]),
    "maintenance/failed": {"worker_id": "n", "methodology_version": 1,
                           "outcome": "failed", "reason": "fetch timed out",
                           "usage": USAGE},
}


@pytest.mark.parametrize("record", VALID.values(), ids=list(VALID))
def test_valid_records_accepted(record):
    assert list(WHOLE.iter_errors(record)) == []


# --- invalid records -------------------------------------------------------

INVALID = {
    "reviewed missing coverage": without(reviewed(), "coverage"),
    "reviewed missing usage": without(reviewed(), "usage"),
    "reviewed carrying a reason": reviewed(reason="should not be here"),
    "deferred carrying a verdict": {"worker_id": "n", "methodology_version": 1,
                                    "outcome": "deferred", "reason": "x",
                                    "verdict": "clean"},
    "deferred missing reason": {"worker_id": "n", "methodology_version": 1,
                                "outcome": "deferred"},
    "unknown outcome": {"worker_id": "n", "methodology_version": 1,
                        "outcome": "maybe", "reason": "x"},
    "stray top-level key": {**reviewed(), "oops": 1},
    "bad severity": reviewed(findings=[{**FINDING, "severity": "showstopper"}]),
    "bad anchor patch format": reviewed(findings=[{
        **FINDING, "anchor": {"patch": "two", "file": "x.c", "line": 1}}]),
    "candidate fired without ref": reviewed(candidate_outcomes=[{
        "candidate_id": "c", "applied": True, "fired": True}]),
    "methodology_version zero": reviewed(methodology_version=0),
    "completed missing proposals": without(completed(), "proposals"),
    "completed carrying a reason": completed(reason="should not be here"),
    "failed missing reason": {"worker_id": "n", "methodology_version": 1,
                              "outcome": "failed", "usage": USAGE},
    "graduate with revise payload": completed(proposals=[{
        "recommendation": "graduate", "subject": "c", "rationale": "r",
        "payload": {"new_body": "x"}}]),
    "consolidate with single-slug subject": completed(proposals=[{
        "recommendation": "consolidate", "subject": "only-one",
        "rationale": "r",
        "payload": {"merged": {"id": "m", "title": "t", "body": "b",
                               "stage": "2"}}}]),
    "prune-ineffective with non-empty payload": completed(proposals=[{
        "recommendation": "prune-ineffective", "subject": "c",
        "rationale": "r", "payload": {"subsumed_by": "x"}}]),
}


@pytest.mark.parametrize("record", INVALID.values(), ids=list(INVALID))
def test_invalid_records_rejected(record):
    assert list(WHOLE.iter_errors(record)) != []


# --- branch isolation ------------------------------------------------------

def test_review_branch_accepts_review_rejects_maintenance():
    assert list(REVIEW.iter_errors(reviewed())) == []
    assert list(REVIEW.iter_errors(completed())) != []


def test_maintenance_branch_accepts_maintenance_rejects_review():
    assert list(MAINTENANCE.iter_errors(completed())) == []
    assert list(MAINTENANCE.iter_errors(reviewed())) != []
