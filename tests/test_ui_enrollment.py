"""Tests for the operator node-management / enrollment UI (core/ui.py),
driven through FastAPI's TestClient. TestClient follows the redirect-after-POST,
so a POST assertion lands on the refreshed /nodes page."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import core_db, runtime_config, ui


@pytest.fixture
def ctx(tmp_path, fake_admin_session):
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.include_router(ui.router)
    fake_admin_session(app)
    app.state.db = db
    # /nodes now sorts rows into health buckets and needs the
    # runtime config's heartbeat threshold to decide what counts as
    # stale (see ui._nodes_view).
    app.state.runtime_config = runtime_config.load(
        str(tmp_path / "config.yaml"))
    return SimpleNamespace(client=TestClient(app), db=db)


def test_nodes_page_when_empty(ctx):
    r = ctx.client.get("/nodes")
    assert r.status_code == 200 and "No node is waiting" in r.text


def test_pending_enrollment_is_listed(ctx):
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7",
                                     task_types=["review"])
    r = ctx.client.get("/nodes")
    assert enr["user_code"] in r.text and "builder-7" in r.text
    assert "review" in r.text                            # capabilities cell


def test_enroll_page_looks_up_a_code(ctx):
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    r = ctx.client.get("/enroll", params={"code": enr["user_code"]})
    assert r.status_code == 200
    assert "builder-7" in r.text and "Approve" in r.text


def test_enroll_page_unknown_code(ctx):
    r = ctx.client.get("/enroll", params={"code": "ZZZZ-ZZZZ"})
    assert "No enrollment found" in r.text


def test_enroll_page_already_decided(ctx):
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    core_db.deny_enrollment(ctx.db, enr["user_code"])
    r = ctx.client.get("/enroll", params={"code": enr["user_code"]})
    assert "already denied" in r.text


def test_approve_enrollment(ctx):
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    r = ctx.client.post(f"/nodes/enrollments/{enr['user_code']}/approve")
    assert r.status_code == 200
    assert "No node is waiting" in r.text          # off the pending queue
    nodes = core_db.list_nodes(ctx.db)
    assert len(nodes) == 1 and nodes[0]["name"] == "builder-7"
    assert nodes[0]["state"] == core_db.NODE_STATE_ACTIVE


def test_deny_enrollment(ctx):
    enr = core_db.create_enrollment(ctx.db)
    r = ctx.client.post(f"/nodes/enrollments/{enr['user_code']}/deny")
    assert r.status_code == 200
    assert core_db.get_enrollment_by_user_code(
        ctx.db, enr["user_code"])["state"] \
        == core_db.NODE_ENROLLMENT_STATE_DENIED


def test_approve_silently_skips_a_now_conflicting_enrollment(ctx):
    """If a duplicate-name conflict materialises between when the
       pending enrollment was created and when the operator clicks
       Approve (e.g. another enrollment for the same name landed
       first), the click silently redirects rather than 500-ing. The
       row stays on the page; the operator can deny it."""
    # Plant a pending enrollment for "builder-7", then a separate
    # already-approved active node with the same name.
    enr_pending = core_db.create_enrollment(ctx.db, node_name="builder-7")
    # Manually wedge an active node into the table so the approve-time
    # guard fires (the create-time guard would have rejected, but the
    # race-protection check at approve_enrollment is what we want to
    # exercise here).
    ctx.db.execute(
        "INSERT INTO nodes (name, state, enrolled_at) VALUES (?, ?, 0)",
        ("builder-7", core_db.NODE_STATE_ACTIVE))
    ctx.db.commit()

    r = ctx.client.post(
        f"/nodes/enrollments/{enr_pending['user_code']}/approve")
    assert r.status_code == 200             # redirected to /nodes, no crash
    # The pending enrollment is still pending — operator can now deny it.
    enr = core_db.get_enrollment_by_user_code(
        ctx.db, enr_pending["user_code"])
    assert enr["state"] == core_db.NODE_ENROLLMENT_STATE_PENDING


def test_enrolled_node_renders_state_active(ctx):
    enr = core_db.create_enrollment(ctx.db, node_name="builder-9")
    core_db.approve_enrollment(ctx.db, enr["user_code"])
    r = ctx.client.get("/nodes")
    assert "builder-9" in r.text and "active" in r.text


# --- health snapshot rendering -------------------------------------------

def test_nodes_page_renders_a_clean_health_snapshot(ctx):
    """A node that has reported a healthy snapshot renders disk + repo
       sizes in operator-friendly units (GB > 1024 MB), and no error
       row — the `<i class="bi-exclamation-triangle-fill">` warning
       icon stays absent."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    core_db.update_node_health(ctx.db, node_id, {
        "free_disk_mb": 2048,                # 2.0 GB
        "refrepo_size_mb": 800,              # 800 MB
        "last_anthropic_error": None})
    body = ctx.client.get("/nodes").text
    assert "2.0 GB" in body and "800 MB" in body
    assert "exclamation-triangle-fill" not in body


def test_nodes_page_renders_a_health_warning_for_anthropic_errors(ctx):
    """A snapshot reporting last_anthropic_error="auth" surfaces a
       red warning row with the friendly label `auth key rejected`.
       Other categories (rate_limit, connection, other) get the
       same warning treatment with their own labels."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    core_db.update_node_health(ctx.db, node_id, {
        "free_disk_mb": 1024,
        "refrepo_size_mb": 4500,
        "last_anthropic_error": "auth"})
    body = ctx.client.get("/nodes").text
    assert "auth key rejected" in body
    assert "exclamation-triangle-fill" in body
    assert "text-danger" in body


# --- node detail page ----------------------------------------------------

def test_node_detail_renders_header_health_claims_and_reviews(ctx):
    """The per-node detail page mirrors the patchset detail layout —
       header card + health card + recent claims + recent reviews."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    core_db.update_node_health(ctx.db, node_id, {
        "free_disk_mb": 2048, "refrepo_size_mb": 800,
        "last_anthropic_error": None})
    # Plant a claim and a review attributed to this node.
    core_db.add_methodology_version(ctx.db, {"name": "t", "version": 1})
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="a patch", n_patches=1)
    core_db.upsert_patchset_metadata(
        ctx.db, "<r1@x>", mode="heuristic",
        tree_state={}, subsystem={"primary": "n"},
        patch_size={"bucket": "small"}, maintainer={},
        patch_type={"primary": "bugfix"},
        review_intensity={"bucket_overall": "light"},
        preparation_notes={})
    core_db.enqueue_review(ctx.db, "<r1@x>")
    core_db.claim_work_item(
        ctx.db, "builder-7", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    core_db.upsert_ai_review(
        ctx.db, "<r1@x>", concerns=[], model="claude-opus-4-7",
        node_id=node_id)

    r = ctx.client.get(f"/nodes/{node_id}")
    assert r.status_code == 200
    body = r.text
    # Header + identity
    assert "builder-7" in body
    assert f"node id <code>{node_id}</code>" in body
    # Health card
    assert "2.0 GB" in body and "800 MB" in body
    assert "clean" in body                          # no error category
    # Recent claims table
    assert "Recent claims" in body
    assert "a patch" in body
    # Reviews produced
    assert "Reviews produced" in body
    # ← Back default → /nodes
    assert 'href="/nodes"' in body


def test_node_detail_renders_refrepo_churn_signals(ctx):
    """The reference-repo instrumentation (anchors / last fetch / last gc)
       renders on the detail Health card in operator-friendly units, so the
       delta-vs-full-refetch signal is visible without a DB query."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    core_db.update_node_health(ctx.db, node_id, {
        "free_disk_mb": 2048, "refrepo_size_mb": 800,
        "last_anthropic_error": None,
        "refrepo_tracking_refs": 7,
        "refrepo_fetch": {"commit": "deadbeefcafe", "remote": "stable",
                          "objects_added": 4231, "ms": 1200},
        "refrepo_gc": {"size_mb_before": 8300, "size_mb_after": 1200,
                       "tracking_refs": 7, "ms": 3400, "ok": True}})
    body = ctx.client.get(f"/nodes/{node_id}").text
    assert "Ancestry anchors" in body
    assert "Last base fetch" in body and "4,231 objs from stable" in body
    assert "Last gc" in body and "8.1 GB" in body and "3.4 s" in body
    assert "failed" not in body                       # ok=True → no failure mark


def test_node_detail_omits_refrepo_rows_for_old_snapshot(ctx):
    """A snapshot predating the instrumentation (no refrepo_* fields) adds
       no churn rows — the card degrades to the original four."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    core_db.update_node_health(ctx.db, node_id, {
        "free_disk_mb": 2048, "refrepo_size_mb": 800,
        "last_anthropic_error": None})
    body = ctx.client.get(f"/nodes/{node_id}").text
    assert "Ancestry anchors" not in body
    assert "Last base fetch" not in body and "Last gc" not in body


def test_refrepo_health_display_partials_and_failure():
    """Unit-level: each sub-field is independent (a fetched-but-not-gc'd
       node shows the fetch, omits gc), and a failed gc carries ok=False
       for the template's failure mark. A pre-instrumentation snapshot
       returns None so no rows render."""
    from core import ui
    assert ui._refrepo_health_display({"free_disk_mb": 1}) is None
    # anchors + fetch present, no gc yet (just restarted, one fetch done)
    d = ui._refrepo_health_display({
        "refrepo_tracking_refs": 3,
        "refrepo_fetch": {"remote": "mainline", "objects_added": 50, "ms": 90}})
    assert d["anchors"] == "3" and "from mainline" in d["fetch"]
    assert "90 ms" in d["fetch"] and "gc" not in d
    # failed gc surfaces ok=False
    d2 = ui._refrepo_health_display({
        "refrepo_tracking_refs": 0,
        "refrepo_gc": {"size_mb_before": 5, "size_mb_after": 5,
                       "tracking_refs": 0, "ms": 10, "ok": False}})
    assert d2["gc_ok"] is False and d2["anchors"] == "0"
    # a None objects_added (count-objects failed) degrades to em-dash
    d3 = ui._refrepo_health_display({
        "refrepo_fetch": {"remote": "net", "objects_added": None, "ms": 5}})
    assert "—" in d3["fetch"]


def test_node_detail_404_for_unknown_id(ctx):
    r = ctx.client.get("/nodes/99999")
    assert r.status_code == 404


def test_node_detail_back_link_honours_query_param(ctx):
    """Same shared `?back=` pattern as the patchset detail page — an
       opener passes its current URL so ← Back returns there."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    from urllib.parse import quote
    r = ctx.client.get(f"/nodes/{node_id}?back={quote('/nodes?x=1', safe='')}")
    assert r.status_code == 200
    # Jinja escapes `&` so the path "?x=1" (no &) renders verbatim.
    assert 'href="/nodes?x=1"' in r.text


def test_node_detail_rejects_offsite_back_url(ctx):
    """Same-origin paths only — open-redirect guard is the shared
       _safe_back helper used by both detail pages."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    from urllib.parse import quote
    r = ctx.client.get(
        f"/nodes/{node_id}?back={quote('https://attacker.example/', safe='')}")
    assert r.status_code == 200
    assert "attacker.example" not in r.text


def test_nodes_list_rows_link_to_the_detail_page(ctx):
    """Each enrolled-node row carries a data-href to its detail page,
       and the Name cell is a real anchor for keyboard / screen-reader
       navigation. The ?back= round-trips the current /nodes URL so
       ← Back from the detail page returns the operator here."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    body = ctx.client.get("/nodes").text
    from urllib.parse import quote
    expected = f'/nodes/{node_id}?back={quote("/nodes", safe="")}'
    assert f'data-href="{expected}"' in body
    assert f'href="{expected}"' in body              # the Name anchor


def test_nodes_page_shows_em_dash_for_nodes_that_havent_reported(ctx):
    """A freshly-enrolled node hasn't posted its first health snapshot
       yet — the Health column shows a single em-dash rather than
       three (one per field) so the operator can spot "this is a new
       node, give it a moment"."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    core_db.approve_enrollment(ctx.db, enr["user_code"])
    body = ctx.client.get("/nodes").text
    # The Health column for this row has exactly one — and no disk:
    # / repo: tags, no warning icon. We pin to "builder-7" then check
    # the slice up to the next row's </tr>.
    row = body.split("builder-7", 1)[1].split("</tr>", 1)[0]
    assert "disk:" not in row and "repo:" not in row
    assert "exclamation-triangle-fill" not in row


# --- delete an enrolled node ---------------------------------------------

def test_nodes_page_renders_a_delete_form_per_enrolled_row(ctx):
    """Each enrolled-node row carries a POST form that targets the
       delete endpoint and hooks the shared confirm-modal via the
       `data-confirm` attribute. A fat-finger click on Delete is
       intercepted by the modal in base.html, themed to match the
       rest of the UI."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    body = ctx.client.get("/nodes").text
    assert f'action="/nodes/{node_id}/delete"' in body
    assert "Delete</button>" in body
    # The data-confirm attribute carries the message the modal shows;
    # data-confirm-title + data-confirm-button parameterise the modal
    # chrome. base.html ships the shared modal markup + handler.
    assert "data-confirm=" in body and "builder-7" in body
    assert 'data-confirm-button="Delete"' in body
    assert 'id="confirm-modal"' in body


def test_delete_node_via_ui_removes_the_node_and_redirects(ctx):
    """A POST to /nodes/{id}/delete drops the row, kills tokens, and
       lands back on the refreshed /nodes page where the row is gone."""
    enr = core_db.create_enrollment(ctx.db, node_name="builder-7")
    node_id = core_db.approve_enrollment(ctx.db, enr["user_code"])
    tok = core_db.issue_tokens(ctx.db, node_id)

    r = ctx.client.post(f"/nodes/{node_id}/delete")
    assert r.status_code == 200             # TestClient follows the redirect
    assert "builder-7" not in r.text        # off the enrolled list
    assert core_db.get_node(ctx.db, node_id) is None
    assert core_db.resolve_access_token(ctx.db, tok["access_token"]) is None


def test_delete_node_via_ui_is_idempotent_on_unknown_id(ctx):
    """A double-submit from a stale tab — or a click against an
       already-deleted node — must not 500; it just redirects to
       /nodes again."""
    r = ctx.client.post("/nodes/99999/delete")
    assert r.status_code == 200             # redirect followed, no crash
