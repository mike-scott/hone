"""patchview.py — render a stored thread message for the patchset view.

Messages are stored as the full raw RFC822 message (transport headers and
all — for the sample patch, 124 header lines / ~80% of the bytes precede
any content). This module turns one into display HTML by:

  1. stripping the leading RFC822 header block (kept separately so the
     template can offer a "raw headers" toggle), and
  2. classifying each remaining line so the patch stands out — file/hunk
     markers muted, additions on a faint green wash, deletions on red.

Diff classification is region-aware on purpose: a generic diff lexer
miscolours the shapes that actually occur in kernel patches —
`--- /dev/null` and `+++ b/…` are file markers (not a deletion/addition),
a bare `---` is the format-patch separator, `+---` (a YAML doc marker in
added content) *is* an addition, and the diffstat's `+`/`-` runs are not
diff content. So we walk the body with an in-diff flag rather than match
line prefixes blindly.

The body is untrusted email content, so every line is HTML-escaped before
being wrapped in a span; `body_html` is therefore safe to mark |safe in
the template. `headers` is returned as plain text (the template escapes
it).
"""
import html
import re
from typing import NamedTuple

# Stands in for an empty source line so its <span> keeps a line box (and
# thus a full-height coloured row); a zero-width space adds no width.
_BLANK = "​"


class Rendered(NamedTuple):
    headers: str    # raw RFC822 header block, "" when none was found
    body_html: str  # escaped content wrapped in classified <span> lines


# A header field name at the very start of a line: "Received:", "From:",
# "ARC-Seal:". Used only to decide whether the block before the first
# blank line really is an RFC822 header block (a prose reply that happens
# to contain a blank line must not be mistaken for one).
_HEADER_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:")

# A diffstat path line: " drivers/foo.c | 52 ++++" / " bar | Bin 0 -> 9 bytes".
_DIFFSTAT_PATH = re.compile(r"^\s+\S.*\|\s+(?:\d+|Bin)\b")

# A hunk header — capture the old/new line counts (absent → 1) so we can
# tell where the hunk body ends. Past that point a `-- ` / `--` is the
# email/git signature separator, not a deletion.
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _looks_like_headers(block: str) -> bool:
    first = block.lstrip("\n").split("\n", 1)[0]
    return bool(_HEADER_FIELD.match(first))


def split_headers(body: str):
    """Split a raw message into (header_block, content) on the first blank
       line — the RFC822 separator. When the leading block doesn't look
       like headers (an already-clean body), everything is content."""
    head, sep, rest = body.partition("\n\n")
    if sep and _looks_like_headers(head):
        return head, rest
    return "", body


def _is_diffstat(line: str) -> bool:
    if _DIFFSTAT_PATH.match(line):
        return True
    s = line.strip()
    return (bool(re.match(r"\d+ files? changed", s))
            or s.startswith(("create mode ", "delete mode ",
                             "rename ", "copy ", "mode change ")))


# Lines that, inside the diff region, are metadata about a file rather
# than its content.
_FILE_MARKERS = ("index ", "new file mode", "deleted file mode",
                 "old mode", "new mode", "similarity index",
                 "dissimilarity index", "rename ", "copy ",
                 "GIT binary patch", "Binary files")


def _preamble_class(line: str) -> str:
    """Classify a line before the diff begins: the commit message, its
       trailers, the `---` separator and the diffstat."""
    if line.strip() == "---":              # format-patch separator
        return "meta"
    if _is_diffstat(line):
        return "meta"
    return "plain"


def _classify(content: str):
    """Walk the body yielding (css_class, line) for each line. A hunk's
       `@@ -a,b +c,d @@` header gives the old/new line budget; we spend it on
       the body lines so we know when the hunk ends. Outside a hunk a `-`/`+`
       line is a file marker or the trailing `-- ` signature — never a +/-
       change — which is what keeps the signature out of the deletion colour."""
    in_diff = False
    old_rem = new_rem = 0                  # unspent old/new lines in the hunk
    for line in content.split("\n"):
        if line.startswith("diff -"):
            in_diff, old_rem, new_rem = True, 0, 0
            cls = "file"
        elif in_diff and line.startswith("@@"):
            m = _HUNK.match(line)
            old_rem = int(m.group(1)) if m and m.group(1) else (1 if m else 0)
            new_rem = int(m.group(2)) if m and m.group(2) else (1 if m else 0)
            cls = "hunk"
        elif in_diff and (old_rem > 0 or new_rem > 0):   # inside the hunk body
            if line.startswith("\\"):                    # "\ No newline…"
                cls = "meta"
            elif line.startswith("+"):
                cls, new_rem = "add", new_rem - 1
            elif line.startswith("-"):
                cls, old_rem = "del", old_rem - 1
            else:                                         # context / blank
                cls, old_rem, new_rem = "ctx", old_rem - 1, new_rem - 1
        elif in_diff:                                     # between/after hunks
            cls = ("file" if line.startswith(("+++ ", "--- ", *_FILE_MARKERS))
                   else "meta")                           # incl. the "-- " sig
        else:
            cls = _preamble_class(line)
        yield cls, line


def _span(cls: str, line: str) -> str:
    return f'<span class="pl pl-{cls}">{html.escape(line) or _BLANK}</span>'


def _diff_html(content: str) -> str:
    return "".join(_span(cls, line) for cls, line in _classify(content))


def diff_line_spans(body: str):
    """Render a patch body to per-line classified spans for the inline-review
       view, returning (origin, spans). `spans[i]` is the markup for content
       line i (same classes as `render(is_patch=True)`); `origin` is the index
       of the first `diff --git` line — the 0-point a concern's
       `spans_lines_in_diff` counts from — or None when the body has no diff."""
    _, content = split_headers(body)
    content = content.strip("\n")
    spans, origin = [], None
    for i, (cls, line) in enumerate(_classify(content)):
        if origin is None and line.startswith("diff --git"):
            origin = i
        spans.append(_span(cls, line))
    return origin, spans


def _prose_html(content: str) -> str:
    out = []
    for line in content.split("\n"):
        cls = "quote" if line.startswith(">") else "plain"
        text = html.escape(line) or _BLANK
        out.append(f'<span class="pl pl-{cls}">{text}</span>')
    return "".join(out)


def render(body: str, *, is_patch: bool) -> Rendered:
    """Render a stored message body for display. Strips the RFC822 header
       block; classifies diff lines when `is_patch` (otherwise the content
       is prose — only quoted `>` lines are dimmed, never +/- coloured)."""
    if not body:
        return Rendered("", "")
    headers, content = split_headers(body)
    content = content.strip("\n")
    body_html = _diff_html(content) if is_patch else _prose_html(content)
    return Rendered(headers, body_html)
