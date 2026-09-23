"""Tests for the optional add-on installer.

The cases that matter are the ones where being wrong is expensive: a tampered
archive being accepted, a half-finished install reading as complete, and the
gating that stops the enrichment toggles turning on before the model is there.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.addon_manager import (  # noqa: E402
    AddonError,
    AddonManager,
    version_at_least,
    version_tuple,
)
from app.core.download_utils import SecurityError  # noqa: E402

CATALOGUE = REPO_ROOT / "app" / "core" / "addons.json"


# ─── Version comparison ──────────────────────────────────────────────────────


def test_version_tuple_and_at_least():
    assert version_tuple("4.0.0") == (4, 0, 0)
    assert version_tuple("12.1") == (12, 1)
    assert version_at_least("4.0.0", "4.0.0")
    assert version_at_least("12.0.0", "4.0.0")
    assert not version_at_least("1.0.0", "4.0.0")
    # An unreadable or absent version must fail a requirement, never satisfy it.
    assert not version_at_least(None, "4.0.0")
    assert not version_at_least("", "4.0.0")
    assert not version_at_least("garbage", "4.0.0")
    # No requirement is always satisfied.
    assert version_at_least("1.0.0", "")
    assert version_at_least(None, None)
    print("[OK] test_version_tuple_and_at_least passed")


def test_shipped_catalogue_is_well_formed():
    raw = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    entry = raw["addons"]["code_enrichment"]
    assert entry["model_dir"] == "docling-project--CodeFormulaV2"
    # Must match the pack version Phase 4's workflow derives from docling-pack-v4.
    assert entry["min_pack_version"] == "4.0.0"
    print("[OK] test_shipped_catalogue_is_well_formed passed")


def test_unreleased_addon_is_not_installable():
    """While url/sha256 are blank the toggles must stay disabled."""
    mgr = AddonManager(catalogue_path=CATALOGUE)
    st = mgr.get_status("code_enrichment")
    assert st["available"] is False
    assert st["installable"] is False
    assert st["usable"] is False
    print("[OK] test_unreleased_addon_is_not_installable passed")


def test_unknown_addon_reports_cleanly():
    mgr = AddonManager(catalogue_path=CATALOGUE)
    st = mgr.get_status("does-not-exist")
    assert st["available"] is False
    assert "Unknown" in st["reason"]
    try:
        mgr.install("does-not-exist")
    except AddonError:
        pass
    else:
        raise AssertionError("installing an unknown add-on should raise")
    print("[OK] test_unknown_addon_reports_cleanly passed")


def test_missing_catalogue_degrades_quietly():
    mgr = AddonManager(catalogue_path=REPO_ROOT / "does" / "not" / "exist.json")
    assert mgr.list_addon_names() == []
    assert mgr.get_status("code_enrichment")["available"] is False
    print("[OK] test_missing_catalogue_degrades_quietly passed")


# ─── Install lifecycle against a local fixture ───────────────────────────────


def _make_fixture(tmp: Path, *, corrupt_file: bool = False):
    """Build a tiny add-on archive and a catalogue that points at it."""
    model = tmp / "src" / "docling-project--CodeFormulaV2"
    (model / "nested").mkdir(parents=True)
    (model / "config.json").write_text('{"ok": true}', encoding="utf-8")
    (model / "nested" / "weights.bin").write_bytes(b"weights" * 100)

    files = sorted(p for p in model.rglob("*") if p.is_file())
    sha_files = {
        p.relative_to(model).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in files
    }
    if corrupt_file:
        # Catalogue claims a hash the archive does not have.
        sha_files["config.json"] = "0" * 64

    archive = tmp / "addon.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(model, arcname=model.name, recursive=True)

    catalogue = tmp / "addons.json"
    catalogue.write_text(json.dumps({
        "addons": {
            "code_enrichment": {
                "title": "Test Addon",
                "description": "fixture",
                "model_dir": "docling-project--CodeFormulaV2",
                "url": f"https://github.com/example/releases/download/x/{archive.name}",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "archive_format": "tar.gz",
                "size_bytes": archive.stat().st_size,
                "uncompressed_size_bytes": sum(p.stat().st_size for p in files),
                "min_pack_version": "4.0.0",
                "min_app_version": "",
                "sha256_files": sha_files,
            }
        }
    }), encoding="utf-8")
    return archive, catalogue


def _manager_for(tmp: Path, catalogue: Path, pack_version: str = "4.0.0"):
    """An AddonManager pointed at a fake pack, with the network stubbed out."""
    import app.core.addon_manager as am

    pack = tmp / "engines" / "docling"
    (pack / "models").mkdir(parents=True)
    (pack / "pack_meta.json").write_text(
        json.dumps({"pack_version": pack_version}), encoding="utf-8"
    )

    mgr = AddonManager(catalogue_path=catalogue)
    mgr._models_dir = lambda: pack / "models"  # type: ignore[method-assign]

    class _FakeEngineManager:
        @staticmethod
        def get_instance():
            return _FakeEngineManager()

        def is_pack_installed(self, _name="docling"):
            return True

        def get_engine_dir(self, _name="docling"):
            return pack

    import app.core.engine_manager as em

    original = em.EngineManager
    em.EngineManager = _FakeEngineManager  # type: ignore[misc]
    return mgr, pack, (em, original, am)


def _stub_download(am_module, archive: Path):
    """Copy the local fixture instead of making a network request."""
    original_stream = am_module.stream_download
    original_validate = am_module.validate_download_url

    def fake_stream(url, destination_path, **kwargs):
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(archive.read_bytes())
        cb = kwargs.get("progress_callback")
        if cb:
            cb(archive.stat().st_size, archive.stat().st_size)
        return hashlib.sha256(archive.read_bytes()).hexdigest()

    am_module.stream_download = fake_stream
    am_module.validate_download_url = lambda url, **kw: None
    return original_stream, original_validate


def test_install_verify_and_remove_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        archive, catalogue = _make_fixture(tmp)
        mgr, pack, (em, original_em, am) = _manager_for(tmp, catalogue)
        orig_stream, orig_validate = _stub_download(am, archive)
        try:
            assert mgr.get_status("code_enrichment")["installable"] is True
            assert mgr.is_installed("code_enrichment") is False

            mgr.install("code_enrichment")

            model = pack / "models" / "docling-project--CodeFormulaV2"
            assert model.is_dir()
            assert (model / "nested" / "weights.bin").is_file()
            assert mgr.is_installed("code_enrichment") is True
            assert mgr.get_status("code_enrichment")["usable"] is True
            assert mgr.verify("code_enrichment")["valid"] is True

            mgr.remove("code_enrichment")
            assert mgr.is_installed("code_enrichment") is False
            assert not model.exists()
        finally:
            am.stream_download, am.validate_download_url = orig_stream, orig_validate
            em.EngineManager = original_em
    print("[OK] test_install_verify_and_remove_roundtrip passed")


def test_tampered_file_is_rejected_and_nothing_installed():
    """A file whose hash does not match must abort and leave no model behind."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        archive, catalogue = _make_fixture(tmp, corrupt_file=True)
        mgr, pack, (em, original_em, am) = _manager_for(tmp, catalogue)
        orig_stream, orig_validate = _stub_download(am, archive)
        try:
            try:
                mgr.install("code_enrichment")
            except SecurityError:
                pass
            else:
                raise AssertionError("a tampered file should raise SecurityError")
            assert mgr.is_installed("code_enrichment") is False
            assert not (pack / "models" / "docling-project--CodeFormulaV2").exists()
            assert mgr.get_progress("code_enrichment")["status"] == "error"
        finally:
            am.stream_download, am.validate_download_url = orig_stream, orig_validate
            em.EngineManager = original_em
    print("[OK] test_tampered_file_is_rejected_and_nothing_installed passed")


def test_old_pack_blocks_install():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        archive, catalogue = _make_fixture(tmp)
        mgr, _pack, (em, original_em, am) = _manager_for(tmp, catalogue, pack_version="1.0.0")
        try:
            st = mgr.get_status("code_enrichment")
            assert st["installable"] is False
            assert "4.0.0" in st["reason"]
        finally:
            em.EngineManager = original_em
    print("[OK] test_old_pack_blocks_install passed")


def test_interrupted_install_reads_as_not_installed():
    """Model directory present but metadata missing must not count as installed."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _archive, catalogue = _make_fixture(tmp)
        mgr, pack, (em, original_em, am) = _manager_for(tmp, catalogue)
        try:
            (pack / "models" / "docling-project--CodeFormulaV2").mkdir(parents=True)
            assert mgr.is_installed("code_enrichment") is False
            assert mgr.get_status("code_enrichment")["usable"] is False
        finally:
            em.EngineManager = original_em
    print("[OK] test_interrupted_install_reads_as_not_installed passed")


if __name__ == "__main__":
    test_version_tuple_and_at_least()
    test_shipped_catalogue_is_well_formed()
    test_unreleased_addon_is_not_installable()
    test_unknown_addon_reports_cleanly()
    test_missing_catalogue_degrades_quietly()
    test_install_verify_and_remove_roundtrip()
    test_tampered_file_is_rejected_and_nothing_installed()
    test_old_pack_blocks_install()
    test_interrupted_install_reads_as_not_installed()
    print("\nALL ADDON MANAGER TESTS PASSED!")
