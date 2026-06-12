"""Tests for the patchset list — the operator UI's home page (core/ui.py
GET /): search by subject/author, state filter, sortable columns, paging,
and row links into the per-patchset detail page."""
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import core_db, ui


@pytest.fixture
def ctx(tmp_path, fake_admin_session):
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.include_router(ui.router)
    fake_admin_session(app)
    app.state.db = db
    return SimpleNamespace(client=TestClient(app), db=db)


def _plant(db, root, *, subject, author, sent, n_patches=1, parts=1, comments=0,
           skipped=False, prepared=False, reviewed=False, training=False,
           tags=None, patch_type="bugfix"):
    """Plant a patchset + its root message (carrying the author name) +
       `parts` patch messages + `comments` comment messages, so the listing's
       author / Parts / comment-count columns have something to read.
       `n_patches` sets the patchset's `[PATCH N/M]` metadata total (which the
       Parts column deliberately does NOT use); `parts` is how many patch
       messages are actually attached. The lifecycle flags create the backing
       rows the State column reads: prepared → patchset_metadata, reviewed →
       ai_reviews, training → patchset_session_history."""
    core_db.upsert_patchset(db, root, subject=subject,
                            submitter_email=f"{author.split()[0].lower()}@e.x",
                            sent=sent, n_patches=n_patches)
    # The root message (message_id == root) is part 1 and supplies the author
    # name the listing joins on; parts 2..N are the rest of the attached series.
    core_db.upsert_message(db, root, root_message_id=root,
                           type=core_db.MSG_TYPE_PATCH, part_index=1,
                           author_name=author, author_email="a@e.x",
                           subject=subject, sent=sent, body="diff")
    for i in range(2, parts + 1):
        core_db.upsert_message(db, f"<p{i}-{root.strip('<>')}>",
                               root_message_id=root,
                               type=core_db.MSG_TYPE_PATCH, part_index=i,
                               author_name=author, author_email="a@e.x",
                               subject=f"[PATCH {i}] part", sent=sent, body="diff")
    for i in range(comments):
        core_db.upsert_message(db, f"<c{i}-{root.strip('<>')}>",
                               root_message_id=root,
                               type=core_db.MSG_TYPE_COMMENT, body="reply",
                               parent_message_id=root, author_name="Reviewer")
    if skipped:
        core_db.mark_skipped(db, root, "not enabled")
    if prepared:
        core_db.upsert_patchset_metadata(
            db, root, mode="heuristic", tree_state={},
            subsystem={"primary": "net"}, patch_size={"bucket": "small"},
            maintainer={}, patch_type={"primary": patch_type},
            review_intensity={"bucket_overall": "light"}, preparation_notes={})
    if reviewed:
        core_db.upsert_ai_review(db, root, concerns=[])
    if training:
        sid = core_db.create_session_draft(db, "standard")
        core_db.add_session_patchset(db, sid, root,
                                     role=core_db.SESSION_ROLE_POOL,
                                     stratum_label="net:light")
    if tags:
        core_db.set_patchset_tags(db, root, tags)


def test_home_page_is_the_corpus_list_with_search_box(ctx):
    """The home page is the gathered-patchset corpus — titled "Corpus"
       to distinguish it from a user's own uploads on My patchsets."""
    r = ctx.client.get("/")
    assert r.status_code == 200
    assert "Corpus" in r.text
    # the search placeholder is present before the user types
    assert "Search by subject or author name" in r.text


def test_lists_patchset_columns(ctx):
    _plant(ctx.db, "<r1@x>", subject="net: fix a leak", author="Alice Smith",
           sent=2000, parts=3, comments=2)
    body = ctx.client.get("/").text
    assert "net: fix a leak" in body
    assert "Alice Smith" in body
    assert "Parts" in body             # column header (renamed from "Patches")
    assert ">3<" in body               # Parts cell — 3 attached patch messages
    assert ">2<" in body               # n_comments cell


def test_parts_counts_attached_patches_not_metadata(ctx):
    """The Parts column reflects the patch messages actually attached to the
       patchset, NOT the `[PATCH N/M]` series total in n_patches — so a
       partial series (cover/some patches missing) shows what the corpus
       really holds."""
    # metadata claims a 10-patch series, but only 3 patch messages are present
    _plant(ctx.db, "<r1@x>", subject="partial series", author="A", sent=1,
           n_patches=10, parts=3)
    row = ctx.client.get("/").text
    assert ">3<" in row
    assert ">10<" not in row           # the metadata total is not shown


def test_state_column_shows_lifecycle_flags(ctx):
    """The State column carries abbreviated, colour-coded lifecycle flags —
       P=Prepared, R=Reviewed, T=Training — and a patchset can show several
       at once. 'gathered' is never shown (it's the universal default)."""
    _plant(ctx.db, "<r1@x>", subject="all three", author="A", sent=3,
           prepared=True, reviewed=True, training=True)
    body = ctx.client.get("/").text
    assert 'title="Prepared">P<' in body
    assert 'title="Reviewed">R<' in body
    assert 'title="Training">T<' in body
    assert "gathered" not in body


def test_state_column_bare_and_skipped(ctx):
    _plant(ctx.db, "<r-bare@x>", subject="just gathered", author="A", sent=2)
    _plant(ctx.db, "<r-skip@x>", subject="dropped", author="B", sent=1,
           skipped=True)
    body = ctx.client.get("/").text
    # the skipped patchset is labelled; the bare one shows no P/R/T flags
    assert 'title="Skipped">skipped<' in body
    assert 'title="Prepared"' not in body


def test_search_matches_partial_subject(ctx):
    _plant(ctx.db, "<r1@x>", subject="net: frobnicate", author="Alice", sent=2)
    _plant(ctx.db, "<r2@x>", subject="mm: tidy slab", author="Bob", sent=1)
    body = ctx.client.get("/", params={"q": "frob"}).text
    assert "net: frobnicate" in body
    assert "mm: tidy slab" not in body


def test_search_matches_partial_author_name(ctx):
    _plant(ctx.db, "<r1@x>", subject="patch one", author="Alice Zimmer", sent=2)
    _plant(ctx.db, "<r2@x>", subject="patch two", author="Bob Young", sent=1)
    body = ctx.client.get("/", params={"q": "zimmer"}).text     # case-insensitive
    assert "patch one" in body
    assert "patch two" not in body


def test_search_shorter_than_a_trigram_falls_back_to_like(ctx):
    """A 1-2 char term can't produce a trigram for the FTS index, so it
       keeps the LIKE scan — short searches must degrade to slow, never
       to empty."""
    _plant(ctx.db, "<r1@x>", subject="net: frobnicate", author="Alice", sent=2)
    _plant(ctx.db, "<r2@x>", subject="mm: tidy slab", author="Bob", sent=1)
    body = ctx.client.get("/", params={"q": "fr"}).text
    assert "net: frobnicate" in body
    assert "mm: tidy slab" not in body


def test_search_treats_fts_syntax_as_literal_text(ctx):
    """User-typed FTS5 syntax (quotes, stars, booleans) is matched as
       literal text — never a query-parse 500, never an operator."""
    _plant(ctx.db, "<r1@x>", subject='fix the "frob" path', author="A", sent=2)
    _plant(ctx.db, "<r2@x>", subject="unrelated", author="B", sent=1)
    r = ctx.client.get("/", params={"q": '"frob" OR'})
    assert r.status_code == 200
    for q in ('the "frob"', "frob*", "(frob"):
        assert ctx.client.get("/", params={"q": q}).status_code == 200
    body = ctx.client.get("/", params={"q": '"frob"'}).text
    assert "frob" in body and "unrelated" not in body


def test_search_index_follows_author_and_subject_rewrites(ctx):
    """The FTS mirror refreshes when the indexed fields are re-written:
       a re-gathered root message with a corrected author, or a patchset
       upsert with a new subject, must be findable by the NEW values
       (and not linger under the old author)."""
    _plant(ctx.db, "<r1@x>", subject="net: fix frob", author="Wrong Name",
           sent=2)
    core_db.upsert_message(ctx.db, "<r1@x>", root_message_id="<r1@x>",
                           type=core_db.MSG_TYPE_PATCH, part_index=1,
                           author_name="Right Person", author_email="rp@e.x",
                           subject="net: fix frob", sent=2, body="diff")
    body = ctx.client.get("/", params={"q": "Right Person"}).text
    assert "net: fix frob" in body
    body = ctx.client.get("/", params={"q": "Wrong Name"}).text
    assert "net: fix frob" not in body


def test_state_filter_by_lifecycle_flag(ctx):
    """The state filter partitions by lifecycle flag: prepared / reviewed /
       training each show only patchsets carrying that flag, and skipped
       shows the skipped base state."""
    _plant(ctx.db, "<r-prep@x>",  subject="prepared one", author="A", sent=5,
           prepared=True)
    _plant(ctx.db, "<r-rev@x>",   subject="reviewed one", author="B", sent=4,
           reviewed=True)
    _plant(ctx.db, "<r-train@x>", subject="trained one",  author="C", sent=3,
           training=True)
    _plant(ctx.db, "<r-skip@x>",  subject="skipped one",  author="D", sent=2,
           skipped=True)
    _plant(ctx.db, "<r-bare@x>",  subject="bare one",     author="E", sent=1)

    prepared = ctx.client.get("/", params={"state": "prepared"}).text
    assert "prepared one" in prepared
    for other in ("reviewed one", "trained one", "skipped one", "bare one"):
        assert other not in prepared

    reviewed = ctx.client.get("/", params={"state": "reviewed"}).text
    assert "reviewed one" in reviewed and "bare one" not in reviewed

    training = ctx.client.get("/", params={"state": "training"}).text
    assert "trained one" in training and "bare one" not in training

    skipped = ctx.client.get("/", params={"state": "skipped"}).text
    assert "skipped one" in skipped and "bare one" not in skipped


def test_comments_filter_shows_only_threads_with_comments(ctx):
    """The 'With comments' filter restricts to patchsets whose thread drew at
       least one comment message — an axis independent of the lifecycle
       state."""
    _plant(ctx.db, "<r-disc@x>",  subject="discussed one", author="A", sent=2,
           comments=3)
    _plant(ctx.db, "<r-quiet@x>", subject="quiet one",     author="B", sent=1,
           comments=0)
    body = ctx.client.get("/", params={"comments": "with"}).text
    assert "discussed one" in body
    assert "quiet one" not in body


def test_comments_filter_composes_with_state(ctx):
    """The comments axis AND-composes with the state axis: prepared + with
       comments shows only patchsets that are both."""
    _plant(ctx.db, "<r1@x>", subject="prepared and discussed", author="A",
           sent=3, prepared=True, comments=2)
    _plant(ctx.db, "<r2@x>", subject="prepared but quiet", author="B",
           sent=2, prepared=True, comments=0)
    _plant(ctx.db, "<r3@x>", subject="discussed not prepared", author="C",
           sent=1, comments=2)
    body = ctx.client.get(
        "/", params={"state": "prepared", "comments": "with"}).text
    assert "prepared and discussed" in body
    assert "prepared but quiet" not in body
    assert "discussed not prepared" not in body


def test_comments_filter_chip_present(ctx):
    body = ctx.client.get("/").text
    assert "Comments:" in body
    assert "With comments" in body
    assert "comments=with" in body


def test_mailing_list_filter(ctx):
    """The mailing-list filter restricts to patchsets tagged with that list;
       the dropdown offers a shortened label and an option per list present."""
    _plant(ctx.db, "<r1@x>", subject="net change", author="A", sent=2,
           tags=["netdev.vger.kernel.org"])
    _plant(ctx.db, "<r2@x>", subject="mm change", author="B", sent=1,
           tags=["linux-mm.kvack.org"])
    body = ctx.client.get("/", params={"list_tag": "netdev.vger.kernel.org"}).text
    assert "net change" in body and "mm change" not in body
    # the dropdown shows the shortened list labels
    home = ctx.client.get("/").text
    assert ">netdev<" in home and ">linux-mm<" in home


def test_patch_type_filter(ctx):
    """The patch-type filter restricts to prepared patchsets whose primary
       type matches (from prepare metadata)."""
    _plant(ctx.db, "<r1@x>", subject="a real fix", author="A", sent=2,
           prepared=True, patch_type="bugfix")
    _plant(ctx.db, "<r2@x>", subject="shiny new thing", author="B", sent=1,
           prepared=True, patch_type="feature")
    body = ctx.client.get("/", params={"patch_type": "bugfix"}).text
    assert "a real fix" in body and "shiny new thing" not in body


def test_list_and_type_filters_compose_with_other_axes(ctx):
    """Mailing list + patch type + comments all AND-compose."""
    _plant(ctx.db, "<r1@x>", subject="match all", author="A", sent=3,
           prepared=True, patch_type="bugfix", comments=2,
           tags=["netdev.vger.kernel.org"])
    _plant(ctx.db, "<r2@x>", subject="wrong list", author="B", sent=2,
           prepared=True, patch_type="bugfix", comments=2,
           tags=["linux-mm.kvack.org"])
    _plant(ctx.db, "<r3@x>", subject="no comments", author="C", sent=1,
           prepared=True, patch_type="bugfix", comments=0,
           tags=["netdev.vger.kernel.org"])
    body = ctx.client.get("/", params={
        "list_tag": "netdev.vger.kernel.org", "patch_type": "bugfix",
        "comments": "with"}).text
    assert "match all" in body
    assert "wrong list" not in body
    assert "no comments" not in body


def test_unknown_list_or_type_falls_back_to_all(ctx):
    _plant(ctx.db, "<r1@x>", subject="only one", author="A", sent=1,
           prepared=True, patch_type="bugfix", tags=["netdev.vger.kernel.org"])
    body = ctx.client.get("/", params={"list_tag": "bogus.list",
                                       "patch_type": "bogus"}).text
    assert "only one" in body          # bogus values ignored → all shown


def test_default_sort_is_newest_date_first(ctx):
    _plant(ctx.db, "<r-old@x>", subject="older one", author="A", sent=1000)
    _plant(ctx.db, "<r-new@x>", subject="newer one", author="B", sent=3000)
    body = ctx.client.get("/").text
    assert body.index("newer one") < body.index("older one")


def test_sort_by_subject_ascending(ctx):
    _plant(ctx.db, "<r1@x>", subject="zebra", author="A", sent=3000)
    _plant(ctx.db, "<r2@x>", subject="alpha", author="B", sent=1000)
    body = ctx.client.get(
        "/", params={"sort": "subject", "direction": "asc"}).text
    assert body.index("alpha") < body.index("zebra")    # despite newer date


def test_sort_by_annotated_columns(ctx):
    """Sorts whose key is a display annotation (author / parts /
       comments) take the single-query slow path in list_patchsets_page
       — the fast path LIMITs before the annotations exist. Both paths
       must order identically and carry the same row fields."""
    _plant(ctx.db, "<r1@x>", subject="busy thread", author="Zoe", sent=1000,
           parts=2, comments=3)
    _plant(ctx.db, "<r2@x>", subject="quiet thread", author="Abe", sent=3000,
           parts=1, comments=0)
    body = ctx.client.get(
        "/", params={"sort": "comments", "direction": "desc"}).text
    assert body.index("busy thread") < body.index("quiet thread")
    body = ctx.client.get(
        "/", params={"sort": "author", "direction": "asc"}).text
    assert body.index("quiet thread") < body.index("busy thread")  # Abe < Zoe


def test_fast_and_slow_sort_paths_return_identical_rows(ctx):
    """The two-phase fast path (date/subject sorts) must produce the
       same annotated rows as the slow path — same authors, counts and
       lifecycle flags — just computed for the page instead of the
       whole corpus."""
    _plant(ctx.db, "<r1@x>", subject="first", author="Ann", sent=2000,
           parts=2, comments=1, prepared=True, reviewed=True)
    _plant(ctx.db, "<r2@x>", subject="second", author="Bob", sent=1000)
    fast = core_db.list_patchsets_page(ctx.db, sort="date", direction="asc")
    slow = core_db.list_patchsets_page(ctx.db, sort="parts", direction="asc")
    by_root = {r["root_message_id"]: r for r in slow}
    for r in fast:
        assert r == by_root[r["root_message_id"]]
    assert fast[0]["author"] == "Bob"
    assert by_root["r1@x"]["n_parts"] == 2
    assert by_root["r1@x"]["n_comments"] == 1
    assert by_root["r1@x"]["is_prepared"] == 1
    assert by_root["r1@x"]["is_reviewed"] == 1


def test_unknown_sort_and_state_fall_back(ctx):
    _plant(ctx.db, "<r1@x>", subject="a patch", author="A", sent=1)
    r = ctx.client.get("/", params={"sort": "bogus", "state": "bogus"})
    assert r.status_code == 200 and "a patch" in r.text


def _seed(db, n):
    for i in range(n):
        _plant(db, f"<r{i:03d}@x>", subject=f"subject {i:03d}",
               author=f"Author {i:03d}", sent=1000 + i)


def test_paginates_at_default_size_25(ctx):
    _seed(ctx.db, 40)
    body = ctx.client.get("/").text
    assert 'aria-label="Pagination"' in body
    assert "Showing <strong>1</strong>–<strong>25</strong>" in body
    assert "of <strong>40</strong>" in body


def test_page_2_shows_the_next_slice(ctx):
    _seed(ctx.db, 40)
    body = ctx.client.get("/", params={"page": 2}).text
    assert "Showing <strong>26</strong>" in body


def test_size_clamps_to_allowed_set(ctx):
    _seed(ctx.db, 40)
    body = ctx.client.get("/", params={"size": "999999"}).text
    assert "Showing <strong>1</strong>–<strong>25</strong>" in body


def test_size_option_200_is_offered(ctx):
    _seed(ctx.db, 5)
    body = ctx.client.get("/").text
    for s in (25, 50, 100, 200):
        assert f'value="{s}"' in body


def test_paging_bar_renders_top_and_bottom(ctx):
    _seed(ctx.db, 40)
    body = ctx.client.get("/").text
    assert body.count("Per page:") == 2
    assert body.count('aria-label="Pagination"') == 2


def test_row_links_to_patchset_detail_with_back(ctx):
    _plant(ctx.db, "<r1@x>", subject="clickable", author="A", sent=1)
    body = ctx.client.get("/").text
    assert f"/patchsets/{quote('r1@x')}?back=" in body


def test_empty_state(ctx):
    body = ctx.client.get("/").text
    assert "No patchsets match." in body


# --- corpus access: maintainers + admins only -------------------------------

def test_corpus_redirects_a_regular_user_to_their_dashboard(tmp_path):
    """A non-maintainer landing on / is sent to /my-patchsets — the
       corpus is the training / review-selection population, gated to
       maintainers and admins. The nav hides the Corpus entry too."""
    from core import auth
    db = core_db.connect(str(tmp_path / "hone.db"))
    uid = core_db.create_user(db, "u@x", "u", "local")
    user = auth.SessionUser(id=uid, email="u@x", display_name="u",
                            is_config_admin=False)
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: user
    app.state.db = db
    client = TestClient(app)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["Location"] == "/my-patchsets"
    assert ">Corpus<" not in client.get("/my-patchsets").text


def test_corpus_renders_for_a_maintainer(tmp_path):
    from core import auth
    db = core_db.connect(str(tmp_path / "hone.db"))
    uid = core_db.create_user(db, "m@x", "m", "local")
    core_db.set_user_maintainer(db, uid, True)
    user = auth.SessionUser(id=uid, email="m@x", display_name="m",
                            is_config_admin=False, is_maintainer=True)
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_session] = lambda: user
    app.state.db = db
    body = TestClient(app).get("/").text
    assert "Corpus" in body
    assert "Search by subject or author name" in body
