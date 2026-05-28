"""hone-node maintainer resolution — runs the kernel's get_maintainer.pl
against a patch at its base commit, producing the authoritative
subsystem + maintainer / reviewer / list sets for prepare's
deterministic phase (Tier 0). See docs/ARCHITECTURE-PREPARE.md.

With `--no-git --no-tree`, get_maintainer.pl resolves purely from the
MAINTAINERS file and the patch's file paths — no tree walk — so we
fetch just two blobs at the base commit via the cgit client and run
perl in a throwaway temp dir. Spike-confirmed (2026-05-27): empty
stderr, base perl, no other tree files touched.

Three layers, split for testability:
  - parse_get_maintainer(output)  — pure parser of --rolestats lines
  - run_get_maintainer(...)       — temp dir + perl subprocess
  - resolve_maintainers(client, …) — fetch the two blobs + run + parse
Any failure (fetch miss, perl absent, non-zero exit, timeout) returns
None so the caller degrades that field to heuristic mode.
"""
import logging
import re
import subprocess
import tempfile
from collections import namedtuple
from pathlib import Path

log = logging.getLogger("hone.node.maintainers")

_GM_TIMEOUT_SECONDS = 30

# One parsed `get_maintainer.pl --rolestats` line. `role` is the raw
# label the script emits ("maintainer", "reviewer", "open list",
# "moderated list", "supporter", …); `subsystem` is the MAINTAINERS
# section name or None; `name` is None for a bare list address. The
# Tier-0 resolver buckets these raw roles into the methodology's
# maintainer / reviewer / list sets.
MaintainerEntry = namedtuple("MaintainerEntry",
                              ["name", "email", "role", "subsystem"])

# A rolestat line is `<address> (<role>[:<subsystem>])`. The trailing
# parenthesised group is anchored to end-of-line; `[^()]*` keeps it from
# swallowing a stray `(` earlier in a name, and the `$` anchor makes the
# address absorb everything up to the LAST group.
_ROLESTAT_RE = re.compile(r"^(?P<addr>.*?)\s*\((?P<role>[^()]*)\)\s*$")
_ADDR_RE     = re.compile(r"^(?P<name>.*?)\s*<(?P<email>[^>]+)>\s*$")


def parse_get_maintainer(output):
    """Parse `get_maintainer.pl --rolestats` stdout into a list of
       MaintainerEntry. Lines that don't match the rolestat shape
       (blank lines, anything unexpected) are skipped rather than
       erroring."""
    entries = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _ROLESTAT_RE.match(line)
        if not m:
            continue
        addr = m.group("addr").strip()
        role, _sep, subsystem = m.group("role").strip().partition(":")
        role = role.strip()
        subsystem = subsystem.strip() or None
        am = _ADDR_RE.match(addr)
        if am:
            name = am.group("name").strip().strip('"').strip() or None
            email = am.group("email").strip()
        else:
            name, email = None, addr        # bare list address
        if email:
            entries.append(MaintainerEntry(name, email, role, subsystem))
    return entries


def run_get_maintainer(maintainers_text, script_text, patch_text, *,
                        timeout=_GM_TIMEOUT_SECONDS):
    """Run `get_maintainer.pl --no-git --no-tree --rolestats` against
       the patch, with MAINTAINERS + the script written to a throwaway
       dir (cwd, so the script finds MAINTAINERS). Returns stdout on
       success, None if perl is missing, the script exits non-zero, or
       it times out."""
    with tempfile.TemporaryDirectory(prefix="hone-gm-") as d:
        dpath = Path(d)
        (dpath / "MAINTAINERS").write_text(maintainers_text, encoding="utf-8")
        (dpath / "get_maintainer.pl").write_text(script_text,
                                                  encoding="utf-8")
        (dpath / "patch").write_text(patch_text, encoding="utf-8")
        cmd = ["perl", "get_maintainer.pl",
               "--no-git", "--no-tree", "--rolestats", "patch"]
        try:
            r = subprocess.run(cmd, cwd=d, capture_output=True, text=True,
                                timeout=timeout)
        except FileNotFoundError:
            log.warning("get_maintainer: perl not found in PATH")
            return None
        except subprocess.TimeoutExpired:
            log.warning("get_maintainer: timed out after %ss", timeout)
            return None
        if r.returncode != 0:
            log.warning("get_maintainer: exit %d: %s",
                         r.returncode, (r.stderr or "").strip()[:200])
            return None
        return r.stdout


def resolve_maintainers(client, base_sha, patch_text, *,
                         timeout=_GM_TIMEOUT_SECONDS):
    """Fetch MAINTAINERS + scripts/get_maintainer.pl at `base_sha` via
       the cgit `client` (the tree the base resolved in), run the script
       against `patch_text`, and return the parsed MaintainerEntry list.
       None if either blob can't be fetched or the run fails — the
       caller degrades the maintainer/subsystem fields to heuristic."""
    maintainers = client.fetch_file_at("MAINTAINERS", base_sha)
    if maintainers is None:
        return None
    script = client.fetch_file_at("scripts/get_maintainer.pl", base_sha)
    if script is None:
        return None
    output = run_get_maintainer(maintainers, script, patch_text,
                                 timeout=timeout)
    if output is None:
        return None
    return parse_get_maintainer(output)
