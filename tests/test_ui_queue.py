"""Tests for the work-queue home page (core/ui.py GET /) — review + train
work items rendered via core_db.list_work_items / work_item_counts."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.version import __version__
from core import core_db, ui


@pytest.fixture
def ctx(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    # claim_work_item stamps methodology_version on the row, which
    # FK-references methodology_versions — plant one for the tests
    # that exercise the queue's claimed-state branches.
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    app = FastAPI()
    app.include_router(ui.router)
    app.state.db = db
    return SimpleNamespace(client=TestClient(app), db=db)


def _plant_metadata(db, root):
    core_db.upsert_patchset_metadata(
        db, root, mode="heuristic",
        tree_state={}, subsystem={"primary": "net"},
        patch_size={"bucket": "small"}, maintainer={},
        patch_type={"primary": "bugfix"},
        review_intensity={"bucket_overall": "light"},
        preparation_notes={})


def _enqueue_review(db, root, subject):
    core_db.upsert_patchset(db, root, subject=subject, n_patches=1)
    _plant_metadata(db, root)
    return core_db.enqueue_review(db, root)


def _enqueue_train(db, root, message_id, subject="patch one"):
    """Plant a session-bound train work-item for queue-UI fixtures.

       Trains are exclusively session-driven; this helper sets up a
       minimal session + per-(patch, comment) pair and creates the
       work-item through the session orchestrator's entrypoint."""
    core_db.upsert_patchset(db, root, subject=subject, n_patches=1)
    _plant_metadata(db, root)
    core_db.upsert_message(db, message_id,
                           root_message_id=root,
                           type=core_db.MSG_TYPE_PATCH, body="diff",
                           part_index=1)
    comment_id = f"<c{message_id.strip('<>')}>"
    core_db.upsert_message(db, comment_id, root_message_id=root,
                           type=core_db.MSG_TYPE_COMMENT, body="nack",
                           parent_message_id=message_id)
    core_db.upsert_ai_review(db, root, concerns=[])
    sid = core_db.create_session_draft(db, "standard")
    return core_db.enqueue_session_train(
        db, session_id=sid, root_message_id=root,
        patch_message_id=message_id, comment_message_id=comment_id,
        session_role=core_db.SESSION_ROLE_POOL, stratum_label="net:light")


def test_queue_home_page_empty(ctx):
    r = ctx.client.get("/")
    assert r.status_code == 200
    assert "Work queue" in r.text and "queue is empty" in r.text
    assert f"hone-core-{__version__}" in r.text          # base-template footer


def test_queue_sorted_by_most_recent_activity(ctx):
    """list_work_items orders rows by COALESCE(completed_at, claimed_at,
       enqueued_at) DESC, so any state change bubbles the row to the
       top — the operator's open queue page always shows where the
       fleet is currently working. See core_db.list_work_items."""
    # Three patchsets, planted with explicit distinct enqueued_at
    # timestamps. Real-world gather batches share a single second; the
    # test fakes spread so the sort key isn't tied (the id DESC
    # tiebreaker would otherwise mask the bubbling).
    _enqueue_review(ctx.db, "<r-old@x>",    "oldest-subj")
    _enqueue_review(ctx.db, "<r-mid@x>",    "middle-subj")
    _enqueue_review(ctx.db, "<r-newest@x>", "newest-subj")
    ctx.db.executemany(
        "UPDATE work_items SET enqueued_at=? WHERE root_message_id=?",
        [(1000, "r-old@x"), (2000, "r-mid@x"), (3000, "r-newest@x")])
    ctx.db.commit()
    # The FIFO claim picker takes the oldest row — claimed_at is
    # int(time.time()), several orders of magnitude larger than our
    # synthetic 1000-3000 enqueued_at values, so the claimed row's
    # COALESCE wins and it bubbles to the TOP.
    claim = core_db.claim_work_item(
        ctx.db, "worker-1", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    assert claim is not None
    body = ctx.client.get("/").text
    # The just-claimed row's subject should appear BEFORE the others in
    # the rendered listing — even though it was enqueued first.
    assert (body.index("oldest-subj")
            < body.index("newest-subj")
            < body.index("middle-subj"))


def test_queue_pane_carries_htmx_polling_attributes(ctx):
    """The #queue-pane wrapper polls its current URL every 5s and swaps
       itself with outerHTML — so new rows + updated chip counts appear
       without a manual reload. See _queue_pane.html."""
    _enqueue_review(ctx.db, "<r-1@x>", "a queued patchset")
    r = ctx.client.get("/?type=review")
    assert r.status_code == 200
    body = r.text
    # The polling URL preserves the active filter (so the auto-refresh
    # stays scoped to whatever view the operator is currently watching).
    assert 'id="queue-pane"' in body
    assert 'hx-get="/?type=review"' in body
    assert 'hx-trigger="every 5s"' in body
    assert 'hx-target="#queue-pane"' in body
    assert 'hx-swap="outerHTML"' in body
    # The pane echoes the current queue version back on every poll, so
    # the handler can 204 a tick when nothing has changed.
    assert 'X-Queue-Version' in body


# --- 304-style short-circuit ----------------------------------------------

def test_queue_version_changes_only_on_activity(ctx):
    """queue_version moves on enqueue, on claim, and on result; two reads
       with no activity in between return the same value."""
    v0 = core_db.queue_version(ctx.db)
    assert core_db.queue_version(ctx.db) == v0           # idempotent
    _enqueue_review(ctx.db, "<r-1@x>", "p")
    v_enqueued = core_db.queue_version(ctx.db)
    assert v_enqueued != v0                              # enqueue moves it
    core_db.claim_work_item(
        ctx.db, "w-1", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    v_claimed = core_db.queue_version(ctx.db)
    assert v_claimed != v_enqueued                       # claim moves it
    assert core_db.queue_version(ctx.db) == v_claimed    # then settles


def test_queue_version_is_filter_scoped(ctx):
    """queue_version is computed over the filtered set, so a `type=train`
       polling client doesn't get bumped when only review rows change."""
    _enqueue_review(ctx.db, "<r-1@x>", "p")
    v_review = core_db.queue_version(
        ctx.db, type=core_db.WORK_ITEM_TYPE_REVIEW)
    v_train  = core_db.queue_version(
        ctx.db, type=core_db.WORK_ITEM_TYPE_TRAIN)
    # New review row → the train-filtered version is unchanged.
    _enqueue_review(ctx.db, "<r-2@x>", "p2")
    assert core_db.queue_version(
        ctx.db, type=core_db.WORK_ITEM_TYPE_REVIEW) != v_review
    assert core_db.queue_version(
        ctx.db, type=core_db.WORK_ITEM_TYPE_TRAIN) == v_train


def test_hx_request_with_matching_version_returns_204(ctx):
    """An HTMX auto-poll whose X-Queue-Version matches the current
       version gets a 204 — HTMX then skips the swap. The auto-poll
       fires on the #queue-pane wrapper itself, so HX-Trigger is the
       wrapper's id; the handler keys the short-circuit on that to
       avoid mis-204-ing descendant clicks."""
    _enqueue_review(ctx.db, "<r-1@x>", "p")
    version = core_db.queue_version(ctx.db)
    r = ctx.client.get("/", headers={"HX-Request": "true",
                                       "HX-Trigger": "queue-pane",
                                       "X-Queue-Version": version})
    assert r.status_code == 204
    assert r.text == ""


def test_hx_request_with_stale_version_returns_200_with_new_version(ctx):
    """A poll with a stale (or missing) X-Queue-Version is the cache
       miss: the server renders the partial and embeds the new version
       in the wrapper's hx-headers attribute. The next poll will then
       round-trip that fresh value."""
    _enqueue_review(ctx.db, "<r-1@x>", "p")
    fresh_version = core_db.queue_version(ctx.db)
    r = ctx.client.get("/", headers={"HX-Request": "true",
                                       "HX-Trigger": "queue-pane",
                                       "X-Queue-Version": "stale-0"})
    assert r.status_code == 200
    assert fresh_version in r.text


def test_pagination_click_with_matching_version_still_renders(ctx):
    """REGRESSION: paginator links inside #queue-pane inherit the
       wrapper's `hx-headers` (X-Queue-Version), so a click on page 2
       sent the current version back. The handler used to 204 on that
       — HTMX would then skip the swap and the operator would stay
       on page 1. The short-circuit must only apply to the wrapper's
       OWN auto-poll (HX-Trigger="queue-pane"), not to descendant
       clicks (no HX-Trigger or a link's id)."""
    _seed_n_reviews(ctx.db, 40)
    version = core_db.queue_version(ctx.db)
    # Pagination click: HX-Request true, X-Queue-Version current
    # (inherited from wrapper), HX-Trigger absent because the link
    # has no id. Must render page 2, not 204.
    r = ctx.client.get("/", params={"page": 2},
                       headers={"HX-Request": "true",
                                "X-Queue-Version": version})
    assert r.status_code == 200
    assert "Showing <strong>26</strong>" in r.text


def test_full_page_load_never_short_circuits(ctx):
    """A real browser navigation always renders, even when the client
       (somehow) sent X-Queue-Version. We only short-circuit HTMX
       polls — never a fresh page load."""
    _enqueue_review(ctx.db, "<r-1@x>", "p")
    version = core_db.queue_version(ctx.db)
    r = ctx.client.get("/", headers={"X-Queue-Version": version})
    assert r.status_code == 200
    assert "Work queue" in r.text         # full base.html chrome rendered


def test_queue_htmx_partial_returns_self_renewing_pane(ctx):
    """An HTMX request returns just the pane (chips + body + the
       self-renewing hx-* wrapper) — NOT the full base.html chrome.
       Without this, polling would re-inject the sidebar / footer
       inside itself."""
    _enqueue_review(ctx.db, "<r-1@x>", "p")
    r = ctx.client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 200
    body = r.text
    assert 'id="queue-pane"' in body
    assert 'hx-trigger="every 5s"' in body
    # The full-page chrome is OUT of the partial — no <html>, no sidebar.
    assert "<html" not in body
    assert "app-sidebar" not in body


def test_queue_lists_a_review_work_item(ctx):
    _enqueue_review(ctx.db, "<r-1@x>", "a queued patchset")
    body = ctx.client.get("/").text
    assert "a queued patchset" in body
    assert "claimable" in body and "review" in body


def test_queue_lists_review_and_train_items(ctx):
    _enqueue_review(ctx.db, "<r-1@x>", "rv subject")
    _enqueue_train(ctx.db, "<r-2@x>", "<p-2@x>", "tr subject")
    body = ctx.client.get("/").text
    assert "rv subject" in body and "tr subject" in body
    assert "review" in body and "train" in body


def test_work_item_counts_zero_filled(ctx):
    _enqueue_review(ctx.db, "<r-1@x>", "one")
    _enqueue_review(ctx.db, "<r-2@x>", "two")
    core_db.claim_work_item(ctx.db, worker_id="node-1",
                            methodology_version=1,
                            types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    counts = core_db.work_item_counts(ctx.db)
    assert counts[core_db.WORK_ITEM_TYPE_REVIEW] == {
        core_db.WORK_ITEM_STATE_CLAIMABLE:   1,
        core_db.WORK_ITEM_STATE_CLAIMED:     1,
        core_db.WORK_ITEM_STATE_COMPLETED:   0,
        core_db.WORK_ITEM_STATE_UNAPPLIABLE: 0,
        core_db.WORK_ITEM_STATE_DEFERRED:    0}
    assert counts[core_db.WORK_ITEM_TYPE_TRAIN] == {
        s: 0 for s in core_db.WORK_ITEM_STATE_NAMES}


def test_queue_type_filter_partitions_review_and_train(ctx):
    _enqueue_review(ctx.db, "<r-1@x>", "review-only subject")
    _enqueue_train(ctx.db, "<r-2@x>", "<p-2@x>", "train-only subject")
    review_only = ctx.client.get("/", params={"type": "review"}).text
    train_only  = ctx.client.get("/", params={"type": "train"}).text
    assert "review-only subject" in review_only
    assert "train-only subject" not in review_only
    assert "train-only subject" in train_only
    assert "review-only subject" not in train_only


def test_queue_state_filter_partitions_the_listing(ctx):
    _enqueue_review(ctx.db, "<r-1@x>", "patchset one")
    _enqueue_review(ctx.db, "<r-2@x>", "patchset two")
    core_db.claim_work_item(ctx.db, worker_id="node-1",
                            methodology_version=1,
                            types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    claimable = ctx.client.get("/", params={"state": "claimable"}).text
    claimed   = ctx.client.get("/", params={"state": "claimed"}).text
    for subject in ("patchset one", "patchset two"):
        assert (subject in claimable) != (subject in claimed)


def test_queue_ignores_unknown_axis_values(ctx):
    _enqueue_review(ctx.db, "<r-1@x>", "a queued patchset")
    r = ctx.client.get("/", params={"state": "bogus", "type": "bogus"})
    assert r.status_code == 200 and "a queued patchset" in r.text


def test_queue_chips_show_per_axis_counts(ctx):
    _enqueue_review(ctx.db, "<r-1@x>", "review one")
    _enqueue_train(ctx.db, "<r-2@x>", "<p-2@x>", "train one")
    body = ctx.client.get("/").text
    # type chips: All=2, review=1, train=1
    assert "Type:" in body and "Work state:" in body
    # filter URLs round-trip the axes
    assert 'href="/?type=review"' in body
    assert 'href="/?state=claimable"' in body


def test_queue_type_and_state_compose(ctx):
    _enqueue_review(ctx.db, "<r-1@x>", "review-claimable")
    _enqueue_train(ctx.db, "<r-2@x>", "<p-2@x>", "train-claimable")
    r = ctx.client.get("/", params={"type": "review", "state": "claimable"})
    assert "review-claimable" in r.text
    assert "train-claimable" not in r.text


# --- pagination ------------------------------------------------------------

def _seed_n_reviews(db, n):
    """Enqueue `n` review work-items oldest-first so the LATEST has the
       highest enqueued_at (matching the queue's DESC ordering)."""
    for i in range(n):
        _enqueue_review(db, f"<r{i:03d}@x>", f"subject {i:03d}")


def test_count_work_items_matches_filter(ctx):
    _enqueue_review(ctx.db, "<r-1@x>", "one")
    _enqueue_review(ctx.db, "<r-2@x>", "two")
    _enqueue_train(ctx.db, "<r-3@x>", "<p-3@x>", "three")
    assert core_db.count_work_items(ctx.db) == 3
    assert core_db.count_work_items(
        ctx.db, type=core_db.WORK_ITEM_TYPE_REVIEW) == 2
    assert core_db.count_work_items(
        ctx.db, type=core_db.WORK_ITEM_TYPE_TRAIN) == 1


def test_list_work_items_respects_offset(ctx):
    _seed_n_reviews(ctx.db, 10)
    page1 = core_db.list_work_items(ctx.db, limit=4, offset=0)
    page2 = core_db.list_work_items(ctx.db, limit=4, offset=4)
    assert [r["id"] for r in page1] != [r["id"] for r in page2]
    assert len(page1) == 4 and len(page2) == 4
    # contiguous: the second page starts after the first ends
    assert set(r["id"] for r in page1).isdisjoint(r["id"] for r in page2)


def test_queue_page_paginates_at_default_size(ctx):
    """Default size is 25 — with 40 items the page shows 25 + a paginator."""
    _seed_n_reviews(ctx.db, 40)
    body = ctx.client.get("/").text
    # paginator renders + indicates "Showing 1–25 of 40"
    assert 'aria-label="Pagination"' in body
    assert "Showing <strong>1</strong>" in body
    assert "of <strong>40</strong>" in body
    # numbered links present: 1 (active) and 2
    assert 'aria-label="Next page"' in body


def test_queue_paginator_hidden_when_one_page(ctx):
    _seed_n_reviews(ctx.db, 3)
    body = ctx.client.get("/").text
    assert 'aria-label="Pagination"' not in body


def test_queue_page_2_shows_the_next_slice(ctx):
    _seed_n_reviews(ctx.db, 40)
    body = ctx.client.get("/", params={"page": 2}).text
    # the latest (highest-numbered) subjects are on page 1; page 2 shows
    # the older subjects (smaller numbers).
    assert "Showing <strong>26</strong>" in body
    assert "of <strong>40</strong>" in body


def test_queue_size_clamps_to_allowed_set(ctx):
    """`?size=` outside the allowed PAGE_SIZES set falls back to the
       default — guards against attacker-supplied giant page sizes."""
    _seed_n_reviews(ctx.db, 40)
    body = ctx.client.get("/", params={"size": "999999"}).text
    # default 25 applied; paginator shows 25/page
    assert "Showing <strong>1</strong>–<strong>25</strong>" in body


def test_queue_partial_swap_for_htmx_requests(ctx):
    """An HTMX-driven page click sends `HX-Request: true`; the handler
       returns just the swap-target body partial, no base layout chrome."""
    _seed_n_reviews(ctx.db, 40)
    r = ctx.client.get("/", params={"page": 2},
                       headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'id="queue-body"' in r.text
    assert "<html" not in r.text          # no base layout
    assert "Showing <strong>26</strong>" in r.text


def test_queue_filter_chips_reset_page(ctx):
    """Changing filter shouldn't carry the operator's old page number —
       a new filter starts on page 1. The chip URLs drop `page=`."""
    _seed_n_reviews(ctx.db, 75)
    body = ctx.client.get("/", params={"page": 2}).text
    # the chip URLs do NOT carry the page parameter
    assert 'href="/?state=claimable"' in body
    assert 'href="/?state=claimable&page=' not in body


def test_queue_paging_preserves_filter(ctx):
    """Paginator links carry the active type filter — clicking page 2
       stays on the same filter, doesn't reset to All. (`&` is rendered
       as `&amp;` because Jinja autoescapes attribute values.)"""
    _seed_n_reviews(ctx.db, 75)
    body = ctx.client.get("/", params={"type": "review"}).text
    assert 'href="/?type=review&amp;page=2"' in body


def test_queue_paging_bar_mirrors_top_and_bottom(ctx):
    """The whole 3-col paging bar — Showing | paginator | Per page —
       renders both above and below the items table. The operator gets
       the same controls and the same Showing/total readout at both ends
       so they don't have to scroll to flip pages or change page size."""
    _seed_n_reviews(ctx.db, 75)
    body = ctx.client.get("/").text
    # both paginator <nav>s (the macro is invoked from each paging_bar)
    assert body.count('aria-label="Pagination"') == 2
    # Showing/total readout appears in both bars
    assert body.count("Showing <strong>1</strong>") == 2
    assert body.count("Per page:") == 2
    # the centered middle column appears in both bars
    assert body.count('class="col-4 text-center"') == 2


def test_queue_paging_bar_renders_when_one_page(ctx):
    """With only one page, both bars still render — Showing + Per page —
       but the centered middle column is empty (no paginator). Layout
       stays the same shape whether or not paging is needed."""
    _seed_n_reviews(ctx.db, 3)
    body = ctx.client.get("/").text
    assert 'aria-label="Pagination"' not in body
    # both bars still render
    assert body.count("Showing <strong>1</strong>") == 2
    assert body.count("Per page:") == 2
    assert body.count('class="col-4 text-center"') == 2


def test_queue_paging_bar_select_has_no_duplicate_id(ctx):
    """The size <select> renders twice (top + bottom) so it can't carry
       an `id` — duplicate IDs are invalid HTML. The label associates
       implicitly by visual proximity."""
    _seed_n_reviews(ctx.db, 75)
    body = ctx.client.get("/").text
    assert 'id="page-size"' not in body
