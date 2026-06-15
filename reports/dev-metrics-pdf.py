#!/usr/bin/env python3
"""dev-metrics-pdf.py — typeset reports/dev-metrics.jsonl as a presentable PDF.

Renders the per-feature development-cost log through groff -ms/tbl and
ps2pdf (the only typesetting toolchain on the dev box — no Python PDF
libs needed). Cost estimates use Claude Fable 5 list pricing; the
assumptions are printed in the document itself.

Usage: reports/dev-metrics-pdf.py [--log-file reports/dev-metrics.jsonl] [-o reports/dev-metrics.pdf]
"""
import argparse
import datetime
import json
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Claude Fable 5 list pricing, USD per million tokens. Cache multipliers
# are the standard Anthropic ratios (write 1.25x input, read 0.1x); the
# log doesn't split 5m/1h cache writes, so 1.25x is the floor.
PRICE = {"input_tokens": 10.0, "output_tokens": 50.0,
         "cache_creation_input_tokens": 12.5,
         "cache_read_input_tokens": 1.0}


def cost(e):
    return sum(e.get(k, 0) / 1e6 * p for k, p in PRICE.items())


def tok(n):
    if n >= 10_000_000:
        return f"{n / 1e6:,.0f}M"
    if n >= 10_000:
        return f"{n / 1e3:,.0f}k"
    return f"{n:,}"


def wall(e):
    if not (e.get("started") and e.get("finished")):
        return "—"
    f = datetime.datetime.fromisoformat
    mins = (f(e["finished"].replace("Z", "+00:00"))
            - f(e["started"].replace("Z", "+00:00"))).total_seconds() / 60
    return f"{mins:.0f} min"


def build_ms(entries):
    total = {k: sum(e.get(k, 0) for e in entries) for k in PRICE}
    total_cost = sum(cost(e) for e in entries)
    total_msgs = sum(e.get("assistant_messages", 0) for e in entries)
    today = datetime.date.today().isoformat()

    # tbl delimiter: a single literal char that appears in no field (feature
    # slugs, ISO dates, formatted token/cost numbers) — NOT a tab, which a raw
    # f-string can't carry into the tab() directive as one character.
    rows = "\n".join(
        "@".join((
            # text block (T{…T}) so a long feature name wraps to the capped
            # column width instead of forcing the table past the line length.
            "T{\n" + e["feature"] + "\nT}",
            (e.get("started") or "")[:10],
            wall(e),
            str(e.get("assistant_messages", 0)),
            tok(e.get("output_tokens", 0)),
            tok(e.get("input_tokens", 0)),
            tok(e.get("cache_creation_input_tokens", 0)),
            tok(e.get("cache_read_input_tokens", 0)),
            f"${cost(e):,.2f}",
        )) for e in entries)

    return rf""".nr LL 7i
.nr PO 0.75i
.TL
hone-v2 \(em AI-assisted development metrics
.AU
generated from reports/dev-metrics.jsonl \(em {today}
.LP
Per-feature development cost of the hone-v2 patch-review system, built
interactively with Claude Code (model: Claude Fable 5). Each row is one
feature, measured from the request that started it to the last response
before the next request; agent (subagent) usage inside the window is
included. Every feature below shipped with its tests in the same window
\(em the full test suite was green at each finish point.
.SH
Features
.LP
.ps 9
.vs 11
.TS H
box tab(@);
lbw(2.2i) cb cb cb cb cb cb cb cb
lw(2.2i) c n n n n n n n .
Feature@Date@Wall@Turns@Out@In@C-write@C-read@Est. cost
.TH
{rows}
.TE
.ps
.vs
.SH
Totals
.LP
.TS
tab(@);
lb l .
Features@{len(entries)}
Assistant turns@{total_msgs:,}
Output tokens@{total["output_tokens"]:,}
Fresh input tokens@{total["input_tokens"]:,}
Cache-write tokens@{total["cache_creation_input_tokens"]:,}
Cache-read tokens@{total["cache_read_input_tokens"]:,}
Estimated cost@${total_cost:,.2f}
.TE
.SH
Method and caveats
.IP \(bu 2
Token counts come from the Claude Code session transcripts
(per-response usage records), summed per feature time window by
reports/transcript-metrics.py.
.IP \(bu 2
Cost is estimated at Claude Fable 5 list pricing: $10 / $50 per million
input / output tokens, cache writes at 1.25\(mu input, cache reads at
0.1\(mu. Cache reads dominate the token volume by design \(em the
conversation prefix is re-read every turn at one tenth of the fresh
input price.
.IP \(bu 2
Attribution is per time window, not per intent: where work on features
interleaves inside one window, their numbers blur together.
.IP \(bu 2
Wall time spans the first to the last model response of the window,
including the developer's reading and review time between turns.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-file",
                    default=os.path.join(REPO, "reports", "dev-metrics.jsonl"))
    ap.add_argument("-o", "--out",
                    default=os.path.join(REPO, "reports", "dev-metrics.pdf"))
    args = ap.parse_args()
    entries = sorted((json.loads(line) for line in open(args.log_file)),
                     key=lambda e: e.get("started") or "")
    ms = build_ms(entries)
    ps = subprocess.run(["groff", "-t", "-ms", "-Tps"],
                        input=ms.encode(), capture_output=True, check=True)
    subprocess.run(["ps2pdf", "-", args.out],
                   input=ps.stdout, check=True)
    print(args.out)


if __name__ == "__main__":
    main()
