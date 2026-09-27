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
import importlib
import json
import os
import re
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
SOURCE_REPO = "docling-project/CodeFormulaV2"
DOCLING_MODEL_ID = "code_formula"

# Same repository and URL shape build_pack.py uses for pack assets.
GITHUB_REPO = "AbdoslamB/InkDoc"
ARCHIVE_NAME = f"inkdoc-addon-{ADDON_NAME}.tar.gz"
CATALOGUE_PATH = REPO_ROOT / "app" / "core" / "addons.json"
MANIFEST_PATH = REPO_ROOT / "app" / "core" / "manifest.json"
DOCLING_CONSTRAINTS = REPO_ROOT / "scripts" / "docling-constraints.txt"

# The first pack version whose worker implements the "capabilities" RPC the
# installer uses to confirm a fresh install, and which refuses enrichment outright
# when the model is absent -- every pack built from this repository does. worker.py
# is hashed into the pack manifest and the launcher rejects a modified copy, so
# raise this whenever the add-on comes to depend on worker behaviour that an
# already-published pack lacks.
MIN_PACK_VERSION = "1.0.0"


def asset_url(release_tag: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/releases/download/{release_tag}/{ARCHIVE_NAME}"


def pinned_docling_version(constraints: Path = DOCLING_CONSTRAINTS) -> str:
    """The `docling==X` pin shared with the engine pack build."""
    for line in constraints.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\s*docling\s*==\s*([^\s#]+)\s*(#.*)?", line)
        if match:
            return match.group(1)
    raise SystemExit(f"[FAIL] {constraints} does not pin docling with '=='.")


def pack_docling_versions(manifest: Path = MANIFEST_PATH) -> dict[str, str]:
    """Docling version the published pack ships, per platform.

    Read from the dist-info directory names in the manifest's per-file hashes,
    which is what the pack actually contains rather than what it was asked to
    install. A platform with no docling dist-info maps to "".
    """
    data = json.loads(manifest.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for platform, info in (data.get("supported_platforms") or {}).items():
        version = ""
        for path in info.get("sha256_files") or {}:
            match = re.search(r"(?:^|/)docling-([0-9][^/]*)\.dist-info/", path)
            if match:
                version = match.group(1)
                break
        found[platform] = version
    return found


def check_docling_consistency(downloading: bool) -> str:
    """Refuse to build an add-on the shipped pack could not load.

    The model directory name and the weights revision come from the Docling that
    downloads them; the pack's Docling decides which directory it looks in. If
    they differ, users download 640 MB and the install then fails the worker's
    capability probe. Checked here, before any download, so a mismatch costs a
    failed CI job instead.

    `downloading` is False for --model-dir, where the weights came from elsewhere
    and the Docling installed here is irrelevant; the pack check still applies.
    """
    pinned = pinned_docling_version()

    shipped = pack_docling_versions()
    if not shipped:
        raise SystemExit(f"[FAIL] {MANIFEST_PATH} lists no platforms.")
    wrong = {plat: ver or "(none)" for plat, ver in shipped.items() if ver != pinned}
    if wrong:
        detail = ", ".join(f"{plat}={ver}" for plat, ver in sorted(wrong.items()))
        raise SystemExit(
            f"[FAIL] The engine pack in {MANIFEST_PATH.name} does not ship the pinned "
            f"Docling {pinned}: {detail}.\n"
            "       Build and publish the pack first, so its manifest lands here, "
            "then build the add-on."
        )

    if downloading:
        from importlib.metadata import PackageNotFoundError, version

        try:
            installed = version("docling")
        except PackageNotFoundError:
            raise SystemExit(
                "[FAIL] docling is not installed; it is needed to fetch the model."
            ) from None
        if installed != pinned:
            raise SystemExit(
                f"[FAIL] Installed docling is {installed}, but "
                f"{DOCLING_CONSTRAINTS.name} pins {pinned}. Install with "
                f"`pip install -c scripts/{DOCLING_CONSTRAINTS.name} docling`."
            )

    print(f"[OK] Docling {pinned}: pinned, shipped by the pack on "
          f"{', '.join(sorted(shipped))}"
          + (", and installed here" if downloading else ""))
    return pinned


def _code_formula_stage():
    """Docling's code/formula stage: (model class, options class, CPU accelerator).

    Resolved wherever this Docling version keeps it -- the module has moved once
    already, from models/ to models/stages/code_formula/, and the worker resolves
    it the same way. Every Docling lookup verify_model_loads makes goes through
    here, so tests can substitute the stage without Docling installed.
    """
    for module in (
        "docling.models.stages.code_formula.code_formula_model",
        "docling.models.code_formula_model",
    ):
        try:
            mod = importlib.import_module(module)
            stage_cls, options_cls = mod.CodeFormulaModel, mod.CodeFormulaModelOptions
            break
        except (ImportError, AttributeError):
            continue
    else:
        raise SystemExit("[FAIL] This Docling version has no CodeFormulaModel.")

    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions

    return stage_cls, options_cls, AcceleratorOptions(device=AcceleratorDevice.CPU)


def verify_model_loads(model_path: Path) -> None:
    """Build Docling's own code/formula stage from the directory about to ship.

    Docling fetches this model from the `main` branch of
    docling-project/CodeFormulaV2 rather than a pinned commit, so pinning Docling
    pins the downloader, not the weights. The archive's SHA-256 fixes what users
    receive, but not whether the pack can load it: an upstream change needing a
    newer transformers would publish, immutably, a model that fails every
    install's capability probe.

    This constructs the stage exactly as the pack's pipeline does at conversion
    time, from exactly the files being archived, under the pinned stack. It
    loads the full weights on CPU -- slow, but it runs once per add-on release
    and is the only check that proves the published bytes work.
    """
    # Local files only. Set before transformers or huggingface_hub is imported in
    # this process (the download ran in a subprocess), so the loader cannot
    # quietly substitute a fresh copy from the Hub for the one being checked.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    stage_cls, options_cls, cpu = _code_formula_stage()
    expected = getattr(stage_cls, "_model_repo_folder", "")
    if model_path.name != expected:
        raise SystemExit(
            f"[FAIL] Model directory is '{model_path.name}', but this Docling looks "
            f"for '{expected}'. The pack would never find the installed model."
        )

    print(f"[*] Loading {model_path.name} with Docling's own stage (CPU)...")
    try:
        stage_cls(
            enabled=True,
            artifacts_path=model_path.parent,
            options=options_cls(),
            accelerator_options=cpu,
        )
    except Exception as exc:
        raise SystemExit(
            f"[FAIL] The model does not load under the pinned Docling stack, so the "
            f"pack could not load it either: {type(exc).__name__}: {exc}"
        ) from exc
    print("[OK] Model loads with Docling's code/formula stage")


def source_revision(model_path: Path) -> str:
    """The Hugging Face commit the downloaded weights came from, or "".

    Docling fetches this model from the moving `main` branch, so the add-on's
    version alone does not say which weights it contains. huggingface_hub's
    local-dir download leaves one .metadata file per downloaded file under
    .cache/huggingface/download/, whose first line is the commit it came from.
    A snapshot is a single commit, so every file must agree; disagreement means
    a mixed or interrupted download and is fatal. None at all (--model-dir from
    somewhere else) is reported as unknown rather than guessed.
    """
    meta_dir = model_path / ".cache" / "huggingface" / "download"
    commits = set()
    for meta in sorted(meta_dir.glob("*.metadata")) if meta_dir.is_dir() else []:
        first = meta.read_text(encoding="utf-8").splitlines()[:1]
        if first:
            commits.add(first[0].strip())

    if not commits:
        print("[!] No Hugging Face download metadata; source revision unknown.")
        return ""
    if len(commits) > 1:
        raise SystemExit(
            f"[FAIL] Downloaded files come from {len(commits)} different commits "
            f"({', '.join(sorted(commits))}): a mixed or interrupted download."
        )
    (commit,) = commits
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit(f"[FAIL] Unexpected commit id in download metadata: {commit!r}")
    return commit


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
    check_docling_consistency(downloading=model_source is None)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="inkdoc_addon_build_") as td:
        staging = Path(td)
        model_path = model_source if model_source else download_model(staging)
        if not model_path.is_dir():
            raise SystemExit(f"Model directory does not exist: {model_path}")
        verify_model_loads(model_path)
        revision = source_revision(model_path)
        if revision:
            print(f"[OK] Weights are {SOURCE_REPO}@{revision}")

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
            # Provenance only; nothing reads these at runtime. They are the one
            # record of which upstream weights this release contains, since
            # Docling fetches the model from a moving branch.
            "source_repo": SOURCE_REPO,
            "source_revision": revision,
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
