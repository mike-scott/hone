"""Unit tests for the methodology helpers in core_db: candidate counters,
severity_witness histograms, candidate state transitions, the proposals
queue, and the merge-gate disposition rules."""
import pytest

from core import core_db


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


# --- candidate counters ----------------------------------------------------

def test_add_candidate_is_idempotent_on_id(db):
    core_db.add_candidate(db, "c-1", body="first", origin="miss A")
    core_db.add_candidate(db, "c-1", body="second", origin="miss B")
    rows = core_db.list_candidates(db)
    assert len(rows) == 1
    # INSERT OR IGNORE — first write wins.
    assert rows[0]["body"] == "first"
    assert rows[0]["origin"] == "miss A"


def test_bump_candidate_advances_pool_counters(db):
    core_db.add_candidate(db, "c-1", body="...")
    core_db.bump_candidate(db, "c-1", applied=3, catches=2, unique_catches=1)
    core_db.bump_candidate(db, "c-1", applied=1, catches=1)
    row = core_db.list_candidates(db)[0]
    assert row["applied"] == 4
    assert row["catches"] == 3
    assert row["unique_catches"] == 1


def test_bump_candidate_raises_on_unknown_id(db):
    with pytest.raises(KeyError):
        core_db.bump_candidate(db, "c-missing", applied=1)


# --- severity_witness histograms ------------------------------------------

def test_bump_severity_witness_by_string_severity(db):
    core_db.add_candidate(db, "c-1", body="...")
    core_db.bump_severity_witness(db, "c-1", "major")
    core_db.bump_severity_witness(db, "c-1", "major", n=2)
    core_db.bump_severity_witness(db, "c-1", "nit")
    row = core_db.list_candidates(db)[0]
    assert row["severity_witness_introduced"] == {"major": 3, "nit": 1}
    assert row["severity_witness_preexisting"] == {}


def test_bump_severity_witness_by_int_severity(db):
    """The helper accepts both int (SEVERITY_*) and string tags."""
    core_db.add_candidate(db, "c-1", body="...")
    core_db.bump_severity_witness(db, "c-1", core_db.SEVERITY_CRITICAL)
    core_db.bump_severity_witness(db, "c-1", core_db.SEVERITY_MODERATE)
    row = core_db.list_candidates(db)[0]
    assert row["severity_witness_introduced"] == {
        "critical": 1, "moderate": 1}


def test_bump_severity_witness_routes_preexisting_to_its_own_histogram(db):
    """The two histograms (introduced vs preexisting) are parallel — same
       severity tag accumulates in different columns based on the flag."""
    core_db.add_candidate(db, "c-1", body="...")
    core_db.bump_severity_witness(db, "c-1", "major")
    core_db.bump_severity_witness(db, "c-1", "major", is_preexisting=True)
    core_db.bump_severity_witness(db, "c-1", "minor", is_preexisting=True)
    row = core_db.list_candidates(db)[0]
    assert row["severity_witness_introduced"] == {"major": 1}
    assert row["severity_witness_preexisting"] == {"major": 1, "minor": 1}


def test_bump_severity_witness_rejects_an_unknown_tag(db):
    core_db.add_candidate(db, "c-1", body="...")
    with pytest.raises(ValueError):
        core_db.bump_severity_witness(db, "c-1", "high")    # not in the scale


def test_bump_severity_witness_raises_on_unknown_candidate(db):
    with pytest.raises(KeyError):
        core_db.bump_severity_witness(db, "c-missing", "major")


# --- candidate state ------------------------------------------------------

def test_set_candidate_state_transitions(db):
    core_db.add_candidate(db, "c-1", body="...")
    core_db.set_candidate_state(db, "c-1",
                                 core_db.METHODOLOGY_CANDIDATE_STATE_GRADUATED)
    row = core_db.list_candidates(db)[0]
    assert row["state"] == core_db.METHODOLOGY_CANDIDATE_STATE_GRADUATED


def test_set_candidate_state_rejects_a_bad_state(db):
    core_db.add_candidate(db, "c-1", body="...")
    with pytest.raises(ValueError):
        core_db.set_candidate_state(db, "c-1", 99)


def test_list_candidates_filters_by_state(db):
    core_db.add_candidate(db, "c-1", body="a")
    core_db.add_candidate(db, "c-2", body="b")
    core_db.set_candidate_state(db, "c-1",
                                 core_db.METHODOLOGY_CANDIDATE_STATE_GRADUATED)
    graduated = core_db.list_candidates(
        db, state=core_db.METHODOLOGY_CANDIDATE_STATE_GRADUATED)
    proposed = core_db.list_candidates(
        db, state=core_db.METHODOLOGY_CANDIDATE_STATE_PROPOSED)
    assert {c["id"] for c in graduated} == {"c-1"}
    assert {c["id"] for c in proposed} == {"c-2"}


def test_list_candidates_decodes_both_histograms_to_dicts(db):
    """The histograms are stored as JSON; consumers expect parsed dicts."""
    core_db.add_candidate(db, "c-1", body="...")
    core_db.bump_severity_witness(db, "c-1", "major")
    row = core_db.list_candidates(db)[0]
    assert isinstance(row["severity_witness_introduced"], dict)
    assert isinstance(row["severity_witness_preexisting"], dict)


# --- proposals queue ------------------------------------------------------

def test_add_proposal_stores_json_payload_and_returns_id(db):
    pid = core_db.add_proposal(
        db, core_db.METHODOLOGY_PROPOSAL_TYPE_GRADUATE,
        {"recommendation": "graduate", "subject_ids": ["c-1"]})
    assert pid >= 1
    rows = core_db.list_proposals(db)
    assert len(rows) == 1
    import json
    payload = json.loads(rows[0]["payload"])
    assert payload["recommendation"] == "graduate"


def test_add_proposal_rejects_unknown_type(db):
    with pytest.raises(ValueError):
        core_db.add_proposal(db, 99, {})


def test_list_proposals_filters_by_state(db):
    p1 = core_db.add_proposal(db, core_db.METHODOLOGY_PROPOSAL_TYPE_GRADUATE,
                               {"recommendation": "graduate"})
    p2 = core_db.add_proposal(db, core_db.METHODOLOGY_PROPOSAL_TYPE_REVISE,
                               {"recommendation": "revise"})
    core_db.decide_proposal(db, p1,
                             core_db.METHODOLOGY_PROPOSAL_STATE_ACCEPTED)
    pending = core_db.list_proposals(db)         # default = PENDING
    accepted = core_db.list_proposals(
        db, state=core_db.METHODOLOGY_PROPOSAL_STATE_ACCEPTED)
    assert {r["id"] for r in pending} == {p2}
    assert {r["id"] for r in accepted} == {p1}


def test_decide_proposal_returned_bumps_redraft_count(db):
    pid = core_db.add_proposal(db,
                                core_db.METHODOLOGY_PROPOSAL_TYPE_GRADUATE,
                                {})
    core_db.decide_proposal(db, pid,
                             core_db.METHODOLOGY_PROPOSAL_STATE_RETURNED,
                             note="wording off")
    row = db.execute("SELECT redraft_count, note FROM methodology_proposals "
                     "WHERE id=?", (pid,)).fetchone()
    assert row["redraft_count"] == 1
    assert row["note"] == "wording off"


def test_decide_proposal_other_terminals_do_not_bump_redraft(db):
    """Only RETURNED bumps the redraft counter — Accept/Defer/Reject don't."""
    for decision in (core_db.METHODOLOGY_PROPOSAL_STATE_ACCEPTED,
                     core_db.METHODOLOGY_PROPOSAL_STATE_DEFERRED,
                     core_db.METHODOLOGY_PROPOSAL_STATE_REJECTED):
        pid = core_db.add_proposal(
            db, core_db.METHODOLOGY_PROPOSAL_TYPE_GRADUATE, {})
        core_db.decide_proposal(db, pid, decision)
        row = db.execute(
            "SELECT redraft_count FROM methodology_proposals WHERE id=?",
            (pid,)).fetchone()
        assert row["redraft_count"] == 0


def test_decide_proposal_rejects_non_terminal_decisions(db):
    pid = core_db.add_proposal(db,
                                core_db.METHODOLOGY_PROPOSAL_TYPE_GRADUATE,
                                {})
    with pytest.raises(ValueError):
        # PENDING isn't a terminal — can't be a decision.
        core_db.decide_proposal(db, pid,
                                 core_db.METHODOLOGY_PROPOSAL_STATE_PENDING)


# --- methodology versions -------------------------------------------------

def test_add_methodology_version_supersedes_the_previous_active(db):
    v1 = core_db.add_methodology_version(db, {"name": "v1"})
    v2 = core_db.add_methodology_version(db, {"name": "v2"})
    assert v1 == 1 and v2 == 2
    states = dict(db.execute(
        "SELECT version, state FROM methodology_versions").fetchall())
    assert states[1] == core_db.METHODOLOGY_VERSION_STATE_SUPERSEDED
    assert states[2] == core_db.METHODOLOGY_VERSION_STATE_ACTIVE
    active = core_db.active_methodology(db)
    assert active == (2, {"name": "v2"})


def test_active_methodology_returns_none_when_unbootstrapped(db):
    assert core_db.active_methodology(db) is None
