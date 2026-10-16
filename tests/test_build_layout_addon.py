"""scripts/build_layout_addon.py: the layout model add-on's archive and catalogue entry.

Built from a stand-in model folder against a catalogue that pins it, so no
download and no real model are needed.
"""
from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from app.core.addon_manager import AddonInfo, CatalogueState, validate_addon_entry
from scripts import build_layout_addon as build

MODEL = b"stand-in layout model"


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    for name in build.UPSTREAM_FILES:
        (source / name).write_bytes(MODEL if name == build.MODEL_FILE else f"{name}\n".encode())
    catalogue = json.loads(build.CATALOGUE_PATH.read_text(encoding="utf-8"))
    catalogue["addons"][build.ADDON_NAME]["pack_copy"]["sha256_files"][build.MODEL_FILE] = (
        hashlib.sha256(MODEL).hexdigest()
    )
    path = tmp_path / "addons.json"
    path.write_text(json.dumps(catalogue), encoding="utf-8")
    return source, path


def test_the_archive_is_reproducible_and_its_entry_is_published(tmp_path):
    source, catalogue = _setup(tmp_path)
    first = build.build(tmp_path / "a", source, "layout-addon-v1", catalogue=catalogue, check_load=False)
    second = build.build(tmp_path / "b", source, "layout-addon-v1", catalogue=catalogue, check_load=False)
    assert first["sha256"] == second["sha256"], "same files, same bytes"
    assert first["url"] == (
        "https://github.com/AbdoslamB/InkDoc/releases/download/layout-addon-v1/inkdoc-addon-layout_model.tar.gz"
    )
    known = {k: v for k, v in first.items() if k not in ("source_repo", "source_revision")}
    assert validate_addon_entry(AddonInfo(name=build.ADDON_NAME, **known)) == (CatalogueState.PUBLISHED, [])

    written = json.loads((tmp_path / "a" / "addons.json").read_text(encoding="utf-8"))
    assert written["addons"][build.ADDON_NAME] == first
    assert "code_enrichment" in written["addons"], "the rest of the catalogue is carried through"


def test_the_archive_holds_exactly_what_the_installer_checks(tmp_path):
    source, catalogue = _setup(tmp_path)
    entry = build.build(tmp_path / "out", source, "layout-addon-v1", catalogue=catalogue, check_load=False)
    with tarfile.open(tmp_path / "out" / build.ARCHIVE_NAME) as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    root = entry["model_dir"]
    assert set(members) == {f"{root}/{name}" for name in (*build.UPSTREAM_FILES, "LICENSE")}
    assert {name.split("/", 1)[1]: hashlib.sha256(data).hexdigest() for name, data in members.items()} == (
        entry["sha256_files"]
    )
    assert b"Apache License" in members[f"{root}/LICENSE"]


def test_only_the_pinned_model_is_packaged(tmp_path):
    source, catalogue = _setup(tmp_path)
    (source / build.MODEL_FILE).write_bytes(b"some other model")
    with pytest.raises(SystemExit, match="not the pinned model"):
        build.build(tmp_path / "out", source, "layout-addon-v1", catalogue=catalogue, check_load=False)
    (source / "config.json").unlink()
    with pytest.raises(SystemExit, match="lacks config.json"):
        build.build(tmp_path / "out", source, "layout-addon-v1", catalogue=catalogue, check_load=False)
