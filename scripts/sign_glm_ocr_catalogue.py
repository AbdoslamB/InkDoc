#!/usr/bin/env python3
"""Sign a GLM-OCR catalogue offline and (optionally) publish it.

Milestone 2 ("latest tested"): .github/workflows/glm-ocr-track.yml tests a new
upstream model/runtime, mirrors the exact files to a draft release glm-ocr-vN,
and attaches the UNSIGNED payload glm-ocr-catalogue.payload.json. This script is
the maintainer's offline step, modelled on scripts/sign_manifest.py:

1. loads the payload (a local file, or the draft release's asset via `gh`);
2. validates it with validate_glm_catalogue (must be PUBLISHED);
3. re-downloads every file from BOTH upstream and the mirror and checks each
   against the payload's sha256 (skip with --skip-download-check, dev only);
4. asks for the key passphrase and signs the base64 payload (same envelope as
   the update manifest);
5. with --publish: publishes the draft glm-ocr-vN and replaces the single asset
   on the never-Latest "glm-ocr-catalogue" release. Rollback is impossible
   because the app only accepts a higher catalogue_version than it has seen.

The private key never touches CI (docs/RELEASE_RUNBOOK.md).

Usage:
    python scripts/sign_glm_ocr_catalogue.py --tag glm-ocr-v2 [--payload file.json]
        [--key-file ~/.inkdoc-keys/inkdoc_signing_key.pem] [--publish]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from app.core.download_utils import (  # noqa: E402
    MODEL_DOWNLOAD_HOST_SUFFIXES,
    MODEL_DOWNLOAD_HOSTS,
    StrictRedirectHandler,
    validate_download_url,
)
from app.core.glm_ocr_catalogue import CatalogueState, validate_glm_catalogue  # noqa: E402

PAYLOAD_ASSET = "glm-ocr-catalogue.payload.json"
ENVELOPE_ASSET = "inkdoc-glm-ocr-catalogue.json"
POINTER_TAG = "glm-ocr-catalogue"


def remote_sha256(url: str) -> str:
    """Stream a URL through the same host rules the app uses and hash it (nothing saved)."""
    validate_download_url(url, MODEL_DOWNLOAD_HOSTS, MODEL_DOWNLOAD_HOST_SUFFIXES)
    opener = urllib.request.build_opener(StrictRedirectHandler(MODEL_DOWNLOAD_HOSTS, MODEL_DOWNLOAD_HOST_SUFFIXES))
    req = urllib.request.Request(url, headers={"User-Agent": "InkDoc-GlmOcrSigner"})
    h = hashlib.sha256()
    with opener.open(req, timeout=60) as resp:
        while chunk := resp.read(4 << 20):
            h.update(chunk)
    return h.hexdigest()


def every_download(payload: dict) -> list[tuple[str, str, list[dict]]]:
    out = []
    for v in payload["model"]["variants"].values():
        for f in v["files"]:
            out.append((f["name"], f["sha256"], f["sources"]))
    for vid, v in payload["runtime"]["variants"].items():
        out.append((vid, v["sha256"], v["sources"]))
    # The same file can appear in two variants (the mmproj); check it once.
    seen, unique = set(), []
    for item in out:
        if item[1] not in seen:
            seen.add(item[1])
            unique.append(item)
    return unique


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser(description="Sign the GLM-OCR catalogue offline.")
    ap.add_argument("--tag", required=True, help="the mirror release that carries the payload, e.g. glm-ocr-v2")
    ap.add_argument("--payload", type=Path, help="local payload instead of downloading it from --tag")
    ap.add_argument("--key-file", type=Path, default=Path.home() / ".inkdoc-keys" / "inkdoc_signing_key.pem")
    ap.add_argument("--repo", default="AbdoslamB/InkDoc")
    ap.add_argument("--out", type=Path, default=Path(ENVELOPE_ASSET))
    ap.add_argument("--skip-download-check", action="store_true", help="dev only: do not re-download and re-hash")
    ap.add_argument("--publish", action="store_true", help="publish the draft and replace the pointer asset")
    args = ap.parse_args()

    if args.payload:
        raw = args.payload.read_bytes()
    else:
        if not shutil.which("gh"):
            print("[Error] gh is required to fetch the payload; pass --payload instead.", file=sys.stderr)
            return 1
        with tempfile.TemporaryDirectory() as tmp:
            gh("release", "download", args.tag, "--repo", args.repo, "--pattern", PAYLOAD_ASSET, "--dir", tmp)
            raw = (Path(tmp) / PAYLOAD_ASSET).read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    payload.pop("_comment", None)

    state, defects = validate_glm_catalogue(payload)
    if state is not CatalogueState.PUBLISHED:
        print(f"[Error] payload is {state.value}:", *defects, sep="\n  ", file=sys.stderr)
        return 1
    print(f"[*] catalogue_version {payload['catalogue_version']}: model {payload['model']['version']}, "
          f"runtime {payload['runtime']['version']}")

    if not args.skip_download_check:
        for name, expected, sources in every_download(payload):
            for src in sources:
                print(f"    checking {name} from {src['kind']} ...", flush=True)
                got = remote_sha256(src["url"])
                if got != expected:
                    print(f"[Error] {name} from {src['kind']} hashes to {got}, expected {expected}", file=sys.stderr)
                    return 1
        print("[*] every file matches its hash on both upstream and the mirror")

    from sign_manifest import build_envelope, load_private_key

    key = load_private_key(args.key_file.expanduser())
    envelope = build_envelope(payload, key)
    args.out.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    print(f"[*] signed envelope written to {args.out}")

    # Self-check with the app's own verifier and public keys.
    from app.core.update_verifier import get_official_public_keys, verify_signed_envelope

    verify_signed_envelope(args.out.read_bytes(), get_official_public_keys(), what="GLM-OCR catalogue")
    print("[*] verified against the public keys embedded in the app")

    if args.publish:
        gh("release", "edit", args.tag, "--repo", args.repo, "--draft=false", "--latest=false")
        try:
            gh("release", "view", POINTER_TAG, "--repo", args.repo)
        except subprocess.CalledProcessError:
            gh("release", "create", POINTER_TAG, "--repo", args.repo, "--latest=false",
               "--title", "GLM-OCR catalogue (pointer)",
               "--notes", "Signed GLM-OCR catalogue read by InkDoc. Not an app release.")
        gh("release", "upload", POINTER_TAG, str(args.out), "--repo", args.repo, "--clobber")
        print(f"[*] published {args.tag} and replaced {ENVELOPE_ASSET} on {POINTER_TAG}")
    else:
        print("[*] not published (pass --publish to publish the draft and the pointer asset)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
