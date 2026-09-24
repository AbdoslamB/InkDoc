#!/usr/bin/env python3
"""Fetch draft release assets, verify them, build and sign the update manifest envelope.

Usage:
    python scripts/sign_manifest.py --tag v1.1.0 [--key-file ~/.inkdoc-keys/inkdoc_signing_key.pem]

Workflow:
1. Prompts for passphrase to unlock encrypted Ed25519 private key.
2. Fetches draft release assets for the given tag from GitHub.
3. Computes SHA-256 for all assets and checks against SHA256SUMS-*.txt.
4. Constructs manifest payload and signs base64 payload bytes using Ed25519.
5. Encapsulates in single-file JSON envelope and uploads to the draft release.

Note: this script does NOT run `gh attestation verify`. Verify build provenance
separately before signing (see docs/RELEASE_RUNBOOK.md).
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

DEFAULT_KEY_PATH = Path.home() / ".inkdoc-keys" / "inkdoc_signing_key.pem"


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


def build_manifest_payload(
    version: str,
    issued_at: str,
    release_url: str,
    release_notes: str,
    assets: dict[str, Any],
    min_supported_version: str = "1.0.0",
) -> dict[str, Any]:
    """Build the inner manifest payload that UpdateManager consumes.

    Field names matter and are not free choices: app/core/update_manager.py reads
    `notes` and `html_url`. This payload previously emitted `release_notes` and
    `release_url`, so the first genuinely signed release would have shown an empty
    release-notes panel and no link to the release, silently -- both are read with
    .get() and default to empty. Nothing caught it because the tests built their own
    manifests by hand instead of using the signer's output.
    """
    return {
        "manifest_version": "1.0.0",
        "version": version,
        "min_supported_version": min_supported_version,
        "issued_at": issued_at,
        "html_url": release_url,
        "notes": release_notes,
        "assets": assets,
    }


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
        default=DEFAULT_KEY_PATH,
        help=f"Path to encrypted Ed25519 private key (default: {DEFAULT_KEY_PATH})",
    )
    parser.add_argument("--repo", default="AbdoslamB/InkDoc", help="GitHub repository (owner/repo)")
    parser.add_argument("--dry-run", action="store_true", help="Build and sign envelope locally without uploading")
    parser.add_argument("--out-manifest", type=Path, default=None, help="Save generated envelope to this path")

    args = parser.parse_args()
    clean_version = args.tag.lstrip("vV")

    # 1. Load private key
    key_file = args.key_file.expanduser()
    try:
        private_key = load_private_key(key_file)
    except Exception as exc:
        print(f"[Error] Failed to load private key from {key_file}: {exc}", file=sys.stderr)
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

    def canonical_url(filename: str) -> str:
        """Where the asset will live once the release is published.

        Deliberately not the URL the API reports now. While the release is a
        draft that is .../releases/download/untagged-<slug>/<file>, and the slug
        stops resolving the moment the draft is published under its tag --
        signing it would ship dead download links to every client.
        """
        return f"https://github.com/{args.repo}/releases/download/{args.tag}/{filename}"

    try:
        wanted = [
            a["name"] for a in assets
            if a["name"] in TARGET_ASSETS or a["name"].startswith("SHA256SUMS")
        ]
        if not wanted:
            print("[Error] Release has none of the expected assets.", file=sys.stderr)
            return 1

        print("[*] Downloading and verifying release assets...")
        for name in wanted:
            print(f"  -> Fetching {name}...")

        if gh_available:
            # gh authenticates. Draft assets require it: their public download
            # URL returns 404 until the release is published, so urlretrieve --
            # which sends no credentials -- cannot fetch them.
            patterns: list[str] = []
            for name in wanted:
                patterns += ["--pattern", name]
            subprocess.run(
                ["gh", "release", "download", args.tag, "--repo", args.repo,
                 "--dir", str(temp_dir), "--clobber", *patterns],
                check=True,
            )
        else:
            # The REST fallback does expose browser_download_url, but it can only
            # reach a published release.
            for asset in assets:
                if asset["name"] not in wanted:
                    continue
                src = asset.get("browser_download_url")
                if not src:
                    print(
                        f"[Error] No download URL for {asset['name']} and the gh CLI "
                        "is unavailable. Draft assets need gh to authenticate.",
                        file=sys.stderr,
                    )
                    return 1
                urllib.request.urlretrieve(src, temp_dir / asset["name"])

        for name in wanted:
            dest = temp_dir / name
            if not dest.is_file():
                print(f"[Error] {name} was not downloaded.", file=sys.stderr)
                return 1

            if name in TARGET_ASSETS:
                manifest_assets[TARGET_ASSETS[name]] = {
                    "filename": name,
                    "url": canonical_url(name),
                    "sha256": compute_sha256(dest),
                    "size_bytes": dest.stat().st_size,
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
        manifest_payload = build_manifest_payload(
            version=clean_version,
            issued_at=now_iso,
            # Same reason as canonical_url: for a draft, release_info["url"] is
            # the untagged-<slug> page, which disappears on publish.
            release_url=f"https://github.com/{args.repo}/releases/tag/{args.tag}",
            release_notes=release_info.get("body") or "",
            assets=manifest_assets,
        )

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
