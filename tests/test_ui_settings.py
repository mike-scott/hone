"""Tests for the operator Settings page (core/ui.py /settings + /settings/tags)
— runtime-config edits and the list-tag gather filter."""
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import core_db, runtime_config, ui


@pytest.fixture
def ctx(tmp_path):
    config_path = str(tmp_path / "config.yaml")
    db = core_db.connect(str(tmp_path / "hone.db"))
    app = FastAPI()
    app.include_router(ui.router)
    app.state.db = db
    app.state.runtime_config = runtime_config.load(config_path)   # writes it
    app.state.config = SimpleNamespace(
        config_path=config_path, hostname="core.example", http_port=8000,
        public_url="https://core.example:8000", data_dir="/data",
        fleet_secret="FLEETSECRETVALUE", admin_token="ADMINTOKENVALUE")
    # Lore-clone status — same shape `core.main._initial_lore_status` builds
    # at lifespan startup. The tests cover the absent / ready / cloning /
    # error branches by overriding fields on this dict.
    app.state.lore_clone = {
        "phase": "absent", "percent": 0, "git_phase": None,
        "last_line": None, "started_at": None, "completed_at": None,
        "error": None, "archive_present": False,
        "archive_path": str(tmp_path / "archive" / "lore"),
        "autoclone_enabled": False}
    # The gather supervisor publishes its trigger event here at startup;
    # the "Gather now" button (POST /settings/gather/trigger) `set()`s it.
    # An asyncio.Event needs a running loop to instantiate — TestClient
    # provides one, but the fixture is sync, so we set it lazily below.
    app.state.gather_trigger = _LazyEvent()
    return SimpleNamespace(client=TestClient(app), app=app, db=db,
                           config_path=config_path)


class _LazyEvent:
    """Test stand-in for asyncio.Event — records set() calls so a sync
       test can assert the button fired without needing a running loop."""

    def __init__(self):
        self.set_calls = 0

    def set(self):
        self.set_calls += 1

    def is_set(self):
        return self.set_calls > 0

    def clear(self):
        self.set_calls = 0


def _form(**overrides):
    """A complete settings form at the defaults, with `overrides` applied. A
       "sources" field defaults to [] (every toggle off); pass a list of source
       names to turn toggles on."""
    form = {}
    for group, key, _label, _unit, kind in runtime_config.FIELDS:
        if kind == "sources":
            form[f"{group}.{key}"] = []
        else:
            form[f"{group}.{key}"] = str(runtime_config.DEFAULTS[group][key])
    form.update(overrides)
    return form


# --- runtime-config form ---------------------------------------------------

def test_settings_page_shows_values_and_masks_secrets(ctx):
    r = ctx.client.get("/settings")
    assert r.status_code == 200
    assert "GATHER cadence" in r.text and 'value="600"' in r.text
    assert "Enabled gather sources" in r.text and "form-switch" in r.text
    assert "lore" in r.text                          # a per-source toggle
    assert "Deployment configuration" in r.text and "core.example" in r.text
    assert "FLEETSECRETVALUE" not in r.text          # secret is masked
    assert "ADMINTOKENVALUE" not in r.text


def test_save_settings_persists_and_applies_live(ctx):
    r = ctx.client.post("/settings", data=_form(**{
        "gather.interval_seconds": "300", "gather.sources": ["lore"]}))
    assert r.status_code == 200 and "Settings saved" in r.text
    on_disk = yaml.safe_load(open(ctx.config_path, encoding="utf-8"))
    assert on_disk["gather"]["interval_seconds"] == 300
    assert on_disk["gather"]["sources"] == ["lore"]
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


def test_save_can_unselect_all_sources(ctx):
    r = ctx.client.post("/settings", data=_form())
    assert r.status_code == 200 and "Settings saved" in r.text
    on_disk = yaml.safe_load(open(ctx.config_path, encoding="utf-8"))
    assert on_disk["gather"]["sources"] == []
    assert ctx.app.state.runtime_config.gather_sources == ()


def test_save_keeps_only_the_toggled_on_sources(ctx):
    r = ctx.client.post("/settings",
                        data=_form(**{"gather.sources": ["lore"]}))
    assert r.status_code == 200
    on_disk = yaml.safe_load(open(ctx.config_path, encoding="utf-8"))
    assert on_disk["gather"]["sources"] == ["lore"]


# --- list-tag filter -------------------------------------------------------

def test_settings_page_lists_known_tags_with_switches(ctx):
    core_db.seed_list_tags(ctx.db, [
        ("linux-arm-msm.vger.kernel.org", "linux-arm-msm"),
        ("linux-kernel.vger.kernel.org",  "LKML")])
    core_db.set_tag_enabled(ctx.db, "linux-arm-msm.vger.kernel.org", True)
    body = ctx.client.get("/settings").text
    assert "List-tag filter" in body
    assert "linux-arm-msm.vger.kernel.org" in body
    assert "linux-kernel.vger.kernel.org" in body
    # one input per tag (the manifest origin label appears for each)
    assert body.count('name="tag"') == 2
    assert body.count("manifest") >= 2
    # the enabled tag's switch is pre-checked, the other isn't
    assert body.count("checked") == 1


def test_settings_page_when_no_tags_known(ctx):
    body = ctx.client.get("/settings").text
    assert "No list tags known yet" in body


def test_save_tags_enables_only_the_ticked_set(ctx):
    core_db.seed_list_tags(ctx.db, [
        ("a.kernel.org", "A"), ("b.kernel.org", "B"), ("c.kernel.org", "C")])
    core_db.set_tag_enabled(ctx.db, "a.kernel.org", True)   # currently on
    # post only b → a goes off, b goes on, c stays off
    r = ctx.client.post("/settings/tags",
                        data={"tag": ["b.kernel.org"]})
    assert r.status_code == 200 and "Tag filter saved" in r.text
    assert core_db.enabled_tags(ctx.db) == ["b.kernel.org"]


def test_save_tags_with_no_ticks_clears_the_filter(ctx):
    core_db.seed_list_tags(ctx.db, [("a.kernel.org", "A")])
    core_db.set_tag_enabled(ctx.db, "a.kernel.org", True)
    r = ctx.client.post("/settings/tags", data={})
    assert r.status_code == 200
    assert core_db.enabled_tags(ctx.db) == []


def test_save_tags_ignores_a_tag_not_in_the_table(ctx):
    # an attacker-posted tag for a tag we don't know about must not get added
    core_db.seed_list_tags(ctx.db, [("a.kernel.org", "A")])
    r = ctx.client.post("/settings/tags",
                        data={"tag": ["bogus.kernel.org"]})
    assert r.status_code == 200
    rows = {t["tag"] for t in core_db.list_tags(ctx.db)}
    assert rows == {"a.kernel.org"}                # bogus never landed
    assert core_db.enabled_tags(ctx.db) == []


# --- lore-clone panel ------------------------------------------------------

def test_settings_page_shows_lore_clone_panel_absent_by_default(ctx):
    """First-run shape: no archive, no autoclone — the panel surfaces the
       helper command so the operator knows what to do."""
    body = ctx.client.get("/settings").text
    assert "Lore archive" in body and "Not present" in body
    assert "python3 core/gather-modules/lore.py clone" in body
    assert "HONE_LORE_AUTOCLONE" in body


def test_lore_clone_panel_when_cloning_renders_progress_and_polls(ctx):
    """Mid-clone: shows the progress bar + percent and includes the HTMX
       attributes that drive the 5-second poll."""
    ctx.app.state.lore_clone.update(
        phase="cloning", percent=47, git_phase="Receiving objects",
        last_line="Receiving objects:  47% (71590/152318)",
        started_at=__import__("time").time() - 120)
    body = ctx.client.get("/settings").text
    assert "Cloning" in body and "47%" in body
    # the HTMX poll attributes attach to the swap target
    assert 'hx-get="/settings/lore-clone-status"' in body
    assert 'hx-trigger="every 5s"' in body
    # the most recent git progress line is surfaced
    assert "Receiving objects:  47%" in body


def test_lore_clone_panel_when_ready_shows_archive_path(ctx, tmp_path):
    archive = tmp_path / "archive" / "lore"
    (archive / ".git").mkdir(parents=True)
    ctx.app.state.lore_clone.update(
        phase="ready", percent=100, archive_present=True,
        archive_path=str(archive))
    body = ctx.client.get("/settings").text
    assert "Lore archive" in body and "Ready" in body
    assert str(archive) in body
    # no live-poll attributes once the clone is done
    assert "every 5s" not in body


def test_lore_clone_panel_when_error_shows_message(ctx):
    ctx.app.state.lore_clone.update(
        phase="error",
        error="fatal: unable to access 'https://lore.kernel.org/all/0/'")
    body = ctx.client.get("/settings").text
    assert "Clone failed" in body and "unable to access" in body


def test_lore_clone_status_partial_renders_standalone(ctx):
    """The partial endpoint returns just the panel (no full page chrome)
       — that's what HTMX swaps in via outerHTML."""
    r = ctx.client.get("/settings/lore-clone-status")
    assert r.status_code == 200
    assert 'id="lore-clone-panel"' in r.text
    assert "<html" not in r.text          # not the full settings.html page


def test_lore_clone_panel_picks_up_an_out_of_band_clone(ctx, tmp_path):
    """If the operator clones the archive themselves (CLI helper, or
       cp'd from elsewhere), the panel re-stats on each render and flips
       to 'ready' next refresh — no autoclone state update needed."""
    archive = tmp_path / "archive" / "lore"
    (archive / ".git").mkdir(parents=True)
    # state still says 'absent' (autoclone never ran)
    ctx.app.state.lore_clone["archive_path"] = str(archive)
    body = ctx.client.get("/settings/lore-clone-status").text
    assert "Ready" in body


# --- "Gather now" trigger --------------------------------------------------

def test_settings_page_renders_the_gather_now_button(ctx):
    body = ctx.client.get("/settings").text
    assert "Trigger gather now" in body
    assert 'action="/settings/gather/trigger"' in body


def test_trigger_gather_sets_the_event_and_redirects(ctx):
    r = ctx.client.post("/settings/gather/trigger")
    assert r.status_code == 200                # TestClient follows the 303
    assert "GATHER triggered" in r.text
    assert ctx.app.state.gather_trigger.set_calls == 1


def test_trigger_gather_is_a_no_op_when_supervisor_isnt_running(ctx):
    """No gather supervisor (e.g. tests, or pre-startup) → the endpoint
       still returns cleanly without raising."""
    ctx.app.state.gather_trigger = None
    r = ctx.client.post("/settings/gather/trigger")
    assert r.status_code == 200 and "GATHER triggered" in r.text
