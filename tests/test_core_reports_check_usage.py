"""Tests for reports.check_usage_stats — the check-usage analytics layer over
ai_reviews.check_coverage (Wilson rates, effective-applicable, version cohorts,
data-quality flags)."""
import os

from core import core_db, reports


def _mv(db):
    """Register a methodology version (ai_reviews.methodology_version is an FK)
       and return its number. Sequential from 1."""
    return core_db.add_methodology_version(db, {"name": "t", "checks": []})


def _add_review(db, root, mv, coverage):
    core_db.upsert_patchset(db, root, subject="s", n_patches=1)
    core_db.upsert_ai_review(db, root, concerns=[], methodology_version=mv,
                             check_coverage=coverage)


def _cov(cid, *, applicable, fired, gate="specific", n=0):
    return {"id": cid, "applicable": applicable, "gate": gate,
            "fired": fired, "n_concerns": n}


# --- Wilson CI --------------------------------------------------------------

def test_wilson_ci_edges():
    assert reports._wilson_ci(0, 0) == (0.0, 0.0)
    lo, hi = reports._wilson_ci(0, 10)              # zero fires, wide upper band
    assert lo == 0.0 and 0.0 < hi < 0.35
    lo, hi = reports._wilson_ci(10, 10)            # all fires
    assert hi == 1.0 and lo < 1.0
    lo, hi = reports._wilson_ci(1, 2)              # interval brackets the point
    assert lo < 0.5 < hi


# --- aggregation ------------------------------------------------------------

def test_check_usage_stats_rates_and_effective_applicable(tmp_path):
    db = core_db.connect(os.path.join(tmp_path, "h.db"))
    v = _mv(db)
    _add_review(db, "<r1@x>", v, [
        _cov("concurrency", applicable=True, fired=True, n=2),
        _cov("subsystem-checklists", applicable=False, fired=False)])
    _add_review(db, "<r2@x>", v, [
        _cov("concurrency", applicable=True, fired=False),
        # fired despite a not-applicable gate → mismatch; effective-applicable
        # counts it so the rate can't exceed 100%.
        _cov("subsystem-checklists", applicable=False, fired=True, n=1)])
    s = reports.check_usage_stats(db)
    rows = {r["id"]: r for r in s["rows"]}

    assert rows["concurrency"]["applicable"] == 2
    assert rows["concurrency"]["fired"] == 1
    assert rows["concurrency"]["rate_pct"] == 50
    assert rows["concurrency"]["concerns"] == 2
    # fired-but-not-applicable: effective applicable = 1, mismatch flagged
    assert rows["subsystem-checklists"]["applicable"] == 1
    assert rows["subsystem-checklists"]["fired"] == 1
    assert rows["subsystem-checklists"]["mismatch"] == 1
    assert rows["subsystem-checklists"]["rate_pct"] == 100
    # CI brackets the point estimate
    c = rows["concurrency"]
    assert c["ci_lo_pct"] <= 50 <= c["ci_hi_pct"]
    assert s["n_reviews"] == 2 and s["versions"] == [v]
    assert s["small_n"] is True and s["has_data"] is True


def test_check_usage_stats_scopes_to_version_cohort(tmp_path):
    db = core_db.connect(os.path.join(tmp_path, "h.db"))
    a, b = _mv(db), _mv(db)
    _add_review(db, "<r1@x>", a, [_cov("concurrency", applicable=True,
                                       fired=True)])
    _add_review(db, "<r2@x>", b, [_cov("concurrency", applicable=True,
                                       fired=False)])
    all_v = reports.check_usage_stats(db)
    v1 = reports.check_usage_stats(db, methodology_version=a)
    assert all_v["n_reviews"] == 2 and set(all_v["versions"]) == {a, b}
    assert {r["id"]: r["fired"] for r in all_v["rows"]}["concurrency"] == 1
    assert v1["n_reviews"] == 1 and v1["selected_version"] == a
    assert {r["id"]: r["applicable"] for r in v1["rows"]}["concurrency"] == 1
    assert {r["id"]: r["fired"] for r in v1["rows"]}["concurrency"] == 1


def test_check_usage_stats_default_gate_flag(tmp_path):
    db = core_db.connect(os.path.join(tmp_path, "h.db"))
    _add_review(db, "<r1@x>", _mv(db), [_cov("brand-new-check", applicable=True,
                                             fired=False, gate="default")])
    s = reports.check_usage_stats(db)
    assert s["rows"][0]["default_gate"] == 1


def test_check_usage_stats_empty(tmp_path):
    db = core_db.connect(os.path.join(tmp_path, "h.db"))
    s = reports.check_usage_stats(db)
    assert s["has_data"] is False and s["rows"] == [] and s["n_reviews"] == 0
    assert s["versions"] == []


def test_check_usage_chart_config_is_floating_ci_bars():
    cfg = reports.check_usage_chart_config(
        [{"id": "concurrency", "ci_lo_pct": 20.0, "ci_hi_pct": 80.0}])
    assert cfg["type"] == "bar"
    assert cfg["data"]["labels"] == ["concurrency"]
    assert cfg["data"]["datasets"][0]["data"] == [[20.0, 80.0]]  # floating bar
    assert cfg["options"]["indexAxis"] == "y"
