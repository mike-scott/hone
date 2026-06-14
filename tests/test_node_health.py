"""Tests for node/health.py — the snapshot the runner sends to
hone-core's /v1/nodes/me/health endpoint. Each gather helper is
monkeypatchable independently so the suite stays hermetic (no
real `du` calls, no dependency on a real disk free value)."""
from types import SimpleNamespace

import pytest

from node import ai, health, refrepo


def _cfg(data_dir="/data", repo_dir="/data/linux", min_free_disk_mb=0):
    return SimpleNamespace(data_dir=data_dir, repo_dir=repo_dir,
                           min_free_disk_mb=min_free_disk_mb)


# --- _free_disk_mb --------------------------------------------------------

def test_free_disk_mb_returns_megabytes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        health.shutil, "disk_usage",
        lambda p: SimpleNamespace(total=0, used=0,
                                    free=5 * 1024 ** 3))   # 5 GiB
    assert health._free_disk_mb(str(tmp_path)) == 5 * 1024


def test_free_disk_mb_returns_none_for_missing_path():
    """A missing path returns None — boot sequence may run before the
       volume is mounted; the UI renders `—` rather than 0."""
    assert health._free_disk_mb("/no/such/path") is None
    assert health._free_disk_mb("") is None
    assert health._free_disk_mb(None) is None


# --- _refrepo_size_mb -----------------------------------------------------

def test_refrepo_size_mb_parses_du_output(monkeypatch, tmp_path):
    """`du -sm` returns size-in-MiB tab path on stdout. The helper
       reads the first whitespace-separated field as an int."""
    monkeypatch.setattr(
        health.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0, stdout=f"12345\t{tmp_path}\n", stderr=""))
    assert health._refrepo_size_mb(str(tmp_path)) == 12345


def test_refrepo_size_mb_none_when_du_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        health.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="",
                                          stderr="permission denied"))
    assert health._refrepo_size_mb(str(tmp_path)) is None


def test_refrepo_size_mb_none_when_path_absent():
    assert health._refrepo_size_mb("/no/such/repo") is None


# --- collect --------------------------------------------------------------

def test_collect_packages_the_snapshot_fields(monkeypatch):
    """The snapshot carries the operationally-cheap fields the operator
       UI renders. Anthropic-error category comes from
       node.ai.get_last_error (set by call_claude on failure, cleared on
       success); claude_version from node.ai.get_cli_version (cached)."""
    monkeypatch.setattr(health, "_free_disk_mb", lambda p: 1024)
    monkeypatch.setattr(health, "_refrepo_size_mb", lambda p: 4500)
    monkeypatch.setattr(ai, "_LAST_ERROR", "rate_limit")
    monkeypatch.setattr(ai, "get_cli_version", lambda: "2.1.161 (Claude Code)")
    # The refrepo instrumentation getters read process-global state other
    # tests mutate; patch them to fixed values so this exact-match stays
    # hermetic (their own behaviour is covered in test_node_refrepo).
    monkeypatch.setattr(refrepo, "tracking_ref_count", lambda: 7)
    monkeypatch.setattr(refrepo, "last_fetch_stats", lambda: None)
    monkeypatch.setattr(refrepo, "last_resolve_stats", lambda: None)
    monkeypatch.setattr(refrepo, "last_gc_stats", lambda: None)
    snap = health.collect(_cfg())
    assert snap == {
        "free_disk_mb":         1024,
        "refrepo_size_mb":      4500,
        "last_anthropic_error": "rate_limit",
        "disk_low":             False,        # 1024 MB free, guard off (floor 0)
        "claude_version":       "2.1.161 (Claude Code)",
        # The stub cfg has no token caps and no ledger on disk, so the
        # budget reads as empty/disabled — see test_node_budget for the
        # accrual/rollover behaviour behind this field.
        "token_budget":         {"day_tokens": 0, "day_limit": 0,
                                 "week_tokens": 0, "week_limit": 0,
                                 "exhausted": None},
        "refrepo_tracking_refs": 7,
        "refrepo_fetch":        None,
        "refrepo_resolve":      None,
        "refrepo_gc":           None,
    }


def test_collect_carries_a_clean_status_when_anthropic_is_happy(monkeypatch):
    """get_last_error returns None when the latest call_claude
       succeeded; the snapshot mirrors that as a None value (NOT a
       missing key — the UI distinguishes "no report yet" from
       "reported, no error")."""
    monkeypatch.setattr(health, "_free_disk_mb", lambda p: 1024)
    monkeypatch.setattr(health, "_refrepo_size_mb", lambda p: 4500)
    monkeypatch.setattr(ai, "_LAST_ERROR", None)
    snap = health.collect(_cfg())
    assert snap["last_anthropic_error"] is None


# --- collect: disk_low flag (the safety-valve signal to the operator) -----

def test_collect_disk_low_true_below_floor(monkeypatch):
    monkeypatch.setattr(health, "_free_disk_mb", lambda p: 4000)
    monkeypatch.setattr(health, "_refrepo_size_mb", lambda p: 0)
    snap = health.collect(_cfg(min_free_disk_mb=5000))
    assert snap["disk_low"] is True


def test_collect_disk_low_false_above_floor(monkeypatch):
    monkeypatch.setattr(health, "_free_disk_mb", lambda p: 6000)
    monkeypatch.setattr(health, "_refrepo_size_mb", lambda p: 0)
    snap = health.collect(_cfg(min_free_disk_mb=5000))
    assert snap["disk_low"] is False


def test_collect_disk_low_false_when_free_unknown(monkeypatch):
    """No reading (volume not mounted) → not flagged low, mirroring the
       runner guard that won't pause on a measurement gap."""
    monkeypatch.setattr(health, "_free_disk_mb", lambda p: None)
    monkeypatch.setattr(health, "_refrepo_size_mb", lambda p: 0)
    snap = health.collect(_cfg(min_free_disk_mb=5000))
    assert snap["disk_low"] is False


# --- ai._record_outcome integration --------------------------------------

def test_ai_records_auth_error_for_health(monkeypatch):
    """Indirect test: a CallClaudeAuthError raised by call_claude
       records 'auth' on the module slot, which collect picks up.
       Verifies the integration without making a real SDK call."""
    import anthropic

    class _FakeResp:
        status_code = 401
        headers = {}
        request = None

    class _StubMessages:
        def create(self, **kw):
            raise anthropic.AuthenticationError(
                "bad key", response=_FakeResp(),
                body={"error": {"message": "bad key"}})

    class _StubClient:
        def __init__(self, **kw): pass
        @property
        def messages(self): return _StubMessages()

    monkeypatch.setattr(anthropic, "Anthropic", _StubClient)
    ai._record_outcome(None)        # start clean
    with pytest.raises(ai.CallClaudeAuthError):
        ai.call_claude(SimpleNamespace(anthropic_api_key="sk-bad",
                                         anthropic_model="",
                                         claude_backend="sdk"),
                       "system", "user")
    assert ai.get_last_error() == "auth"
