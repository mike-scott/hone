"""Tests for the operator Settings page (core/ui.py /settings)."""
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import runtime_config, ui


@pytest.fixture
def ctx(tmp_path):
    config_path = str(tmp_path / "config.yaml")
    app = FastAPI()
    app.include_router(ui.router)
    app.state.runtime_config = runtime_config.load(config_path)   # writes it
    app.state.config = SimpleNamespace(
        config_path=config_path, hostname="core.example", http_port=8000,
        public_url="https://core.example:8000", data_dir="/data",
        fleet_secret="FLEETSECRETVALUE", admin_token="ADMINTOKENVALUE")
    return SimpleNamespace(client=TestClient(app), app=app,
                           config_path=config_path)


def _form(**overrides):
    """A complete settings form at the defaults, with `overrides` applied."""
    form = {}
    for group, key, _label, _unit, kind in runtime_config.FIELDS:
        default = runtime_config.DEFAULTS[group][key]
        form[f"{group}.{key}"] = "" if kind == "csv" else str(default)
    form.update(overrides)
    return form


def test_settings_page_shows_values_and_masks_secrets(ctx):
    r = ctx.client.get("/settings")
    assert r.status_code == 200
    assert "GATHER cadence" in r.text and 'value="600"' in r.text
    assert "Deployment configuration" in r.text and "core.example" in r.text
    assert "FLEETSECRETVALUE" not in r.text          # secret is masked
    assert "ADMINTOKENVALUE" not in r.text


def test_save_settings_persists_and_applies_live(ctx):
    r = ctx.client.post("/settings", data=_form(**{
        "gather.interval_seconds": "300", "gather.sources": "sashiko"}))
    assert r.status_code == 200 and "Settings saved" in r.text
    on_disk = yaml.safe_load(open(ctx.config_path, encoding="utf-8"))
    assert on_disk["gather"]["interval_seconds"] == 300
    assert on_disk["gather"]["sources"] == ["sashiko"]
    assert ctx.app.state.runtime_config.gather_interval == 300   # live


def test_save_rejects_a_non_positive_interval(ctx):
    r = ctx.client.post("/settings",
                        data=_form(**{"gather.interval_seconds": "0"}))
    assert r.status_code == 400 and "must be 1 or more" in r.text
    on_disk = yaml.safe_load(open(ctx.config_path, encoding="utf-8"))
    assert on_disk["gather"]["interval_seconds"] == 600          # untouched


def test_save_rejects_a_non_numeric_value(ctx):
    r = ctx.client.post("/settings",
                        data=_form(**{"work_queue.lease_seconds": "soon"}))
    assert r.status_code == 400 and "whole number" in r.text


def test_save_rejects_an_unknown_gather_source(ctx):
    r = ctx.client.post("/settings",
                        data=_form(**{"gather.sources": "sashiko, bogus-src"}))
    assert r.status_code == 400 and "bogus-src" in r.text
    on_disk = yaml.safe_load(open(ctx.config_path, encoding="utf-8"))
    assert on_disk["gather"]["sources"] == []                    # untouched
