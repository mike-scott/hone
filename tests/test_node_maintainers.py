"""Tests for node/maintainers.py — the get_maintainer.pl runner +
parser that feeds prepare's deterministic phase. The pure parser is
covered exhaustively; the subprocess + fetch layers are exercised with
mocks (no perl, no network)."""
from types import SimpleNamespace

from node import maintainers


# --- parse_get_maintainer --------------------------------------------------

def test_parse_maintainer_line_with_subsystem():
    out = '"David S. Miller" <davem@davemloft.net> (maintainer:NETWORKING [GENERAL])'
    [e] = maintainers.parse_get_maintainer(out)
    assert e.name == "David S. Miller"          # surrounding quotes stripped
    assert e.email == "davem@davemloft.net"
    assert e.role == "maintainer"
    assert e.subsystem == "NETWORKING [GENERAL]"


def test_parse_reviewer_role():
    out = "Some Reviewer <rev@example.com> (reviewer:SOME SUBSYSTEM)"
    [e] = maintainers.parse_get_maintainer(out)
    assert e.role == "reviewer" and e.subsystem == "SOME SUBSYSTEM"


def test_parse_bare_list_address_with_subsystem():
    out = "netdev@vger.kernel.org (open list:NETWORKING [GENERAL])"
    [e] = maintainers.parse_get_maintainer(out)
    assert e.name is None                       # no display name
    assert e.email == "netdev@vger.kernel.org"
    assert e.role == "open list"
    assert e.subsystem == "NETWORKING [GENERAL]"


def test_parse_list_without_subsystem():
    out = "linux-kernel@vger.kernel.org (open list)"
    [e] = maintainers.parse_get_maintainer(out)
    assert e.email == "linux-kernel@vger.kernel.org"
    assert e.role == "open list" and e.subsystem is None


def test_parse_name_with_apostrophe():
    out = '"Theodore Ts\'o" <tytso@mit.edu> (maintainer:EXT4 FILE SYSTEM)'
    [e] = maintainers.parse_get_maintainer(out)
    assert e.name == "Theodore Ts'o" and e.email == "tytso@mit.edu"


def test_parse_skips_blank_and_unmatched_lines():
    out = ("\n"
           "garbage line with no rolestat\n"
           "Real Person <r@x> (maintainer:FOO)\n"
           "   \n")
    entries = maintainers.parse_get_maintainer(out)
    assert len(entries) == 1 and entries[0].email == "r@x"


def test_parse_multiple_lines_preserve_order():
    out = ("A <a@x> (maintainer:S1)\n"
           "B <b@x> (reviewer:S1)\n"
           "l@vger (open list:S1)")
    roles = [(e.email, e.role) for e in maintainers.parse_get_maintainer(out)]
    assert roles == [("a@x", "maintainer"), ("b@x", "reviewer"),
                      ("l@vger", "open list")]


# --- run_get_maintainer (subprocess mocked) --------------------------------

def _fake_run_ok(stdout="OUT", returncode=0, stderr=""):
    def _run(cmd, cwd=None, capture_output=False, text=False, timeout=None):
        return SimpleNamespace(returncode=returncode, stdout=stdout,
                                stderr=stderr)
    return _run


def test_run_writes_three_files_and_invokes_perl(monkeypatch):
    captured = {}

    def fake_run(cmd, cwd=None, capture_output=False, text=False,
                 timeout=None):
        from pathlib import Path
        p = Path(cwd)
        captured["cmd"] = cmd
        captured["maintainers"] = (p / "MAINTAINERS").read_text()
        captured["script"] = (p / "get_maintainer.pl").read_text()
        captured["patch"] = (p / "patch").read_text()
        return SimpleNamespace(returncode=0, stdout="OUT", stderr="")

    monkeypatch.setattr(maintainers.subprocess, "run", fake_run)
    assert maintainers.run_get_maintainer("M", "S", "P") == "OUT"
    assert captured["cmd"][:2] == ["perl", "get_maintainer.pl"]
    for flag in ("--no-git", "--no-tree", "--rolestats"):
        assert flag in captured["cmd"]
    assert (captured["maintainers"], captured["script"],
            captured["patch"]) == ("M", "S", "P")


def test_run_returns_none_when_perl_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("perl")
    monkeypatch.setattr(maintainers.subprocess, "run", boom)
    assert maintainers.run_get_maintainer("M", "S", "P") is None


def test_run_returns_none_on_timeout(monkeypatch):
    import subprocess as sp

    def boom(*a, **k):
        raise sp.TimeoutExpired("perl", 30)
    monkeypatch.setattr(maintainers.subprocess, "run", boom)
    assert maintainers.run_get_maintainer("M", "S", "P") is None


def test_run_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(maintainers.subprocess, "run",
                         _fake_run_ok(stdout="", returncode=2,
                                       stderr="not a kernel tree"))
    assert maintainers.run_get_maintainer("M", "S", "P") is None


# --- resolve_maintainers (fetch + run + parse) -----------------------------

class _FakeClient:
    """Stand-in for CgitClient.fetch_file_at — returns canned blobs by
       path, or None to simulate a fetch miss."""

    def __init__(self, files):
        self._files = files          # {path: text-or-None}

    def fetch_file_at(self, path, sha):
        return self._files.get(path)


def test_resolve_fetches_both_blobs_runs_and_parses(monkeypatch):
    client = _FakeClient({
        "MAINTAINERS": "…maintainers text…",
        "scripts/get_maintainer.pl": "…script…"})
    monkeypatch.setattr(
        maintainers, "run_get_maintainer",
        lambda m, s, p, timeout=30: "A <a@x> (maintainer:FOO)")
    entries = maintainers.resolve_maintainers(client, "deadbeef", "PATCH")
    assert [e.email for e in entries] == ["a@x"]


def test_resolve_none_when_maintainers_fetch_misses(monkeypatch):
    client = _FakeClient({"MAINTAINERS": None,
                           "scripts/get_maintainer.pl": "s"})
    # run should never be called — assert by making it explode if it is.
    monkeypatch.setattr(maintainers, "run_get_maintainer",
                         lambda *a, **k: pytest_fail())
    assert maintainers.resolve_maintainers(client, "sha", "P") is None


def test_resolve_none_when_script_fetch_misses():
    client = _FakeClient({"MAINTAINERS": "m",
                           "scripts/get_maintainer.pl": None})
    assert maintainers.resolve_maintainers(client, "sha", "P") is None


def test_resolve_none_when_run_fails(monkeypatch):
    client = _FakeClient({"MAINTAINERS": "m",
                           "scripts/get_maintainer.pl": "s"})
    monkeypatch.setattr(maintainers, "run_get_maintainer",
                         lambda *a, **k: None)
    assert maintainers.resolve_maintainers(client, "sha", "P") is None


def pytest_fail():
    raise AssertionError("run_get_maintainer should not be called")
