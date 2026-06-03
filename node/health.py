"""hone-node health snapshot — the periodic / on-event report the node
sends to hone-core's /v1/nodes/me/health endpoint.

One function — `collect(cfg)` — returns the dict the operator UI
renders on /nodes. The first cut covers what's most operationally
valuable and cheapest to gather: free disk on the data volume, the
reference repo's on-disk size (it's the one volume that grows
unbounded under arbitrary base-commit fetches if `git gc --prune=now`
isn't run periodically), and the category of the most recent Anthropic
API failure (a 'auth' value strongly suggests a wrong key; 'rate_limit'
suggests budget pressure; cleared on a successful call).

The wire is a loose JSON dict — adding fields here doesn't need a
hone-core migration because the column is TEXT JSON. Keep additions
cheap (no privileged calls, no long-running greps).
"""
import logging
import os
import shutil
import subprocess

from node import ai

log = logging.getLogger("hone.node.health")


def _free_disk_mb(path):
    """Free space on the filesystem holding `path`, in MiB. Returns
       None when the path doesn't exist (the volume isn't mounted yet
       in some boot sequences) so the UI can render `—` rather than
       a misleading 0."""
    if not path or not os.path.isdir(path):
        return None
    try:
        return shutil.disk_usage(path).free // (1024 ** 2)
    except OSError:
        return None


def _refrepo_size_mb(path):
    """The reference repo's on-disk size in MiB. Uses `du -sm` rather
       than os.walk because a kernel checkout is ~5M files; the syscall
       loop in du is an order of magnitude faster and matches what an
       operator would type. Returns None when the repo isn't there
       (first start, before refrepo.clone())."""
    if not path or not os.path.isdir(path):
        return None
    try:
        r = subprocess.run(["du", "-sm", path],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.split()[0])
    except (ValueError, IndexError):
        return None


def collect(cfg):
    """Build the health snapshot the runner posts to hone-core.

       Fields:
         - free_disk_mb: free space on cfg.data_dir, MiB
         - refrepo_size_mb: on-disk size of cfg.repo_dir, MiB
         - last_anthropic_error: short category string from
           node.ai (auth / rate_limit / connection / other), or
           None if the most recent call_claude returned cleanly
         - disk_low: True when free_disk_mb is below the configured
           floor (cfg.min_free_disk_mb) — the node has paused claiming
           until space recovers (runner._disk_too_low). False when
           space is unknown or the guard is disabled, so the operator
           can tell a paused node from a merely idle one.

       Keep this fast — it runs every tick of the claim loop."""
    free = _free_disk_mb(cfg.data_dir)
    floor = getattr(cfg, "min_free_disk_mb", 0) or 0
    return {
        "free_disk_mb":         free,
        "refrepo_size_mb":      _refrepo_size_mb(cfg.repo_dir),
        "last_anthropic_error": ai.get_last_error(),
        "disk_low":             bool(free is not None and floor > 0
                                     and free < floor),
    }
