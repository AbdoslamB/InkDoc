#!/usr/bin/env python3
"""Merge per-platform engine manifest fragments into unified manifest.json.

Requirement 10: The pack workflow publishes a per-platform manifest fragment;
this script merges fragments from CI build jobs into a validated, unified manifest.json.

Usage:
    python scripts/merge_manifest_fragments.py dist/fragments --output app/core/manifest.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_PATH = REPO_ROOT / "app" / "core" / "manifest.json"

_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def is_valid_sha256(val: str | None) -> bool:
    """Return True if val is a valid 64-character hex SHA-256 string."""
    if not val or not isinstance(val, str):
        return False
    cleaned = val.strip()
    if cleaned.lower().startswith("placeholder_"):
        return False
    return bool(_SHA256_HEX_RE.match(cleaned))


def merge_fragments(
    fragments_dir: Path,
    output_path: Path,
    pack_version: str | None = None,
    min_app_version: str | None = None,
    expected_platforms: list[str] | None = None,
) -> dict[str, Any]:
    """Discover, validate, and merge platform fragments into unified manifest."""
    if not fragments_dir.is_dir():
        raise FileNotFoundError(f"Fragments directory not found: {fragments_dir}")

    fragment_files = sorted(
        list(fragments_dir.glob("*.fragment.json")) +
        [f for f in fragments_dir.glob("*.json") if not f.name.endswith(".fragment.json") and f.name != "manifest.json"]
    )
    if not fragment_files:
        raise ValueError(f"No manifest fragment files (*.json) found in {fragments_dir}")

    merged_platforms: dict[str, Any] = {}
    detected_pack_version: str | None = pack_version
    detected_min_app_version: str | None = min_app_version

    for frag_file in fragment_files:
        try:
            data = json.loads(frag_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Failed to parse JSON fragment '{frag_file.name}': {exc}") from exc

        # Extract platform entries
        supported = data.get("supported_platforms")
        if not supported and "pack_info" in data and "platform" in data:
            supported = {data["platform"]: data["pack_info"]}

        if not supported:
            print(f"[-] Skipping {frag_file.name}: no platform data found.")
            continue

        if not detected_pack_version and "pack_version" in data:
            detected_pack_version = data["pack_version"]
        if not detected_min_app_version and "min_app_version" in data:
            detected_min_app_version = data["min_app_version"]

        for plat_key, pack_info in supported.items():
            # Validate pack_info integrity
            sha = pack_info.get("sha256", "")
            if not is_valid_sha256(sha):
                raise ValueError(
                    f"Fragment '{frag_file.name}' has invalid SHA-256 for platform '{plat_key}': '{sha}'"
                )
            if not pack_info.get("url") or not pack_info["url"].startswith("https://"):
                raise ValueError(
                    f"Fragment '{frag_file.name}' has invalid URL for platform '{plat_key}': {pack_info.get('url')}"
                )
            if int(pack_info.get("size_bytes", 0)) <= 0:
                raise ValueError(
                    f"Fragment '{frag_file.name}' has non-positive size_bytes for platform '{plat_key}'"
                )

            # Check sha256_files if present
            files = pack_info.get("sha256_files", {})
            for rel_file, f_hash in files.items():
                if not is_valid_sha256(f_hash):
                    raise ValueError(
                        f"Fragment '{frag_file.name}' contains invalid tree hash for '{rel_file}': '{f_hash}'"
                    )

            merged_platforms[plat_key] = pack_info
            print(f"[+] Merged platform entry: '{plat_key}' from {frag_file.name}")

    if not merged_platforms:
        raise ValueError("No valid platform entries could be merged from fragments.")

    if expected_platforms:
        missing = [p for p in expected_platforms if p not in merged_platforms]
        if missing:
            raise ValueError(f"Missing expected platforms in merged manifest: {missing}")

    final_manifest = {
        "manifest_version": "1.0.0",
        "pack_version": detected_pack_version or "1.0.0",
        "min_app_version": detected_min_app_version or "1.0.0",
        "supported_platforms": merged_platforms,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(final_manifest, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] Successfully wrote unified manifest with {len(merged_platforms)} platforms to: {output_path}")
    return final_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge per-platform manifest fragments into manifest.json")
    parser.add_argument(
        "fragments_dir",
        type=Path,
        help="Directory containing per-platform fragment JSON files",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Path to output manifest.json",
    )
    parser.add_argument(
        "--pack-version",
        type=str,
        help="Override pack version",
    )
    parser.add_argument(
        "--min-app-version",
        type=str,
        help="Override minimum app version",
    )
    parser.add_argument(
        "--require-platform",
        action="append",
        dest="expected_platforms",
        help="Platform key required to be present in final manifest (e.g. windows-x86_64)",
    )
    args = parser.parse_args()

    try:
        merge_fragments(
            fragments_dir=args.fragments_dir,
            output_path=args.output,
            pack_version=args.pack_version,
            min_app_version=args.min_app_version,
            expected_platforms=args.expected_platforms,
        )
        return 0
    except Exception as exc:
        print(f"[FAIL] Merge error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
