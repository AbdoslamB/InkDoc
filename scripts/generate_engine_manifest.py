#!/usr/bin/env python3
"""Generate, update, and validate InkDoc Engine Pack Manifests.

Computes cryptographic SHA-256 digests for engine pack archives and their
contained file trees. Enforces fail-closed verification: placeholder, empty,
or malformed hashes are strictly rejected.

Usage:
    # 1. Verify that manifest.json contains NO placeholders or malformed hashes (CI / Release guard):
    python scripts/generate_engine_manifest.py --verify-manifest

    # 2. Compute hashes from a built pack archive and print manifest entry:
    python scripts/generate_engine_manifest.py --archive dist/packs/docling-windows-x86_64.zip --platform windows-x86_64

    # 3. Compute hashes and update app/core/manifest.json:
    python scripts/generate_engine_manifest.py --archive dist/packs/docling-windows-x86_64.zip --platform windows-x86_64 --update-manifest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST_PATH = REPO_ROOT / "app" / "core" / "manifest.json"

_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def is_valid_sha256(val: str | None) -> bool:
    """Return True if val is a valid 64-character hex SHA-256 string."""
    if not val or not isinstance(val, str):
        return False
    cleaned = val.strip()
    if cleaned.lower().startswith("placeholder_"):
        return False
    return bool(_SHA256_HEX_RE.match(cleaned))


def compute_file_sha256(file_path: Path) -> str:
    """Compute SHA-256 hex digest of a file using 64KB streaming chunks."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def compute_tree_hashes(root_dir: Path) -> dict[str, str]:
    """Compute relative path -> sha256 mapping for all files under root_dir."""
    tree: dict[str, str] = {}
    for p in root_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root_dir).as_posix()
            tree[rel] = compute_file_sha256(p)
    return tree


def inspect_archive(
    archive_path: Path,
    platform_key: str,
    base_url: str = "https://github.com/AbdoslamB/InkDoc/releases/download",
    pack_version: str = "1.0.0",
    release_tag: str = "docling-pack-v3",
) -> dict[str, Any]:
    """Extract archive to temporary directory, compute full tree hashes and metadata."""
    if not archive_path.is_file():
        raise FileNotFoundError(f"Archive not found: {archive_path}")

    archive_sha256 = compute_file_sha256(archive_path)
    archive_size = archive_path.stat().st_size

    is_zip = archive_path.name.endswith(".zip")
    archive_format = "zip" if is_zip else "tar.gz"

    with tempfile.TemporaryDirectory() as tmp_dir:
        extract_dir = Path(tmp_dir)
        if is_zip:
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(extract_dir)
        else:
            with tarfile.open(archive_path, "r:*") as tf:
                tf.extractall(extract_dir)

        sha256_files = compute_tree_hashes(extract_dir)
        uncompressed_size = sum(p.stat().st_size for p in extract_dir.rglob("*") if p.is_file())

        # Determine interpreter and worker relative paths
        if platform_key.startswith("windows"):
            interp_candidates = ["env/Scripts/python.exe", "python/python.exe", "python.exe"]
        else:
            interp_candidates = ["env/bin/python", "python/bin/python3", "bin/python"]

        interp_path = interp_candidates[0]
        for c in interp_candidates:
            if (extract_dir / c).is_file():
                interp_path = c
                break

        worker_path = "worker.py"

    pack_info = {
        "url": f"{base_url}/{release_tag}/{archive_path.name}",
        "archive_format": archive_format,
        "sha256": archive_sha256,
        "size_bytes": archive_size,
        "uncompressed_size_bytes": uncompressed_size,
        "interpreter_path": interp_path,
        "worker_script": worker_path,
        "sha256_files": sha256_files,
    }
    return pack_info


def validate_manifest(manifest_path: Path) -> tuple[bool, list[str]]:
    """Validate that manifest has no placeholders, empty, or malformed hashes.

    Acts as a fail-closed release guard.
    """
    errors: list[str] = []
    if not manifest_path.is_file():
        return False, [f"Manifest file not found: {manifest_path}"]

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"Manifest JSON syntax error: {exc}"]

    supported = data.get("supported_platforms", {})

    # An empty manifest must fail, not pass vacuously.
    #
    # This loop reports per-platform problems, so zero platforms produced zero
    # errors and the guard returned success. v1.0.2 shipped a 120-byte manifest
    # with "supported_platforms": {} through this check and through CI, and every
    # user saw Docling as NOT AVAILABLE: engine_manifest.get_platform_pack()
    # returns None for every host, so is_platform_supported() is False everywhere.
    # The guard existed precisely to catch that and reported PASS.
    if not supported:
        return False, [
            "Manifest has no supported_platforms entries. A manifest without platforms "
            "makes Docling report UNSUPPORTED on every host. Refusing to pass it."
        ]

    for plat_key, plat_info in supported.items():
        # 1. Main archive sha256
        sha256 = plat_info.get("sha256", "")
        if not is_valid_sha256(sha256):
            errors.append(
                f"Platform '{plat_key}' has invalid/placeholder/missing archive sha256: {sha256!r}"
            )

        # 2. URL must be valid
        url = plat_info.get("url", "")
        if not url or not url.startswith("https://"):
            errors.append(f"Platform '{plat_key}' has invalid URL: {url!r}")

        # 3. sha256_files tree
        sha256_files = plat_info.get("sha256_files", {})
        for file_rel, f_hash in sha256_files.items():
            if not is_valid_sha256(f_hash):
                errors.append(
                    f"Platform '{plat_key}' file '{file_rel}' has invalid/placeholder sha256: {f_hash!r}"
                )

    return len(errors) == 0, errors


def update_manifest_file(manifest_path: Path, platform_key: str, pack_info: dict[str, Any]) -> None:
    """Safely update a platform entry in manifest.json."""
    if manifest_path.is_file():
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest_data = {
            "manifest_version": "1.0.0",
            "pack_version": "1.0.0",
            "min_app_version": "1.0.0",
            "supported_platforms": {},
        }

    manifest_data.setdefault("supported_platforms", {})[platform_key] = pack_info
    manifest_path.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
    print(f"[+] Successfully updated manifest at: {manifest_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="InkDoc Engine Manifest Tool & Release Guard")
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Path to manifest.json",
    )
    parser.add_argument(
        "--verify-manifest",
        action="store_true",
        help="Validate that manifest.json contains NO placeholders or malformed hashes (Release Guard)",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="Path to built pack archive (.zip or .tar.gz)",
    )
    parser.add_argument(
        "--platform",
        type=str,
        help="Platform key (e.g. windows-x86_64, linux-x86_64, macos-arm64)",
    )
    parser.add_argument(
        "--pack-version",
        type=str,
        default="1.0.0",
        help="Pack version string",
    )
    parser.add_argument(
        "--min-app-version",
        type=str,
        default="1.0.0",
        help="Minimum required InkDoc application version",
    )
    parser.add_argument(
        "--release-tag",
        type=str,
        default="docling-pack-v3",
        help="Dedicated release tag for pack assets (default: docling-pack-v3)",
    )
    parser.add_argument(
        "--output-fragment",
        type=Path,
        help="Path to write single platform manifest fragment JSON file",
    )
    parser.add_argument(
        "--update-manifest",
        action="store_true",
        help="Update manifest.json directly with computed hashes",
    )
    args = parser.parse_args()

    if args.verify_manifest:
        valid, errors = validate_manifest(args.manifest_path)
        if not valid:
            print("[FAIL] Manifest release guard failed! The following integrity errors were found:")
            for err in errors:
                print(f"  - {err}")
            return 1
        print(f"[PASS] Manifest at {args.manifest_path} is valid and contains zero placeholder or malformed hashes.")
        return 0

    if args.archive:
        if not args.platform:
            print("[Error] --platform is required when specifying --archive", file=sys.stderr)
            return 1

        print(f"[*] Inspecting archive: {args.archive} for platform {args.platform}...")
        pack_info = inspect_archive(
            args.archive,
            args.platform,
            pack_version=args.pack_version,
            release_tag=args.release_tag,
        )
        print(f"[+] Archive SHA-256: {pack_info['sha256']}")
        print(f"[+] Indexed {len(pack_info['sha256_files'])} files in tree.")

        if args.output_fragment:
            fragment_data = {
                "manifest_version": "1.0.0",
                "pack_version": args.pack_version,
                "min_app_version": args.min_app_version,
                "release_tag": args.release_tag,
                "platform": args.platform,
                "supported_platforms": {
                    args.platform: pack_info
                }
            }
            args.output_fragment.parent.mkdir(parents=True, exist_ok=True)
            args.output_fragment.write_text(json.dumps(fragment_data, indent=2) + "\n", encoding="utf-8")
            print(f"[+] Saved platform manifest fragment to: {args.output_fragment}")

        if args.update_manifest:
            update_manifest_file(args.manifest_path, args.platform, pack_info)
        elif not args.output_fragment:
            print("\nGenerated platform entry:")
            print(json.dumps({args.platform: pack_info}, indent=2))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
