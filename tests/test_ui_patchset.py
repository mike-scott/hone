"""Tests for the per-patchset detail page (core/ui.py GET /patchsets/{root})
and the work-queue's row → detail wiring."""
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import core_db, ui


@pytest.fixture
def ctx(tmp_path):
    db = core_db.connect(str(tmp_path / "hone.db"))
    # claim_work_item stamps methodology_version on the row; the tests
    # below claim a row to exercise the work-item history table.
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    app = FastAPI()
    app.include_router(ui.router)
    app.state.db = db
    return SimpleNamespace(client=TestClient(app), db=db)


def _plant_patchset(db, root="<r1@x>", subject="[PATCH 1/1] frob"):
    core_db.upsert_patchset(db, root, subject=subject, n_patches=1,
                             submitter_email="alice@example.com")
    core_db.upsert_patchset_metadata(
        db, root, mode="heuristic",
        tree_state={"tree_available": False},
        subsystem={"primary": "drivers/net"},
        patch_size={"bucket": "small"},
        maintainer={"primary": "alice@k.org"},
        patch_type={"primary": "bugfix"},
        review_intensity={"bucket_overall": "light", "per_reply": []},
        preparation_notes={"mode": "heuristic"})
    core_db.upsert_message(db, "<p1@x>", root_message_id=root,
                            type=core_db.MSG_TYPE_PATCH, body="--- patch ---",
                            part_index=1, subject=subject,
                            author_email="alice@example.com")
    core_db.upsert_message(db, "<c1@x>", root_message_id=root,
                            type=core_db.MSG_TYPE_COMMENT,
                            body="LGTM",
                            parent_message_id="<p1@x>",
                            subject="Re: " + subject,
                            author_email="bob@kernel.org")


# --- detail page renders --------------------------------------------------

def test_patchset_detail_renders_header_metadata_and_thread(ctx):
    _plant_patchset(ctx.db)
    r = ctx.client.get(f"/patchsets/{quote('r1@x')}")
    assert r.status_code == 200
    body = r.text
    # Header
    assert "[PATCH 1/1] frob" in body
    assert "r1@x" in body
    assert "alice@example.com" in body
    # Metadata card
    assert "heuristic" in body
    assert "drivers/net" in body
    # Thread
    assert "p1@x" in body and "c1@x" in body
    assert "bob@kernel.org" in body
    # ← Back button defaults to "/"
    assert 'href="/"' in body


def test_patchset_detail_back_link_honours_query_param(ctx):
    """The opener-passed `?back=` URL drives the ← Back link, so the
       operator returns to the same filtered / paged queue view."""
    _plant_patchset(ctx.db)
    back = "/?type=review&state=claimable&page=2"
    r = ctx.client.get(
        f"/patchsets/{quote('r1@x')}?back={quote(back, safe='')}")
    assert r.status_code == 200
    # Jinja2 auto-escapes `&` in attribute values, so we look for the
    # HTML-escaped form.
    escaped = back.replace("&", "&amp;")
    assert f'href="{escaped}"' in r.text


def test_patchset_detail_rejects_offsite_back_url(ctx):
    """Same-origin paths only — an absolute or protocol-relative URL is
       dropped (open-redirect guard) and the link falls back to `/`."""
    _plant_patchset(ctx.db)
    for evil in ("https://attacker.example/", "//attacker.example/",
                  "javascript:alert(1)"):
        r = ctx.client.get(
            f"/patchsets/{quote('r1@x')}?back={quote(evil, safe='')}")
        assert r.status_code == 200
        # The back-link `href` should be `/`, not the malicious URL.
        # We assert on the malicious URL's absence rather than an exact
        # match because `/` also appears in other anchor href values
        # (Queue sidebar item, etc.).
        assert evil not in r.text


def test_patchset_detail_renders_work_item_history(ctx):
    _plant_patchset(ctx.db)
    core_db.enqueue_review(ctx.db, "<r1@x>")
    claim = core_db.claim_work_item(
        ctx.db, "node-1", methodology_version=1,
        types=(core_db.WORK_ITEM_TYPE_REVIEW,))
    assert claim is not None
    r = ctx.client.get(f"/patchsets/{quote('r1@x')}")
    assert r.status_code == 200
    body = r.text
    # The work-item history table renders the type, the claim worker,
    # and the methodology_version that was stamped on the row at claim.
    assert "review" in body and "node-1" in body
    # methodology_version=1 appears in the work-item history cell.
    assert "Methodology v" in body


def test_patchset_detail_404_for_unknown_root(ctx):
    r = ctx.client.get(f"/patchsets/{quote('not-a-real-root@x')}")
    assert r.status_code == 404


# --- AI review producer attribution --------------------------------------

def _approved_node(db, name):
    enr = core_db.create_enrollment(db, node_name=name)
    return core_db.approve_enrollment(db, enr["user_code"])


def test_ai_review_renders_the_producing_node_name(ctx):
    """When the ai_review row's node_id resolves to a current node,
       the detail page renders the node's name next to the review
       summary — operator can tell at a glance which node produced
       which review."""
    _plant_patchset(ctx.db)
    node_id = _approved_node(ctx.db, "builder-7")
    core_db.upsert_ai_review(ctx.db, "<r1@x>", concerns=[],
                              node_id=node_id, model="claude-opus-4-7")
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "by <strong>builder-7</strong>" in body


def test_ai_review_drops_attribution_after_node_deletion(ctx):
    """delete_node nulls out ai_reviews.node_id (the FK forces it to,
       otherwise the DELETE would fail). The detail page therefore
       drops the `by …` clause entirely after a delete — the review
       row stays, the producer label goes."""
    _plant_patchset(ctx.db)
    node_id = _approved_node(ctx.db, "builder-7")
    core_db.upsert_ai_review(ctx.db, "<r1@x>", concerns=[],
                              node_id=node_id, model="m")
    # Before delete: producer renders.
    assert "builder-7" in ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    core_db.delete_node(ctx.db, node_id)
    # After delete: producer line is gone, review row is intact.
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert " by <strong>" not in body
    assert "AI review" in body                       # row itself survives


def test_ai_review_renders_revoked_node_name(ctx):
    """A revoked node is a tombstone — the row still exists, so the FK
       still resolves, and the review remains attributed to that
       (now revoked) node. Operator looking at history can tell a
       review was produced by a node they later revoked."""
    _plant_patchset(ctx.db)
    node_id = _approved_node(ctx.db, "builder-7")
    core_db.upsert_ai_review(ctx.db, "<r1@x>", concerns=[],
                              node_id=node_id, model="m")
    core_db.revoke_node(ctx.db, node_id)
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "by <strong>builder-7</strong>" in body


def test_ai_review_skips_attribution_when_node_id_is_null(ctx):
    """Legacy rows from before the audit fix landed have NULL node_id.
       The page renders the review summary without a `by …` clause —
       no `<deleted>` noise, just the historical record."""
    _plant_patchset(ctx.db)
    core_db.upsert_ai_review(ctx.db, "<r1@x>", concerns=[],
                              node_id=None, model="m")
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert " by <strong>" not in body
    assert "&lt;deleted&gt;" not in body


# --- queue row → detail wiring --------------------------------------------

def test_queue_row_links_to_the_work_item_detail_page(ctx):
    """Each queue row links to /work-items/{id} (the queue is a list
       of work-items — clicking a row should drill into the work-item,
       not the patchset). The current queue URL is carried in ?back=
       so the work-item detail's ← Back returns to this exact view."""
    _plant_patchset(ctx.db)
    work_item_id = core_db.enqueue_review(ctx.db, "<r1@x>")
    r = ctx.client.get("/?type=review&state=claimable")
    assert r.status_code == 200
    body = r.text
    expected_detail = (f'/work-items/{work_item_id}'
                       f'?back={quote("/?type=review&state=claimable", safe="")}')
    assert expected_detail in body
    assert f'data-href="{expected_detail}"' in body
