"""Tests for node/config.py — the env-driven config and the
HONE_CLAUDE_BACKEND gate that decides which Claude backend is in use."""
import pytest

from node.config import Config, CLAUDE_BACKENDS


def _set_minimum(monkeypatch, **extra):
    """Set the env vars Config.from_env needs across the test matrix.
       Per-test overrides come in via **extra."""
    monkeypatch.setenv("HONE_CORE_URL", "https://core.example")
    monkeypatch.setenv("HONE_FLEET_SECRET", "fleet")
    monkeypatch.delenv("HONE_CLAUDE_BACKEND", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_default_backend_is_sdk_and_requires_anthropic_api_key(monkeypatch):
    """The default backend is sdk, which means ANTHROPIC_API_KEY is
       required — fails fast if not set."""
    _set_minimum(monkeypatch)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        Config.from_env()


def test_sdk_backend_with_api_key_succeeds(monkeypatch):
    _set_minimum(monkeypatch, ANTHROPIC_API_KEY="sk-ant-test")
    cfg = Config.from_env()
    assert cfg.claude_backend == "sdk"
    assert cfg.anthropic_api_key == "sk-ant-test"


def test_cli_backend_does_not_require_anthropic_api_key(monkeypatch):
    """The CLI backend reads OAuth credentials from $HOME/.claude, so
       ANTHROPIC_API_KEY is unused and shouldn't be required at
       startup. Claude Code subscribers without API billing rely on
       this."""
    _set_minimum(monkeypatch, HONE_CLAUDE_BACKEND="cli")
    cfg = Config.from_env()
    assert cfg.claude_backend == "cli"
    assert cfg.anthropic_api_key == ""             # not set, not used


def test_cli_backend_keeps_api_key_if_supplied(monkeypatch):
    """Setting both is benign — the CLI path just ignores
       ANTHROPIC_API_KEY. Lets an operator flip between backends
       without scrubbing .env."""
    _set_minimum(monkeypatch,
                  HONE_CLAUDE_BACKEND="cli",
                  ANTHROPIC_API_KEY="sk-ant-test")
    cfg = Config.from_env()
    assert cfg.claude_backend == "cli"
    assert cfg.anthropic_api_key == "sk-ant-test"


def test_unknown_backend_is_rejected(monkeypatch):
    _set_minimum(monkeypatch, HONE_CLAUDE_BACKEND="bogus")
    with pytest.raises(RuntimeError, match="HONE_CLAUDE_BACKEND"):
        Config.from_env()


def test_backend_value_is_lowercased(monkeypatch):
    """Case-insensitive — operators sometimes set HONE_CLAUDE_BACKEND=CLI
       and that shouldn't be a startup failure."""
    _set_minimum(monkeypatch, HONE_CLAUDE_BACKEND="CLI")
    cfg = Config.from_env()
    assert cfg.claude_backend == "cli"


def test_anthropic_model_defaults_to_empty_when_unset(monkeypatch):
    """Unset ANTHROPIC_MODEL leaves cfg.anthropic_model empty — the
       dispatcher then falls back to ai.DEFAULT_MODEL."""
    _set_minimum(monkeypatch, HONE_CLAUDE_BACKEND="cli")
    cfg = Config.from_env()
    assert cfg.anthropic_model == ""


def test_anthropic_model_is_read_from_env(monkeypatch):
    """ANTHROPIC_MODEL flows through Config.from_env so operators can
       pin the model from .env without code changes."""
    _set_minimum(monkeypatch,
                  HONE_CLAUDE_BACKEND="cli",
                  ANTHROPIC_MODEL="claude-sonnet-4-6")
    cfg = Config.from_env()
    assert cfg.anthropic_model == "claude-sonnet-4-6"


def test_claude_backends_enum_documents_supported_values():
    """The exported tuple is what node.config validates against; keep
       it in lockstep with the operator-facing .env.example docs."""
    assert CLAUDE_BACKENDS == ("sdk", "cli")


def test_cli_timeout_defaults_to_600(monkeypatch):
    _set_minimum(monkeypatch, HONE_CLAUDE_BACKEND="cli")
    assert Config.from_env().cli_timeout == 600


def test_cli_timeout_overridden_by_env(monkeypatch):
    """HONE_CLI_TIMEOUT overrides the 600s default — the knob an operator
       turns when a node's agentic review turns legitimately run long."""
    _set_minimum(monkeypatch, HONE_CLAUDE_BACKEND="cli",
                 HONE_CLI_TIMEOUT="1800")
    assert Config.from_env().cli_timeout == 1800
