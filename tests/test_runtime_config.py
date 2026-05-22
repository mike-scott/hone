"""Unit tests for core/runtime_config.py — the operator-tunable config layer
(config.yaml: defaults, env seeding, file overlay, save)."""
import os

import pytest
import yaml

from core import runtime_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear the tunable env vars so tests are deterministic; the seeding test
       sets the ones it needs explicitly."""
    for env, _parse in runtime_config._ENV_SEEDS.values():
        monkeypatch.delenv(env, raising=False)


def test_first_run_writes_a_config_file(tmp_path):
    path = str(tmp_path / "config.yaml")
    rc = runtime_config.load(path)
    assert os.path.exists(path)
    assert rc.gather_interval == 600 and rc.redraft_cap == 3      # the defaults
    on_disk = yaml.safe_load(open(path, encoding="utf-8"))
    assert on_disk["gather"]["interval_seconds"] == 600


def test_first_run_seeds_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HONE_GATHER_INTERVAL", "120")
    monkeypatch.setenv("HONE_GATHER_SOURCES", "sashiko")
    rc = runtime_config.load(str(tmp_path / "config.yaml"))
    assert rc.gather_interval == 120
    assert rc.gather_sources == ("sashiko",)


def test_first_run_seeds_sources_from_installed_modules(tmp_path):
    # with no HONE_GATHER_SOURCES set, the enabled sources default to every
    # installed module — gather-everything stays the out-of-the-box default.
    rc = runtime_config.load(str(tmp_path / "config.yaml"),
                             all_sources=["alpha", "beta"])
    assert rc.gather_sources == ("alpha", "beta")


def test_existing_file_overrides_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("gather:\n  interval_seconds: 42\n", encoding="utf-8")
    rc = runtime_config.load(str(path))
    assert rc.gather_interval == 42                # from the file
    assert rc.lease_seconds == 1800                # default fills the rest


def test_unknown_and_malformed_keys_are_ignored(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "gather:\n  interval_seconds: 99\n  bogus_key: 1\n"
        "nope:\n  x: 1\n"
        "work_queue:\n  lease_seconds: not-a-number\n", encoding="utf-8")
    rc = runtime_config.load(str(path))
    assert rc.gather_interval == 99                # the valid override took
    assert rc.lease_seconds == 1800                # wrong type -> default kept
    assert "nope" not in rc.as_dict()              # unknown group dropped


def test_save_round_trips(tmp_path):
    path = str(tmp_path / "config.yaml")
    runtime_config.load(path)                      # first run writes it
    data = runtime_config.load(path).as_dict()
    data["merge_gate"]["redraft_cap"] = 5
    runtime_config.save(path, runtime_config.RuntimeConfig(data))
    assert runtime_config.load(path).redraft_cap == 5
