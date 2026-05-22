"""Tests for the review-queue home page (core/ui.py GET /) and the queue
query helpers in core_db."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import core_db, ui


@pytest.fixture
def ctx(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.include_router(ui.router)
    app.state.db = db
    return SimpleNamespace(client=TestClient(app), db=db)


def _enqueue(db, root, subject, source="sashiko"):
    core_db.upsert_patchset(db, root, subject=subject, source=source)
    core_db.enqueue_reviews_for_patchset(db, root)


def test_queue_home_page_empty(ctx):
    r = ctx.client.get("/")
    assert r.status_code == 200
    assert "Review queue" in r.text and "queue is empty" in r.text


def test_queue_lists_an_enqueued_review(ctx):
    _enqueue(ctx.db, "<r-1@x>", "a queued patchset")
    body = ctx.client.get("/").text
    assert "a queued patchset" in body and "claimable" in body
    assert "sashiko" in body


def test_review_counts(ctx):
    _enqueue(ctx.db, "<r-1@x>", "one")
    _enqueue(ctx.db, "<r-2@x>", "two")
    core_db.claim_review(ctx.db, "node-1")          # one -> claimed
    assert core_db.review_counts(ctx.db) == {
        "claimable": 1, "claimed": 1, "reviewed": 0,
        "unappliable": 0, "deferred": 0}


def test_queue_state_filter_partitions_the_listing(ctx):
    _enqueue(ctx.db, "<r-1@x>", "patchset one")
    _enqueue(ctx.db, "<r-2@x>", "patchset two")
    core_db.claim_review(ctx.db, "node-1")          # claims one of the two
    claimable = ctx.client.get("/", params={"state": "claimable"}).text
    claimed = ctx.client.get("/", params={"state": "claimed"}).text
    # each patchset shows in exactly one of the two filtered views
    for subject in ("patchset one", "patchset two"):
        assert (subject in claimable) != (subject in claimed)


def test_queue_ignores_an_unknown_state_filter(ctx):
    _enqueue(ctx.db, "<r-1@x>", "a queued patchset")
    r = ctx.client.get("/", params={"state": "bogus"})
    assert r.status_code == 200 and "a queued patchset" in r.text
