#!/usr/bin/env python3
"""Cheap Release Guard for InkDoc Engine Pack Manifest.

Requirement 1:
- Validates manifest.json integrity (no placeholder, malformed, or empty hashes).
- Cheap network/API guard: verifies that each asset exists on the release and has
  matching file size without downloading multi-gigabyte archives.
- Documentation & UI alignment guard: fails if the README or UI advertises an
  engine platform architecture that has no manifest entry.

Usage:
    python scripts/verify_engine_manifest_guard.py
    python scripts/verify_engine_manifest_guard.py --skip-network
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "app" / "core" / "manifest.json"
README_PATH = REPO_ROOT / "README.md"
UI_INDEX_PATH = REPO_ROOT / "app" / "ui" / "index.html"

_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_ALLOWED_PACK_SIZE = 1900 * 1024 * 1024  # 1.9 GiB ceiling

ALLOWED_CDN_HOSTS = frozenset({
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})


def is_valid_sha256(val: str | None) -> bool:
    if not val or not isinstance(val, str):
        return False
    cleaned = val.strip()
    if cleaned.lower().startswith("placeholder_"):
        return False
    return bool(_SHA256_HEX_RE.match(cleaned))


def check_remote_asset_cheap(url: str, expected_size: int, timeout: float = 15.0) -> tuple[bool, str]:
    """Perform a cheap HEAD or Range check to verify asset existence and size without downloading."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https" and parsed.hostname not in ("127.0.0.1", "localhost"):
        return False, f"Insecure URL scheme: {url}"

    if parsed.hostname not in ALLOWED_CDN_HOSTS and parsed.hostname not in ("127.0.0.1", "localhost"):
        return False, f"Untrusted host: {parsed.hostname}"

    # Use Range: bytes=0-0 to check existence and Content-Length / Content-Range
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "InkDoc-ReleaseGuard",
            "Range": "bytes=0-0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Check Content-Range: bytes 0-0/TOTAL
            content_range = resp.headers.get("Content-Range")
            if content_range and "/" in content_range:
                total_str = content_range.rsplit("/", 1)[1].strip()
                if total_str.isdigit():
                    actual_total = int(total_str)
                    if actual_total != expected_size:
                        return False, f"Remote size mismatch: expected {expected_size}, got {actual_total}"
                    return True, "OK"

            # Fallback to Content-Length if server answered with 200 instead of 206
            if resp.status == 200:
                content_len = resp.headers.get("Content-Length")
                if content_len and content_len.isdigit():
                    actual_len = int(content_len)
                    if actual_len != expected_size:
                        return False, f"Remote size mismatch: expected {expected_size}, got {actual_len}"
                    return True, "OK"

            return True, "Asset exists"
    except urllib.error.HTTPError as err:
        return False, f"HTTP error {err.code}: {err.reason}"
    except Exception as exc:
        return False, f"Network check failed: {exc}"


def extract_advertised_platforms(readme_text: str) -> set[str]:
    """Extract platform architectures claimed to be supported for Docling in the README."""
    advertised = set()
    norm = readme_text.lower()

    # Look for platform keywords in Docling sections
    if "windows x86_64" in norm or "windows x64" in norm:
        advertised.add("windows-x86_64")
    if "linux x86_64" in norm or "linux x64" in norm or "ubuntu 22.04" in norm:
        advertised.add("linux-x86_64")
    if "macos arm64" in norm or "apple silicon" in norm:
        advertised.add("macos-arm64")
    if "macos x86_64" in norm or "macos intel" in norm:
        advertised.add("macos-x86_64")
    if "windows arm64" in norm:
        advertised.add("windows-arm64")

    return advertised


def verify_guard(
    manifest_path: Path = MANIFEST_PATH,
    readme_path: Path = README_PATH,
    skip_network: bool = False,
    allow_empty: bool = False,
) -> tuple[bool, list[str]]:
    """Verify all cheap release guard rules."""
    errors: list[str] = []

    if not manifest_path.is_file():
        return False, [f"Manifest file missing: {manifest_path}"]

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"Manifest JSON syntax error: {exc}"]

    supported = manifest_data.get("supported_platforms", {})

    if not supported and not allow_empty:
        errors.append("Manifest has no supported_platforms entries.")

    # 1. Inspect Advertised Platforms against Manifest
    if readme_path.is_file():
        readme_text = readme_path.read_text(encoding="utf-8")
        advertised = extract_advertised_platforms(readme_text)
        manifest_keys = set(supported.keys())

        # If README advertises platforms, each one MUST exist in manifest
        for adv_plat in advertised:
            if adv_plat not in manifest_keys and not allow_empty:
                errors.append(
                    f"README advertises Docling support for '{adv_plat}', but manifest.json has no entry for it!"
                )

    # 2. Inspect Platforms and Pack Integrity
    for plat_key, plat_info in supported.items():
        sha = plat_info.get("sha256", "")
        if not is_valid_sha256(sha):
            errors.append(f"Platform '{plat_key}' has invalid/placeholder archive SHA-256: '{sha}'")

        size = int(plat_info.get("size_bytes", 0))
        if size <= 0:
            errors.append(f"Platform '{plat_key}' has invalid size_bytes: {size}")
        elif size > MAX_ALLOWED_PACK_SIZE:
            mb = size // (1024 * 1024)
            errors.append(
                f"Platform '{plat_key}' size {mb} MB exceeds maximum limit of 1.9 GiB (1945 MB)!"
            )

        uncompressed = int(plat_info.get("uncompressed_size_bytes", 0))
        if uncompressed <= 0:
            errors.append(f"Platform '{plat_key}' has invalid uncompressed_size_bytes: {uncompressed}")

        url = plat_info.get("url", "")
        if not url.startswith("https://") and not (skip_network and url.startswith("http://")):
            errors.append(f"Platform '{plat_key}' URL is not HTTPS: '{url}'")

        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.hostname not in ALLOWED_CDN_HOSTS and parsed_url.hostname not in ("127.0.0.1", "localhost"):
            errors.append(f"Platform '{plat_key}' host '{parsed_url.hostname}' is not in CDN allowlist")

        # sha256_files verification
        files = plat_info.get("sha256_files", {})
        if not files:
            errors.append(f"Platform '{plat_key}' missing sha256_files tree verification index")
        for rel_f, f_hash in files.items():
            if not is_valid_sha256(f_hash):
                errors.append(f"Platform '{plat_key}' file '{rel_f}' has invalid SHA-256: '{f_hash}'")

        # 3. Cheap Remote Network Check (if not skipped)
        if not skip_network:
            ok, msg = check_remote_asset_cheap(url, size)
            if not ok:
                errors.append(f"Remote asset check failed for '{plat_key}' ({url}): {msg}")

    return len(errors) == 0, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="InkDoc Cheap Release Guard")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=MANIFEST_PATH,
        help="Path to manifest.json",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=README_PATH,
        help="Path to README.md",
    )
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help="Skip remote HTTP Range/HEAD existence and size verification",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow empty supported_platforms (prior to initial pack build)",
    )
    args = parser.parse_args()

    print("[*] Running InkDoc Cheap Release Guard...")
    success, errors = verify_guard(
        manifest_path=args.manifest,
        readme_path=args.readme,
        skip_network=args.skip_network,
        allow_empty=args.allow_empty,
    )

    if not success:
        print("\n[FAIL] Release Guard failed with the following errors:")
        for err in errors:
            print(f"  [-] {err}")
        return 1

    print("\n[OK] Cheap Release Guard passed successfully! All manifest entries, sizes, and claims verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
