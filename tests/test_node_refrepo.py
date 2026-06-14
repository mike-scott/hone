"""Tests for node/refrepo.py — the reference-tree manager. Focused on
the registry-derived remote set and the base_tree fetch-hint ordering;
the git subprocess calls are mocked (no real repo, no network)."""
from types import SimpleNamespace

from node import cgit, refrepo


# --- REMOTES derived from the shared registry ------------------------------

def test_remotes_derived_from_cgit_registry():
    """refrepo's remotes are the cgit named-trees registry — same names,
       same trees — so a recorded tree_state.base_tree maps straight to a
       remote here."""
    assert list(refrepo.REMOTES) == [t.name for t in cgit.DEFAULT_TREES]
    for t in cgit.DEFAULT_TREES:
        assert refrepo.REMOTES[t.name] == t.git_url
    # canonical names, not the old "origin"
    assert "mainline" in refrepo.REMOTES and "origin" not in refrepo.REMOTES


# --- _fetch_order ----------------------------------------------------------

def test_fetch_order_default_is_registry_order():
    assert refrepo._fetch_order() == list(refrepo.REMOTES)
    assert refrepo._fetch_order(None) == list(refrepo.REMOTES)


def test_fetch_order_promotes_known_hint_to_front():
    order = refrepo._fetch_order("stable")
    assert order[0] == "stable"
    assert set(order) == set(refrepo.REMOTES)        # no remote dropped
    assert len(order) == len(refrepo.REMOTES)        # none duplicated


def test_fetch_order_ignores_unknown_hint():
    # a base_tree the review-side registry doesn't have → default order
    assert refrepo._fetch_order("some-vendor-tree") == list(refrepo.REMOTES)


# --- prepare honours the hint, falls back on the serial scan ---------------

def _mock_git_recording(fetched, *, have_after_fetch=True):
    """A refrepo._git replacement that records fetch remote names and
       always succeeds; pair with a have() that flips True once a fetch
       has happened."""
    def fake_git(*args):
        if args and args[0] == "fetch":             # ("fetch","--quiet",name,sha)
            fetched.append(args[2])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return fake_git


def test_prepare_fetches_hinted_remote_first(monkeypatch):
    fetched = []
    monkeypatch.setattr(refrepo, "_git", _mock_git_recording(fetched))
    monkeypatch.setattr(refrepo, "have", lambda c: bool(fetched))   # absent→present
    monkeypatch.setattr(refrepo.os.path, "lexists", lambda p: False)
    wt, how = refrepo.prepare("deadbeef", "/tmp/wt", base_tree="stable")
    assert (wt, how) == ("/tmp/wt", "fetched")
    assert fetched[0] == "stable"                    # hint tried first


def test_prepare_without_hint_uses_default_order(monkeypatch):
    fetched = []
    monkeypatch.setattr(refrepo, "_git", _mock_git_recording(fetched))
    monkeypatch.setattr(refrepo, "have", lambda c: bool(fetched))
    monkeypatch.setattr(refrepo.os.path, "lexists", lambda p: False)
    refrepo.prepare("deadbeef", "/tmp/wt")
    assert fetched[0] == list(refrepo.REMOTES)[0]     # linux-next


def test_prepare_skips_fetch_when_commit_already_present(monkeypatch):
    fetched = []
    monkeypatch.setattr(refrepo, "_git", _mock_git_recording(fetched))
    monkeypatch.setattr(refrepo, "have", lambda c: True)   # already present
    monkeypatch.setattr(refrepo.os.path, "lexists", lambda p: False)
    wt, how = refrepo.prepare("deadbeef", "/tmp/wt", base_tree="mainline")
    assert how == "present"
    assert fetched == []                              # no fetch needed


def test_prepare_falls_back_through_remotes_when_hint_misses(monkeypatch):
    """The hinted remote doesn't have the commit (e.g. linux-next rebased
       it away); prepare keeps trying the rest until one does. The commit
       only 'arrives' after the 3rd fetch attempt."""
    fetched = []
    monkeypatch.setattr(refrepo, "_git", _mock_git_recording(fetched))
    # have() False until 3 fetches have been attempted, then True
    monkeypatch.setattr(refrepo, "have", lambda c: len(fetched) >= 3)
    monkeypatch.setattr(refrepo.os.path, "lexists", lambda p: False)
    wt, how = refrepo.prepare("deadbeef", "/tmp/wt", base_tree="linux-next")
    assert how == "fetched"
    assert fetched[0] == "linux-next"                 # hint still tried first
    assert len(fetched) == 3                          # then fell through


def test_prepare_raises_when_no_remote_has_the_commit(monkeypatch):
    import pytest
    fetched = []
    monkeypatch.setattr(refrepo, "_git", _mock_git_recording(fetched))
    monkeypatch.setattr(refrepo, "have", lambda c: False)   # never arrives
    with pytest.raises(RuntimeError, match="not found"):
        refrepo.prepare("deadbeef", "/tmp/wt")
    assert len(fetched) == len(refrepo.REMOTES)       # tried every remote


# --- resolve_tip (no_base tip-at-submission fallback) ----------------------

def _mock_git_tip(*, fetch_rc=0, rev_list_out="", rev_list_rc=0, calls=None):
    """A refrepo._git replacement for resolve_tip: records the git
       subcommands it sees and returns canned results for fetch / rev-list."""
    def fake_git(*args):
        if calls is not None:
            calls.append(args)
        if args and args[0] == "fetch":
            return SimpleNamespace(returncode=fetch_rc, stdout="", stderr="")
        if args and args[0] == "rev-list":
            return SimpleNamespace(returncode=rev_list_rc,
                                    stdout=rev_list_out, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return fake_git


def test_resolve_tip_returns_newest_commit_before_submission(monkeypatch):
    calls = []
    monkeypatch.setattr(refrepo, "_git",
                        _mock_git_tip(rev_list_out="cafef00d1234\n", calls=calls))
    sha = refrepo.resolve_tip("net-next", 1_700_000_000)
    assert sha == "cafef00d1234"
    # fetched the named tree, then asked for the tip at/-before submission
    assert ("fetch", "--quiet", "net-next") in calls
    rl = [c for c in calls if c[0] == "rev-list"][0]
    assert "--before=@1700000000" in rl and "--remotes=net-next" in rl


def test_resolve_tip_none_for_unknown_tree(monkeypatch):
    # an unknown tree is never fetched — guard short-circuits
    monkeypatch.setattr(refrepo, "_git",
                        lambda *a: pytest_fail_git())
    assert refrepo.resolve_tip("some-vendor-tree", 1_700_000_000) is None


def test_resolve_tip_none_when_no_timestamp(monkeypatch):
    monkeypatch.setattr(refrepo, "_git", lambda *a: pytest_fail_git())
    assert refrepo.resolve_tip("mainline", None) is None


def test_resolve_tip_none_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(refrepo, "_git", _mock_git_tip(fetch_rc=1))
    assert refrepo.resolve_tip("mainline", 1_700_000_000) is None


def test_resolve_tip_none_when_no_commit_predates_submission(monkeypatch):
    # rev-list succeeds but finds nothing at/-before the submission instant
    monkeypatch.setattr(refrepo, "_git",
                        _mock_git_tip(rev_list_out="\n"))
    assert refrepo.resolve_tip("mainline", 1_700_000_000) is None


def pytest_fail_git():
    raise AssertionError("_git must not be called on the guard path")


# --- sweep_worktrees (reclaim leaked review checkouts) ---------------------

def test_sweep_worktrees_reclaims_review_dirs(tmp_path, monkeypatch):
    """Every `review-*` dir under scratch is a leftover (sweep is called only
       when idle); each is removed and the count returned. A non-review dir is
       left untouched, and `git worktree prune` runs once at the end."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "review-aaa").mkdir()
    (scratch / "review-bbb").mkdir()
    (scratch / "keep-me").mkdir()                 # not review-* → untouched
    calls = []
    def fake_git(*args):
        calls.append(args)                        # mocked: doesn't remove dirs
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(refrepo, "_git", fake_git)

    n = refrepo.sweep_worktrees(str(scratch))

    assert n == 2
    assert not (scratch / "review-aaa").exists()  # rm -rf fallback removed it
    assert not (scratch / "review-bbb").exists()
    assert (scratch / "keep-me").exists()          # untouched
    assert ("worktree", "remove", "--force",
            str(scratch / "review-aaa")) in calls
    assert ("worktree", "prune") in calls          # admin refs pruned once


def test_sweep_worktrees_noop_when_scratch_absent(monkeypatch):
    """No scratch dir yet (first start) → a no-op that never shells out."""
    monkeypatch.setattr(refrepo, "_git", lambda *a: pytest_fail_git())
    assert refrepo.sweep_worktrees("/no/such/scratch/dir") == 0


def test_sweep_worktrees_no_prune_when_nothing_to_reclaim(tmp_path,
                                                          monkeypatch):
    """A clean scratch (only non-review entries) reclaims nothing and skips
       the prune call entirely."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "keep-me").mkdir()
    calls = []
    monkeypatch.setattr(refrepo, "_git",
                        lambda *a: calls.append(a) or
                        SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert refrepo.sweep_worktrees(str(scratch)) == 0
    assert calls == []                             # no worktree remove / prune


# --- size_mb (the gc trigger reads this) -----------------------------------

def test_size_mb_parses_du(monkeypatch):
    monkeypatch.setattr(refrepo.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(refrepo.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            returncode=0, stdout="7234\t/data/linux\n",
                            stderr=""))
    assert refrepo.size_mb() == 7234


def test_size_mb_zero_when_repo_absent(monkeypatch):
    monkeypatch.setattr(refrepo.os.path, "isdir", lambda p: False)
    assert refrepo.size_mb() == 0


def test_size_mb_zero_when_du_fails(monkeypatch):
    monkeypatch.setattr(refrepo.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(refrepo.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            returncode=1, stdout="", stderr="du: error"))
    assert refrepo.size_mb() == 0


# --- instrumentation: object count, anchors, fetch & gc stats --------------
# These surface in the health snapshot to tell a cheap delta fetch (a few
# thousand objects, anchors intact) from a full-ancestry re-pull that
# gc --prune=now forced (millions, no surviving anchor).

def test_object_count_sums_loose_and_packed(monkeypatch):
    out = ("count: 12\nsize: 48\nin-pack: 9000\n"
           "packs: 1\nsize-pack: 50000\nprune-packable: 0\n")
    monkeypatch.setattr(refrepo, "_git",
                        lambda *a: SimpleNamespace(returncode=0, stdout=out,
                                                   stderr=""))
    assert refrepo._object_count() == 9012            # loose + in-pack


def test_object_count_none_when_call_fails(monkeypatch):
    monkeypatch.setattr(refrepo, "_git",
                        lambda *a: SimpleNamespace(returncode=1, stdout="",
                                                   stderr="not a git repo"))
    assert refrepo._object_count() is None


def test_tracking_ref_count_counts_remote_refs(monkeypatch):
    out = "refs/remotes/mainline/master\nrefs/remotes/stable/linux-6.6.y\n\n"
    monkeypatch.setattr(refrepo, "_git",
                        lambda *a: SimpleNamespace(returncode=0, stdout=out,
                                                   stderr=""))
    assert refrepo.tracking_ref_count() == 2          # blank line ignored


def test_tracking_ref_count_none_when_call_fails(monkeypatch):
    monkeypatch.setattr(refrepo, "_git",
                        lambda *a: SimpleNamespace(returncode=128, stdout="",
                                                   stderr="error"))
    assert refrepo.tracking_ref_count() is None


def test_prepare_records_fetch_object_delta(monkeypatch):
    """A fetch stamps last_fetch_stats with the hit remote, the short commit
       and the object delta across the fetch — the delta-vs-full signal."""
    fetched = []
    counts = iter(["count: 10\nin-pack: 0\n",         # before the fetch
                   "count: 5\nin-pack: 4000\n"])      # after the fetch
    def fake_git(*args):
        if args[0] == "count-objects":
            return SimpleNamespace(returncode=0, stdout=next(counts), stderr="")
        if args[0] == "fetch":
            fetched.append(args[2])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(refrepo, "_git", fake_git)
    monkeypatch.setattr(refrepo, "have", lambda c: bool(fetched))
    monkeypatch.setattr(refrepo.os.path, "lexists", lambda p: False)
    monkeypatch.setattr(refrepo, "_last_fetch_stats", None, raising=False)

    refrepo.prepare("deadbeefcafe99", "/tmp/wt", base_tree="stable")

    s = refrepo.last_fetch_stats()
    assert s["remote"] == "stable"
    assert s["commit"] == "deadbeefcafe"               # 12-char prefix
    assert s["objects_added"] == (5 + 4000) - (10 + 0)
    assert isinstance(s["ms"], int)


def test_prepare_present_base_leaves_fetch_stats_untouched(monkeypatch):
    """An already-present base does no fetch, so it must not overwrite the
       last recorded fetch (the health snapshot keeps showing the real one)."""
    monkeypatch.setattr(refrepo, "_git",
                        lambda *a: SimpleNamespace(returncode=0, stdout="",
                                                   stderr=""))
    monkeypatch.setattr(refrepo, "have", lambda c: True)   # already present
    monkeypatch.setattr(refrepo.os.path, "lexists", lambda p: False)
    sentinel = {"sentinel": True}
    monkeypatch.setattr(refrepo, "_last_fetch_stats", sentinel, raising=False)

    refrepo.prepare("deadbeef", "/tmp/wt")

    assert refrepo.last_fetch_stats() is sentinel


def test_gc_records_size_anchor_and_duration(monkeypatch):
    sizes = iter([5000, 1200])                         # before, after
    monkeypatch.setattr(refrepo, "size_mb", lambda: next(sizes))
    monkeypatch.setattr(refrepo, "tracking_ref_count", lambda: 9)
    monkeypatch.setattr(refrepo, "_git",
                        lambda *a: SimpleNamespace(returncode=0, stdout="",
                                                   stderr=""))
    monkeypatch.setattr(refrepo, "_fetches_since_gc", 5, raising=False)
    assert refrepo.gc() is True
    s = refrepo.last_gc_stats()
    assert s["size_mb_before"] == 5000
    assert s["size_mb_after"] == 1200
    assert s["tracking_refs"] == 9                     # anchors that survived
    assert s["fetches"] == 5                           # churn this gc reclaimed for
    assert s["ok"] is True
    assert isinstance(s["ms"], int)
    assert refrepo.fetches_since_gc() == 0             # counter reset for next cycle


def test_gc_records_failure(monkeypatch):
    monkeypatch.setattr(refrepo, "size_mb", lambda: 0)
    monkeypatch.setattr(refrepo, "tracking_ref_count", lambda: 0)
    monkeypatch.setattr(refrepo, "_git",
                        lambda *a: SimpleNamespace(returncode=1, stdout="",
                                                   stderr="gc failed"))
    assert refrepo.gc() is False
    assert refrepo.last_gc_stats()["ok"] is False


# --- resolve_tip full-fetch instrumentation + fetch counter ----------------
# resolve_tip's `git fetch <tree>` is the heavy, churn-driving fetch (a whole
# daily-rebased tree); it's tracked apart from prepare's by-SHA delta. Both
# bump fetches_since_gc — the signal the gc trigger gates on so a node that
# fetched nothing never pays a no-op repack.

def test_resolve_tip_records_full_fetch_stats_and_bumps_counter(monkeypatch):
    counts = iter(["count: 100\nin-pack: 0\n",          # before the full fetch
                   "count: 50\nin-pack: 900000\n"])     # after
    def fake_git(*args):
        if args[0] == "count-objects":
            return SimpleNamespace(returncode=0, stdout=next(counts), stderr="")
        if args[0] == "fetch":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "rev-list":
            return SimpleNamespace(returncode=0, stdout="cafef00d1234\n",
                                   stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(refrepo, "_git", fake_git)
    monkeypatch.setattr(refrepo, "_fetches_since_gc", 0, raising=False)
    monkeypatch.setattr(refrepo, "_last_resolve_stats", None, raising=False)

    sha = refrepo.resolve_tip("net-next", 1_700_000_000)

    assert sha == "cafef00d1234"
    s = refrepo.last_resolve_stats()
    assert s["tree"] == "net-next"
    assert s["objects_added"] == (50 + 900000) - (100 + 0)
    assert isinstance(s["ms"], int)
    assert refrepo.fetches_since_gc() == 1


def test_resolve_tip_failed_fetch_records_nothing_and_no_bump(monkeypatch):
    """A failed fetch added no churn — no stats, no counter bump."""
    monkeypatch.setattr(refrepo, "_git", _mock_git_tip(fetch_rc=1))
    monkeypatch.setattr(refrepo, "_fetches_since_gc", 0, raising=False)
    monkeypatch.setattr(refrepo, "_last_resolve_stats", "sentinel",
                        raising=False)
    assert refrepo.resolve_tip("mainline", 1_700_000_000) is None
    assert refrepo.fetches_since_gc() == 0
    assert refrepo.last_resolve_stats() == "sentinel"   # untouched


def test_prepare_fetch_bumps_counter(monkeypatch):
    fetched = []
    monkeypatch.setattr(refrepo, "_git", _mock_git_recording(fetched))
    monkeypatch.setattr(refrepo, "have", lambda c: bool(fetched))
    monkeypatch.setattr(refrepo.os.path, "lexists", lambda p: False)
    monkeypatch.setattr(refrepo, "_fetches_since_gc", 0, raising=False)
    refrepo.prepare("deadbeef", "/tmp/wt", base_tree="stable")
    assert refrepo.fetches_since_gc() == 1


def test_prepare_present_base_does_not_bump_counter(monkeypatch):
    """An already-present base fetches nothing, so it must not count toward
       the gc trigger."""
    monkeypatch.setattr(refrepo, "_git", _mock_git_recording([]))
    monkeypatch.setattr(refrepo, "have", lambda c: True)   # already present
    monkeypatch.setattr(refrepo.os.path, "lexists", lambda p: False)
    monkeypatch.setattr(refrepo, "_fetches_since_gc", 0, raising=False)
    refrepo.prepare("deadbeef", "/tmp/wt")
    assert refrepo.fetches_since_gc() == 0
