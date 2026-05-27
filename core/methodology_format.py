"""Methodology document canonicalization.

A single normalizer that gets called at every write boundary that
touches a methodology document:

  - the operator import endpoint (POST /settings/methodology/import),
  - any future "accept methodology proposal" path emitted by the
    autonomous draft flow,
  - the export endpoint (defensive — covers DB rows that pre-date
    the normalizer landing).

The normalization reflows every multi-line string field through
mdformat at the PROSE_WRAP_COLUMN fill width defined below.
mdformat is
Markdown-aware: it preserves `###` headers, fenced code blocks,
inline `code`, and ordered/bulleted list structure. Single-line
strings (ids, titles, tags) are left untouched.

Idempotence is the property tests pin: `normalize(normalize(d)) ==
normalize(d)`. Without it, every read-through-write cycle would
silently churn the document and produce noise in the audit trail.
"""
import mdformat

# Target line width for prose reflow. Single source of truth for
# any "fill column" reference; comments elsewhere refer to it by
# name rather than hard-coding the value, so a future tuning here
# doesn't leave stale numbers scattered through the codebase.
PROSE_WRAP_COLUMN = 80


def normalize_methodology(document):
    """Return a new methodology document with every multi-line string
       field canonicalized via mdformat at PROSE_WRAP_COLUMN. The
       input is not mutated.

       Heuristic for "this is prose, reflow it": the string contains
       a newline. Single-line fields (ids, titles, mailing-list
       addresses, etc.) never need reflow and are passed through
       unchanged — they don't carry Markdown structure.

       Recursive: walks dicts and lists. Non-string leaves
       (integers, booleans, None) are returned as-is."""
    return _walk(document)


def _walk(node):
    if isinstance(node, dict):
        return {k: _walk(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v) for v in node]
    if isinstance(node, str) and "\n" in node:
        return _reflow_prose(node)
    return node


def _reflow_prose(text):
    """Reflow a Markdown string at PROSE_WRAP_COLUMN. mdformat handles
       the Markdown semantics (preserves code fences, list structure,
       headers, inline code, links); we just hand it the text and
       the rendering options.

       `number: True` keeps ordered-list items consecutively numbered
       (`1.`, `2.`, `3.`, …) in the source. mdformat's default is to
       emit `1.` for every item and let the Markdown renderer assign
       display numbers — semantically equivalent but visually
       confusing in a YAML document an operator will read by eye, and
       it's the form Claude sees in the prompt too."""
    return mdformat.text(text, options={"wrap":   PROSE_WRAP_COLUMN,
                                          "number": True})
