"""Tests for the GATHER pass — ingesting a gather module's output into
core_db (core/gather.py). A network-free fake module stands in for a real
data source; the live sashiko module is exercised separately (smoke tests)."""
import io
import os
import tarfile

import pytest
import zstandard

from core import core_db, gather

PatchsetRef = gather.gather_api.PatchsetRef
Finding = gather.gather_api.Finding


class _FakeModule:
    """A minimal in-memory GatherModule — no network, no git."""
    name = "fake-source"
    kind = "ai"

    def __init__(self, refs):
        self._refs = refs

    def list(self):
        return self._refs

    def base(self, patchset_id):
        return f"base-{patchset_id}"

    def pull(self, patchset_id, dest_dir):
        path = os.path.join(dest_dir, "patch1.patch")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"Subject: [PATCH] {patchset_id}\n\n--- a/x\n+++ b/x\n")
        return [path]

    def findings(self, patchset_id):
        return [Finding(reviewer="fake-source", type="ai",
                        text="a finding", severity="high")]


def _refs():
    return [
        PatchsetRef(id="1", root_message_id="<root-1@x>",
                    subject="patchset one", sent=100),
        PatchsetRef(id="2", root_message_id="<root-2@x>",
                    subject="undated", skip_reason="unresolved-date"),
    ]


@pytest.fixture
def db(tmp_path):
    return core_db.connect(str(tmp_path / "hone.db"))


def test_gather_ingests_a_new_patchset(db):
    stats = gather._gather_source(db, _FakeModule(_refs()))
    assert stats == {"seen": 2, "gathered": 1, "skipped": 1, "known": 0}
    ps = core_db.get_patchset(db, "<root-1@x>")
    assert ps["state"] == "gathered" and ps["base_commit"] == "base-1"
    assert ps["source"] == "fake-source" and ps["n_patches"] == 1


def test_gather_stores_a_tar_zst_blob(db):
    gather._gather_source(db, _FakeModule(_refs()))
    blob = core_db.get_patch_blob(db, "<root-1@x>")
    assert blob is not None
    tar_bytes = zstandard.ZstdDecompressor().decompress(blob)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        assert tar.getnames() == ["patch1.patch"]


def test_gather_records_findings_and_queues_a_review(db):
    gather._gather_source(db, _FakeModule(_refs()))
    findings = core_db.source_findings(db, "<root-1@x>")
    assert len(findings) == 1 and findings[0]["severity"] == "high"
    claim = core_db.claim_review(db, "node-1")
    assert claim and claim["root_message_id"] == "root-1@x"


def test_gather_skip_flags_a_skip_reason_ref(db):
    gather._gather_source(db, _FakeModule(_refs()))
    skipped = core_db.get_patchset(db, "<root-2@x>")
    assert skipped["state"] == "skipped"
    assert skipped["skip_reason"] == "unresolved-date"
    # a skip-flagged patchset is never enqueued for review
    assert core_db.claim_review(db, "node-1")["root_message_id"] != "root-2@x"


def test_gather_is_idempotent(db):
    gather._gather_source(db, _FakeModule(_refs()))
    stats = gather._gather_source(db, _FakeModule(_refs()))
    assert stats == {"seen": 2, "gathered": 0, "skipped": 0, "known": 2}
    assert len(core_db.source_findings(db, "<root-1@x>")) == 1   # no re-ingest


# --- source selection (HONE_GATHER_SOURCES) --------------------------------

_INSTALLED = ["linux-arm-msm", "sashiko"]


def test_select_sources_default_is_every_installed():
    assert gather._select_sources((), _INSTALLED) == _INSTALLED


def test_select_sources_restricts_to_the_configured_set():
    assert gather._select_sources(("sashiko",), _INSTALLED) == ["sashiko"]


def test_select_sources_keeps_the_operators_order():
    assert gather._select_sources(("sashiko", "linux-arm-msm"),
                                  _INSTALLED) == ["sashiko", "linux-arm-msm"]


def test_select_sources_drops_unknown_names():
    assert gather._select_sources(("sashiko", "nope"), _INSTALLED) == ["sashiko"]
