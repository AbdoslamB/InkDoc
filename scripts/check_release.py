#!/usr/bin/env python3
"""Check that installed copies can find and download a published app release.

    python scripts/check_release.py --tag v1.3.1

Fails unless the updater's address (/releases/latest/download/inkdoc-update-manifest.json)
serves a manifest for this tag that passes the app's own check (embedded keys, every
platform), and every file it lists answers at the size it states. v1.3.0 was published
without its manifest, and every installed copy said "No signed update information has been
published" until it was uploaded by hand.

Run by .github/workflows/release-check.yml whenever a release is published. Tags other than
an app release's (vX.Y.Z: packs, add-ons, GLM-OCR mirrors, never Latest) are skipped.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.sign_manifest import check_envelope, fetch_latest_manifest, poll  # noqa: E402

APP_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
# Long enough for a manifest uploaded by hand right after publishing.
WAIT_S = 600.0


def latest_manifest(url: str, version: str, timeout: float = WAIT_S, fetch=fetch_latest_manifest,
                    sleep=time.sleep, clock=time.monotonic) -> dict[str, Any]:
    """The checked manifest the updater's address serves for `version`; raises when it never does."""
    problem = ""

    def attempt() -> dict[str, Any] | None:
        nonlocal problem
        raw = fetch(url)
        try:
            if raw is not None:
                return check_envelope(raw, version)
            problem = f"{url} serves no manifest (an HTTP error, or no connection)"
        except Exception as exc:
            problem = str(exc)
        print(f"[*] Not yet: {problem}")
        return None

    manifest = poll(attempt, timeout=timeout, interval=20.0, sleep=sleep, clock=clock)
    if manifest is None:
        raise ValueError(problem)
    return manifest


def asset_size(url: str) -> int | None:
    """The size the release's download URL answers with, following GitHub's redirects."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "InkDoc-ReleaseCheck"})
    with urllib.request.urlopen(req, timeout=60.0) as resp:
        length = resp.headers.get("Content-Length")
        return int(length) if length is not None else None


def check_assets(manifest: dict[str, Any], size=asset_size) -> list[str]:
    """What is wrong with the files the manifest lists; empty when nothing is."""
    problems = []
    for key, asset in sorted(manifest.get("assets", {}).items()):
        try:
            found = size(asset["url"])
        except Exception as exc:
            problems.append(f"{key}: {asset['url']} can't be downloaded ({exc})")
            continue
        if found != asset.get("size_bytes"):
            problems.append(f"{key}: {asset['url']} is {found} bytes, the manifest says {asset.get('size_bytes')}")
        else:
            print(f"[OK] {key}: {asset['filename']} ({found} bytes)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that installed copies can update to a published release")
    parser.add_argument("--tag", required=True, help="Release tag (e.g. v1.3.1)")
    parser.add_argument("--wait", type=float, default=WAIT_S, help="Seconds to wait for the manifest")
    args = parser.parse_args(argv)

    if not APP_TAG.match(args.tag):
        print(f"[*] {args.tag} is not an app release; nothing to check.")
        return 0
    version = args.tag[1:]

    from app.core.update_manager import OFFICIAL_UPDATE_MANIFEST_URL

    try:
        manifest = latest_manifest(OFFICIAL_UPDATE_MANIFEST_URL, version, timeout=args.wait)
    except ValueError as exc:
        print(f"[FAIL] Installed copies can't find {version}: {exc}", file=sys.stderr)
        print("       Sign and upload the manifest: python scripts/sign_manifest.py --tag " + args.tag,
              file=sys.stderr)
        return 1
    print(f"[OK] {OFFICIAL_UPDATE_MANIFEST_URL} serves {version}, signed by an embedded key")

    problems = check_assets(manifest)
    for problem in problems:
        print(f"[FAIL] {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"[Success] Installed copies can find and download {version}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
