"""Tests for node/tier0.py — the deterministic resolver that produces
prepare's Tier-0 metadata. Pure helpers (trailer, size counting,
maintainer bucketing) are tested directly; the orchestration is tested
with a fake KernelTrees + monkeypatched get_maintainer."""
from node import cgit, maintainers, tier0
from node.maintainers import MaintainerEntry


# --- base_commit_trailer ---------------------------------------------------

def test_base_commit_trailer_found():
    patch = "Subject: x\n\nbase-commit: 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b\n"
    assert tier0.base_commit_trailer(patch) == \
        "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"


def test_base_commit_trailer_absent():
    assert tier0.base_commit_trailer("no trailer here") is None
    assert tier0.base_commit_trailer("") is None


# --- size_bucket -----------------------------------------------------------

def test_size_bucket_boundaries():
    assert tier0.size_bucket(0) == "trivial"
    assert tier0.size_bucket(5) == "trivial"
    assert tier0.size_bucket(6) == "small"
    assert tier0.size_bucket(50) == "small"
    assert tier0.size_bucket(51) == "medium"
    assert tier0.size_bucket(250) == "medium"
    assert tier0.size_bucket(251) == "large"
    assert tier0.size_bucket(1000) == "large"
    assert tier0.size_bucket(1001) == "huge"


# --- count_patch_size ------------------------------------------------------

_DIFF = """Subject: [PATCH] thing
diff --git a/fs/ext4/inode.c b/fs/ext4/inode.c
--- a/fs/ext4/inode.c
+++ b/fs/ext4/inode.c
@@ -1,2 +1,3 @@
 ctx
-old
+new
+added
diff --git a/drivers/new.c b/drivers/new.c
new file mode 100644
--- /dev/null
+++ b/drivers/new.c
@@ -0,0 +1,2 @@
+line one
+line two
diff --git a/old.c b/old.c
deleted file mode 100644
--- a/old.c
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""


def test_count_patch_size_counts_lines_files_hunks():
    ps = tier0.count_patch_size(_DIFF, series_length=3)
    # +new, +added, +line one, +line two = 4 added (+++ lines excluded)
    assert ps["lines_added"] == 4
    # -old, -gone = 2 removed (--- lines excluded)
    assert ps["lines_removed"] == 2
    assert ps["hunks"] == 3
    assert ps["files_added"] == 1            # drivers/new.c
    assert ps["files_deleted"] == 1          # old.c
    assert ps["files_renamed"] == 0
    assert ps["files_modified"] == 1         # 3 total − 1 added − 1 deleted
    assert ps["bucket"] == "small"           # 6 changed lines
    assert ps["series_length"] == 3
    assert ps["churn_ratio"] is None         # tree-only
    assert ps["source"] == "thread"


def test_count_patch_size_counts_renames():
    diff = ("diff --git a/x.c b/y.c\n"
            "similarity index 100%\nrename from x.c\nrename to y.c\n")
    ps = tier0.count_patch_size(diff)
    assert ps["files_renamed"] == 1 and ps["files_modified"] == 0


# --- bucket_maintainer_entries ---------------------------------------------

def _entries():
    return [
        MaintainerEntry("Dave", "dave@x", "maintainer", "NETWORKING"),
        MaintainerEntry("Jake", "jake@x", "maintainer", "NETWORKING"),
        MaintainerEntry("Ann", "ann@x", "reviewer", "EXT4"),
        MaintainerEntry(None, "netdev@vger", "open list", "NETWORKING"),
        MaintainerEntry(None, "lkml@vger", "open list", None),
    ]


def test_bucket_splits_roles_and_lists():
    sub, maint = tier0.bucket_maintainer_entries(_entries())
    assert [m["email"] for m in maint["authoritative_set"]] == \
        ["dave@x", "jake@x"]
    assert [r["email"] for r in maint["authoritative_reviewer_set"]] == \
        ["ann@x"]
    assert [x["address"] for x in maint["mailing_lists"]] == \
        ["netdev@vger", "lkml@vger"]
    assert maint["primary"] == "dave@x" and maint["source"] == "tree"


def test_bucket_subsystem_primary_is_most_common_section():
    sub, _m = tier0.bucket_maintainer_entries(_entries())
    # NETWORKING appears twice (two maintainers), EXT4 once → primary NETWORKING
    assert sub["primary"] == "NETWORKING"
    assert sub["secondary"] == ["EXT4"]
    assert sub["cross_cutting"] is False     # only 2 distinct sections
    assert sub["source"] == "tree"


def test_bucket_cross_cutting_when_three_plus_sections():
    entries = [
        MaintainerEntry("A", "a@x", "maintainer", "S1"),
        MaintainerEntry("B", "b@x", "maintainer", "S2"),
        MaintainerEntry("C", "c@x", "reviewer", "S3"),
    ]
    sub, _m = tier0.bucket_maintainer_entries(entries)
    assert sub["cross_cutting"] is True


def test_bucket_coverage_and_was_cc_d_with_recipients():
    recips = {"dave@x", "netdev@vger"}       # dave Cc'd, jake not; netdev Cc'd
    _sub, maint = tier0.bucket_maintainer_entries(_entries(),
                                                   recipients=recips)
    assert maint["cc_coverage"] == 0.5       # 1 of 2 maintainers
    assert maint["list_coverage"] == 0.5     # 1 of 2 lists
    assert maint["cc_list_size"] == 2
    by_addr = {x["address"]: x["was_cc_d"] for x in maint["mailing_lists"]}
    assert by_addr == {"netdev@vger": True, "lkml@vger": False}


def test_bucket_was_cc_d_null_without_recipients():
    _sub, maint = tier0.bucket_maintainer_entries(_entries())
    assert all(x["was_cc_d"] is None for x in maint["mailing_lists"])
    assert maint["cc_coverage"] is None and maint["cc_list_size"] is None


# --- resolve_deterministic (orchestration) ---------------------------------

class _FakeTrees:
    """Fake KernelTrees — resolve_base returns a programmed BaseLookup;
       tree(name) reports membership for the fallback's registry gate."""

    def __init__(self, lookup, known=()):
        self._lookup = lookup
        self.resolved = []
        self._known = set(known)

    def resolve_base(self, sha):
        self.resolved.append(sha)
        return self._lookup

    def tree(self, name):
        return name if name in self._known else None


def _found_lookup(name="linux-next"):
    tree = cgit.Tree(name, f"https://h/{name}.git", f"git://h/{name}.git")
    return cgit.BaseLookup(cgit.BASE_FOUND, tree, object())


_PATCH = ("base-commit: 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b\n"
          "diff --git a/fs/ext4/inode.c b/fs/ext4/inode.c\n"
          "--- a/fs/ext4/inode.c\n+++ b/fs/ext4/inode.c\n@@ -1 +1 @@\n-a\n+b\n")


def test_resolve_authoritative_when_found_and_get_maintainer_ok(monkeypatch):
    monkeypatch.setattr(maintainers, "resolve_maintainers",
                         lambda c, sha, p, **k: [
                             MaintainerEntry("T", "t@x", "maintainer", "EXT4")])
    trees = _FakeTrees(_found_lookup("mainline"))
    r = tier0.resolve_deterministic(trees, _PATCH)
    assert r["base_in_tree"] is True
    assert r["base_resolution"] == "found"
    assert r["base_tree"] == "mainline"
    assert r["base_commit_source"] == "trailer"
    assert r["subsystem"]["primary"] == "EXT4"
    assert r["subsystem"]["source"] == "tree"
    assert r["maintainer"]["authoritative_set"][0]["email"] == "t@x"
    assert r["resolver_version"] == tier0.RESOLVER_VERSION


def test_resolve_heuristic_when_base_absent(monkeypatch):
    # get_maintainer must not run when there's no resolving tree.
    monkeypatch.setattr(maintainers, "resolve_maintainers",
                         lambda *a, **k: (_ for _ in ()).throw(
                             AssertionError("should not run")))
    trees = _FakeTrees(cgit.BaseLookup(cgit.BASE_ABSENT, None, None))
    r = tier0.resolve_deterministic(trees, _PATCH)
    assert r["base_in_tree"] is False
    assert r["base_resolution"] == "absent"
    assert r["maintainer"]["authoritative_set"] is None   # heuristic = null
    assert r["subsystem"]["primary"] is None
    assert r["subsystem"]["source"] == "thread"


def test_resolve_unknown_leaves_base_in_tree_null(monkeypatch):
    monkeypatch.setattr(maintainers, "resolve_maintainers",
                         lambda *a, **k: None)
    trees = _FakeTrees(cgit.BaseLookup(cgit.BASE_UNKNOWN, None, None))
    r = tier0.resolve_deterministic(trees, _PATCH)
    assert r["base_in_tree"] is None
    assert r["base_resolution"] == "unknown"


def test_resolve_no_trailer_skips_probe(monkeypatch):
    trees = _FakeTrees(_found_lookup())
    r = tier0.resolve_deterministic(trees, "diff --git a/x b/x\n+y\n")
    assert r["base_commit_source"] == "none"
    assert r["base_in_tree"] is None
    assert r["base_resolution"] == "no_base"
    assert trees.resolved == []              # never probed without a base
    # patch_size still computed
    assert r["patch_size"]["lines_added"] == 1


def test_resolve_found_but_get_maintainer_fails_is_mixed(monkeypatch):
    """Base authoritative, maintainer resolution failed → base_in_tree
       True but subsystem/maintainer stay heuristic (the 'mixed' case)."""
    monkeypatch.setattr(maintainers, "resolve_maintainers",
                         lambda *a, **k: None)
    trees = _FakeTrees(_found_lookup())
    r = tier0.resolve_deterministic(trees, _PATCH)
    assert r["base_in_tree"] is True
    assert r["maintainer"]["authoritative_set"] is None
    assert r["maintainer"]["source"] == "thread"


# --- no_base fallback hint (tip-at-submission) -----------------------------

_NO_TRAILER = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"


def test_fallback_tree_prefers_net_next_then_net():
    trees = _FakeTrees(None, known={"net", "net-next"})
    assert tier0.fallback_tree("[PATCH net-next v4] foo: bar", trees) == "net-next"
    assert tier0.fallback_tree("[PATCH v2 net] foo: fix", trees) == "net"


def test_fallback_tree_none_for_bare_patch_or_unregistered():
    trees = _FakeTrees(None, known={"net", "net-next"})
    assert tier0.fallback_tree("[PATCH] net: dsa: scope ...", trees) is None
    assert tier0.fallback_tree(None, trees) is None
    # token present but the named tree isn't in the registry → no hint
    bare = _FakeTrees(None, known=set())
    assert tier0.fallback_tree("[PATCH net-next] x", bare) is None


def test_no_base_records_fallback_hint_from_subject():
    trees = _FakeTrees(_found_lookup(), known={"net", "net-next"})
    r = tier0.resolve_deterministic(
        trees, _NO_TRAILER,
        subject="[PATCH net-next v4] inet: add sysctl", sent=1773000000)
    assert r["base_resolution"] == "no_base"
    assert r["base_fallback"] == {"tree": "net-next",
                                  "strategy": "tip-at-submission",
                                  "as_of": 1773000000}
    assert trees.resolved == []           # no cgit probe without a declared base


def test_no_base_fallback_needs_a_submission_time():
    trees = _FakeTrees(_found_lookup(), known={"net-next"})
    r = tier0.resolve_deterministic(trees, _NO_TRAILER,
                                    subject="[PATCH net-next] x", sent=None)
    assert r["base_fallback"] is None


def test_declared_base_carries_no_fallback(monkeypatch):
    monkeypatch.setattr(maintainers, "resolve_maintainers", lambda *a, **k: None)
    trees = _FakeTrees(_found_lookup(), known={"net-next"})
    r = tier0.resolve_deterministic(trees, _PATCH,
                                    subject="[PATCH net-next] x", sent=1773000000)
    assert r["base_resolution"] == "found"
    assert r["base_fallback"] is None
