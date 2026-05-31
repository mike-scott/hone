"""bootstrap() wiring in node/runner — it enrolls, then initializes the
reference repo so a from-scratch node can fetch base commits at review
time. Before this, bootstrap was a stub and every review deferred with
"base … unobtainable" because /data/linux was never a git repo."""
from types import SimpleNamespace

from node import runner


def _cfg():
    # backoff_initial/backoff_max are read by _with_backoff (enrollment runs
    # through it); enrollment here never fails, so the values are unused.
    return SimpleNamespace(repo_dir="/data/linux", core_url="https://core",
                           backoff_initial=0.001, backoff_max=0.01)


def test_bootstrap_enrolls_then_initializes_repo(monkeypatch):
    calls = []

    client = SimpleNamespace(ensure_enrolled=lambda: calls.append("enroll"))
    monkeypatch.setattr(runner.refrepo, "ensure_repo",
                        lambda: calls.append("ensure_repo"))

    runner.bootstrap(_cfg(), client)

    # enrollment happens first, then the reference repo is initialized
    assert calls == ["enroll", "ensure_repo"]


def test_bootstrap_skips_repo_when_no_tree_bound_task_types(monkeypatch):
    """A prepare-only node (Tier-0/1 are tree-free) needs no reference repo;
       ensure_repo is not called when `review` isn't supported."""
    calls = []
    client = SimpleNamespace(ensure_enrolled=lambda: calls.append("enroll"))
    monkeypatch.setattr(runner.tasks, "SUPPORTED_TASK_TYPES", ("prepare",))
    monkeypatch.setattr(runner.refrepo, "ensure_repo",
                        lambda: calls.append("ensure_repo"))

    runner.bootstrap(_cfg(), client)

    assert calls == ["enroll"]            # no ensure_repo
