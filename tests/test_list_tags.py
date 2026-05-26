"""Tests for the list-tag layer — the operator's gather filter:
- core_db helpers (seed_list_tags, note_observed_tag, set_tag_enabled,
  enabled_tags, list_tags, set_patchset_tags, tags_for_patchset)
- the framework's filter in gather._ingest_ref."""
import pytest

from core import core_db, gather

PatchsetRef = gather.gather_api.PatchsetRef
MessageRef  = gather.gather_api.MessageRef


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


class _ListModule:
    """Yields the given refs (used to push them through _gather_source)."""

    name = "fake-list"
    since_date = ""

    def __init__(self, refs):
        self._refs = refs

    def list(self, state=None, db=None):
        return iter(self._refs)


# --- list_tags helpers ----------------------------------------------------

def test_seed_list_tags_records_the_universe(db):
    n = core_db.seed_list_tags(
        db, [("linux-arm-msm.vger.kernel.org", "linux-arm-msm"),
              ("linux-kernel.vger.kernel.org",  "LKML")])
    assert n == 2
    tags = core_db.list_tags(db)
    assert {t["tag"] for t in tags} == {
        "linux-arm-msm.vger.kernel.org", "linux-kernel.vger.kernel.org"}
    assert all(t["origin"] == core_db.LIST_TAG_ORIGIN_MANIFEST for t in tags)
    assert all(t["enabled"] == 0 for t in tags)


def test_seed_list_tags_does_not_clobber_an_enabled_tag(db):
    core_db.seed_list_tags(db, [("a.kernel.org", "A")])
    core_db.set_tag_enabled(db, "a.kernel.org", True)
    # re-seeding the same tag keeps the operator's enable + the manifest origin
    core_db.seed_list_tags(db, [("a.kernel.org", "A (refreshed)")])
    row = next(t for t in core_db.list_tags(db) if t["tag"] == "a.kernel.org")
    assert row["enabled"] == 1
    assert row["origin"] == core_db.LIST_TAG_ORIGIN_MANIFEST


def test_note_observed_tag_creates_with_observed_origin(db):
    core_db.note_observed_tag(db, "unknown.kernel.org")
    row = next(t for t in core_db.list_tags(db)
               if t["tag"] == "unknown.kernel.org")
    assert row["origin"] == core_db.LIST_TAG_ORIGIN_OBSERVED


def test_note_observed_tag_does_not_demote_a_manifest_tag(db):
    core_db.seed_list_tags(db, [("a.kernel.org", "A")])
    core_db.note_observed_tag(db, "a.kernel.org")
    row = next(t for t in core_db.list_tags(db) if t["tag"] == "a.kernel.org")
    assert row["origin"] == core_db.LIST_TAG_ORIGIN_MANIFEST


def test_enabled_tags_returns_the_enabled_set(db):
    core_db.seed_list_tags(db,
                            [("a.kernel.org", "A"), ("b.kernel.org", "B")])
    core_db.set_tag_enabled(db, "b.kernel.org", True)
    assert core_db.enabled_tags(db) == ["b.kernel.org"]


def test_set_patchset_tags_creates_observed_tags_on_the_fly(db):
    core_db.upsert_patchset(db, "<r1@x>", n_patches=1)
    core_db.set_patchset_tags(db, "<r1@x>",
                              ["new1.kernel.org", "new2.kernel.org"])
    assert sorted(core_db.tags_for_patchset(db, "<r1@x>")) == [
        "new1.kernel.org", "new2.kernel.org"]
    # both tags were auto-added to list_tags as observed
    tags = {t["tag"]: t for t in core_db.list_tags(db)}
    assert tags["new1.kernel.org"]["origin"] == core_db.LIST_TAG_ORIGIN_OBSERVED


def test_set_patchset_tags_replaces_the_set(db):
    core_db.upsert_patchset(db, "<r1@x>", n_patches=1)
    core_db.set_patchset_tags(db, "<r1@x>", ["a.kernel.org"])
    core_db.set_patchset_tags(db, "<r1@x>", ["b.kernel.org"])
    assert core_db.tags_for_patchset(db, "<r1@x>") == ["b.kernel.org"]


# --- gather filter --------------------------------------------------------

def _ref(root, tags, cursor):
    return PatchsetRef(root_message_id=root, subject="ps", sent=100,
                       n_patches=1, list_tags=list(tags), cursor=cursor)


def test_no_enabled_tags_means_no_filter(db):
    refs = [_ref("<r1@x>", ["a.kernel.org"], cursor="1"),
            _ref("<r2@x>", ["b.kernel.org"], cursor="2")]
    stats = gather._gather_source(db, _ListModule(refs))
    assert stats["patchsets"] == 2 and stats["skipped"] == 0
    assert all(core_db.get_patchset(db, r)["state"]
               == core_db.PATCHSET_STATE_GATHERED for r in ("<r1@x>",
                                                              "<r2@x>"))


def test_filter_active_skips_unmatched_patchsets(db):
    core_db.seed_list_tags(db, [("a.kernel.org", "A")])
    core_db.set_tag_enabled(db, "a.kernel.org", True)
    refs = [_ref("<r1@x>", ["a.kernel.org"],    cursor="1"),  # match
            _ref("<r2@x>", ["b.kernel.org"],    cursor="2"),  # no match
            _ref("<r3@x>", [],                   cursor="3")]  # no tags
    stats = gather._gather_source(db, _ListModule(refs))
    assert stats == {"patchsets": 1, "messages": 0,
                     "skipped": 2, "failed": 0}
    assert core_db.get_patchset(db, "<r1@x>")["state"] \
        == core_db.PATCHSET_STATE_GATHERED
    for skipped in ("<r2@x>", "<r3@x>"):
        ps = core_db.get_patchset(db, skipped)
        assert ps["state"] == core_db.PATCHSET_STATE_SKIPPED
        assert ps["skip_reason"] == "tag-not-enabled"


def test_filter_passes_any_intersecting_tag(db):
    core_db.seed_list_tags(db, [("a.kernel.org", "A")])
    core_db.set_tag_enabled(db, "a.kernel.org", True)
    refs = [_ref("<r1@x>", ["other.kernel.org", "a.kernel.org"], cursor="1")]
    stats = gather._gather_source(db, _ListModule(refs))
    assert stats["patchsets"] == 1 and stats["skipped"] == 0
