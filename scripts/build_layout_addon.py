"""Build the layout model add-on ("layout_model" in app/core/addons.json).

Packages the official ONNX export of Docling's layout model "heron"
(docling-project/docling-layout-heron-onnx, Apache-2.0) with its licence text,
and writes a catalogue ready to commit, with the asset URL derived from the
release tag, as scripts/build_addon.py does for the Docling add-on.

The files come from the Hugging Face revision the catalogue pins
(source_revision), or from a local copy (--model-dir, such as the one in an
installed Docling engine pack). Either way model.onnx must hash to the digest
the catalogue pins for the Docling pack's copy (pack_copy), so InkDoc's add-on
and Docling's copy are one and the same file, and the build stops otherwise.
The archive is reproducible: the same files always give the same SHA-256.

    python scripts/build_layout_addon.py --release-tag layout-addon-v1 --output-dir dist/addons
    python scripts/build_layout_addon.py --release-tag layout-addon-v1 --model-dir <folder>

Then publish dist/addons/inkdoc-addon-layout_model.tar.gz on that release tag
and commit dist/addons/addons.json as app/core/addons.json.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ADDON_NAME = "layout_model"
GITHUB_REPO = "AbdoslamB/InkDoc"
ARCHIVE_NAME = f"inkdoc-addon-{ADDON_NAME}.tar.gz"
CATALOGUE_PATH = REPO_ROOT / "app" / "core" / "addons.json"
LICENSE_TEXT = REPO_ROOT / "LICENSES" / "Docling-layout-heron-Apache-2.0.txt"
# What the archive holds besides LICENSE: the model and the upstream files that describe it.
UPSTREAM_FILES = ("model.onnx", "config.json", "preprocessor_config.json", "README.md")
MODEL_FILE = "model.onnx"


def asset_url(release_tag: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/releases/download/{release_tag}/{ARCHIVE_NAME}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def catalogue_entry(catalogue: Path = CATALOGUE_PATH) -> dict:
    return json.loads(catalogue.read_text(encoding="utf-8"))["addons"][ADDON_NAME]


def download(repo: str, revision: str, into: Path) -> Path:
    """The upstream files at `revision`, from Hugging Face."""
    into.mkdir(parents=True, exist_ok=True)
    for name in UPSTREAM_FILES:
        url = f"https://huggingface.co/{repo}/resolve/{revision}/{name}"
        print(f"[*] Downloading {url}")
        with urllib.request.urlopen(url, timeout=60) as response, (into / name).open("wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
    return into


def check_loads(model: Path) -> None:
    """onnxruntime loads the model and it answers a blank page with its three outputs."""
    import numpy as np
    import onnxruntime as ort

    session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    outputs = session.run(None, {
        "images": np.full((1, 3, 640, 640), 255, dtype=np.uint8),
        "orig_target_sizes": np.array([[640, 640]], dtype=np.int64),
    })
    if [o.name for o in session.get_outputs()] != ["labels", "boxes", "scores"] or outputs[1].shape[-1] != 4:
        raise SystemExit("[FAIL] The model's outputs are not labels, boxes and scores.")
    print("[OK] onnxruntime loads the model")


def write_archive(folder: Path, archive: Path, root: str) -> None:
    """A tar.gz of `folder` under `root`/, the same bytes for the same files."""
    with archive.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(folder.iterdir()):
            info = tarfile.TarInfo(f"{root}/{path.name}")
            info.size = path.stat().st_size
            info.mode = 0o644
            info.mtime = 0
            with path.open("rb") as f:
                tar.addfile(info, f)


def build(
    output_dir: Path,
    model_source: Path | None,
    release_tag: str,
    *,
    catalogue: Path = CATALOGUE_PATH,
    check_load: bool = True,
) -> dict:
    entry = dict(catalogue_entry(catalogue))
    pinned = entry["pack_copy"]["sha256_files"][MODEL_FILE]
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="inkdoc_layout_addon_") as td:
        source = model_source or download(entry["source_repo"], entry["source_revision"], Path(td) / "download")
        missing = [name for name in UPSTREAM_FILES if not (source / name).is_file()]
        if missing:
            raise SystemExit(f"[FAIL] {source} lacks {', '.join(missing)}")
        if _sha256(source / MODEL_FILE) != pinned:
            raise SystemExit(f"[FAIL] {source / MODEL_FILE} is not the pinned model (sha256 {pinned}).")
        print(f"[OK] {MODEL_FILE} is {entry['source_repo']}@{entry['source_revision'][:12]}")
        if check_load:
            check_loads(source / MODEL_FILE)

        staged = Path(td) / entry["model_dir"]
        staged.mkdir()
        for name in UPSTREAM_FILES:
            shutil.copyfile(source / name, staged / name)
        shutil.copyfile(LICENSE_TEXT, staged / "LICENSE")

        archive = output_dir / ARCHIVE_NAME
        write_archive(staged, archive, entry["model_dir"])
        files = sorted(staged.iterdir())
        entry.update(
            url=asset_url(release_tag),
            sha256=_sha256(archive),
            size_bytes=archive.stat().st_size,
            uncompressed_size_bytes=sum(p.stat().st_size for p in files),
            sha256_files={p.name: _sha256(p) for p in files},
        )

    print(f"[OK] {archive.name}: {entry['size_bytes'] / 1e6:.1f} MB, sha256 {entry['sha256']}")
    full = json.loads(catalogue.read_text(encoding="utf-8"))
    full["addons"][ADDON_NAME] = entry
    out = output_dir / "addons.json"
    out.write_text(json.dumps(full, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    from scripts.verify_addon_catalogue import verify

    ok, messages = verify(out, require_published=False)
    for line in messages:
        print(f"     {line}")
    if not ok:
        raise SystemExit("[FAIL] The generated catalogue is invalid; refusing to emit it.")
    print(f"[OK] Catalogue written to {out}. Publish {archive.name} on {release_tag}, then commit it.")
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--release-tag", required=True,
                        help="the release the archive will be published on; the asset URL is derived from it")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "dist" / "addons")
    parser.add_argument("--model-dir", type=Path, help="a local copy of the model instead of downloading it")
    args = parser.parse_args()
    build(args.output_dir, args.model_dir, args.release_tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
