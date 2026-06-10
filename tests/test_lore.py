"""Unit tests for the `lore` gather module — Subject classification, raw
message parsing, In-Reply-To threading, the base-commit trailer, and the
operator-provisioning helper (`Lore.clone`). The full git-archive walk is
exercised by smoke once the operator has a public-inbox clone."""
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core import core_db, gather                       # noqa: F401 — gather puts core/gather-modules on sys.path
import lore                                            # noqa: E402

GatherState = gather.gather_api.GatherState


# --- classify --------------------------------------------------------------

@pytest.mark.parametrize("subject,expected", [
    ("[PATCH] foo: bar",            (lore._TYPE_PATCH,   None)),
    ("[PATCH 0/3] foo: series",     (lore._TYPE_COVER,   0)),
    ("[PATCH 1/3] foo: a",          (lore._TYPE_PATCH,   1)),
    ("[PATCH 3/3] foo: c",          (lore._TYPE_PATCH,   3)),
    ("[RFC PATCH v2 1/4] foo: a",   (lore._TYPE_PATCH,   1)),
    ("[PATCH v3] foo: bar",         (lore._TYPE_PATCH,   None)),
    ("Re: [PATCH 1/3] foo: a",      (lore._TYPE_COMMENT, None)),
    ("Re: [PATCH] foo: bar",        (lore._TYPE_COMMENT, None)),
    ("RE: [PATCH] foo: bar",        (lore._TYPE_COMMENT, None)),
    ("kernel question — no patch",  (lore._TYPE_COMMENT, None)),
    ("",                            (lore._TYPE_COMMENT, None)),
])
def test_classify(subject, expected):
    assert lore.classify(subject) == expected


def test_series_total():
    assert lore._series_total("[PATCH 1/7] foo") == 7
    assert lore._series_total("[PATCH 0/3] cover") == 3
    assert lore._series_total("[PATCH] single") is None


# --- base-commit -----------------------------------------------------------

def test_extract_base_from_a_trailer():
    raw = (b"Subject: [PATCH] foo\n\n"
           b"diff --git ...\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
           b"\n--\n2.40.0\n\nbase-commit: 0123456789abcdef0123456789abcdef01234567\n")
    assert lore._extract_base(raw) == "0123456789abcdef0123456789abcdef01234567"


def test_extract_base_returns_none_when_absent():
    assert lore._extract_base(b"Subject: foo\n\nno trailer\n") is None


# --- parse_message ---------------------------------------------------------

def _mk(headers, body=""):
    raw = "".join(f"{k}: {v}\n" for k, v in headers.items()) + "\n" + body
    return raw.encode("utf-8")


def test_parse_message_extracts_headers_and_list_tags():
    raw = _mk({
        "Message-ID":  "<m1@example.com>",
        "Subject":     "[PATCH 1/3] foo: bar",
        "From":        "Alice <alice@example.com>",
        "Date":        "Wed, 20 May 2026 12:34:56 +0000",
        "In-Reply-To": "<cover@example.com>",
        "References":  "<cover@example.com> <prev@example.com>",
        "List-Id":     "<linux-arm-msm.vger.kernel.org>",
    })
    msg = lore.parse_message(raw)
    assert msg["message_id"]   == "m1@example.com"
    assert msg["subject"]      == "[PATCH 1/3] foo: bar"
    assert msg["author_name"]  == "Alice"
    assert msg["author_email"] == "alice@example.com"
    assert msg["in_reply_to"]  == "cover@example.com"
    assert msg["references"]   == ["cover@example.com", "prev@example.com"]
    assert msg["list_tags"]    == ["linux-arm-msm.vger.kernel.org"]
    assert msg["date_ok"] is True and msg["sent"] is not None


def test_parse_message_aggregates_multiple_list_ids():
    raw = _mk({
        "Message-ID": "<m@x>",
        "Subject":    "[PATCH] foo",
        "From":       "A <a@x>",
        "Date":       "Wed, 20 May 2026 12:34:56 +0000",
        "List-Id":    "<a.kernel.org>",
    })
    # email.message lookups treat List-Id as repeated.
    raw = raw.replace(b"List-Id: <a.kernel.org>\n",
                      b"List-Id: <a.kernel.org>\nList-Id: <b.kernel.org>\n")
    msg = lore.parse_message(raw)
    assert sorted(msg["list_tags"]) == ["a.kernel.org", "b.kernel.org"]


def test_parse_message_handles_an_unresolvable_date():
    raw = _mk({
        "Message-ID": "<m@x>",
        "Subject":    "[PATCH] foo",
        "From":       "A <a@x>",
        "Date":       "garbage",
    })
    msg = lore.parse_message(raw)
    assert msg["date_ok"] is False and msg["sent"] is None


def test_parse_message_returns_none_without_a_message_id():
    raw = _mk({"Subject": "no id", "From": "A <a@x>"})
    assert lore.parse_message(raw) is None


# --- resolve_root (thread resolution) -------------------------------------

@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def _msg(message_id, *, in_reply_to=None, references=()):
    return {"message_id": message_id, "in_reply_to": in_reply_to,
            "references": list(references)}


def test_resolve_root_uses_the_in_cycle_cache_first(db):
    cache = {"cover@x": "cover@x"}
    parent, root = lore.resolve_root(
        db, _msg("p1@x", in_reply_to="cover@x"), cache)
    assert (parent, root) == ("cover@x", "cover@x")


def test_resolve_root_falls_back_to_the_corpus(db):
    # an earlier-cycle patch is in the corpus; the new message has no cache
    core_db.upsert_patchset(db, "cover@x", subject="series", n_patches=2)
    core_db.upsert_message(db, "cover@x", root_message_id="cover@x",
                            type=core_db.MSG_TYPE_COVER, part_index=0,
                            body="cover")
    parent, root = lore.resolve_root(
        db, _msg("late-reply@x", in_reply_to="cover@x"), {})
    assert (parent, root) == ("cover@x", "cover@x")


def test_resolve_root_walks_references_when_in_reply_to_is_missing(db):
    cache = {"cover@x": "cover@x"}
    msg = _msg("reply@x", in_reply_to=None,
               references=("unknown@x", "cover@x", "intermediate@x"))
    parent, root = lore.resolve_root(db, msg, cache)
    # references walked newest-first; "intermediate@x" is unknown, then
    # "cover@x" matches the cache.
    assert (parent, root) == ("cover@x", "cover@x")


def test_resolve_root_series_patch_roots_at_its_cover_when_unseen(db):
    """A series patch ([PATCH N/M]) whose cover hasn't been seen yet roots at
       the cover it names (oldest Reference / In-Reply-To), not at itself —
       so grouping doesn't depend on the cover being processed first."""
    # cover unknown (empty cache, empty corpus); only the patch's own headers
    parent, root = lore.resolve_root(
        db, _msg("patch-2@x", in_reply_to="cover@x", references=("cover@x",)),
        {}, series_patch=True)
    assert parent is None
    assert root == "cover@x"


def test_resolve_root_non_series_still_self_roots_when_unseen(db):
    """A standalone patch (no N/M) with an unknown parent still starts its own
       thread — the cover fallback is gated to numbered series patches."""
    parent, root = lore.resolve_root(
        db, _msg("standalone@x", in_reply_to="some-thread@x"), {},
        series_patch=False)
    assert root == "standalone@x"


def test_resolve_root_returns_self_when_no_parent_known(db):
    parent, root = lore.resolve_root(db, _msg("new@x"), {})
    assert (parent, root) == (None, "new@x")


# --- missing-archive resilience -------------------------------------------

def test_list_is_a_clean_no_op_when_the_archive_is_missing(
        db, tmp_path, monkeypatch, caplog):
    """A first-run hone-core without the lore mirror cloned in yet must NOT
    bring the service down. `Lore.list()` is a generator: with no archive it
    logs a warning and yields nothing, the gather cycle ends with an empty
    tally, and the next tick picks up the moment the operator clones it in."""
    monkeypatch.setattr(lore, "ARCHIVE", str(tmp_path / "not-cloned-yet"))
    monkeypatch.setattr(lore, "configured_lists", lambda: ())   # single-list
    with caplog.at_level("WARNING", logger="hone.gather.lore"):
        refs = list(lore.Lore().list(db=db))
    assert refs == []
    assert any("archive missing" in r.message for r in caplog.records)


# --- since_date floor + the clone helper ----------------------------------

def test_since_date_is_the_documented_floor():
    """lore's cold-start floor — the `--shallow-since` boundary the
    bundled clone helper uses, and the date below which `list()`
    refuses to walk."""
    assert lore.Lore.since_date == "2026-03-08"


class _FakePopen:
    """A `subprocess.Popen`-shaped stand-in: records the cmd, presents an
       empty stderr (so `_stream_progress` returns immediately), and reports
       a clean exit. Tests override the class-level `returncode` to simulate
       a git failure."""

    captured_cmd = None
    returncode_value = 0

    def __init__(self, cmd, stdout=None, stderr=None, bufsize=None):
        type(self).captured_cmd = cmd
        self.stderr = io.BytesIO(b"")
        self.returncode = self.returncode_value

    def wait(self):
        return self.returncode_value


def test_clone_builds_a_shallow_partial_command(tmp_path, monkeypatch):
    """The helper composes the right git invocation: --filter=blob:none,
    --shallow-since=<since_date>, --no-tags, --single-branch, --progress
    (so non-TTY stderr still emits), with URL + target from defaults."""
    monkeypatch.setattr(lore.subprocess, "Popen", _FakePopen)
    monkeypatch.setenv("HONE_LORE_URL", "https://lore.kernel.org/netdev/3")
    _FakePopen.captured_cmd = None
    target = str(tmp_path / "lore")
    assert lore.Lore.clone(target) is True
    cmd = _FakePopen.captured_cmd
    assert cmd[:2] == ["git", "clone"]
    assert "--progress" in cmd
    assert "--filter=blob:none" in cmd
    assert f"--shallow-since={lore.Lore.since_date}" in cmd
    assert "--no-tags" in cmd and "--single-branch" in cmd
    assert cmd[-2:] == ["https://lore.kernel.org/netdev/3", target]


def test_clone_requires_a_url(tmp_path, monkeypatch):
    """With neither a url arg nor $HONE_LORE_URL there's no built-in
       default — clone raises rather than fetch the un-cloneable /all/0."""
    monkeypatch.delenv("HONE_LORE_URL", raising=False)
    monkeypatch.delenv("HONE_LORE_LISTS", raising=False)
    with pytest.raises(ValueError, match="HONE_LORE"):
        lore.Lore.clone(str(tmp_path / "lore"))


def test_clone_is_a_no_op_when_target_already_a_repo(tmp_path, monkeypatch):
    """Rerunning the helper after the archive is in place doesn't re-clone."""
    target = tmp_path / "lore"
    (target / ".git").mkdir(parents=True)
    called = {"n": 0}

    class _CountedPopen(_FakePopen):
        def __init__(self, *a, **kw):
            called["n"] += 1
            super().__init__(*a, **kw)

    monkeypatch.setattr(lore.subprocess, "Popen", _CountedPopen)
    assert lore.Lore.clone(str(target)) is False
    assert called["n"] == 0


def test_clone_honors_HONE_LORE_URL_override(tmp_path, monkeypatch):
    """A deployment that points at a single list (or a private mirror)
    sets HONE_LORE_URL — the helper picks it up."""
    monkeypatch.setattr(lore.subprocess, "Popen", _FakePopen)
    monkeypatch.setenv("HONE_LORE_URL",
                       "https://lore.kernel.org/linux-arm-msm/0")
    _FakePopen.captured_cmd = None
    lore.Lore.clone(str(tmp_path / "lore"))
    assert "https://lore.kernel.org/linux-arm-msm/0" in _FakePopen.captured_cmd


def test_clone_raises_on_git_failure(tmp_path, monkeypatch):
    """A non-zero exit from git surfaces as CalledProcessError."""
    class _Failing(_FakePopen):
        returncode_value = 128
    monkeypatch.setattr(lore.subprocess, "Popen", _Failing)
    monkeypatch.setenv("HONE_LORE_URL", "https://lore.kernel.org/netdev/3")
    with pytest.raises(subprocess.CalledProcessError):
        lore.Lore.clone(str(tmp_path / "lore"))


# --- per-cycle patchset cap + cold-start boundary skip ---------------------

def _mbytes(message_id, subject, *, in_reply_to=None, references=None):
    """A minimal RFC-822 message that `parse_message` accepts. Synthetic;
       used to drive `Lore.list()` without a real git archive."""
    h = [f"Message-Id: <{message_id}>",
         f"Subject: {subject}",
         "From: tester <t@example.com>",
         "Date: Sun, 23 May 2026 12:00:00 +0000",
         "List-Id: <linux-kernel.vger.kernel.org>"]
    if in_reply_to:
        h.append(f"In-Reply-To: <{in_reply_to}>")
    if references:
        h.append("References: " + " ".join(f"<{r}>" for r in references))
    return ("\n".join(h) + "\n\nbody\n").encode()


def _drive(monkeypatch, tmp_path, sha_to_msg, *, cursor=None):
    """Drive `Lore.list()` against the given `{sha: msg_bytes}` map by
       monkeypatching `_new_commits` and `_blob`. Points ARCHIVE at an
       existing tmp dir so `list()`'s missing-archive guard passes.
       Returns the yielded refs."""
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    monkeypatch.setattr(lore, "ARCHIVE", str(archive))
    monkeypatch.setattr(lore, "configured_lists", lambda: ())   # single-list
    shas = list(sha_to_msg.keys())
    monkeypatch.setattr(lore.Lore, "_new_commits",
                        lambda self, _cursor, _archive=None: shas)
    monkeypatch.setattr(lore.Lore, "_blob",
                        lambda self, sha, _archive=None: sha_to_msg.get(sha))
    state = GatherState(cursor=cursor) if cursor else None
    return list(lore.Lore().list(state=state))


def _patchset(prefix, n):
    """A cover + n patches as an oldest-first {sha: bytes} dict."""
    cover_id = f"cover-{prefix}@x"
    refs = [(f"sha-{prefix}-0", _mbytes(cover_id, f"[PATCH 0/{n}] {prefix}"))]
    for i in range(1, n + 1):
        refs.append((f"sha-{prefix}-{i}",
                     _mbytes(f"patch-{prefix}-{i}@x",
                             f"[PATCH {i}/{n}] {prefix}: change {i}",
                             in_reply_to=cover_id)))
    return refs


def test_patchset_cap_stops_at_a_clean_boundary(monkeypatch, tmp_path, caplog):
    """The cap counts PATCHSETS, not commits — N full patchsets land this
    cycle, the (N+1)th waits for the next cycle. Each is whole (cover +
    every patch), never split mid-series."""
    n_patchsets = lore.MAX_PATCHSETS_PER_CYCLE + 5
    sha_msg = {}
    for i in range(n_patchsets):
        sha_msg.update(_patchset(f"p{i:03d}", n=3))         # cover + 3 patches
    refs = _drive(monkeypatch, tmp_path, sha_msg)
    patchset_refs = [r for r in refs
                     if isinstance(r, lore.PatchsetRef)]
    msg_refs = [r for r in refs if isinstance(r, lore.MessageRef)]
    assert len(patchset_refs) == lore.MAX_PATCHSETS_PER_CYCLE
    # each gathered patchset is WHOLE — cover + 3 patches = 4 messages
    assert len(msg_refs) == lore.MAX_PATCHSETS_PER_CYCLE * 4


def test_series_groups_under_cover_when_patches_precede_it(monkeypatch, tmp_path):
    """REGRESSION: when the archive's commit order delivers series patches
       before their cover, each patch used to become a standalone ghost
       patchset (root == itself). Each patch's own In-Reply-To/References name
       the cover, so grouping must be order-independent: one patchset, rooted
       at the cover, with every part folded in.

       A non-empty cursor is passed so the cold-start boundary-skip (which
       would otherwise drop leading mid-series patches) is off — this is the
       steady-state mid-stream case where the ghosts were observed."""
    cover_id = "cover-x@x"
    sha_msg = {}
    # two patches FIRST (cover not seen yet), then the cover, then a 3rd patch
    sha_msg["sha-p1"] = _mbytes("patch-x-1@x", "[PATCH 1/3] x: a",
                                in_reply_to=cover_id, references=[cover_id])
    sha_msg["sha-p2"] = _mbytes("patch-x-2@x", "[PATCH 2/3] x: b",
                                in_reply_to=cover_id, references=[cover_id])
    sha_msg["sha-c0"] = _mbytes(cover_id, "[PATCH 0/3] x: the series")
    sha_msg["sha-p3"] = _mbytes("patch-x-3@x", "[PATCH 3/3] x: c",
                                in_reply_to=cover_id, references=[cover_id])
    refs = _drive(monkeypatch, tmp_path, sha_msg, cursor="prev-sha")
    psets = [r for r in refs if isinstance(r, lore.PatchsetRef)]
    msgs = [r for r in refs if isinstance(r, lore.MessageRef)]
    # exactly ONE patchset (no ghosts), rooted at the cover
    assert {r.root_message_id for r in psets} == {cover_id}
    # all four messages grouped under the cover
    assert len(msgs) == 4
    assert all(m.root_message_id == cover_id for m in msgs)
    # and the cover's PatchsetRef refreshed the name (not a patch subject)
    cover_ref = [r for r in psets if r.subject.startswith("[PATCH 0/3]")]
    assert cover_ref and cover_ref[-1].n_patches == 3


def test_patchset_cap_log_fires_when_capping(monkeypatch, tmp_path, caplog):
    n_patchsets = lore.MAX_PATCHSETS_PER_CYCLE + 2
    sha_msg = {}
    for i in range(n_patchsets):
        sha_msg.update(_patchset(f"p{i:03d}", n=2))
    with caplog.at_level("INFO", logger="hone.gather.lore"):
        _drive(monkeypatch, tmp_path, sha_msg)
    assert any(f"gathered {lore.MAX_PATCHSETS_PER_CYCLE} patchsets" in r.message
               for r in caplog.records)


def test_under_cap_does_not_log(monkeypatch, tmp_path, caplog):
    sha_msg = {}
    for i in range(3):
        sha_msg.update(_patchset(f"p{i}", n=2))
    with caplog.at_level("INFO", logger="hone.gather.lore"):
        _drive(monkeypatch, tmp_path, sha_msg)
    assert not any("gathered " in r.message and " patchsets" in r.message
                   for r in caplog.records)


def test_cold_start_skips_mid_series_leading_commits(monkeypatch, tmp_path,
                                                       caplog):
    """The since_date floor doesn't always land on a clean patchset
    boundary — the cover may predate the floor, leaving patches 3..7 of
    a series as the first commits in the slice. Without the boundary
    skip, each of those would become a wrong-rooted ghost patchset.
    With it, we skip forward to the first real cover and the cycle
    starts clean."""
    sha_msg = {}
    # leading: patches 3..7 of a series whose cover is OUT OF SLICE.
    # (their In-Reply-To points at the missing cover; resolve_root can't
    #  find it, so each would fall back to its own message_id as root.)
    for i in range(3, 8):
        sha_msg[f"sha-stray-{i}"] = _mbytes(
            f"patch-stray-{i}@x",
            f"[PATCH {i}/7] stray: continuation {i}",
            in_reply_to="cover-stray@x")        # cover not in slice
    # then a real patchset
    sha_msg.update(_patchset("real", n=2))
    with caplog.at_level("INFO", logger="hone.gather.lore"):
        refs = _drive(monkeypatch, tmp_path, sha_msg)
    # exactly ONE patchset emitted (the real one) — none of the strays
    patchset_refs = [r for r in refs if isinstance(r, lore.PatchsetRef)]
    assert len(patchset_refs) == 1
    assert patchset_refs[0].root_message_id == "cover-real@x"
    assert patchset_refs[0].subject.startswith("[PATCH 0/2]")
    assert any("skipped 5 leading commits" in r.message
               for r in caplog.records)


def test_cursor_present_does_not_skip_boundary(monkeypatch, tmp_path):
    """When a cursor is set we are mid-archive, not cold-starting — a
    comment or a non-cover patch at the head of the slice is a legitimate
    continuation of an already-gathered patchset, not a stray."""
    sha_msg = {}
    # cover predates the cursor (not in this slice); the first commit is
    # a follow-up patch that threads to the cover already in the corpus.
    # the cache won't know about the cover but resolve_root falls back to
    # self — for the test, that's fine because we ingest the message
    # anyway (the framework + cross-source-defer caller is what protects
    # against orphan-on-missing-parent, not the boundary skip).
    sha_msg["sha-followup"] = _mbytes(
        "followup@x", "[PATCH 2/2] series: more",
        in_reply_to="prior-cover@x")
    refs = _drive(monkeypatch, tmp_path, sha_msg, cursor="prev-sha")
    # the followup IS yielded (not skipped) because cursor is set
    msg_refs = [r for r in refs if isinstance(r, lore.MessageRef)]
    assert len(msg_refs) == 1
    assert msg_refs[0].message_id == "followup@x"


def test_skipped_messages_are_not_cached(monkeypatch, tmp_path):
    """A non-patchset message that gets boundary-skipped (or a comment that
    gets orphan-skipped inside _build_refs) must NOT land in the in-cycle
    thread cache. If it did, a later reply would find it in the cache,
    emit a MessageRef with parent=that-id, and the framework's
    upsert_message would trip the FK on messages.parent_message_id
    REFERENCES messages(message_id) — the actual production crash this
    test guards against.

    Concretely: a Thorsten-style discussion-thread root in the slice
    ahead of the first cover (e.g. "RFC: should we…" from
    6e4d21f9-…@leemhuis.info) was getting cached as its own root even
    though no row was written; a comment later in the same cycle replying
    to it would emit `parent=root=6e4d21f9-…` and INSERT would FK-fail."""
    sha_msg = {}
    # leading: a non-patchset discussion-thread root that boundary-skip
    # drops (it's not a cover, not a standalone [PATCH]).
    sha_msg["sha-rfc"] = _mbytes(
        "6e4d21f9-87cb-44ea-bb04-eb4f047f3ff5@leemhuis.info",
        "RFC: should we change something")
    # then a real patchset boundary so the cycle starts emitting
    sha_msg.update(_patchset("real", n=1))
    # then a comment replying to the boundary-skipped RFC. Pre-fix, this
    # ref would emit with parent=6e4d21f9-… (found in the lying cache).
    # Post-fix, the cache miss means resolve_root falls back to db (also
    # a miss), parent stays None, and _build_refs orphan-skips it.
    sha_msg["sha-reply"] = _mbytes(
        "aa6klwnxocivqubt@fue-alewi-winx",
        "Re: RFC: should we change something",
        in_reply_to="6e4d21f9-87cb-44ea-bb04-eb4f047f3ff5@leemhuis.info")
    refs = _drive(monkeypatch, tmp_path, sha_msg)
    # only the real patchset's refs make it through
    patchset_refs = [r for r in refs if isinstance(r, lore.PatchsetRef)]
    msg_refs = [r for r in refs if isinstance(r, lore.MessageRef)]
    assert len(patchset_refs) == 1
    assert patchset_refs[0].root_message_id == "cover-real@x"
    # the orphan reply did NOT make it (its parent wasn't in the corpus
    # or the cache, so _build_refs returned without yielding)
    assert not any(r.message_id == "aa6klwnxocivqubt@fue-alewi-winx"
                   for r in msg_refs)


def test_cold_start_boundary_skip_gives_up_after_safety_limit(monkeypatch,
                                                                tmp_path,
                                                                caplog):
    """If a misaligned start date never finds a cover within the safety
    limit, we accept the next commit as a best-effort boundary and emit
    a warning rather than spin forever."""
    monkeypatch.setattr(lore, "_MAX_BOUNDARY_SKIP", 4)        # tiny for test
    sha_msg = {}
    # 4 strays — no cover in the whole slice
    for i in range(3, 7):
        sha_msg[f"sha-stray-{i}"] = _mbytes(
            f"patch-stray-{i}@x",
            f"[PATCH {i}/7] stray: continuation {i}",
            in_reply_to="cover-stray@x")
    # then more strays the safety-give-up will start emitting from
    sha_msg["sha-after"] = _mbytes(
        "after@x", "[PATCH 5/7] stray: continuation 5",
        in_reply_to="cover-stray@x")
    with caplog.at_level("WARNING", logger="hone.gather.lore"):
        _drive(monkeypatch, tmp_path, sha_msg)
    assert any("boundary not found in 4 commits" in r.message
               for r in caplog.records)


def test_clone_progress_callback_receives_parsed_updates(tmp_path, monkeypatch):
    """The progress callback fires with (phase, percent, line) per git
    progress line — what the autoclone path feeds into app.state.lore_clone
    for the Settings panel."""
    git_output = (
        b"Cloning into '/data/archive/lore'...\n"
        b"remote: Enumerating objects: 152318, done.\n"
        b"remote: Counting objects: 100% (152318/152318), done.\n"
        b"Receiving objects:  47% (71590/152318), 273.41 MiB | 8.43 MiB/s\r"
        b"Receiving objects:  73% (111291/152318), 423.51 MiB | 9.10 MiB/s\r"
        b"Receiving objects: 100% (152318/152318), 587.20 MiB | 8.98 MiB/s\n"
        b"Resolving deltas: 100% (146790/146790), done.\n")

    class _GitProgress(_FakePopen):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.stderr = io.BytesIO(git_output)

    monkeypatch.setattr(lore.subprocess, "Popen", _GitProgress)
    monkeypatch.setenv("HONE_LORE_URL", "https://lore.kernel.org/netdev/3")
    updates = []
    lore.Lore.clone(str(tmp_path / "lore"),
                     progress=lambda p, pct, line: updates.append((p, pct)))
    assert ("Counting objects", 100) in updates
    assert ("Receiving objects", 47) in updates
    assert ("Receiving objects", 100) in updates
    assert ("Resolving deltas", 100) in updates


# --- multi-list mode ($HONE_LORE_LISTS) ------------------------------------

def test_configured_lists_parses_env(monkeypatch):
    monkeypatch.setenv("HONE_LORE_LISTS", " netdev, linux-mm ,dri-devel ")
    monkeypatch.delenv("HONE_LORE_URL", raising=False)
    assert lore.configured_lists() == ("netdev", "linux-mm", "dri-devel")


def test_configured_lists_precedence(monkeypatch):
    """Unset → the wide default; a HONE_LORE_URL override → () (single-list);
       an explicit HONE_LORE_LISTS wins over both."""
    monkeypatch.delenv("HONE_LORE_LISTS", raising=False)
    monkeypatch.delenv("HONE_LORE_URL", raising=False)
    assert lore.configured_lists() == lore.DEFAULT_LISTS    # default

    monkeypatch.setenv("HONE_LORE_URL", "https://lore.kernel.org/netdev/3")
    assert lore.configured_lists() == ()                    # URL → single-list

    monkeypatch.setenv("HONE_LORE_LISTS", "linux-mm")
    assert lore.configured_lists() == ("linux-mm",)         # LISTS wins


def _fake_lsremote(present):
    """A subprocess.run stand-in for `git ls-remote <base>/<list>/<n>` that
       reports the epochs in `present` as existing (rc 0 + a ref)."""
    def run(cmd, capture_output=True, timeout=None, **kw):
        n = int(cmd[-1].rsplit("/", 1)[1])
        ok = n in present
        return SimpleNamespace(
            returncode=0 if ok else 2,
            stdout=b"deadbeef\trefs/heads/master\n" if ok else b"")
    return run


def test_current_epoch_takes_top_of_present_run(monkeypatch):
    monkeypatch.setattr(lore.subprocess, "run", _fake_lsremote({0, 1, 2, 3}))
    assert lore.current_epoch("netdev", max_probe=8) == 3


def test_current_epoch_handles_a_pruned_prefix(monkeypatch):
    # big lists prune low epochs: 0-2 gone, 3-5 live
    monkeypatch.setattr(lore.subprocess, "run", _fake_lsremote({3, 4, 5}))
    assert lore.current_epoch("linux-kernel", max_probe=8) == 5


def test_current_epoch_none_when_not_cloneable(monkeypatch):
    monkeypatch.setattr(lore.subprocess, "run", _fake_lsremote(set()))
    assert lore.current_epoch("all", max_probe=8) is None


def test_current_epoch_treats_a_hang_as_absent(monkeypatch):
    """A throttling/slow lore (probe times out) degrades to no-epoch, not
       a raised exception that would abort the whole provisioning pass."""
    def run(cmd, capture_output=True, timeout=None, **kw):
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(lore.subprocess, "run", run)
    assert lore.current_epoch("netdev", max_probe=4) is None


def test_multi_list_walks_each_archive_with_a_map_cursor(monkeypatch):
    """Each configured list is walked from its own sub-cursor, and every
       emitted ref carries the full {list: sha} map so the cycle resumes
       each list correctly."""
    monkeypatch.setenv("HONE_LORE_LISTS", "a,b")
    monkeypatch.setattr(lore.os.path, "isdir", lambda p: True)

    def fake_walk(self, archive, cursor, db, cap):
        name = os.path.basename(archive)
        yield lore.PatchsetRef(root_message_id=f"<r-{name}@x>",
                               cursor=f"sha-{name}-1")
        yield lore.MessageRef(message_id=f"m-{name}@x",
                              root_message_id=f"<r-{name}@x>",
                              type=lore._TYPE_PATCH, body="x",
                              cursor=f"sha-{name}-2")

    monkeypatch.setattr(lore.Lore, "_walk", fake_walk)
    refs = list(lore.Lore().list(state=None, db=None))
    assert len(refs) == 4                              # 2 lists x 2 refs
    assert json.loads(refs[0].cursor) == {"a": "sha-a-1"}
    assert json.loads(refs[-1].cursor) == {"a": "sha-a-2", "b": "sha-b-2"}


def test_clone_all_discovers_epoch_and_clones_each(monkeypatch, tmp_path):
    monkeypatch.setenv("HONE_LORE_LISTS", "netdev,linux-mm")
    monkeypatch.setattr(lore, "ARCHIVE", str(tmp_path / "lore"))
    monkeypatch.setattr(lore, "current_epoch",
                        lambda name, **kw: {"netdev": 3, "linux-mm": 2}[name])
    calls = []

    def fake_clone(cls, target=None, *, url=None, since_date=None,
                   progress=None):
        calls.append((target, url))
        return True

    monkeypatch.setattr(lore.Lore, "clone", classmethod(fake_clone))
    assert lore.Lore.clone_all() == 2
    assert (str(tmp_path / "lore" / "netdev"),
            "https://lore.kernel.org/netdev/3") in calls
    assert (str(tmp_path / "lore" / "linux-mm"),
            "https://lore.kernel.org/linux-mm/2") in calls


def test_clone_all_uses_default_lists_when_unconfigured(monkeypatch, tmp_path):
    """Neither HONE_LORE_LISTS nor HONE_LORE_URL → clone_all provisions the
       built-in default set (epoch + clone stubbed, so no network)."""
    monkeypatch.delenv("HONE_LORE_LISTS", raising=False)
    monkeypatch.delenv("HONE_LORE_URL", raising=False)
    monkeypatch.setattr(lore, "ARCHIVE", str(tmp_path / "lore"))
    monkeypatch.setattr(lore, "current_epoch", lambda name, **kw: 0)
    cloned = []

    def fake_clone(cls, target=None, *, url=None, since_date=None,
                   progress=None):
        cloned.append(url)
        return True

    monkeypatch.setattr(lore.Lore, "clone", classmethod(fake_clone))
    assert lore.Lore.clone_all() == len(lore.DEFAULT_LISTS)
    assert cloned == [f"https://lore.kernel.org/{n}/0"
                      for n in lore.DEFAULT_LISTS]


def test_is_provisioned_requires_every_configured_list(monkeypatch, tmp_path):
    monkeypatch.setenv("HONE_LORE_LISTS", "a,b")
    monkeypatch.setattr(lore, "ARCHIVE", str(tmp_path / "lore"))
    (tmp_path / "lore" / "a" / ".git").mkdir(parents=True)
    assert lore.Lore.is_provisioned() is False          # b not cloned yet
    (tmp_path / "lore" / "b" / ".git").mkdir(parents=True)
    assert lore.Lore.is_provisioned() is True


# --- per-cycle archive refresh ----------------------------------------------

def test_list_refreshes_the_archive_before_walking(monkeypatch, tmp_path):
    """Each gather cycle fast-forwards the clone before walking it —
       _new_commits reads `cursor..HEAD`, so a frozen clone starves
       gather the moment the backlog drains."""
    monkeypatch.setattr(lore, "ARCHIVE", str(tmp_path))
    monkeypatch.setattr(lore, "configured_lists", lambda: [])
    calls = []
    monkeypatch.setattr(
        lore.Lore, "_refresh",
        classmethod(lambda cls, a: calls.append(("refresh", a))))
    monkeypatch.setattr(
        lore.Lore, "_walk",
        lambda self, archive, cursor, db, cap:
            iter(calls.append(("walk", archive)) or ()))
    list(lore.Lore().list())
    assert calls == [("refresh", str(tmp_path)), ("walk", str(tmp_path))]


def test_list_refreshes_each_configured_list_archive(monkeypatch, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.setattr(lore, "configured_lists", lambda: ["a", "b"])
    monkeypatch.setattr(lore, "_archive_for", lambda n: str(tmp_path / n))
    refreshed = []
    monkeypatch.setattr(lore.Lore, "_refresh",
                        classmethod(lambda cls, a: refreshed.append(a)))
    monkeypatch.setattr(lore.Lore, "_walk",
                        lambda self, archive, cursor, db, cap: iter(()))
    list(lore.Lore().list())
    assert refreshed == [str(tmp_path / "a"), str(tmp_path / "b")]


def test_refresh_is_best_effort(monkeypatch, tmp_path):
    """Offline, a git error, or a timeout: the refresh logs and the
       cycle proceeds on the stale archive — it must never raise into
       the gather supervisor."""
    def failing(*a, **kw):
        class R:
            returncode, stdout, stderr = 1, b"", b"fatal: unable to access"
        return R()
    monkeypatch.setattr(lore.subprocess, "run", failing)
    lore.Lore._refresh(str(tmp_path))             # rc != 0 — no raise

    def raise_timeout(*a, **kw):
        raise lore.subprocess.TimeoutExpired(cmd="git pull", timeout=1)
    monkeypatch.setattr(lore.subprocess, "run", raise_timeout)
    lore.Lore._refresh(str(tmp_path))             # timeout — no raise

    def raise_oserror(*a, **kw):
        raise OSError("git not found")
    monkeypatch.setattr(lore.subprocess, "run", raise_oserror)
    lore.Lore._refresh(str(tmp_path))             # OSError — no raise


# --- series version ----------------------------------------------------------

@pytest.mark.parametrize("subject,expected", [
    ("[PATCH] foo: bar",              1),     # first posting, no marker
    ("[PATCH 0/3] foo: series",       1),
    ("[PATCH v2 1/4] foo: a",         2),
    ("[RFC PATCH v3 0/2] foo",        3),
    ("[v4 PATCH] foo: bar",           4),     # v before the word PATCH
    ("[PATCH net-next v12 07/15] x",  12),
    ("v2: no brackets at all",        1),     # marker must share the bracket
    ("",                              1),
])
def test_series_version(subject, expected):
    assert lore._series_version(subject) == expected


def test_patchset_ref_carries_series_version(monkeypatch, tmp_path):
    """REGRESSION: lore never parsed `[PATCH vN]`, so every gathered
       patchset landed at the dataclass default of 1 and the detail page
       showed v1 for everything."""
    sha_msg = dict(_patchset("verfix", n=2))
    sha_msg["sha-v3"] = _mbytes("cover-v3@x", "[PATCH v3 0/1] y: redo")
    refs = _drive(monkeypatch, tmp_path, sha_msg)
    by_root = {r.root_message_id: r for r in refs
               if isinstance(r, lore.PatchsetRef)}
    assert by_root["cover-verfix@x"].series_version == 1
    assert by_root["cover-v3@x"].series_version == 3
