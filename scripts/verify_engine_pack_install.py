#!/usr/bin/env python3
"""Exercise InkDoc's production extraction and launch checks for every pack.

The release workflow uses this script before publishing.  It intentionally calls
``EngineManager._extract_*_safely`` rather than Python's unrestricted archive
helpers so CI proves the runtime's file-count, size, path, and link policies
accept the packs that will be delivered to users.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.engine_manager import EngineManager
from app.core.engine_manifest import load_engine_manifest


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def inspect_archive(path: Path, archive_format: str) -> tuple[int, int, int]:
    """Return archive entry count, total uncompressed bytes, and symlink count."""
    if archive_format == "zip":
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            symlinks = sum(((member.external_attr >> 16) & 0o170000) == 0o120000 for member in members)
            return len(members), sum(member.file_size for member in members), symlinks

    with tarfile.open(path, "r:*") as archive:
        members = archive.getmembers()
        symlinks = sum(member.issym() or member.islnk() for member in members)
        return len(members), sum(member.size for member in members), symlinks


def archive_path_for(archives_dir: Path, platform_key: str, archive_format: str) -> Path:
    suffix = ".zip" if archive_format == "zip" else ".tar.gz"
    return archives_dir / f"docling-{platform_key}{suffix}"


def verify_packs(manifest_path: Path, archives_dir: Path) -> None:
    manifest = load_engine_manifest(manifest_path)
    if not manifest.supported_platforms:
        raise RuntimeError(f"No platform packs found in {manifest_path}")

    with tempfile.TemporaryDirectory(prefix="inkdoc-engine-pack-verify-") as temp_dir:
        previous_base = os.environ.get("INKDOC_ENGINES_DIR")
        try:
            for platform_key, pack in sorted(manifest.supported_platforms.items()):
                # Keep each platform isolated so stale files cannot mask an error.
                os.environ["INKDOC_ENGINES_DIR"] = str(Path(temp_dir) / platform_key / "engines")
                archive_path = archive_path_for(archives_dir, platform_key, pack.archive_format)
                if not archive_path.is_file():
                    raise FileNotFoundError(f"Missing {platform_key} pack: {archive_path}")

                actual_sha256 = sha256_file(archive_path)
                if actual_sha256.lower() != pack.sha256.lower():
                    raise RuntimeError(
                        f"{platform_key}: archive SHA-256 mismatch; expected {pack.sha256}, got {actual_sha256}"
                    )

                entries, uncompressed_bytes, symlinks = inspect_archive(archive_path, pack.archive_format)
                print(
                    f"[PACK] {platform_key}: entries={entries}, "
                    f"uncompressed_bytes={uncompressed_bytes}, symlinks={symlinks}"
                )

                manager = EngineManager(manifest)
                manager.platform_key = platform_key
                target_dir = manager.get_engine_dir("docling")
                if pack.archive_format == "zip":
                    manager._extract_zip_safely(archive_path, target_dir)
                else:
                    manager._extract_tar_safely(archive_path, target_dir)

                if not manager.verify_launch_integrity("docling"):
                    raise RuntimeError(f"{platform_key}: EngineManager.verify_launch_integrity() failed")
                print(f"[PASS] {platform_key}: EngineManager extraction and launch integrity passed.")
        finally:
            if previous_base is None:
                os.environ.pop("INKDOC_ENGINES_DIR", None)
            else:
                os.environ["INKDOC_ENGINES_DIR"] = previous_base


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify release packs with InkDoc's production extraction path.")
    parser.add_argument("--manifest", type=Path, required=True, help="Merged pack manifest.")
    parser.add_argument("--archives-dir", type=Path, required=True, help="Directory containing release pack archives.")
    args = parser.parse_args()
    verify_packs(args.manifest, args.archives_dir)


if __name__ == "__main__":
    main()
