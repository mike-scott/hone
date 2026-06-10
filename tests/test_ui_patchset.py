"""Tests for the per-patchset detail page (core/ui.py GET /patchsets/{root})
and the work-queue's row → detail wiring."""
import re
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import core_db, ui


@pytest.fixture
def ctx(tmp_path, fake_admin_session):
    db = core_db.connect(str(tmp_path / "hone.db"))
    # claim_work_item stamps methodology_version on the row; the tests
    # below claim a row to exercise the work-item history table.
    core_db.add_methodology_version(db, {"name": "test", "version": 1})
    app = FastAPI()
    app.include_router(ui.router)
    fake_admin_session(app)
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


# --- manual review trigger ------------------------------------------------

def test_review_button_offered_once_prepared(ctx):
    """A prepared patchset with no review yet shows the active Request
       review button (a POST form to the review endpoint)."""
    _plant_patchset(ctx.db)                       # plants patchset_metadata
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "Request review" in body
    assert f'action="/review-requests/{quote("r1@x")}"' in body


def test_review_button_disabled_before_prepare(ctx):
    """Without a patchset_metadata row (prepare not done), the button is
       disabled — review can't be enqueued until prepare lands."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH] x",
                             n_patches=1, submitter_email="a@x")
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "Awaiting prepare" in body
    assert "disabled" in body


def test_request_review_enqueues_and_dims_button(ctx):
    """POSTing the review request enqueues a review work-item (303 back to
       the detail page), and the button then reads 'Review queued' and is
       disabled — the dim-once-present behaviour."""
    _plant_patchset(ctx.db)
    r = ctx.client.post(f"/review-requests/{quote('r1@x')}",
                        follow_redirects=False)
    assert r.status_code == 303
    # rows are keyed on the normalized message-id (norm_msgid strips the
    # angle brackets), so query the bare form.
    n = ctx.db.execute(
        "SELECT COUNT(*) FROM work_items WHERE type=? AND root_message_id=?",
        (core_db.WORK_ITEM_TYPE_REVIEW, "r1@x")).fetchone()[0]
    assert n == 1
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "Review queued" in body
    assert "Request review" not in body           # active button gone


def test_request_review_is_idempotent(ctx):
    """A double-submit (or a patchset that already has a review) is a safe
       no-op — still exactly one review work-item."""
    _plant_patchset(ctx.db)
    ctx.client.post(f"/review-requests/{quote('r1@x')}",
                    follow_redirects=False)
    ctx.client.post(f"/review-requests/{quote('r1@x')}",
                    follow_redirects=False)
    n = ctx.db.execute(
        "SELECT COUNT(*) FROM work_items WHERE type=? AND root_message_id=?",
        (core_db.WORK_ITEM_TYPE_REVIEW, "r1@x")).fetchone()[0]
    assert n == 1


def test_request_review_404_for_unknown_root(ctx):
    r = ctx.client.post(f"/review-requests/{quote('nope@x')}",
                        follow_redirects=False)
    assert r.status_code == 404


def test_request_review_stamps_admin_id_as_null(ctx):
    """The fake-admin-session fixture installs the config-token admin
       (SessionUser.id == None), so an admin-triggered review enqueue
       produces a SYSTEM-origin work item (requested_by_user_id NULL).
       That's the natural fit for admin actions — any system-handling
       node can pick it up."""
    _plant_patchset(ctx.db)
    ctx.client.post(f"/review-requests/{quote('r1@x')}",
                    follow_redirects=False)
    row = ctx.db.execute(
        "SELECT requested_by_user_id FROM work_items "
        "WHERE type=? AND root_message_id=?",
        (core_db.WORK_ITEM_TYPE_REVIEW, "r1@x")).fetchone()
    assert row["requested_by_user_id"] is None


def test_request_review_stamps_current_user_id_for_a_maintainer(tmp_path):
    """A maintainer clicking Request review enqueues a USER-origin work
       item with requested_by_user_id pinned to their id — so the
       resulting review item only feeds onto their own nodes' queue.
       (Corpus review requests are maintainer-gated; a regular user
       gets a 403 — see test_request_review_403_for_regular_user.)"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import auth, runtime_config
    from types import SimpleNamespace

    db = core_db.connect(str(tmp_path / "hone.db"))
    uid = core_db.create_user(db, "alice@x", "alice", "local")
    core_db.set_user_state(db, uid, "approved")
    core_db.set_user_maintainer(db, uid, True)
    alice = auth.SessionUser(id=uid, email="alice@x",
                              display_name="alice", is_config_admin=False,
                              is_maintainer=True)

    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: alice
    app.dependency_overrides[auth.require_csrf] = lambda: None
    app.state.db = db
    app.state.runtime_config = runtime_config.load(
        str(tmp_path / "config.yaml"))
    client = TestClient(app)

    _plant_patchset(db)
    r = client.post(f"/review-requests/{quote('r1@x')}",
                    follow_redirects=False)
    assert r.status_code == 303
    row = db.execute(
        "SELECT requested_by_user_id FROM work_items "
        "WHERE type=? AND root_message_id=?",
        (core_db.WORK_ITEM_TYPE_REVIEW, "r1@x")).fetchone()
    assert row["requested_by_user_id"] == uid


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


# --- AI review inline rendering -------------------------------------------

# A real-shaped single-patch body; content line indices (0-based, after the
# blank that ends the commit message) put the first `diff --git` at 2, so the
# two added lines are diff-relative span [8, 9].
_PATCH_BODY = (
    "Fix a leak in foo_probe\n"
    "\n"
    "diff --git a/drivers/net/foo.c b/drivers/net/foo.c\n"
    "index 1111..2222 100644\n"
    "--- a/drivers/net/foo.c\n"
    "+++ b/drivers/net/foo.c\n"
    "@@ -10,6 +10,7 @@ int foo_probe(struct device *dev)\n"
    " \tint ret;\n"
    " \n"
    " \tret = init(dev);\n"
    "+\tif (ret)\n"
    "+\t\treturn ret;\n"
    " \treturn 0;\n"
    " }")


def test_annotate_patch_injects_comment_after_the_cited_line():
    """_annotate_patch lays the patch's full diff out as line rows and drops a
       concern's comment immediately after the last line of its span (anchor =
       first `diff --git` + span end). An unanchorable span (null / out of
       range) is returned separately to pin at the card's foot."""
    anchored = {"severity": "major", "text": "anchored",
                "patch_scope": {"spans_lines_in_diff": [8, 9]}}
    loose = {"severity": "nit", "text": "loose",
             "patch_scope": {"spans_lines_in_diff": None}}
    rows, unanchored = ui._annotate_patch(_PATCH_BODY, [anchored, loose])
    flat = [("concern", r["concern"]["text"]) if "concern" in r
            else ("line", r["line"]) for r in rows]
    # The comment row immediately follows the "+\t\treturn ret;" line.
    i = next(k for k, (kind, v) in enumerate(flat)
             if kind == "line" and "return ret;" in v)
    assert flat[i + 1] == ("concern", "anchored")
    assert [u["text"] for u in unanchored] == ["loose"]


def test_severity_counts_tally_new_and_preexisting():
    """_severity_counts returns all five levels in order; patch-introduced and
       pre-existing findings are counted separately (pre is parenthesised in
       the header). Unknown severities are ignored."""
    concerns = [
        {"severity": "major", "is_preexisting": False},
        {"severity": "minor", "is_preexisting": False},
        {"severity": "minor", "is_preexisting": True},
        {"severity": "bogus", "is_preexisting": False},   # ignored
    ]
    rows = ui._severity_counts(concerns)
    assert [r["severity"] for r in rows] == \
        ["critical", "major", "moderate", "minor", "nit"]
    by = {r["severity"]: r for r in rows}
    assert (by["major"]["new"], by["major"]["pre"]) == (1, 0)
    assert (by["minor"]["new"], by["minor"]["pre"]) == (1, 1)
    assert (by["critical"]["new"], by["critical"]["pre"]) == (0, 0)


def test_ai_review_renders_concern_inline_with_result_header(ctx):
    """A line-scoped concern renders inside the patch's annotated diff
       (.patch-review), severity-coloured, and is tallied in the "Result:"
       header — patch-introduced bare, pre-existing parenthesised. The full
       patch hunk shows; a mis-transcribed code_snippet never does."""
    _plant_patchset(ctx.db)
    core_db.upsert_message(ctx.db, "<p1@x>", root_message_id="<r1@x>",
                            type=core_db.MSG_TYPE_PATCH, body=_PATCH_BODY,
                            part_index=1, subject="[PATCH 1/1] frob",
                            author_email="alice@example.com")
    concerns = [
        {"concern_id": "rev-c-001", "stage_id": "2",
         "candidate_or_check_id": "unchecked-init",
         "text": "init() return value goes unchecked",
         "severity": "major", "is_preexisting": False,
         "patch_scope": {"kind": "patch", "patches": ["<p1@x>"],
                         "spans_lines_in_diff": [8, 9]},
         # The model mis-transcribes the hunk — this must never be shown.
         "locations": [{"file": "drivers/net/foo.c", "function_symbol": "foo_probe",
                        "code_snippet": "@@ -99,9 +99,9 @@ WRONG\n+bogus"}]},
        {"concern_id": "rev-c-002", "stage_id": "2",
         "candidate_or_check_id": "style",
         "text": "patch-introduced minor",
         "severity": "minor", "is_preexisting": False,
         "patch_scope": {"kind": "patch", "patches": ["<p1@x>"],
                         "spans_lines_in_diff": [8, 9]},
         "locations": []},
        {"concern_id": "rev-c-003", "stage_id": "2",
         "candidate_or_check_id": "style",
         "text": "pre-existing style nit nearby",
         "severity": "minor", "is_preexisting": True,
         "patch_scope": {"kind": "patch", "patches": ["<p1@x>"],
                         "spans_lines_in_diff": [8, 9]},
         "locations": []},
    ]
    core_db.upsert_ai_review(ctx.db, "<r1@x>", concerns=concerns, model="m")
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    # Per-patch finding tally on the title line (inside <summary>, right of the
    # subject): colourised counts (not badges), patch-introduced bare and
    # pre-existing parenthesised ("minor 1 (1)"). No "Result:" label.
    counts = re.search(r'<span class="finding-counts">(.*?)</summary>',
                       body, re.S).group(1)
    assert "Result:" not in body
    assert "sev-badge" not in counts
    assert re.search(r'class="sev-major[^"]*">major <span class="sev-count">1</span>', counts)
    assert re.search(r'class="sev-minor[^"]*">minor <span class="sev-count">1</span> '
                     r'\(<span class="sev-count">1</span>\)', counts)
    # Concern rendered inline in the annotated patch, severity-coloured.
    assert 'class="patch-body patch-review' in body
    assert "init() return value goes unchecked" in body
    assert "sev-major" in body
    # Review patch is an expandable thread-style item, open when it has
    # findings, with the same "patch" type badge the thread uses.
    assert re.search(r'<details class="list-group-item" open>.*?text-bg-primary">patch<',
                     body, re.S)
    # The complete patch hunk shows; the bad transcription never does.
    assert "@@ -10,6 +10,7 @@" in body
    assert "@@ -99,9 +99,9 @@" not in body
    assert "WRONG" not in body and "bogus" not in body


def test_ai_review_null_span_concern_pins_at_card_foot(ctx):
    """A concern with no spans_lines_in_diff can't be line-anchored, so it
       renders in the card's "Not line-anchored" foot — prose and the
       file/function pointer only, never a model-authored code_snippet."""
    _plant_patchset(ctx.db)
    concern = {
        "concern_id": "rev-c-002", "stage_id": "1",
        "candidate_or_check_id": "stale-doc",
        "text": "kerneldoc names the wrong struct",
        "severity": "minor", "is_preexisting": True,
        "patch_scope": {"kind": "patch", "patches": ["<p1@x>"],
                        "spans_lines_in_diff": None},
        "locations": [{"file": "drivers/net/foo.c",
                       "function_symbol": "foo_reset",
                       "code_snippet": "@@ -7,1 +7,1 @@ FABRICATED\n+nonsense"}],
    }
    core_db.upsert_ai_review(ctx.db, "<r1@x>", concerns=[concern], model="m")
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "Not line-anchored" in body
    assert "kerneldoc names the wrong struct" in body
    assert "drivers/net/foo.c" in body and "foo_reset" in body
    assert "FABRICATED" not in body and "nonsense" not in body


def test_ai_review_with_no_concerns_shows_green_result(ctx):
    """A patch with no findings shows a green "No concerns found" on its title
       line instead of a row of zero counts. No "Result:" label."""
    _plant_patchset(ctx.db)
    core_db.upsert_ai_review(ctx.db, "<r1@x>", concerns=[], model="m")
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "No concerns found" in body
    assert "text-success" in body
    assert "Result:" not in body
    assert "critical 0" not in body          # no zero counts when clean


def test_tickcode_wraps_backtick_spans_and_escapes_the_rest():
    """_tickcode turns `…` runs into inline <code> (backticks dropped) while
       HTML-escaping the surrounding untrusted prose; an unpaired backtick is
       left literal."""
    out = str(ui._tickcode("set `foo()` not <bar> & a lone ` tick"))
    assert "<code>foo()</code>" in out
    assert "&lt;bar&gt;" in out and "<bar>" not in out
    assert "&amp;" in out
    assert "lone ` tick" in out          # unpaired backtick untouched


def test_concern_backticks_render_as_inline_code(ctx):
    """A concern's `backtick` spans render as highlighted inline code with the
       backticks removed."""
    _plant_patchset(ctx.db)
    concern = {
        "concern_id": "rev-c-001", "stage_id": "1",
        "candidate_or_check_id": "qualifier",
        "text": "the `static` qualifier contradicts the kerneldoc",
        "severity": "minor", "is_preexisting": False,
        "patch_scope": {"kind": "patch", "patches": ["<p1@x>"],
                        "spans_lines_in_diff": None},
        "locations": [],
    }
    core_db.upsert_ai_review(ctx.db, "<r1@x>", concerns=[concern], model="m")
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "<code>static</code>" in body
    assert "`static`" not in body


# --- queue row → detail wiring --------------------------------------------

def test_queue_row_links_to_the_work_item_detail_page(ctx):
    """Each queue row links to /work-items/{id} (the queue is a list
       of work-items — clicking a row should drill into the work-item,
       not the patchset). The current queue URL is carried in ?back=
       so the work-item detail's ← Back returns to this exact view."""
    _plant_patchset(ctx.db)
    work_item_id = core_db.enqueue_review(ctx.db, "<r1@x>")
    r = ctx.client.get("/queue?type=review&state=claimable")
    assert r.status_code == 200
    body = r.text
    expected_detail = (f'/work-items/{work_item_id}'
                       f'?back={quote("/queue?type=review&state=claimable", safe="")}')
    assert expected_detail in body
    assert f'data-href="{expected_detail}"' in body


# --- delete review (button + endpoint) ------------------------------------

def _planted_review(db, root="<r1@x>"):
    """Plant a patchset, enqueue its review work-item, and record an
       ai_review — the state the Delete-review control acts on."""
    _plant_patchset(db, root)
    core_db.maybe_enqueue_review(db, root)
    core_db.upsert_ai_review(db, root, concerns=[], model="m")


def test_delete_review_button_shown_when_ai_review_exists(ctx):
    _planted_review(ctx.db)
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "Delete review" in body
    # root_message_id is stored normalized (brackets stripped), so the
    # delete form posts to the normalized id.
    assert f"/review-requests/{quote('r1@x', safe='')}/delete" in body


def test_delete_review_button_absent_without_ai_review(ctx):
    _plant_patchset(ctx.db)
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "Delete review" not in body


def test_post_delete_review_removes_ai_review_and_work_item(ctx):
    _planted_review(ctx.db)
    r = ctx.client.post(
        f"/review-requests/{quote('<r1@x>', safe='')}/delete",
        follow_redirects=False)
    assert r.status_code == 303
    assert core_db.get_ai_review(ctx.db, "<r1@x>") is None
    rows = ctx.db.execute(
        "SELECT COUNT(*) c FROM work_items WHERE root_message_id=? AND type=?",
        ("<r1@x>", core_db.WORK_ITEM_TYPE_REVIEW)).fetchone()
    assert rows["c"] == 0


def test_post_delete_review_rearms_request_button(ctx):
    _planted_review(ctx.db)
    ctx.client.post(f"/review-requests/{quote('<r1@x>', safe='')}/delete",
                    follow_redirects=False)
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "Request review" in body
    assert "Delete review" not in body


def test_post_delete_review_unknown_patchset_404(ctx):
    r = ctx.client.post(
        f"/review-requests/{quote('<nope@x>', safe='')}/delete",
        follow_redirects=False)
    assert r.status_code == 404


def test_post_delete_review_without_review_is_safe_noop(ctx):
    _plant_patchset(ctx.db)
    r = ctx.client.post(
        f"/review-requests/{quote('<r1@x>', safe='')}/delete",
        follow_redirects=False)
    assert r.status_code == 303


def test_delete_review_clears_dependent_evaluations(ctx):
    """A review evaluated by a training session has review_evaluations rows
       with a NOT-NULL FK to ai_reviews; delete_review must clear them first
       (foreign_keys is ON, no cascade) rather than hit a constraint error."""
    db = ctx.db
    _plant_patchset(db)
    rid = core_db.upsert_ai_review(db, "<r1@x>", concerns=[], model="m")
    now = 1700000000
    db.execute("INSERT INTO training_sessions (created_at, state, profile) "
               "VALUES (?, ?, ?)", (now, 1, "standard"))
    sid = db.execute("SELECT id FROM training_sessions").fetchone()["id"]
    db.execute("INSERT INTO review_evaluations "
               "(root_message_id, ai_review_id, session_id, evaluated_at) "
               "VALUES (?, ?, ?, ?)",
               (core_db.norm_msgid("<r1@x>"), rid, sid, now))
    db.commit()

    assert core_db.delete_review(db, "<r1@x>") == "ok"
    assert core_db.get_ai_review(db, "<r1@x>") is None
    assert db.execute("SELECT COUNT(*) AS c FROM review_evaluations "
                      "WHERE ai_review_id=?", (rid,)).fetchone()["c"] == 0


# --- manual prepare trigger -------------------------------------------------

def test_prepare_button_offered_when_not_prepared(ctx):
    """A gathered patchset with no prepare item and no metadata shows
       the active Request prepare button — the pipeline normally
       enqueues prepare, so this is the recovery path when it didn't
       (or the item was cancelled)."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH] x",
                             n_patches=1, submitter_email="a@x")
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "Request prepare" in body
    assert f'action="/prepare-requests/{quote("r1@x")}"' in body


def test_prepare_button_reads_complete_once_prepared(ctx):
    """Once the patchset_metadata row exists (prepare's terminal
       product) the button dims to 'Prepare complete'."""
    _plant_patchset(ctx.db)                       # plants patchset_metadata
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "Prepare complete" in body
    assert "Request prepare" not in body          # active button gone


def test_request_prepare_enqueues_and_dims_button(ctx):
    """POSTing the prepare request enqueues a prepare work-item (303
       back to the detail page); the button then reads 'Prepare queued'
       — same dim-once-present behaviour as the review trigger."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH] x",
                             n_patches=1, submitter_email="a@x")
    r = ctx.client.post(f"/prepare-requests/{quote('r1@x')}",
                        follow_redirects=False)
    assert r.status_code == 303
    n = ctx.db.execute(
        "SELECT COUNT(*) FROM work_items WHERE type=? AND root_message_id=?",
        (core_db.WORK_ITEM_TYPE_PREPARE, "r1@x")).fetchone()[0]
    assert n == 1
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "Prepare queued" in body
    assert "Request prepare" not in body


def test_request_prepare_is_idempotent(ctx):
    """A double-submit is a safe no-op — still exactly one prepare
       work-item (maybe_enqueue_prepare's existing guarantee)."""
    core_db.upsert_patchset(ctx.db, "<r1@x>", subject="[PATCH] x",
                             n_patches=1, submitter_email="a@x")
    ctx.client.post(f"/prepare-requests/{quote('r1@x')}",
                    follow_redirects=False)
    ctx.client.post(f"/prepare-requests/{quote('r1@x')}",
                    follow_redirects=False)
    n = ctx.db.execute(
        "SELECT COUNT(*) FROM work_items WHERE type=? AND root_message_id=?",
        (core_db.WORK_ITEM_TYPE_PREPARE, "r1@x")).fetchone()[0]
    assert n == 1


def test_request_prepare_404_for_unknown_root(ctx):
    r = ctx.client.post(f"/prepare-requests/{quote('nope@x')}",
                        follow_redirects=False)
    assert r.status_code == 404


# --- thread reply nesting ---------------------------------------------------

def test_thread_nests_replies_under_the_comment_they_answer(ctx):
    """Comments thread by parent_message_id: a reply to a review comment
       renders immediately below it, indented one level deeper — not in
       plain sent order. Patches stay flush left."""
    _plant_patchset(ctx.db)                       # p1 + bob's c1 on it
    # A second direct comment on the patch, sent BEFORE the reply below —
    # plain sent order would interleave it between c1 and the reply.
    core_db.upsert_message(ctx.db, "<c2@x>", root_message_id="<r1@x>",
                            type=core_db.MSG_TYPE_COMMENT,
                            body="one nit", parent_message_id="<p1@x>",
                            subject="Re: [PATCH 1/1] frob", sent=300,
                            author_email="carol@kernel.org")
    # Alice answers bob's comment, later than carol's mail.
    core_db.upsert_message(ctx.db, "<c1r@x>", root_message_id="<r1@x>",
                            type=core_db.MSG_TYPE_COMMENT,
                            body="thanks!", parent_message_id="<c1@x>",
                            subject="Re: Re: [PATCH 1/1] frob", sent=400,
                            author_email="alice@example.com")
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    # Thread order: the reply follows the comment it answers, ahead of
    # the later-positioned direct comment.
    assert (body.index("c1@x") < body.index("c1r@x")
            < body.index("c2@x"))
    # Depth-1 indent on both direct comments, depth-2 on the reply,
    # nothing deeper — and the patch itself is unindented.
    assert body.count("margin-left: 1.5rem") == 2
    assert body.count("margin-left: 3.0rem") == 1


def test_thread_comment_with_no_parent_stays_top_level(ctx):
    """A comment without a resolvable parent (gather stores NULL when the
       In-Reply-To chain leads nowhere) still renders — flush left, in
       sent order."""
    _plant_patchset(ctx.db)
    core_db.upsert_message(ctx.db, "<lost@x>", root_message_id="<r1@x>",
                            type=core_db.MSG_TYPE_COMMENT,
                            body="re: a mail we never gathered",
                            subject="Re: [PATCH 0/7] elsewhere", sent=500)
    body = ctx.client.get(f"/patchsets/{quote('r1@x')}").text
    assert "lost@x" in body
    # Only bob's reply-to-the-patch comment is indented.
    assert body.count("margin-left:") == 1
