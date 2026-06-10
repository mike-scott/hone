"""Tests for /work-items/{id} — the per-work-item detail page, the
shared back-link pattern, and the cross-links from the patchset detail
+ node detail pages into it."""
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import core_db, runtime_config, ui


@pytest.fixture
def ctx(tmp_path, fake_admin_session):
    db = core_db.connect(str(tmp_path / "hone.db"))
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    app = FastAPI()
    app.include_router(ui.router)
    fake_admin_session(app)
    app.state.db = db
    # /nodes/{id} now consults runtime_config for the freshness
    # threshold (via _node_status_fields). Tests that touch the
    # node-detail page need it populated.
    app.state.runtime_config = runtime_config.load(
        str(tmp_path / "config.yaml"))
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


def test_work_item_detail_renders_agent_messages_trace(ctx):
    """A record whose meta carries the captured Claude turn (meta.trace)
       renders an 'Agent messages' section — assistant text, tool uses
       (with a target from the tool input), and tool results."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    claim = core_db.claim_work_item(
        ctx.db, "builder-7", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    core_db.submit_work_result(
        ctx.db, claim["claim_id"],
        state=core_db.WORK_ITEM_STATE_COMPLETED,
        record={"outcome": "reviewed", "concerns": [],
                "meta": {"trace": [
                    {"step": "assistant_text",
                     "text": "Reading the driver to confirm the base.",
                     "chars": 39},
                    {"step": "tool_use", "name": "Read",
                     "input": {"file_path": "drivers/net/foo.c"}},
                    {"step": "tool_result", "chars": 3210}]}})
    body = ctx.client.get(f"/work-items/{wid}").text
    assert "Agent messages" in body
    assert "Reading the driver to confirm the base." in body
    assert "Read" in body
    assert "drivers/net/foo.c" in body
    assert "3210 chars" in body


def test_work_item_detail_omits_agent_messages_when_no_trace(ctx):
    """No meta.trace → the 'Agent messages' section is absent (the page
       still renders the completion record)."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    claim = core_db.claim_work_item(
        ctx.db, "builder-7", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    core_db.submit_work_result(
        ctx.db, claim["claim_id"],
        state=core_db.WORK_ITEM_STATE_COMPLETED,
        record={"outcome": "reviewed", "concerns": []})
    body = ctx.client.get(f"/work-items/{wid}").text
    assert "Agent messages" not in body
    assert "Full JSON record" in body


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


# --- deferred badge → release-deferred action ------------------------------

def _claimed_review(db):
    """Plant a patchset and claim its review — returns (item_id, claim)."""
    _plant_patchset(db)
    wid = core_db.enqueue_review(db, "<r1@x>")
    claim = core_db.claim_work_item(
        db, "builder-7", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    return wid, claim


def _deferred_review(db):
    """A review work item left in the DEFERRED state — returns its id."""
    wid, claim = _claimed_review(db)
    core_db.submit_work_result(db, claim["claim_id"],
                               state=core_db.WORK_ITEM_STATE_DEFERRED,
                               record={"outcome": "deferred"})
    return wid


def test_deferred_badge_offers_a_release_action(ctx):
    item_id = _deferred_review(ctx.db)
    body = ctx.client.get(f"/work-items/{item_id}").text
    assert f'action="/work-items/{item_id}/release-deferred' in body


def test_non_deferred_badge_is_static(ctx):
    """A claimed item's badge is a plain span — no release form."""
    item_id, _ = _claimed_review(ctx.db)
    body = ctx.client.get(f"/work-items/{item_id}").text
    assert "/release-deferred" not in body


def test_post_release_deferred_reverts_to_claimable(ctx):
    item_id = _deferred_review(ctx.db)
    r = ctx.client.post(f"/work-items/{item_id}/release-deferred",
                        follow_redirects=False)
    assert r.status_code == 303
    state = ctx.db.execute("SELECT state FROM work_items WHERE id=?",
                           (item_id,)).fetchone()["state"]
    assert state == core_db.WORK_ITEM_STATE_CLAIMABLE


def test_post_release_deferred_unknown_id_404s(ctx):
    r = ctx.client.post("/work-items/999999/release-deferred",
                        follow_redirects=False)
    assert r.status_code == 404


# --- unappliable badge → retry-unappliable action --------------------------

def _unappliable_review(db):
    """A review work item left in the UNAPPLIABLE state — returns its id."""
    wid, claim = _claimed_review(db)
    core_db.submit_work_result(db, claim["claim_id"],
                               state=core_db.WORK_ITEM_STATE_UNAPPLIABLE,
                               record={"outcome": "unappliable"})
    return wid


def test_unappliable_badge_offers_a_retry_action(ctx):
    item_id = _unappliable_review(ctx.db)
    body = ctx.client.get(f"/work-items/{item_id}").text
    assert f'action="/work-items/{item_id}/retry-unappliable' in body


def test_non_unappliable_badge_has_no_retry_action(ctx):
    """A claimed item's badge is a plain span — no retry form."""
    item_id, _ = _claimed_review(ctx.db)
    body = ctx.client.get(f"/work-items/{item_id}").text
    assert "/retry-unappliable" not in body


def test_post_retry_unappliable_reverts_to_claimable(ctx):
    item_id = _unappliable_review(ctx.db)
    r = ctx.client.post(f"/work-items/{item_id}/retry-unappliable",
                        follow_redirects=False)
    assert r.status_code == 303
    state = ctx.db.execute("SELECT state FROM work_items WHERE id=?",
                           (item_id,)).fetchone()["state"]
    assert state == core_db.WORK_ITEM_STATE_CLAIMABLE


def test_post_retry_unappliable_unknown_id_404s(ctx):
    r = ctx.client.post("/work-items/999999/retry-unappliable",
                        follow_redirects=False)
    assert r.status_code == 404


# --- admin cancel of unheld (claimable / deferred) items --------------------

def test_cancel_deletes_a_claimable_review_and_rearms_the_button(ctx):
    """Admin cancel removes the queued request entirely: the row is gone,
       the queue no longer lists it, and the patchset's Request-review
       button is offered again (nothing review-related remains)."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    r = ctx.client.post(f"/work-items/{wid}/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["Location"] == "/queue"
    assert ctx.db.execute("SELECT COUNT(*) AS n FROM work_items") \
                 .fetchone()["n"] == 0
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "Request review" in body


def test_cancel_honours_back_param(ctx):
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    r = ctx.client.post(f"/work-items/{wid}/cancel?back=%2Fqueue%3Fpage%3D2",
                        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["Location"] == "/queue?page=2"


def test_cancel_refuses_a_claimed_item(ctx):
    """A claimed row is held by a node — cancel is a no-op that bounces
       back to the detail page so the admin sees the current state."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    core_db.claim_work_item(ctx.db, "w1", methodology_version=1)
    r = ctx.client.post(f"/work-items/{wid}/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["Location"] == f"/work-items/{wid}"
    assert ctx.db.execute("SELECT COUNT(*) AS n FROM work_items") \
                 .fetchone()["n"] == 1


def test_cancel_works_on_a_deferred_item(ctx):
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    ctx.db.execute("UPDATE work_items SET state=? WHERE id=?",
                   (core_db.WORK_ITEM_STATE_DEFERRED, wid))
    ctx.db.commit()
    r = ctx.client.post(f"/work-items/{wid}/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert core_db.cancel_work_item(ctx.db, wid) == "unknown"   # already gone


def test_cancel_is_admin_only(tmp_path):
    """Maintainers and regular users get a 403 — cancel deletes queue
       rows, an operator action like the re-arms."""
    from types import SimpleNamespace
    from core import auth
    db = core_db.connect(str(tmp_path / "hone.db"))
    core_db.add_methodology_version(db, {"name": "t", "version": 1})
    _plant_patchset(db)
    wid = core_db.enqueue_review(db, "<r1@x>")
    maint = auth.SessionUser(id=1, email="m@x", display_name="m",
                             is_config_admin=False, is_maintainer=True)
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: maint
    app.dependency_overrides[auth.require_csrf] = lambda: None
    app.state.db = db
    r = TestClient(app).post(f"/work-items/{wid}/cancel",
                             follow_redirects=False)
    assert r.status_code == 403
    assert db.execute("SELECT COUNT(*) AS n FROM work_items") \
             .fetchone()["n"] == 1


# --- deferral count + parked rendering ---------------------------------------

def test_work_item_page_shows_defer_count_and_parked(ctx):
    """A deferred row's badge carries the retry count, and 'parked' once
       the row hit DEFER_CAP (lease NULL); the queue badge matches."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    ctx.db.execute(
        "UPDATE work_items SET state=?, defer_count=5, lease_expires=NULL "
        "WHERE id=?", (core_db.WORK_ITEM_STATE_DEFERRED, wid))
    ctx.db.commit()
    body = ctx.client.get(f"/work-items/{wid}").text
    assert "×5" in body and "parked" in body
    body = ctx.client.get("/queue").text
    assert "deferred ×5 · parked" in body


def test_work_item_page_shows_claude_cli_provenance(ctx):
    """A record whose meta carries claude_cli_version renders it in the
       Completion record header — permanent attribution to the build
       that produced the result."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    wi = core_db.claim_work_item(ctx.db, "w1", methodology_version=1)
    core_db.submit_work_result(
        ctx.db, wi["claim_id"], state=core_db.WORK_ITEM_STATE_COMPLETED,
        record={"task_type": "review", "outcome": "reviewed",
                "meta": {"claude_cli_version": "2.1.161 (Claude Code)"}})
    body = ctx.client.get(f"/work-items/{wid}").text
    assert "Claude CLI" in body
    assert "2.1.161 (Claude Code)" in body


# --- work-item origin -------------------------------------------------------

def test_detail_page_shows_system_origin(ctx):
    """A pipeline-enqueued item (requested_by_user_id NULL) reads
       Origin: system — same attribution as the queue's Origin column."""
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>")
    body = ctx.client.get(f"/work-items/{wid}").text
    assert "Origin" in body
    assert "system" in body


def test_detail_page_shows_the_requesting_users_email(ctx):
    """A USER-origin item names who asked for it."""
    uid = core_db.create_user(ctx.db, "alice@x", "alice", "local")
    _plant_patchset(ctx.db)
    wid = core_db.enqueue_review(ctx.db, "<r1@x>",
                                 requested_by_user_id=uid)
    body = ctx.client.get(f"/work-items/{wid}").text
    assert "Origin" in body
    assert "alice@x" in body
