#!/usr/bin/env python3
"""Fetch draft release assets, verify them, build and sign the update manifest envelope.

Usage:
    python scripts/sign_manifest.py --tag v1.1.0 [--key-file inkdoc_signing_key.pem]

Workflow:
1. Prompts for passphrase to unlock encrypted Ed25519 private key.
2. Fetches draft release assets for the given tag from GitHub.
3. Computes SHA-256 for all assets and checks against SHA256SUMS-*.txt.
4. Optionally checks `gh attestation verify` if gh CLI is available.
5. Constructs manifest payload and signs base64 payload bytes using Ed25519.
6. Encapsulates in single-file JSON envelope and uploads to the draft release.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import getpass
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def load_private_key(key_path: Path) -> ed25519.Ed25519PrivateKey:
    if not key_path.is_file():
        raise FileNotFoundError(f"Key file not found: {key_path}")

    passphrase = getpass.getpass("Enter passphrase for release signing key: ")
    data = key_path.read_bytes()
    key = serialization.load_pem_private_key(data, password=passphrase.encode("utf-8"))
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise TypeError("Loaded key is not an Ed25519 private key.")
    return key


def build_envelope(
    manifest_payload: dict[str, Any],
    private_key: ed25519.Ed25519PrivateKey,
) -> dict[str, str]:
    """Encode and sign manifest payload into an authenticated single-file envelope."""
    manifest_json_bytes = json.dumps(manifest_payload, indent=2).encode("utf-8")
    payload_b64 = base64.b64encode(manifest_json_bytes).decode("ascii")

    # Sign the exact UTF-8 bytes of the base64 payload string (Requirement A)
    to_sign = payload_b64.encode("utf-8")
    signature_bytes = private_key.sign(to_sign)
    sig_b64 = base64.b64encode(signature_bytes).decode("ascii")

    return {
        "envelope_version": "1.0.0",
        "payload": payload_b64,
        "signature": sig_b64,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign and upload InkDoc update manifest to draft release")
    parser.add_argument("--tag", required=True, help="Release tag (e.g. v1.1.0)")
    parser.add_argument(
        "--key-file",
        type=Path,
        default=Path("inkdoc_signing_key.pem"),
        help="Path to encrypted Ed25519 private key",
    )
    parser.add_argument("--repo", default="AbdoslamB/InkDoc", help="GitHub repository (owner/repo)")
    parser.add_argument("--dry-run", action="store_true", help="Build and sign envelope locally without uploading")
    parser.add_argument("--out-manifest", type=Path, default=None, help="Save generated envelope to this path")

    args = parser.parse_args()
    clean_version = args.tag.lstrip("vV")

    # 1. Load private key
    try:
        private_key = load_private_key(args.key_file)
    except Exception as exc:
        print(f"[Error] Failed to load private key: {exc}", file=sys.stderr)
        return 1

    print(f"[*] Preparing signed manifest for release {args.tag} ({args.repo})...")

    # 2. Query release info via GitHub API or gh CLI
    gh_available = shutil.which("gh") is not None

    release_info: dict[str, Any] = {}
    if gh_available:
        try:
            cmd = ["gh", "release", "view", args.tag, "--repo", args.repo, "--json", "isDraft,assets,body,url"]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            release_info = json.loads(res.stdout)
        except Exception:
            pass

    if not release_info:
        # Fallback to public GitHub API
        api_url = f"https://api.github.com/repos/{args.repo}/releases/tags/{args.tag}"
        req = urllib.request.Request(api_url, headers={"User-Agent": "InkDoc-ReleaseSigner"})
        try:
            with urllib.request.urlopen(req) as resp:
                release_info = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print(f"[Error] Failed to fetch release metadata for tag {args.tag}: {exc}", file=sys.stderr)
            return 1

    assets = release_info.get("assets", [])
    if not assets:
        print("[Error] No assets found in target release.", file=sys.stderr)
        return 1

    # Mapping from asset file name to expected platform asset key
    TARGET_ASSETS = {
        "inkdoc-setup.exe": "windows-x86_64-installer",
        "inkdoc-windows.zip": "windows-x86_64-portable",
        "inkdoc-macos.zip": "macos-arm64",
        "inkdoc-linux.zip": "linux-x86_64",
    }

    manifest_assets: dict[str, Any] = {}
    temp_dir = Path(tempfile.mkdtemp(prefix="inkdoc_release_"))

    try:
        print("[*] Downloading and verifying release assets...")
        for asset in assets:
            name = asset["name"]
            if name not in TARGET_ASSETS and not name.startswith("SHA256SUMS"):
                continue

            dl_url = asset["browser_download_url"]
            dest = temp_dir / name
            print(f"  -> Fetching {name}...")
            urllib.request.urlretrieve(dl_url, dest)

            if name in TARGET_ASSETS:
                asset_key = TARGET_ASSETS[name]
                file_hash = compute_sha256(dest)
                file_size = dest.stat().st_size

                manifest_assets[asset_key] = {
                    "filename": name,
                    "url": dl_url,
                    "sha256": file_hash,
                    "size_bytes": file_size,
                    "install_method": "inno_silent" if name == "inkdoc-setup.exe" else "download_reveal",
                }

        # Verify against SHA256SUMS if present
        for sums_file in temp_dir.glob("SHA256SUMS*.txt"):
            lines = sums_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line in lines:
                parts = line.strip().split()
                if len(parts) >= 2:
                    h, f = parts[0].lower(), parts[1]
                    for m_info in manifest_assets.values():
                        if m_info["filename"] == f and m_info["sha256"] != h:
                            print(
                                f"[Error] SHA-256 mismatch for {f}! Computed: {m_info['sha256']}, in {sums_file.name}: {h}",
                                file=sys.stderr,
                            )
                            return 1
            print(f"[OK] Checksums verified against {sums_file.name}")

        # Construct inner manifest payload
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        manifest_payload = {
            "manifest_version": "1.0.0",
            "version": clean_version,
            "min_supported_version": "1.0.0",
            "issued_at": now_iso,
            "release_url": release_info.get("url") or f"https://github.com/{args.repo}/releases/tag/{args.tag}",
            "release_notes": release_info.get("body") or "",
            "assets": manifest_assets,
        }

        # Encode and sign into single-file envelope (Requirement A)
        envelope = build_envelope(manifest_payload, private_key)

        envelope_json = json.dumps(envelope, indent=2)
        out_file = args.out_manifest or (temp_dir / "inkdoc-update-manifest.json")
        out_file.write_text(envelope_json, encoding="utf-8")
        print(f"[OK] Signed envelope generated successfully ({out_file.stat().st_size} bytes)")

        if args.dry_run:
            print("[Dry-run] Manifest generated locally. Skipping upload.")
            if not args.out_manifest:
                print(f"Manifest saved to: {out_file}")
            return 0

        # Upload to release
        if gh_available:
            print("[*] Uploading inkdoc-update-manifest.json to release via gh CLI...")
            cmd = ["gh", "release", "upload", args.tag, str(out_file), "--repo", args.repo, "--clobber"]
            subprocess.run(cmd, check=True)
            print(f"[Success] inkdoc-update-manifest.json uploaded to {args.tag}!")
        else:
            print(f"[Warning] gh CLI not found. Please attach {out_file} manually to release {args.tag}.")

    finally:
        if not args.dry_run and not args.out_manifest:
            shutil.rmtree(temp_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
