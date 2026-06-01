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
