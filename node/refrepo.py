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
  refrepo.py prepare <commit> <wt-dir>  ensure <commit> present (fetch it once,
                                        serially, from the known remotes if
                                        missing); add a detached worktree
  refrepo.py cleanup <wt-dir> [...]     remove prepared worktree(s)
  refrepo.py gc                         bound the repo (git gc --prune=now)
  refrepo.py status                     repo size + live worktrees

Importable: prepare(), cleanup(), gc(), base_of(), have().
"""
import os
import re
import subprocess
import sys

# The reference kernel repo. $HONE_REPO_DIR in the containerized node; the
# precursor single-host tree otherwise.
REPO = os.environ.get("HONE_REPO_DIR") or os.path.expanduser("~/src/linux-mainline")

# The bounded set of remotes a base commit may be fetched from. Kept small on
# purpose — every extra integration tree is more daily-rebased churn to gc.
REMOTES = {
    "origin":     "git://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
    "linux-next": "git://git.kernel.org/pub/scm/linux/kernel/git/next/linux-next.git",
    "net-next":   "git://git.kernel.org/pub/scm/linux/kernel/git/netdev/net-next.git",
    "tip":        "git://git.kernel.org/pub/scm/linux/kernel/git/tip/tip.git",
}

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


def base_of(patch_file):
    """The base-commit: trailer of a patch file, or None."""
    try:
        text = open(patch_file, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    m = BASE_RE.search(text)
    return m.group(1) if m else None


def prepare(commit, wt_dir):
    """Ensure <commit> is present in the reference repo — fetching it once,
       serially, from the known remotes if missing — then add a detached
       worktree at <wt_dir>. Returns (wt_dir, 'present'|'fetched').
       Raises RuntimeError if the commit cannot be obtained."""
    status = "present"
    if not have(commit):
        for name in REMOTES:
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
        wt, how = prepare(a[2], a[3])
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
