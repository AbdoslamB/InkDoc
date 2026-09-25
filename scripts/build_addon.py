"""Build the optional Docling code/formula recognition add-on.

Fetches the CodeFormulaV2 weights with Docling's own tooling, packages them, and
writes a complete catalogue ready to commit -- including the asset URL, derived
from the release tag exactly as build_pack.py derives a pack's URL.

This used to print an entry with a placeholder URL for a human to paste into
app/core/addons.json. Nobody ever did, so the add-on sat unpublished and the two
enrichment toggles it gates could never be enabled; and because `get_status`
treated any non-empty string as a usable URL, a mis-paste would have produced an
Install button that always failed. Deriving the URL here and validating the
result in CI removes both failure modes.

Deliberately not part of build_pack.py: the whole point of the add-on route is
that these 640 MB ship on their own cadence, independent of the ~1 GB engine
pack. `PREFETCH_MODELS` in build_pack.py stays unchanged.

The artifact is platform-neutral -- model weights, not executables -- so one
archive serves every platform and this runs anywhere Docling is installed.

    # Build the archive and write dist/addons/addons.json for the release tag.
    python scripts/build_addon.py --release-tag docling-addon-v1 \
        --output-dir dist/addons

    # Check a catalogue without building anything.
    python scripts/build_addon.py --verify-catalogue
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
# Same as build_pack.py: makes `scripts.` and `app.` importable when this is
# run directly, where sys.path[0] is the scripts directory rather than the root.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ADDON_NAME = "code_enrichment"
MODEL_DIR = "docling-project--CodeFormulaV2"
DOCLING_MODEL_ID = "code_formula"

# Same repository and URL shape build_pack.py uses for pack assets.
GITHUB_REPO = "AbdoslamB/InkDoc"
ARCHIVE_NAME = f"inkdoc-addon-{ADDON_NAME}.tar.gz"
CATALOGUE_PATH = REPO_ROOT / "app" / "core" / "addons.json"

# The pack version whose worker implements the "capabilities" RPC the installer
# uses to confirm a fresh install, and which refuses enrichment outright when the
# model is absent. worker.py is hashed into the pack manifest and the launcher
# rejects a modified copy, so an older pack cannot be given either behaviour.
MIN_PACK_VERSION = "6.0.0"


def asset_url(release_tag: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/releases/download/{release_tag}/{ARCHIVE_NAME}"


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


def build(output_dir: Path, model_source: Path | None, release_tag: str) -> dict:
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

        archive = output_dir / ARCHIVE_NAME
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
            # Derived, never pasted. The workflow publishes the archive under
            # exactly this name on exactly this tag, and the verify job afterwards
            # re-downloads this URL and checks it against the hash below.
            "url": asset_url(release_tag),
            "sha256": _sha256(archive),
            "archive_format": "tar.gz",
            "size_bytes": archive.stat().st_size,
            "uncompressed_size_bytes": uncompressed,
            "min_pack_version": MIN_PACK_VERSION,
            "min_app_version": "",
            "sha256_files": sha256_files,
        }

    print(f"\n[OK] {archive.name}: {entry['size_bytes'] / 1e6:.1f} MB "
          f"({uncompressed / 1e6:.1f} MB uncompressed, {len(sha256_files)} files)")
    print(f"[OK] sha256: {entry['sha256']}")

    # A whole catalogue, not a fragment to splice in by hand: the existing file is
    # read, this one entry replaced, and everything else -- including the _comment
    # block -- carried through untouched.
    catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    catalogue.setdefault("addons", {})[ADDON_NAME] = entry
    catalogue_out = output_dir / "addons.json"
    catalogue_out.write_text(json.dumps(catalogue, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] Catalogue written to {catalogue_out}")

    # Fail here rather than shipping something the app would reject at runtime.
    ok, messages = _verify(catalogue_out, require_published=True)
    for line in messages:
        print(f"     {line}")
    if not ok:
        raise SystemExit("[FAIL] The generated catalogue is invalid; refusing to emit it.")
    print(f"     Publish {archive.name} on {release_tag}, then commit this catalogue.")
    return entry


def _verify(catalogue_path: Path, require_published: bool) -> tuple[bool, list[str]]:
    """Validate through the same code the application and CI use."""
    from scripts.verify_addon_catalogue import verify

    return verify(catalogue_path, require_published)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "dist" / "addons")
    parser.add_argument(
        "--model-dir",
        type=Path,
        help=f"Use an already-downloaded {MODEL_DIR} directory instead of fetching it",
    )
    parser.add_argument(
        "--release-tag",
        help="Release tag the archive will be published on, e.g. docling-addon-v1. "
             "The asset URL is derived from it.",
    )
    parser.add_argument(
        "--verify-catalogue",
        nargs="?",
        const=str(CATALOGUE_PATH),
        metavar="PATH",
        help="Validate a catalogue and exit without building anything "
             "(default: app/core/addons.json)",
    )
    parser.add_argument(
        "--require-published",
        action="store_true",
        help="With --verify-catalogue, also require a published artifact",
    )
    args = parser.parse_args()

    if args.verify_catalogue:
        path = Path(args.verify_catalogue)
        ok, messages = _verify(path, args.require_published)
        print(f"[*] Verifying {path}")
        for line in messages:
            print(line)
        return 0 if ok else 1

    if not args.release_tag:
        parser.error(
            "--release-tag is required: the asset URL is derived from it rather "
            "than pasted in afterwards."
        )
    build(args.output_dir, args.model_dir, args.release_tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
