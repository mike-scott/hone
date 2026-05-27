"""Tests for core/methodology_format.py — the methodology
canonicalization pass applied at every write boundary (import,
proposal accept, defensive on export)."""
import os

import pytest
import yaml

from core.methodology_format import (PROSE_WRAP_COLUMN,
                                       normalize_methodology)


_DEFAULT_METHODOLOGY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "core", "default-methodology.yaml")


@pytest.fixture(scope="module")
def default_doc():
    """The packaged default methodology, loaded once per module — every
       test that wants a realistic input shares this fixture."""
    with open(_DEFAULT_METHODOLOGY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# --- shape preservation ----------------------------------------------------

def test_normalize_returns_a_new_object_does_not_mutate(default_doc):
    """normalize_methodology must not mutate its input — caller
       semantics expect a returned new dict that can be compared
       against the original (e.g. for the content-identical check
       in the import endpoint)."""
    original_principle_body = default_doc["principles"][0]["body"]
    normalize_methodology(default_doc)
    assert default_doc["principles"][0]["body"] == original_principle_body


def test_normalize_leaves_single_line_strings_untouched():
    """Ids, titles, tags, addresses, and other single-line fields
       have no Markdown semantics; the normalizer leaves them alone.
       Reflowing a single-line string would still be safe but adds
       no value and risks corner-case surprises (e.g., mdformat
       inserting a trailing newline that would then have to be
       stripped on storage)."""
    doc = {"name":    "hone default methodology",
            "id":      "p-1",
            "title":   "Establish the current date",
            "version": 7,
            "enabled": True,
            "nothing": None}
    out = normalize_methodology(doc)
    assert out == doc


def test_normalize_leaves_non_string_leaves_alone():
    """Booleans, ints, floats, None — passed through as-is."""
    doc = {"version": 42, "enabled": True, "ratio": 0.5, "x": None}
    assert normalize_methodology(doc) == doc


def test_normalize_walks_lists_and_nested_dicts(default_doc):
    """The normalizer must reach every multi-line string regardless
       of nesting depth. principles[N].body, stages[N].body,
       checks[N].body, severity_scale.levels[N].criteria[M].
       description, operations.{prepare,review,train,draft}.
       {guidance,return} all need touching."""
    out = normalize_methodology(default_doc)
    # Sanity: keys at every nesting level are preserved.
    assert set(out.keys()) == set(default_doc.keys())
    assert len(out["principles"]) == len(default_doc["principles"])
    assert len(out["operations"]) == len(default_doc["operations"])
    # Each principle still has body content.
    for p in out["principles"]:
        assert p["body"]


# --- idempotence -----------------------------------------------------------

def test_normalize_is_idempotent_on_the_default_methodology(default_doc):
    """The load-bearing property: a second pass equals the first.
       Without this every read-through-write cycle would silently
       churn the document and pollute the audit trail in
       methodology_versions."""
    once  = normalize_methodology(default_doc)
    twice = normalize_methodology(once)
    assert once == twice


def test_normalize_is_idempotent_on_a_handcrafted_prose_field():
    """Idempotence holds for typical prose with mixed Markdown
       structure — lists, bold, inline code, paragraph breaks —
       not just the default methodology's specific content."""
    text = (
        "First paragraph with **bold** and `inline code` "
        "and a long sentence that absolutely will need reflowing.\n\n"
        "- bullet one with some text that runs long enough to wrap\n"
        "- bullet two\n"
        "  - nested bullet\n\n"
        "Second paragraph after the list, another line.\n")
    doc = {"body": text}
    once  = normalize_methodology(doc)
    twice = normalize_methodology(once)
    assert once == twice


# --- structure preservation ------------------------------------------------

def test_normalize_preserves_markdown_headers(default_doc):
    """### Section Headers inside body strings are load-bearing
       structure for Claude's understanding of the prompt. mdformat
       keeps the level (h3 stays h3) — verify against the prepare
       operation's guidance, which has the richest header set."""
    import re
    out = normalize_methodology(default_doc)
    before = re.findall(r"^(#+) (.+)$",
                         default_doc["operations"]["prepare"]["guidance"],
                         re.MULTILINE)
    after  = re.findall(r"^(#+) (.+)$",
                         out["operations"]["prepare"]["guidance"],
                         re.MULTILINE)
    assert before == after, ("Header levels or text changed during reflow")


def test_normalize_preserves_variable_tokens(default_doc):
    """`%DATE_LONG%` and `%COMPLETION_RECORD_SCHEMA_JSON%` are
       expanded at claim time by node/ai.py. mdformat must not split,
       escape, or otherwise mangle them."""
    out = normalize_methodology(default_doc)
    assert "%DATE_LONG%" in out["principles"][0]["body"]
    assert "%COMPLETION_RECORD_SCHEMA_JSON%" in (
        out["operations"]["prepare"]["return"])


def test_normalize_keeps_standalone_variable_tokens_on_their_own_line(
        default_doc):
    """The multi-line variable substituter in node/ai.py expects
       %COMPLETION_RECORD_SCHEMA_JSON% on its own line so the indent-
       aware re-substitution can pick up the indent of that line.
       mdformat must not reflow it into surrounding prose."""
    out = normalize_methodology(default_doc)
    ret = out["operations"]["prepare"]["return"]
    # The token sits on a line by itself, surrounded by newlines (or
    # by start/end of string).
    assert "\n%COMPLETION_RECORD_SCHEMA_JSON%\n" in ret \
        or ret.endswith("\n%COMPLETION_RECORD_SCHEMA_JSON%\n") \
        or ret.endswith("%COMPLETION_RECORD_SCHEMA_JSON%")


def test_normalize_preserves_inline_anti_autocorrect_literals(default_doc):
    """The anti-autocorrect example in prepare.return calls out
       `was_cc_d` literally so the model sees the underscore form.
       mdformat must not normalize backticks or swap the literal."""
    out = normalize_methodology(default_doc)
    assert "was_cc_d" in out["operations"]["prepare"]["return"]


# --- wrap-column behaviour -------------------------------------------------

def test_normalize_reflows_long_paragraphs_at_target_column():
    """A long single-line paragraph is wrapped at PROSE_WRAP_COLUMN.
       Output lines may be SHORTER (word-wrap rounds down to the
       previous word boundary) but not longer."""
    text = " ".join(["word"] * 80) + "\n"
    out = normalize_methodology({"body": text})["body"]
    for line in out.splitlines():
        # Allow lines that have no spaces to exceed the limit — a
        # single un-breakable token is fine. The methodology's prose
        # doesn't carry such tokens, but the assertion stays correct
        # for prose with URLs / message-ids too.
        if " " in line:
            assert len(line) <= PROSE_WRAP_COLUMN, (
                f"line exceeds wrap column: {line!r}")


# --- the schema still validates after normalization -----------------------

def test_normalized_default_still_validates_against_schema(default_doc):
    """A normalized document is still a valid methodology — the
       canonicalization changes prose-string content only, not the
       document's structural shape."""
    import jsonschema
    schema_path = os.path.join(os.path.dirname(__file__), "..", "common",
                                "schema", "methodology.schema.yaml")
    with open(schema_path, encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    out = normalize_methodology(default_doc)
    jsonschema.validate(out, schema, cls=jsonschema.Draft202012Validator)
