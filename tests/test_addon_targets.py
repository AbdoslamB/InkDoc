"""Add-ons InkDoc loads itself (target "inkdoc" in app/core/addons.json).

They install into InkDoc's own add-on folder, beside the engines folder, need
no Docling pack, and may be provided by the pack's pinned copy of the same
model (pack_copy), in which case nothing is downloaded.
"""
from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest

import app.core.addon_manager as am
import app.core.engine_manager as em
from app.core.addon_manager import AddonInfo, AddonManager, CatalogueState, validate_addon_entry

MODEL = b"stand-in layout model"
PIN = hashlib.sha256(MODEL).hexdigest()
PACK_DIR = "docling-project--docling-layout-heron-onnx"


def _entry(**overrides) -> dict:
    entry = {
        "title": "Layout model", "description": "Finds tables.", "target": "inkdoc", "license": "Apache-2.0",
        "model_dir": "docling-layout-heron-onnx", "url": "", "sha256": "", "archive_format": "tar.gz",
        "size_bytes": 0, "uncompressed_size_bytes": 0, "min_pack_version": "", "min_app_version": "",
        "pack_copy": {"dir": PACK_DIR, "sha256_files": {"model.onnx": PIN}}, "sha256_files": {},
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake InkDoc data folder (engines/, addons/) with a Docling pack that can come and go."""
    engines = tmp_path / "InkDoc" / "engines"
    pack = engines / "docling"
    state = {"pack": False}

    class _Engines:
        @staticmethod
        def get_instance():
            return _Engines()

        def get_engines_base_dir(self):
            engines.mkdir(parents=True, exist_ok=True)
            return engines

        def get_engine_dir(self, _name="docling"):
            return pack

        def is_pack_installed(self, _name="docling"):
            return state["pack"]

    monkeypatch.setattr(em, "EngineManager", _Engines)

    def manager(entry: dict) -> AddonManager:
        catalogue = tmp_path / "addons.json"
        catalogue.write_text(json.dumps({"addons": {"layout_model": entry}}), encoding="utf-8")
        return AddonManager(catalogue_path=catalogue)

    def install_pack(model: bytes = MODEL) -> None:
        (pack / "models" / PACK_DIR).mkdir(parents=True, exist_ok=True)
        (pack / "models" / PACK_DIR / "model.onnx").write_bytes(model)
        state["pack"] = True

    return {"manager": manager, "install_pack": install_pack, "state": state, "root": tmp_path / "InkDoc"}


# ─── Catalogue ───────────────────────────────────────────────────────────────
def test_an_inkdoc_add_on_needs_no_pack_version():
    state, defects = validate_addon_entry(AddonInfo(name="layout_model", **_entry()))
    assert (state, defects) == (CatalogueState.UNRELEASED, [])


@pytest.mark.parametrize("overrides, fragment", [
    ({"target": "elsewhere"}, "target 'elsewhere' is not one of"),
    ({"target": "docling_pack", "min_pack_version": "1.0.0"}, "pack_copy is only meaningful for target 'inkdoc'"),
    ({"pack_copy": {"dir": "../models", "sha256_files": {"model.onnx": PIN}}}, "must be one folder name"),
    ({"pack_copy": {"dir": PACK_DIR, "sha256_files": {}}}, "pack_copy has no sha256_files"),
    ({"pack_copy": {"dir": PACK_DIR, "sha256_files": {"model.onnx": "abc"}}}, "malformed digest"),
])
def test_a_wrong_target_or_pack_copy_is_a_defect(overrides, fragment):
    state, defects = validate_addon_entry(AddonInfo(name="layout_model", **_entry(**overrides)))
    assert state is CatalogueState.DEFECTIVE
    assert any(fragment in d for d in defects), defects


def test_the_shipped_layout_model_entry_pins_the_docling_packs_copy():
    entry = json.loads(am.ADDONS_CATALOGUE_PATH.read_text(encoding="utf-8"))["addons"]["layout_model"]
    from app.core.engines.layout_model import ADDON, MODEL_FILE

    assert ADDON == "layout_model" and entry["target"] == "inkdoc" and entry["license"] == "Apache-2.0"
    assert MODEL_FILE in entry["pack_copy"]["sha256_files"]
    state, defects = validate_addon_entry(AddonInfo(name="layout_model", **{
        k: v for k, v in entry.items() if k not in ("source_repo", "source_revision")}))
    assert state in (CatalogueState.UNRELEASED, CatalogueState.PUBLISHED), defects


# ─── Status ──────────────────────────────────────────────────────────────────
def test_the_docling_packs_copy_means_nothing_to_download(env):
    mgr = env["manager"](_entry())
    status = mgr.get_status("layout_model")
    assert not status["usable"] and not status["installable"]
    assert status["reason"] == "This add-on has not been published yet. The Docling engine comes with it."
    assert mgr.model_location("layout_model") is None

    env["install_pack"]()
    status = mgr.get_status("layout_model")
    assert status["usable"] and status["provided_by_pack"] and not status["installed"]
    assert not status["installable"] and status["reason"] == ""
    folder, digests = mgr.model_location("layout_model")
    assert folder.name == PACK_DIR and digests == {"model.onnx": PIN}
    assert mgr.verify("layout_model")["valid"]
    # The same keys as every other add-on status, so the page needs no special case.
    assert set(status) == set(AddonManager().get_status("code_enrichment"))


def test_a_different_model_in_the_pack_fails_the_check(env):
    env["install_pack"](b"another model")
    mgr = env["manager"](_entry())
    result = mgr.verify("layout_model")
    assert not result["valid"] and "integrity check failed" in result["reason"]


# ─── Install ─────────────────────────────────────────────────────────────────
def _published(tmp: Path) -> tuple[dict, Path]:
    folder = tmp / "src" / "docling-layout-heron-onnx"
    folder.mkdir(parents=True)
    (folder / "model.onnx").write_bytes(MODEL)
    (folder / "LICENSE").write_text("Apache-2.0", encoding="utf-8")
    archive = tmp / "layout.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(folder, arcname=folder.name)
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    return _entry(
        url="https://github.com/example/InkDoc/releases/download/layout-addon-v1/inkdoc-addon-layout_model.tar.gz",
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), size_bytes=archive.stat().st_size,
        uncompressed_size_bytes=sum(p.stat().st_size for p in folder.iterdir()), sha256_files=files,
    ), archive


def test_install_goes_to_inkdocs_own_folder_and_needs_no_pack(env, tmp_path, monkeypatch):
    entry, archive = _published(tmp_path)

    def fake_download(url, destination_path, **kwargs):
        destination_path.write_bytes(archive.read_bytes())
        return hashlib.sha256(archive.read_bytes()).hexdigest()

    monkeypatch.setattr(am, "stream_download", fake_download)
    monkeypatch.setattr(am, "validate_download_url", lambda url, **kw: None)
    mgr = env["manager"](entry)
    monkeypatch.setattr(mgr, "probe_worker_capability", lambda **kw: pytest.fail("no Docling worker for this add-on"))
    assert mgr.get_status("layout_model")["installable"], "no Docling pack needed"

    result = mgr.install("layout_model")
    installed = env["root"] / "addons" / "docling-layout-heron-onnx"
    assert Path(result["path"]) == installed and (installed / "model.onnx").read_bytes() == MODEL
    record = json.loads((env["root"] / "addons" / ".inkdoc-addon-layout_model.json").read_text(encoding="utf-8"))
    assert "docling_version" not in record and "worker_verified" not in record
    status = mgr.get_status("layout_model")
    assert status["installed"] and status["usable"] and not status["provided_by_pack"]
    assert mgr.verify("layout_model")["valid"]

    # InkDoc's own install comes first; removing it falls back to the pack's copy.
    env["install_pack"]()
    assert mgr.model_location("layout_model")[0] == installed
    mgr.remove("layout_model")
    assert not installed.exists()
    assert mgr.model_location("layout_model")[0].name == PACK_DIR


# ─── API ─────────────────────────────────────────────────────────────────────
def test_the_add_on_api_lists_it_with_where_it_installs():
    from fastapi.testclient import TestClient

    from app.server.server import app

    addons = {a["name"]: a for a in TestClient(app).get("/addons").json()["addons"]}
    layout = addons["layout_model"]
    assert layout["target"] == "inkdoc" and layout["license"] == "Apache-2.0"
    assert set(layout) == set(addons["code_enrichment"])
    assert addons["code_enrichment"]["target"] == "docling_pack" and addons["code_enrichment"]["provided_by_pack"] is False
