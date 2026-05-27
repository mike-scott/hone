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
        methodology_dir=str(tmp_path / "methodology"),
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


# --- methodology import / export ------------------------------------------

def _seed_active_methodology(db):
    """Drop a tiny but schema-valid methodology into the DB so the
       export + active-version surfaces have something to render.
       Mirrors the shape of core/default-methodology.yaml at the
       structural level (principles, stages, checks, severity_scale,
       operations) — using the real packaged default keeps the fixture
       honest against methodology.schema.yaml evolution (a hand-rolled
       minimal doc would drift the moment the schema adds a required
       field)."""
    import os as _os
    default_path = _os.path.join(_os.path.dirname(__file__), "..",
                                   "core", "default-methodology.yaml")
    with open(default_path, encoding="utf-8") as f:
        document = yaml.safe_load(f)
    return core_db.add_methodology_version(db, document,
                                            note="test seed"), document


def test_settings_page_shows_methodology_panel_with_active_version(ctx):
    version, _doc = _seed_active_methodology(ctx.db)
    body = ctx.client.get("/settings").text
    assert "Methodology" in body
    assert f"Export active (v{version})" in body
    assert 'action="/settings/methodology/import"' in body


def test_settings_page_shows_no_methodology_when_unbootstrapped(ctx):
    body = ctx.client.get("/settings").text
    assert "No methodology bootstrapped yet" in body


def test_export_methodology_returns_yaml_with_versioned_filename(ctx):
    """The export renders the active methodology as YAML with a
       version-stamped filename. Body round-trips through YAML; the
       prose fields land in canonicalized form (Markdown reflowed
       at PROSE_WRAP_COLUMN by core/methodology_format) which is
       what the seeded default-methodology.yaml fixture already is
       — so an idempotent equality holds."""
    from core.methodology_format import normalize_methodology
    version, doc = _seed_active_methodology(ctx.db)
    r = ctx.client.get("/settings/methodology/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-yaml")
    assert (f'filename="methodology-v{version}.yaml"'
            in r.headers["content-disposition"])
    # Export canonicalizes prose fields — compare against the
    # normalized form of the seeded document.
    assert yaml.safe_load(r.text) == normalize_methodology(doc)


def test_export_methodology_preserves_default_style(ctx):
    """The exported YAML mirrors core/default-methodology.yaml's style
       — literal block scalars (`|`) for multi-line strings, list
       items indented under their parent key — so an operator can
       diff the export against the on-disk default by eye and hand-
       edit without fighting PyYAML's default escape-heavy quoted-
       string form.

       Tracks the cosmetic side of the export-format audit: a round-
       trip through yaml.safe_dump (PyYAML defaults) was producing
       valid-but-ugly YAML that obscured real content drift behind
       formatting noise."""
    _seed_active_methodology(ctx.db)
    body = ctx.client.get("/settings/methodology/export").text
    # Literal block scalars: at least one `: |\n` survives the dump,
    # rather than the default `: "…\n…"` quoted form.
    assert ": |\n" in body
    # List items indented under their parent key. The default
    # methodology has principles at the top level, so the export
    # should show `principles:\n  - id:` not `principles:\n- id:`.
    assert "principles:\n  - id:" in body
    # No PyYAML line-continuation backslashes — the custom dumper's
    # literal-block representer keeps multi-line strings out of
    # quoted-scalar territory where PyYAML's wrap would inject `\`.
    assert "\\\n" not in body
    # And the bytes round-trip back to the active document in
    # canonicalized form (export normalizes prose via methodology_
    # format.normalize_methodology).
    import yaml
    from core.methodology_format import normalize_methodology
    assert yaml.safe_load(body) == normalize_methodology(
        core_db.active_methodology(ctx.db)[1])


def test_export_methodology_404_when_unbootstrapped(ctx):
    r = ctx.client.get("/settings/methodology/export")
    assert r.status_code == 404


def test_import_methodology_installs_new_active_version_in_the_db(ctx):
    """The import endpoint creates a new active version in the DB. The
       DB is on the persistent data volume, so the imported methodology
       survives container restarts without any sidecar disk file."""
    _v1, _ = _seed_active_methodology(ctx.db)
    # Upload a content-different copy so the identical-rejection
    # branch doesn't fire — the exact doc.version auto-bump rules
    # are covered in their own tests below.
    _, doc = _seed_active_methodology(ctx.db)             # second seed
    doc["description"] = "uploaded copy"
    payload = yaml.safe_dump(doc, sort_keys=False).encode("utf-8")
    r = ctx.client.post("/settings/methodology/import",
                         files={"file": ("uploaded.yaml", payload,
                                          "application/x-yaml")})
    assert r.status_code == 200                          # follows the 303
    assert "Methodology imported" in r.text
    active = core_db.active_methodology(ctx.db)
    assert active is not None
    assert active[1]["description"] == "uploaded copy"


def test_import_methodology_rejects_unparseable_yaml(ctx):
    """Bad YAML doesn't crash the endpoint — it lands the operator
       back on /settings with an error banner and no DB or disk
       write."""
    r = ctx.client.post("/settings/methodology/import",
                         files={"file": ("bad.yaml",
                                          b"key: : :\n  - [", "text/yaml")})
    assert r.status_code == 200                          # follows the 303
    assert "Import failed" in r.text
    assert core_db.active_methodology(ctx.db) is None    # never imported


def test_import_methodology_rejects_schema_invalid_document(ctx):
    """A YAML that parses but doesn't match methodology.schema.yaml
       (here: missing required top-level keys) is rejected at validate
       time before any DB write."""
    payload = yaml.safe_dump({"this": "is not a methodology"}).encode("utf-8")
    r = ctx.client.post("/settings/methodology/import",
                         files={"file": ("bad.yaml", payload,
                                          "application/x-yaml")})
    assert r.status_code == 200
    assert "Import failed" in r.text
    assert core_db.active_methodology(ctx.db) is None


def test_import_methodology_rejects_byte_identical_upload(ctx):
    """Re-uploading the export of the active methodology must NOT
       create a duplicate DB row — the import endpoint canonical-JSON-
       compares the upload against the active and rejects with the
       'identical' flash message. Audit-trail discipline: every row in
       methodology_versions reflects a real change."""
    _v, doc = _seed_active_methodology(ctx.db)
    before_rows = ctx.db.execute(
        "SELECT COUNT(*) FROM methodology_versions").fetchone()[0]
    payload = yaml.safe_dump(doc, sort_keys=False).encode("utf-8")
    r = ctx.client.post("/settings/methodology/import",
                         files={"file": ("same.yaml", payload,
                                          "application/x-yaml")})
    assert r.status_code == 200                              # follows the 303
    assert "byte-identical" in r.text
    after_rows = ctx.db.execute(
        "SELECT COUNT(*) FROM methodology_versions").fetchone()[0]
    assert after_rows == before_rows                         # no new row


def test_import_methodology_autobumps_doc_version_above_active(ctx):
    """hone-core takes ownership of the document `version` field on
       import — the stored value is max(active.version, uploaded.version)+1
       regardless of what the operator put in the file. Schema's stated
       design: doc.version is hone-core-controlled, bumped on every
       merge-gate-equivalent change."""
    _v1, _ = _seed_active_methodology(ctx.db)                # active.version=1
    _, doc = _seed_active_methodology(ctx.db)
    doc["version"] = 1                                       # below active
    doc["description"] = "edited offline"                    # break identity
    payload = yaml.safe_dump(doc, sort_keys=False).encode("utf-8")
    r = ctx.client.post("/settings/methodology/import",
                         files={"file": ("edit.yaml", payload,
                                          "application/x-yaml")})
    assert r.status_code == 200
    assert "Methodology imported" in r.text
    active = core_db.active_methodology(ctx.db)
    assert active is not None
    # active.doc.version was 1; uploaded was 1 → stored as max(1,1)+1 = 2
    assert active[1]["version"] == 2


def test_import_methodology_autobump_respects_uploaded_higher_version(ctx):
    """If the operator hand-bumps doc.version above active, the auto-
       bump uses THAT as the floor — `max(active, uploaded) + 1`. So
       upload v99 over active v1 → stored as v100. Lets an operator
       carry an externally-coordinated version number forward without
       hone-core silently collapsing it."""
    _v1, _ = _seed_active_methodology(ctx.db)
    _, doc = _seed_active_methodology(ctx.db)
    doc["version"] = 99
    doc["description"] = "externally-versioned"
    payload = yaml.safe_dump(doc, sort_keys=False).encode("utf-8")
    r = ctx.client.post("/settings/methodology/import",
                         files={"file": ("v99.yaml", payload,
                                          "application/x-yaml")})
    assert r.status_code == 200
    active = core_db.active_methodology(ctx.db)
    assert active[1]["version"] == 100                       # max(1,99)+1


def test_import_methodology_rejects_oversized_upload(ctx):
    """A multi-megabyte upload short-circuits with the size-cap error
       — the YAML parser never sees the input."""
    big = b"# padding\n" * (200 * 1024)                  # ~2 MiB
    r = ctx.client.post("/settings/methodology/import",
                         files={"file": ("big.yaml", big, "text/yaml")})
    assert r.status_code == 200
    assert "exceeds the methodology size cap" in r.text
