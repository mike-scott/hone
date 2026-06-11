# reports/ — per-feature AI development-cost metrics

Development-process tooling, NOT part of hone-core/hone-node. It
measures what each AI-built feature cost (wall time, assistant turns,
tokens, estimated dollars) and renders `dev-metrics.pdf` for reporting.
This README is written so an agent (or human) can regenerate everything
from scratch.

## The pipeline

```
Claude Code transcripts          transcript-metrics.py           dev-metrics-pdf.py
~/.claude/projects/<proj>/*.jsonl ──── log --feature ────▶ dev-metrics.jsonl ────▶ dev-metrics.pdf
```

1. **Source data.** Claude Code writes a JSONL transcript per session
   under `~/.claude/projects/<project-dir>/`, where `<project-dir>` is
   this repo's absolute path with `/` replaced by `-`
   (e.g. `-home-scottml-fio-hone-v2`). Every `"type": "assistant"`
   entry carries `message.usage` with `input_tokens`, `output_tokens`,
   `cache_creation_input_tokens`, `cache_read_input_tokens`, and a
   `timestamp`. Subagent traffic is marked `isSidechain: true` (its
   usage is real spend and IS counted); `"type": "user"` entries that
   are not `isMeta`/sidechain/tool-results are the human's prompts.

2. **Feature windows.** A "feature" is a time window between two user
   prompts — from the prompt that requested it to the prompt that
   started the next thing (usually the feature's "commit desc" + 1).
   Find the boundary numbers with:

       reports/transcript-metrics.py list

   then sum-and-append one log entry:

       reports/transcript-metrics.py log --feature "<short-name>" --from N --to M

   (`--to` omitted = end of transcript; bounds also accept raw ISO
   timestamps. `sum` does the same without writing.) The script
   defaults to the newest transcript in the project dir; pass
   `--transcript PATH` for older sessions.

3. **The log.** `dev-metrics.jsonl` — one JSON object per feature:
   `feature`, `started`/`finished` (first/last assistant message in the
   window), `assistant_messages`, the four token sums, `transcript`,
   `logged_at`. Convention: append an entry for each feature when its
   commit description is written.

4. **The PDF.** `reports/dev-metrics-pdf.py` reads the log (sorted by
   `started`), typesets a one-page report via `groff -t -ms -Tps |
   ps2pdf` (the only PDF toolchain assumed on the dev box — no Python
   PDF libraries), and writes `reports/dev-metrics.pdf` (gitignored;
   regenerate at will). The document contains a per-feature table,
   totals, and an estimated cost.

## Cost model (stated in the PDF itself)

Claude Fable 5 list pricing, USD per million tokens: input $10,
output $50, cache writes 1.25× input ($12.50), cache reads 0.1× ($1).
The log doesn't split 5-minute vs 1-hour cache writes, so the 1.25×
figure is a floor. Cache reads dominate token volume by design — the
conversation prefix is re-read every turn at a tenth of input price.
Update `PRICE` in `dev-metrics-pdf.py` if pricing or model changes.

## Caveats (also printed in the PDF)

- Attribution is per time window, not per intent: if work on several
  features interleaves in one window, their numbers blur together.
- Wall time spans first-to-last model response in the window and so
  includes the human's reading/review time between turns.
- Token counts are exact (from the API's own usage records); only the
  dollar figure is an estimate.
