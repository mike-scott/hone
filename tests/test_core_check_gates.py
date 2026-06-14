"""Tests for core/check_gates.py — deterministic per-check coverage (the
denominator the 'most used checks' metric needs). Pure feature/gate logic plus
the DB-aware wrapper and the ai_reviews round-trip."""
import os

from core import check_gates, core_db

CF = check_gates


# --- feature extraction -----------------------------------------------------

def test_features_touches_c_from_diff_paths():
    f = CF.extract_features(["+++ b/drivers/gpu/x.c\n+ int a;\n"])
    assert f[CF.FEATURE_TOUCHES_C] is True
    g = CF.extract_features(["+++ b/Makefile\n+ obj-y += x.o\n"])
    assert g[CF.FEATURE_TOUCHES_C] is False


def test_features_rcu_and_locks():
    f = CF.extract_features(
        ["+++ b/x.c\n+ rcu_read_lock();\n+ mutex_lock(&m);\n"])
    assert f[CF.FEATURE_USES_RCU] is True
    assert f[CF.FEATURE_USES_LOCKS] is True
    g = CF.extract_features(["+++ b/x.c\n+ int a = 1;\n"])
    assert g[CF.FEATURE_USES_RCU] is False and g[CF.FEATURE_USES_LOCKS] is False


def test_features_adds_function_via_export_or_definition():
    exp = CF.extract_features(["+++ b/x.c\n+EXPORT_SYMBOL_GPL(foo);\n"])
    assert exp[CF.FEATURE_ADDS_FUNCTION] is True
    defn = CF.extract_features(["+++ b/x.c\n+int foo(int a, int b)\n+{\n"])
    assert defn[CF.FEATURE_ADDS_FUNCTION] is True
    none = CF.extract_features(["+++ b/x.c\n+ a = foo(1, 2);\n"])   # a call
    assert none[CF.FEATURE_ADDS_FUNCTION] is False


def test_features_bugfix_from_covariate_and_doc_contract_from_path():
    assert CF.extract_features(["+++ b/x.c\n+a;\n"],
                               patch_type_primary="bugfix")[
        CF.FEATURE_IS_BUGFIX] is True
    assert CF.extract_features(["+++ b/x.c\n+a;\n"],
                               patch_type_primary="feature")[
        CF.FEATURE_IS_BUGFIX] is False
    uapi = CF.extract_features(["+++ b/include/uapi/drm/msm_drm.h\n+a;\n"])
    assert uapi[CF.FEATURE_TOUCHES_DOC_CONTRACT] is True


# --- compute_coverage -------------------------------------------------------

def test_coverage_gates_applicability_per_check():
    """Narrow-gate checks are applicable only when their feature holds; broad
       (touches_c) checks apply to any C patch."""
    cov = {c["id"]: c for c in CF.compute_coverage(
        ["concurrency", "lock-storage-lifetime", "subsystem-checklists",
         "function-contract", "efficacy-and-root-cause"],
        ["+++ b/x.c\n+ int a;\n"],                 # plain C, no rcu/lock/fn/fix
        patch_type_primary="feature")}
    assert cov["concurrency"]["applicable"] is True            # any C
    assert cov["lock-storage-lifetime"]["applicable"] is False  # no locks
    assert cov["subsystem-checklists"]["applicable"] is False   # no rcu
    assert cov["function-contract"]["applicable"] is False      # no new fn
    assert cov["efficacy-and-root-cause"]["applicable"] is False  # not bugfix
    assert all(c["gate"] == "specific" for c in cov.values())


def test_coverage_unknown_check_falls_back_to_default_gate():
    cov = CF.compute_coverage(["some-new-check"], ["+++ b/x.c\n+a;\n"])
    assert cov[0]["gate"] == "default"
    assert cov[0]["applicable"] is True            # default = touches_c


def test_coverage_fired_counts_primary_and_contributing():
    concerns = [
        {"candidate_or_check_id": "concurrency"},
        {"candidate_or_check_id": "concurrency"},
        {"candidate_or_check_id": "object-lifetime",
         "contributing_check_ids": ["object-lifetime", "integer-safety"]},
    ]
    cov = {c["id"]: c for c in CF.compute_coverage(
        ["concurrency", "object-lifetime", "integer-safety", "error-teardown"],
        ["+++ b/x.c\n+a;\n"], concerns=concerns)}
    assert cov["concurrency"]["fired"] is True
    assert cov["concurrency"]["n_concerns"] == 2
    assert cov["object-lifetime"]["n_concerns"] == 1
    # credited purely as a contributor (Stage-C merge) → still fired
    assert cov["integer-safety"]["fired"] is True
    assert cov["integer-safety"]["n_concerns"] == 1
    assert cov["error-teardown"]["fired"] is False


def test_coverage_applicable_and_fired_are_independent():
    """A concern can fire under a check our gate marked not-applicable (gate too
       narrow / model applied it anyway). That mismatch must be preserved as
       signal, not papered over."""
    cov = CF.compute_coverage(
        ["subsystem-checklists"],                  # gate: uses_rcu
        ["+++ b/x.c\n+ int a;\n"],                 # NO rcu → not applicable
        concerns=[{"candidate_or_check_id": "subsystem-checklists"}])
    assert cov[0]["applicable"] is False and cov[0]["fired"] is True


# --- coverage_for_review + ai_reviews round-trip ----------------------------

def _seed(db, root="<r1@x>", patch_body=None, patch_type="bugfix"):
    core_db.upsert_patchset(db, root, subject="[PATCH] x", n_patches=1)
    core_db.upsert_message(
        db, "<p1@x>", root_message_id=root, type=core_db.MSG_TYPE_PATCH,
        part_index=1, body=patch_body or
        "+++ b/drivers/x.c\n+ rcu_read_lock();\n+int foo(void)\n+{\n")
    core_db.upsert_patchset_metadata(
        db, root, mode="heuristic", tree_state={}, subsystem={},
        patch_size={}, maintainer={}, patch_type={"primary": patch_type},
        review_intensity={}, preparation_notes={})
    return root


def test_coverage_for_review_pulls_patch_and_covariates(tmp_path):
    db = core_db.connect(os.path.join(tmp_path, "h.db"))
    root = _seed(db)
    doc = {"checks": [{"id": "subsystem-checklists"},
                      {"id": "efficacy-and-root-cause"},
                      {"id": "function-contract"}]}
    cov = {c["id"]: c for c in CF.coverage_for_review(
        db, root, doc, concerns=[{"candidate_or_check_id": "function-contract"}])}
    assert cov["subsystem-checklists"]["applicable"] is True   # rcu in diff
    assert cov["efficacy-and-root-cause"]["applicable"] is True  # bugfix
    assert cov["function-contract"]["applicable"] is True       # new fn
    assert cov["function-contract"]["fired"] is True


def test_coverage_for_review_none_when_no_checks(tmp_path):
    db = core_db.connect(os.path.join(tmp_path, "h.db"))
    root = _seed(db)
    assert CF.coverage_for_review(db, root, {"checks": []}, concerns=[]) is None
    assert CF.coverage_for_review(db, root, {}, concerns=[]) is None


def test_ai_review_check_coverage_roundtrips(tmp_path):
    db = core_db.connect(os.path.join(tmp_path, "h.db"))
    root = _seed(db)
    coverage = [{"id": "concurrency", "applicable": True, "gate": "specific",
                 "fired": True, "n_concerns": 1}]
    core_db.upsert_ai_review(db, root, concerns=[{"concern_id": "c1"}],
                             check_coverage=coverage)
    got = core_db.get_ai_review(db, root)
    assert got["check_coverage"] == coverage           # decoded from JSON


def test_ai_review_check_coverage_defaults_null(tmp_path):
    db = core_db.connect(os.path.join(tmp_path, "h.db"))
    root = _seed(db)
    core_db.upsert_ai_review(db, root, concerns=[])
    assert core_db.get_ai_review(db, root)["check_coverage"] is None
