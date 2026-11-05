#!/usr/bin/env python3
"""Fetch draft release assets, verify them, build and sign the update manifest envelope.

Usage:
    python scripts/sign_manifest.py --tag v1.1.0 [--key-file ~/.inkdoc-keys/inkdoc_signing_key.pem] [--publish]

Workflow:
1. Prompts for passphrase to unlock encrypted Ed25519 private key.
2. Fetches draft release assets for the given tag from GitHub.
3. Computes SHA-256 for all assets and checks against SHA256SUMS-*.txt.
4. Constructs manifest payload and signs base64 payload bytes using Ed25519.
5. Checks the envelope as the app will: against the public keys the app embeds, with
   an asset for every platform.
6. Encapsulates in single-file JSON envelope and uploads to the draft release.

With --publish (the release runbook's one command) it also checks every asset's build
provenance (`gh attestation verify`) before signing, and after the upload reads the
manifest back from the draft, publishes the draft as Latest, and waits until the
updater's own address serves that exact manifest. The draft is published only once its
manifest is up, so no installed copy ever finds a Latest release without one. Without
--publish, build provenance is not checked: verify it separately before signing (see
docs/RELEASE_RUNBOOK.md).
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
import time
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

REPO_ROOT = Path(__file__).resolve().parent.parent
# Run as a script, Python puts scripts/ on sys.path, not the repo root, and some
# interpreters put neither (an embeddable Python's ._pth file, `python -I`): signing
# v1.3.0 failed there with "No module named 'changelog_notes'".
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.update_manager import OFFICIAL_UPDATE_MANIFEST_URL  # noqa: E402
from scripts.changelog_notes import changelog_section  # noqa: E402

DEFAULT_KEY_PATH = Path.home() / ".inkdoc-keys" / "inkdoc_signing_key.pem"

# Release asset file name -> the platform key the updater looks up. Every asset release.yml
# requires is here except inkdoc.exe, which has no update path (tests/test_release_assets.py).
TARGET_ASSETS = {
    "inkdoc-setup.exe": "windows-x86_64-installer",
    "inkdoc-windows.zip": "windows-x86_64-portable",
    "inkdoc-macos.dmg": "macos-arm64",
    "inkdoc-linux-x86_64.AppImage": "linux-x86_64",
}
# The oldest glibc each asset runs on: the build host's (release.yml's runner). Clients
# from 1.3.0 on refuse an update their system can't start; older ones ignore the field.
MIN_GLIBC = {"linux-x86_64": "2.39"}
# The asset the updater fetches, named by its address.
MANIFEST_ASSET = OFFICIAL_UPDATE_MANIFEST_URL.rsplit("/", 1)[1]
# How long --publish waits for the updater's address to serve the new manifest.
LATEST_WAIT_S = 300.0


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


def changelog_at_tag(tag: str) -> str | None:
    """CHANGELOG.md as committed at the tag, else the working tree's copy."""
    try:
        res = subprocess.run(
            ["git", "show", f"{tag}:CHANGELOG.md"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        )
        return res.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.CalledProcessError):
        pass
    try:
        return (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        return None


def pick_release_notes(version: str, changelog: str | None, release_body: str) -> tuple[str, str]:
    """The notes the app will show for this release, and where they came from.

    The CHANGELOG's section for the version, as on the release page release.yml
    creates. The release body only when the CHANGELOG has none: if GitHub
    generated it, it is mostly links, and Settings -> Updates can show nothing
    but the link to the release.
    """
    section = changelog_section(changelog, version) if changelog else None
    if section:
        return section, "CHANGELOG.md"
    return release_body or "", "release body"


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


def check_envelope(envelope_raw: bytes, version: str) -> dict[str, Any]:
    """The manifest inside the envelope, checked as the app will check it.

    Signed by a key the app embeds (a signature by any other key would reach every
    installed copy as "verification failed"), for this version, with an asset for
    every platform the updater looks up.
    """
    from app.core.update_verifier import verify_envelope_bytes

    manifest = verify_envelope_bytes(envelope_raw)
    if manifest.get("version") != version:
        raise ValueError(f"The manifest is for {manifest.get('version')}, not {version}.")
    missing = sorted(set(TARGET_ASSETS.values()) - set(manifest.get("assets", {})))
    if missing:
        raise ValueError(f"The manifest has no asset for: {', '.join(missing)}.")
    return manifest


def verify_attestations(paths: list[Path], repo: str, run=None) -> None:
    """`gh attestation verify` for each file; raises on the first that fails."""
    run = run or subprocess.run
    for path in paths:
        res = run(["gh", "attestation", "verify", str(path), "--repo", repo],
                  capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            raise RuntimeError(f"Build provenance did not verify for {path.name}:\n{res.stderr or res.stdout}")
        print(f"[OK] Build provenance verified: {path.name}")


def fetch_latest_manifest(url: str) -> bytes | None:
    """What the updater's address serves now; None for an HTTP error (404 before publishing)
    or a connection that failed, so a caller waiting for it tries again."""
    req = urllib.request.Request(url, headers={"User-Agent": "InkDoc-ReleaseSigner", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            return resp.read()
    except OSError:  # urllib's HTTP and connection errors included
        return None


def poll(attempt, *, timeout: float, interval: float, sleep=time.sleep, clock=time.monotonic):
    """Call `attempt` until it returns something other than None or `timeout` passes; its last result."""
    deadline = clock() + timeout
    while True:
        result = attempt()
        if result is not None or clock() >= deadline:
            return result
        sleep(interval)


def wait_for_latest(
    expected: bytes,
    url: str,
    timeout: float = LATEST_WAIT_S,
    fetch=fetch_latest_manifest,
    sleep=time.sleep,
    clock=time.monotonic,
) -> bool:
    """True once `url` serves exactly `expected`, as installed copies will fetch it."""
    served = poll(lambda: True if fetch(url) == expected else None, timeout=timeout, interval=10.0,
                  sleep=sleep, clock=clock)
    return served is True


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
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Also check build provenance, publish the draft as Latest once its manifest is up, "
             "and wait until the updater's address serves it",
    )

    args = parser.parse_args()
    clean_version = args.tag.lstrip("vV")
    if args.publish and args.dry_run:
        print("[Error] --publish and --dry-run exclude each other.", file=sys.stderr)
        return 1

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
            # gh writes UTF-8; Windows' default code page can't decode the notes' ☐ or →.
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
            release_info = json.loads(res.stdout)
        except Exception as exc:
            detail = getattr(exc, "stderr", None) or exc
            print(f"[Warning] gh could not read {args.tag} ({detail}); trying the public API, "
                  "which can't see a draft.", file=sys.stderr)

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

    if args.publish and not release_info.get("isDraft"):
        print(
            f"[Error] {args.tag} is already published. --publish signs a draft and then publishes it; "
            "run without --publish to replace the manifest of a published release.",
            file=sys.stderr,
        )
        return 1

    assets = release_info.get("assets", [])
    if not assets:
        print("[Error] No assets found in target release.", file=sys.stderr)
        return 1

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
                key = TARGET_ASSETS[name]
                manifest_assets[key] = {
                    "filename": name,
                    "url": canonical_url(name),
                    "sha256": compute_sha256(dest),
                    "size_bytes": dest.stat().st_size,
                    "install_method": "inno_silent" if name == "inkdoc-setup.exe" else "download_reveal",
                }
                if key in MIN_GLIBC:
                    manifest_assets[key]["min_glibc"] = MIN_GLIBC[key]

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

        if args.publish:
            try:
                verify_attestations([temp_dir / name for name in wanted], args.repo)
            except RuntimeError as exc:
                print(f"[Error] {exc}", file=sys.stderr)
                return 1

        release_notes, notes_source = pick_release_notes(
            clean_version, changelog_at_tag(args.tag), release_info.get("body") or "",
        )
        if notes_source == "CHANGELOG.md":
            print(f"[OK] Release notes: the CHANGELOG's [{clean_version}] section")
        else:
            print(
                f"[Warning] CHANGELOG.md has no [{clean_version}] section; signing the release "
                "body as the notes. The app shows them under Settings -> Updates.",
            )

        # Construct inner manifest payload
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        manifest_payload = build_manifest_payload(
            version=clean_version,
            issued_at=now_iso,
            # Same reason as canonical_url: for a draft, release_info["url"] is
            # the untagged-<slug> page, which disappears on publish.
            release_url=f"https://github.com/{args.repo}/releases/tag/{args.tag}",
            release_notes=release_notes,
            assets=manifest_assets,
        )

        # Encode and sign into single-file envelope (Requirement A)
        envelope = build_envelope(manifest_payload, private_key)

        envelope_json = json.dumps(envelope, indent=2)
        out_file = args.out_manifest or (temp_dir / MANIFEST_ASSET)
        out_file.write_text(envelope_json, encoding="utf-8")
        signed = out_file.read_bytes()
        print(f"[OK] Signed envelope generated successfully ({len(signed)} bytes)")

        try:
            check_envelope(signed, clean_version)
        except Exception as exc:
            print(f"[Error] The signed manifest would not pass the app's own check: {exc}", file=sys.stderr)
            return 1
        print("[OK] The manifest passes the app's own check (embedded keys, every platform)")

        if args.dry_run:
            print("[Dry-run] Manifest generated locally. Skipping upload.")
            if not args.out_manifest:
                print(f"Manifest saved to: {out_file}")
            return 0

        # Upload to release, under the name the updater fetches whatever --out-manifest
        # called the file (gh names an asset after the file it uploads).
        upload_file = temp_dir / MANIFEST_ASSET
        if upload_file != out_file:
            shutil.copyfile(out_file, upload_file)
        if gh_available:
            print(f"[*] Uploading {MANIFEST_ASSET} to release via gh CLI...")
            cmd = ["gh", "release", "upload", args.tag, str(upload_file), "--repo", args.repo, "--clobber"]
            subprocess.run(cmd, check=True)
            print(f"[Success] {MANIFEST_ASSET} uploaded to {args.tag}!")
        else:
            if args.publish:
                print("[Error] --publish needs the gh CLI.", file=sys.stderr)
                return 1
            print(f"[Warning] gh CLI not found. Please attach {out_file} manually to release {args.tag}.")
            return 0

        if not args.publish:
            print("[*] Not published. Publish the draft as Latest, or use --publish next time.")
            return 0

        # Read it back from the draft before anyone can download it.
        back_dir = temp_dir / "uploaded"
        back_dir.mkdir()
        subprocess.run(
            ["gh", "release", "download", args.tag, "--repo", args.repo, "--dir", str(back_dir),
             "--pattern", MANIFEST_ASSET],
            check=True,
        )
        uploaded = (back_dir / MANIFEST_ASSET).read_bytes()
        if uploaded != signed:
            print("[Error] The manifest on the draft differs from the one signed; not publishing.", file=sys.stderr)
            return 1
        print("[OK] The draft carries the signed manifest")

        subprocess.run(
            ["gh", "release", "edit", args.tag, "--repo", args.repo, "--draft=false", "--latest"],
            check=True,
        )
        print(f"[*] Published {args.tag} as Latest; waiting for the updater's address to serve it...")
        if not wait_for_latest(uploaded, OFFICIAL_UPDATE_MANIFEST_URL):
            print(
                f"[Error] {OFFICIAL_UPDATE_MANIFEST_URL} did not serve the new manifest within "
                f"{LATEST_WAIT_S:.0f}s. Check that {args.tag} is marked Latest on GitHub.",
                file=sys.stderr,
            )
            return 1
        print(f"[Success] Installed copies now find {clean_version}: {OFFICIAL_UPDATE_MANIFEST_URL}")

    finally:
        if not args.dry_run and not args.out_manifest:
            shutil.rmtree(temp_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
