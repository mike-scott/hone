#!/usr/bin/env python3
"""refrepo.py - the hone-node reference-tree manager.

A patch is reviewed against its stated base commit. Each node owns one
reference kernel repo in its data volume and adds a detached worktree at the
base commit for every review — reading code at that commit, never building
it. A base commit is fetched once, serially, from a small fixed set of
remotes; `gc --prune=now` then bounds the repo, because arbitrary base
fetches (especially daily-rebased linux-next) otherwise accrete unbounded
unreachable churn — left unchecked this reached 116 GB.

CLI:
  refrepo.py base <patch-file>          print the patch's base-commit: trailer
  refrepo.py prepare <commit> <wt-dir> [tree]
                                        ensure <commit> present (fetch it once,
                                        serially, from the known remotes if
                                        missing — trying [tree] first when
                                        given); add a detached worktree
  refrepo.py cleanup <wt-dir> [...]     remove prepared worktree(s)
  refrepo.py gc                         bound the repo (git gc --prune=now)
  refrepo.py status                     repo size + live worktrees

Importable: prepare(), cleanup(), gc(), base_of(), have().
"""
import os
import re
import subprocess
import sys

from node import cgit

# The reference kernel repo. $HONE_REPO_DIR in the containerized node; the
# precursor single-host tree otherwise.
REPO = os.environ.get("HONE_REPO_DIR") or os.path.expanduser("~/src/linux-mainline")

# The remotes a base commit may be fetched from, derived from the shared
# named-trees registry (node/cgit.py) so prepare's Tier-0 base resolution
# and review's base fetch agree on the tree set and names — a recorded
# `tree_state.base_tree` maps straight to a remote here. Keyed by the
# registry's canonical name; the git_url is the fetch URL. (Every extra
# integration tree is more daily-rebased churn to gc, so the registry is
# kept small.)
REMOTES = {t.name: t.git_url for t in cgit.DEFAULT_TREES}

BASE_RE = re.compile(r'^base-commit:\s*([0-9a-f]{12,40})\b', re.I | re.M)


def _git(*args):
    return subprocess.run(["git", "-C", REPO, *args],
                          capture_output=True, text=True)


def have(commit):
    """True if <commit> is already an object in the reference repo."""
    return _git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


def _ensure_remote(name):
    if name in REMOTES and _git("remote", "get-url", name).returncode != 0:
        _git("remote", "add", name, REMOTES[name])


def is_initialized():
    """True if REPO is already a git repository (a restarted node reuses
       the volume rather than re-initializing)."""
    return os.path.isdir(REPO) and _git("rev-parse", "--git-dir").returncode == 0


def ensure_repo():
    """Initialize the reference repo if absent, idempotently. Creates an
       empty repo at REPO and registers every named-trees remote; base
       commits are fetched on demand by prepare() (no clone here, so
       startup is instant and disk stays minimal — the first review for a
       given base pays its one-time fetch). A no-op when REPO already
       holds a git repository, so a node restart reuses its volume.

       Returns REPO. Raises RuntimeError if `git init` fails."""
    if is_initialized():
        # Reconcile remotes anyway — the registry may have grown since the
        # volume was first initialized; _ensure_remote only adds missing ones.
        for name in REMOTES:
            _ensure_remote(name)
        return REPO
    os.makedirs(REPO, exist_ok=True)
    r = _git("init", "--quiet")
    if r.returncode != 0:
        raise RuntimeError(f"git init {REPO} failed: {r.stderr.strip()}")
    for name in REMOTES:
        _ensure_remote(name)
    return REPO


def base_of(patch_file):
    """The base-commit: trailer of a patch file, or None."""
    try:
        text = open(patch_file, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    m = BASE_RE.search(text)
    return m.group(1) if m else None


def _fetch_order(base_tree=None):
    """The order to try remotes in. With a `base_tree` hint that names a
       known remote, try it first, then the rest; otherwise the default
       registry order. The hint is just a reordering — every remote is
       still tried — so a stale hint (linux-next force-pushes daily, so
       a base recorded there at prepare time may be gone by review time)
       costs at most one failed fetch, never a missed commit."""
    names = list(REMOTES)
    if base_tree and base_tree in REMOTES:
        names = [base_tree] + [n for n in names if n != base_tree]
    return names


def prepare(commit, wt_dir, *, base_tree=None):
    """Ensure <commit> is present in the reference repo — fetching it once,
       serially, from the known remotes if missing — then add a detached
       worktree at <wt_dir>. Returns (wt_dir, 'present'|'fetched').
       Raises RuntimeError if the commit cannot be obtained.

       `base_tree` is the tree the prepare phase resolved the base in
       (tree_state.base_tree); when it names a known remote that remote
       is fetched first, falling back to the full serial scan."""
    status = "present"
    if not have(commit):
        for name in _fetch_order(base_tree):
            _ensure_remote(name)
            _git("fetch", "--quiet", name, commit)
            if have(commit):
                status = "fetched"
                break
    if not have(commit):
        raise RuntimeError(
            f"base {commit} not found in {', '.join(REMOTES)} — "
            f"review provisionally against the nearest local tip")
    if os.path.lexists(wt_dir):
        _git("worktree", "remove", "--force", wt_dir)
        _git("worktree", "prune")
    r = _git("worktree", "add", "--detach", wt_dir, commit)
    if r.returncode != 0:
        raise RuntimeError(f"worktree add failed: {r.stderr.strip()}")
    return wt_dir, status


def cleanup(*wt_dirs):
    """Remove prepared worktree(s) and prune dangling worktree refs."""
    for wt in wt_dirs:
        _git("worktree", "remove", "--force", wt)
    _git("worktree", "prune")


def gc():
    """Bound the repo — discard the unreachable churn that arbitrary base
       fetches (especially daily-rebased linux-next) leave behind."""
    return _git("gc", "--prune=now").returncode == 0


def _du():
    r = subprocess.run(["du", "-sh", os.path.join(REPO, ".git")],
                       capture_output=True, text=True)
    return r.stdout.split("\t")[0] if r.returncode == 0 else "?"


def main():
    a = sys.argv
    if len(a) < 2:
        print(__doc__)
        return
    if not os.path.isdir(REPO):
        sys.exit(f"reference repo missing: {REPO}")
    cmd = a[1]
    if cmd == "base" and len(a) >= 3:
        print(base_of(a[2]) or "")
    elif cmd == "prepare" and len(a) >= 4:
        wt, how = prepare(a[2], a[3], base_tree=a[4] if len(a) >= 5 else None)
        print(f"{wt}\t{how}")
    elif cmd == "cleanup" and len(a) >= 3:
        cleanup(*a[2:])
        print(f"removed {len(a) - 2} worktree(s)")
    elif cmd == "gc":
        before = _du()
        ok = gc()
        print(f".git {before} -> {_du()}  ({'ok' if ok else 'FAILED'})")
    elif cmd == "status":
        print(f"reference repo: {REPO}")
        print(f".git size:      {_du()}")
        print(_git("worktree", "list").stdout.rstrip())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
