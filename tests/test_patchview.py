"""Tests for core/patchview.py — RFC822 header stripping and region-aware
diff line classification. The tricky cases are the ones a naive
prefix-matcher gets wrong (`--- /dev/null`, `+++ b/…`, bare `---`, `+---`,
diffstat lines), plus escaping of untrusted bodies."""
from core import patchview


# A realistic raw message: transport headers, blank line, then a git
# format-patch body (commit log, ---, diffstat, the diff itself).
RAW_PATCH = """\
Received: from mx.example.org (mx.example.org [10.0.0.1])
\tby smtp.kernel.org (Postfix) with ESMTPS id ABC123
From: Dev <dev@example.org>
Subject: [PATCH] foo: do a thing
Date: Sun, 8 Mar 2026 23:37:02 +0000

From: Dev <dev@example.org>

Make foo do the thing.

Signed-off-by: Dev <dev@example.org>
---
 drivers/foo.c | 3 ++-
 1 file changed, 2 insertions(+), 1 deletion(-)

diff --git a/drivers/foo.c b/drivers/foo.c
index 000000000000..ea093b71d269 100644
--- a/drivers/foo.c
+++ b/drivers/foo.c
@@ -1,3 +1,3 @@
 context line
-old line
+new line
+---
\\ No newline at end of file
"""


def _spans(html):
    """Map each classified line's text to its pl-* class, in order."""
    import re
    return [(m.group(1), m.group(2))
            for m in re.finditer(r'<span class="pl pl-(\w+)">(.*?)</span>', html)]


# --- header stripping ------------------------------------------------------

def test_strips_rfc822_headers():
    r = patchview.render(RAW_PATCH, is_patch=True)
    assert r.headers.startswith("Received: from mx.example.org")
    assert "Subject: [PATCH] foo" in r.headers
    # the transport headers must not leak into the rendered body
    assert "Received:" not in r.body_html
    assert "ESMTPS" not in r.body_html
    # the content (commit log + diff) is what remains
    assert "Make foo do the thing." in r.body_html


def test_clean_body_keeps_everything_as_content():
    # a reply with no transport-header block: nothing stripped
    body = "On Mon, 8 Mar 2026, Dev wrote:\n\n> quoted\n\nmy reply"
    r = patchview.render(body, is_patch=False)
    assert r.headers == ""
    assert "my reply" in r.body_html


def test_empty_body():
    r = patchview.render("", is_patch=True)
    assert r == patchview.Rendered("", "")


# --- diff line classification ----------------------------------------------

def test_additions_and_deletions_coloured():
    spans = dict_of(_spans(patchview.render(RAW_PATCH, is_patch=True).body_html))
    assert spans["+new line"] == "add"
    assert spans["-old line"] == "del"
    assert spans[" context line"] == "ctx"


def test_file_markers_not_mistaken_for_add_del():
    spans = dict_of(_spans(patchview.render(RAW_PATCH, is_patch=True).body_html))
    # --- a/… and +++ b/… are file headers, NOT a deletion/addition
    assert spans["--- a/drivers/foo.c"] == "file"
    assert spans["+++ b/drivers/foo.c"] == "file"
    assert spans["diff --git a/drivers/foo.c b/drivers/foo.c"] == "file"
    assert spans["index 000000000000..ea093b71d269 100644"] == "file"
    assert spans["@@ -1,3 +1,3 @@"] == "hunk"


def test_dev_null_marker_is_file_not_deletion():
    body = ("X\n\ndiff --git a/f b/f\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/f\n@@ -0,0 +1 @@\n+hi\n")
    spans = dict_of(_spans(patchview.render(body, is_patch=True).body_html))
    assert spans["--- /dev/null"] == "file"
    assert spans["+hi"] == "add"


def test_added_yaml_doc_marker_is_addition():
    # "+---" is a `+` content line (a YAML --- in added text), not a marker
    spans = dict_of(_spans(patchview.render(RAW_PATCH, is_patch=True).body_html))
    assert spans["+---"] == "add"


def test_bare_separator_is_meta_not_deletion():
    spans = _spans(patchview.render(RAW_PATCH, is_patch=True).body_html)
    # the lone "---" format-patch separator (escaped text is exactly ---)
    assert ("meta", "---") in spans
    assert ("del", "---") not in spans


def test_diffstat_lines_are_meta_not_additions():
    spans = dict_of(_spans(patchview.render(RAW_PATCH, is_patch=True).body_html))
    assert spans[" drivers/foo.c | 3 ++-"] == "meta"
    assert spans[" 1 file changed, 2 insertions(+), 1 deletion(-)"] == "meta"


def test_no_newline_marker_is_meta():
    spans = dict_of(_spans(patchview.render(RAW_PATCH, is_patch=True).body_html))
    assert spans["\\ No newline at end of file"] == "meta"


# The git/email signature trailer ("-- " then the version) sits past the
# hunk body, so it must not be coloured as a deletion.
RAW_SIG = """\
hdr: x

Subject

---
 f | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)

diff --git a/f b/f
index 1111111..2222222 100644
--- a/f
+++ b/f
@@ -1,2 +1,2 @@
 keep
-gone
+added
--
2.43.0
"""


def test_signature_separator_not_deletion():
    spans = _spans(patchview.render(RAW_SIG, is_patch=True).body_html)
    d = dict_of(spans)
    assert d["-gone"] == "del"          # the real deletion is still red
    assert d["+added"] == "add"
    assert d["--"] == "meta"            # the signature is not
    assert d["2.43.0"] == "meta"
    assert ("del", "--") not in spans


def test_real_in_hunk_dashdash_stays_deletion():
    # deleting a line whose content is "- " (diff line "-- ") inside the
    # hunk body must remain a deletion — the budget says we're mid-hunk
    body = ("hdr: x\n\nS\n\ndiff --git a/f b/f\nindex 1..2 100644\n"
            "--- a/f\n+++ b/f\n@@ -1,2 +1,1 @@\n keep\n-- \n")
    d = dict_of(_spans(patchview.render(body, is_patch=True).body_html))
    assert d["-- "] == "del"


# --- escaping & prose ------------------------------------------------------

def test_untrusted_body_is_escaped():
    body = "hdr: x\n\n+<script>alert(1)</script>\ndiff --git a/x b/x\n"
    r = patchview.render(body, is_patch=True)
    assert "<script>" not in r.body_html
    assert "&lt;script&gt;" in r.body_html


def test_prose_mode_does_not_colour_plus_minus():
    # a reply that happens to start lines with +/- (e.g. a list) must not
    # be rendered as a diff
    body = "hdr: x\n\n- a bullet\n+ another\n> quoted reply"
    spans = dict_of(_spans(patchview.render(body, is_patch=False).body_html))
    assert spans["- a bullet"] == "plain"
    assert spans["+ another"] == "plain"
    assert spans["&gt; quoted reply"] == "quote"


def test_prose_mode_classifies_authored_vs_context_lines():
    """The comment-thread highlight (app.css .msg-comment) washes only
       pl-plain: the author's own words. Quotes, blank separators and
       the wrote:-attribution — single-line OR hard-wrapped, even
       mid-word in "wrote:" — must all classify as context."""
    body = ("hdr: x\n\n"
            # raw quoted-printable: "=" is a soft line break, splitting
            # "wrote:" mid-word across the wrap
            "On Fri, May 22, 2026 at 8:59 AM Nico Pache <n@r.com> wrot=\n"
            "e:\n"
            "> the quoted patch line\n"
            "\n"
            "On Sat, Vlastimil Babka (SUSE)\n"
            "wrote:\n"
            "Ann Author wrote:\n"
            "I think this leaks the mTHP refcount.\n"
            "\n"
            # a QP-soft-wrapped QUOTE: the continuation has no ">" but
            # must stay quote-classed, not read as authored text
            "> if a mTHP collapse is attempted, we don't perform the=\n"
            "collapse at all.\n")
    spans = dict_of(_spans(patchview.render(body, is_patch=False).body_html))
    assert spans["I think this leaks the mTHP refcount."] == "plain"
    assert spans["&gt; the quoted patch line"] == "quote"
    assert spans["On Fri, May 22, 2026 at 8:59 AM Nico Pache &lt;n@r.com&gt; wrot="] == "quote"
    assert spans["e:"] == "quote"
    assert spans["On Sat, Vlastimil Babka (SUSE)"] == "quote"
    assert spans["wrote:"] == "quote"
    assert spans["Ann Author wrote:"] == "quote"
    assert spans["&gt; if a mTHP collapse is attempted, we don&#x27;t perform the="] == "quote"
    assert spans["collapse at all."] == "quote"   # soft-wrap continuation
    assert spans["​"] == "blank"          # the blank separator line


def dict_of(pairs):
    """{text: cls} from (cls, text) span pairs; last write wins."""
    return {text: cls for cls, text in pairs}
