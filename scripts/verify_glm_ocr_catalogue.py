#!/usr/bin/env python3
"""CI guard for the GLM-OCR catalogue (bundled file, or any payload given).

Fails when the catalogue is DEFECTIVE: half-filled, a placeholder or malformed
hash, a source URL outside the allowlist, a mirror URL outside InkDoc's own
releases, a file over the 1.9 GB mirror limit, an entry missing from
sha256_files. UNRELEASED (no URLs and no hashes at all) passes: the app then
hides GLM-OCR and shows "Coming soon".

Uses the exact validate_glm_catalogue the app runs, so CI and the app can never
disagree about what is installable.

Usage:
    python scripts/verify_glm_ocr_catalogue.py [path/to/catalogue.json] [--require-published]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.core.glm_ocr_catalogue import (  # noqa: E402
    BUNDLED_CATALOGUE_PATH,
    CatalogueState,
    validate_glm_catalogue,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a GLM-OCR catalogue.")
    ap.add_argument("path", nargs="?", type=Path, default=BUNDLED_CATALOGUE_PATH)
    ap.add_argument("--require-published", action="store_true",
                    help="fail on UNRELEASED too (used before signing a catalogue)")
    args = ap.parse_args()

    try:
        data = json.loads(args.path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"::error::cannot read {args.path}: {exc}")
        return 1
    state, defects = validate_glm_catalogue(data)
    if state is CatalogueState.DEFECTIVE:
        print(f"::error::{args.path} is DEFECTIVE:")
        for d in defects:
            print(f"  - {d}")
        return 1
    if args.require_published and state is not CatalogueState.PUBLISHED:
        print(f"::error::{args.path} is {state.value}, expected published")
        return 1
    version = data.get("catalogue_version")
    print(f"{args.path}: {state.value} (catalogue_version {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
