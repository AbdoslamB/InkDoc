"""Build the optional Docling code/formula recognition add-on.

Fetches the CodeFormulaV2 weights with Docling's own tooling, packages them, and
prints the catalogue entry to paste into app/core/addons.json.

Deliberately not part of build_pack.py: the whole point of the add-on route is
that these 640 MB ship on their own cadence, independent of the ~1 GB engine
pack. `PREFETCH_MODELS` in build_pack.py stays unchanged.

The artifact is platform-neutral -- model weights, not executables -- so one
archive serves every platform and this runs anywhere Docling is installed.

    python scripts/build_addon.py --output-dir dist/addons

Then publish the archive, put its URL in the printed entry, and commit that
entry into app/core/addons.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ADDON_NAME = "code_enrichment"
MODEL_DIR = "docling-project--CodeFormulaV2"
DOCLING_MODEL_ID = "code_formula"


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def download_model(into: Path) -> Path:
    """Fetch the weights using docling-tools, the same route build_pack.py uses."""
    into.mkdir(parents=True, exist_ok=True)
    print(f"[*] Downloading '{DOCLING_MODEL_ID}' into {into} ...")
    subprocess.run(
        [sys.executable, "-m", "docling.cli.models", "download",
         "--output-dir", str(into), DOCLING_MODEL_ID],
        check=True,
    )
    model_path = into / MODEL_DIR
    if not model_path.is_dir():
        candidates = [p.name for p in into.iterdir() if p.is_dir()]
        raise SystemExit(
            f"Expected '{MODEL_DIR}' in {into}, found: {candidates or 'nothing'}"
        )
    return model_path


def build(output_dir: Path, model_source: Path | None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="inkdoc_addon_build_") as td:
        staging = Path(td)
        model_path = model_source if model_source else download_model(staging)
        if not model_path.is_dir():
            raise SystemExit(f"Model directory does not exist: {model_path}")

        files = sorted(p for p in model_path.rglob("*") if p.is_file())
        if not files:
            raise SystemExit(f"No files found under {model_path}")

        # Hash every file, relative to the model directory, which is exactly how
        # AddonManager._verify_tree walks it after extraction.
        sha256_files = {
            p.relative_to(model_path).as_posix(): _sha256(p) for p in files
        }
        uncompressed = sum(p.stat().st_size for p in files)

        archive = output_dir / f"inkdoc-addon-{ADDON_NAME}.tar.gz"
        print(f"[*] Packaging {len(files)} files -> {archive}")
        with tarfile.open(archive, "w:gz") as tar:
            # Archived under the model directory name so extraction produces it
            # directly, with no wrapper directory to unwrap.
            tar.add(model_path, arcname=MODEL_DIR, recursive=True)

        entry = {
            "title": "Code & Formula Recognition",
            "description": (
                "Transcribes code blocks and mathematical formulas from page images "
                "using Docling's recognition model, and labels the programming "
                "language of each code block."
            ),
            "model_dir": MODEL_DIR,
            "url": "<publish the archive and put its URL here>",
            "sha256": _sha256(archive),
            "archive_format": "tar.gz",
            "size_bytes": archive.stat().st_size,
            "uncompressed_size_bytes": uncompressed,
            "min_pack_version": "4.0.0",
            "min_app_version": "",
            "sha256_files": sha256_files,
        }

    print(f"\n[OK] {archive.name}: {entry['size_bytes'] / 1e6:.1f} MB "
          f"({uncompressed / 1e6:.1f} MB uncompressed, {len(sha256_files)} files)")
    print(f"[OK] sha256: {entry['sha256']}")

    entry_path = output_dir / f"{ADDON_NAME}.catalogue-entry.json"
    entry_path.write_text(json.dumps({ADDON_NAME: entry}, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] Catalogue entry written to {entry_path}")
    print("     Publish the archive, set 'url', then merge this into app/core/addons.json")
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "dist" / "addons")
    parser.add_argument(
        "--model-dir",
        type=Path,
        help=f"Use an already-downloaded {MODEL_DIR} directory instead of fetching it",
    )
    args = parser.parse_args()
    build(args.output_dir, args.model_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
