#!/usr/bin/env python3
"""transcript-metrics.py — per-feature development cost from Claude Code
session transcripts.

Reads the ~/.claude/projects/<project>/*.jsonl transcript(s), where every
assistant message carries a `usage` block (input/output tokens plus cache
creation/read), and sums usage over a time window — typically one feature:
from the user message that requested it to the last assistant message
before the next request. Subagent (sidechain) usage inside the window is
included; it's real spend.

Honest caveat: attribution is only as clean as the session. When several
features interleave in one window, their tokens blur together — the
numbers are per-window, not per-intent.

Usage:
  reports/transcript-metrics.py list [--transcript PATH]
      Number and print the real user prompts (feature boundaries) with
      timestamps.
  reports/transcript-metrics.py sum --from N|ISO [--to N|ISO] [--transcript PATH]
      Sum assistant usage in [from, to). N = a prompt number from `list`;
      `--to` defaults to end-of-transcript.
  reports/transcript-metrics.py log --feature NAME --from N|ISO [--to N|ISO]
      [--log-file reports/dev-metrics.jsonl] [--transcript PATH]
      Sum and append one JSON line to the dev-metrics log.

The default transcript is the most recently modified .jsonl in the
project dir derived from this repo's path.
"""
import argparse
import datetime
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG = os.path.join(REPO, "reports", "dev-metrics.jsonl")

USAGE_KEYS = ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens")


def project_dir():
    return os.path.expanduser(
        "~/.claude/projects/" + REPO.replace("/", "-"))


def default_transcript():
    paths = glob.glob(os.path.join(project_dir(), "*.jsonl"))
    if not paths:
        sys.exit(f"no transcripts under {project_dir()}")
    return max(paths, key=os.path.getmtime)


def entries(path):
    with open(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def user_prompts(path):
    """The real user prompts — boundary candidates. Skips meta entries,
       subagent (sidechain) traffic and tool-result payloads."""
    out = []
    for e in entries(path):
        if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain"):
            continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, list):
            c = " ".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
        if not isinstance(c, str) or not c.strip():
            continue
        out.append({"ts": e.get("timestamp", ""), "text": c.strip()})
    return out


def resolve_bound(val, prompts, *, end):
    """A window bound: a prompt number from `list`, or an ISO timestamp
       (prefix match is fine — entries compare lexicographically)."""
    if val is None:
        return "9999" if end else ""
    try:
        n = int(val)
    except ValueError:
        return val
    if not 1 <= n <= len(prompts):
        sys.exit(f"prompt number {n} out of range 1..{len(prompts)}")
    return prompts[n - 1]["ts"]


def sum_window(path, lo, hi):
    tot = {k: 0 for k in USAGE_KEYS}
    n, first, last = 0, None, None
    for e in entries(path):
        if e.get("type") != "assistant":
            continue
        ts = e.get("timestamp", "")
        if not (lo <= ts < hi):
            continue
        usage = (e.get("message") or {}).get("usage") or {}
        for k in USAGE_KEYS:
            tot[k] += usage.get(k) or 0
        n += 1
        first = first or ts
        last = ts
    return {"assistant_messages": n, "started": first, "finished": last,
            **tot}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=("list", "sum", "log"))
    ap.add_argument("--transcript")
    ap.add_argument("--from", dest="lo")
    ap.add_argument("--to", dest="hi")
    ap.add_argument("--feature")
    ap.add_argument("--log-file", default=DEFAULT_LOG)
    args = ap.parse_args()
    path = args.transcript or default_transcript()
    prompts = user_prompts(path)

    if args.mode == "list":
        for i, p in enumerate(prompts, 1):
            text = " ".join(p["text"].split())
            print(f"{i:4d}  {p['ts']}  {text[:90]}")
        return

    if args.lo is None:
        sys.exit("--from is required for sum/log")
    lo = resolve_bound(args.lo, prompts, end=False)
    hi = resolve_bound(args.hi, prompts, end=True)
    s = sum_window(path, lo, hi)
    if args.mode == "sum":
        print(json.dumps(s, indent=2))
        return

    if not args.feature:
        sys.exit("--feature is required for log")
    entry = {"feature": args.feature,
             **s,
             "transcript": os.path.basename(path),
             "logged_at": datetime.datetime.now(
                 datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    os.makedirs(os.path.dirname(args.log_file), exist_ok=True)
    with open(args.log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(json.dumps(entry, indent=2))


if __name__ == "__main__":
    main()
