"""Tests for /work-items/{id} — the per-work-item detail page, the
shared back-link pattern, and the cross-links from the patchset detail
+ node detail pages into it."""
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import core_db, ui


@pytest.fixture
def ctx(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    app = FastAPI()
    app.include_router(ui.router)
    app.state.db = db
    return SimpleNamespace(client=TestClient(app), db=db)


def _plant_patchset(db, root="<r1@x>", subject="[PATCH 1/1] frob"):
    """Plant a gathered patchset + metadata, ready for enqueueing
       work-items against."""
    core_db.upsert_patchset(db, root, subject=subject, n_patches=1)
    core_db.upsert_patchset_metadata(
        db, root, mode="heuristic",
        tree_state={}, subsystem={"primary": "drivers/net"},
        patch_size={"bucket": "small"}, maintainer={},
        patch_type={"primary": "bugfix"},
        review_intensity={"bucket_overall": "light"},
        preparation_notes={})


# --- core_db.get_work_item helper -----------------------------------------

def test_get_work_item_decodes_the_record_json(ctx):
    """get_work_item returns the row with `record` decoded to a dict —
       the UI never has to json.loads it."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    claim = core_db.claim_work_item(
        ctx.db, "builder-7", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    core_db.submit_work_result(
        ctx.db, claim["claim_id"],
        state=core_db.WORK_ITEM_STATE_COMPLETED,
        record={"outcome": "reviewed", "model": "claude-opus-4-7",
                "concerns": [{"concern_id": "c-1"}]})
    w = core_db.get_work_item(ctx.db, wid)
    assert w["id"] == wid
    assert isinstance(w["record"], dict)
    assert w["record"]["outcome"] == "reviewed"
    assert w["record"]["concerns"] == [{"concern_id": "c-1"}]


def test_get_work_item_returns_none_for_unknown_id(ctx):
    assert core_db.get_work_item(ctx.db, 99999) is None


# --- detail page renders --------------------------------------------------

def test_work_item_detail_renders_lifecycle_and_record(ctx):
    """The detail page surfaces lifecycle (claim id, worker, lease,
       methodology_version) AND the completion record (outcome,
       model, usage). The patchset's subject is rendered as a link
       carrying ?back=/work-items/{id} so navigating out and back
       returns here."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    claim = core_db.claim_work_item(
        ctx.db, "builder-7", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    core_db.submit_work_result(
        ctx.db, claim["claim_id"],
        state=core_db.WORK_ITEM_STATE_COMPLETED,
        record={"outcome": "reviewed", "model": "claude-opus-4-7",
                "concerns": [],
                "usage": {"input_tokens": 100, "output_tokens": 50,
                           "duration_ms": 5000}})

    r = ctx.client.get(f"/work-items/{wid}")
    assert r.status_code == 200
    body = r.text
    # Identity + state
    assert f"work-item <code>{wid}</code>" in body
    assert "review" in body
    assert "completed" in body
    # Lifecycle
    assert claim["claim_id"] in body
    assert "builder-7" in body
    # Record
    assert "reviewed" in body
    assert "claude-opus-4-7" in body
    # The patchset link carries ?back= so back-nav round-trips. The
    # root_message_id is URL-quoted (`@` → `%40`) and the back URL is
    # also fully URL-encoded — same `quote(..., safe="")` shape the
    # patchset and node detail pages use.
    expected_back = quote(f"/work-items/{wid}", safe="")
    expected_root = quote("r1@x")
    assert (f'href="/patchsets/{expected_root}?back={expected_back}"'
            in body)
    # ← Back default lands at "/" — the work-item detail's default
    # home is the queue.
    assert 'href="/"' in body


def test_work_item_detail_back_link_honours_query_param(ctx):
    """?back= takes the operator wherever the opener came from —
       same _safe_back helper as the other detail pages."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    r = ctx.client.get(
        f"/work-items/{wid}?back={quote('/?type=review', safe='')}")
    assert r.status_code == 200
    # Jinja escapes `&` but `/?type=review` has none, so it renders raw.
    assert 'href="/?type=review"' in r.text


def test_work_item_detail_rejects_offsite_back_url(ctx):
    """Same-origin paths only — _safe_back is the shared guard."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    r = ctx.client.get(
        f"/work-items/{wid}?back={quote('https://attacker.example/', safe='')}")
    assert r.status_code == 200
    assert "attacker.example" not in r.text


def test_work_item_detail_surfaces_meta_schema_error(ctx):
    """A fallback record from the 422-rejection path carries the
       schema error + the rejected payload on `meta`. The detail
       page hoists those into a dedicated alert + collapsible
       block so an operator scanning for failures sees the
       pinpoint message without expanding the raw JSON."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    claim = core_db.claim_work_item(
        ctx.db, "builder-7", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    core_db.submit_work_result(
        ctx.db, claim["claim_id"],
        state=core_db.WORK_ITEM_STATE_UNAPPLIABLE,
        record={"outcome": "unappliable",
                "model": "claude-opus-4-7",
                "reason": "hone-core 422: schema rejection",
                "usage": {"input_tokens": 0, "output_tokens": 0,
                           "duration_ms": 0},
                "meta": {"schema_error":
                          "maintainer/mailing_lists/0: 'was_cc'd' "
                          "was unexpected",
                          "rejected_record": {"outcome": "reviewed"}}})
    body = ctx.client.get(f"/work-items/{wid}").text
    assert "schema rejection" in body
    assert "maintainer/mailing_lists/0" in body


def test_work_item_detail_404_for_unknown_id(ctx):
    r = ctx.client.get("/work-items/99999")
    assert r.status_code == 404


# --- cross-page wiring ----------------------------------------------------

def test_patchset_detail_work_item_history_links_to_detail(ctx):
    """The Work-item history table on /patchsets/{root} now turns
       each row into a link to /work-items/{id} with ?back= round-
       tripping back to the patchset detail."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    r = ctx.client.get(f"/patchsets/{quote('r1@x')}")
    assert r.status_code == 200
    expected_back = quote("/patchsets/r1@x", safe="")
    assert f"/work-items/{wid}?back={expected_back}" in r.text


def test_node_detail_recent_claims_links_to_work_item_detail(ctx):
    """Each row in a node's Recent claims table links to the work-
       item detail with ?back=/nodes/{node_id}."""
    _plant_patchset(ctx.db)
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    core_db.claim_work_item(
        ctx.db, "builder-7", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))

    body = ctx.client.get(f"/nodes/{node_id}").text
    expected_back = quote(f"/nodes/{node_id}", safe="")
    assert f"/work-items/{wid}?back={expected_back}" in body
