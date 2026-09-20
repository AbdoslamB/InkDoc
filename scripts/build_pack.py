#!/usr/bin/env python3
"""Build and package an isolated Docling engine pack for the current OS/architecture.

Hardened with:
- CPU-only PyTorch index to keep package sizes under the 1.9 GiB ceiling
- Pinned release tag and per-platform manifest fragment output
- macOS ad-hoc binary re-signing
- Post-build smoke test: clean directory extraction, Ping RPC, and offline PDF conversion
- Mandatory archive size check (< 1.9 GiB)

Usage:
    python scripts/build_pack.py [--output-dir dist/packs] [--release-tag docling-pack-v2]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

# Add project root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.engine_manifest import get_current_platform_key

MAX_ALLOWED_PACK_SIZE = 1900 * 1024 * 1024  # 1.9 GiB maximum limit
DEFAULT_RELEASE_TAG = "docling-pack-v2"
MINIMAL_PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<<>>>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n0000000101 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n178\n%%EOF\n"
)


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 hex digest of a file using 64KB chunks."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_tree_hashes(root_dir: Path) -> dict[str, str]:
    """Compute relative path -> sha256 mapping for all files under root_dir."""
    tree: dict[str, str] = {}
    for p in root_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root_dir).as_posix()
            tree[rel] = compute_sha256(p)
    return tree


def count_model_files(models_dir: Path) -> tuple[int, int]:
    """Return the count and size of bundled Docling model artifacts."""
    files = [path for path in models_dir.rglob("*") if path.is_file()]
    if not files:
        raise RuntimeError(f"Docling model prefetch produced no files in {models_dir}")
    return len(files), sum(path.stat().st_size for path in files)


def resign_macos_binaries(root_dir: Path) -> None:
    """On macOS, re-sign ad-hoc (-s -) all dylib, so, and executable binaries."""
    if sys.platform != "darwin":
        return
    codesign = shutil.which("codesign")
    if not codesign:
        print("    [!] Warning: 'codesign' tool not found, skipping ad-hoc re-signing.")
        return

    print("[*] Re-signing macOS binaries ad-hoc (codesign -f -s -)...")
    for p in root_dir.rglob("*"):
        if p.is_file() and (p.suffix in (".dylib", ".so") or os.access(p, os.X_OK)):
            try:
                subprocess.run([codesign, "-f", "-s", "-", str(p)], check=True, capture_output=True)
            except Exception as exc:
                print(f"    [!] Warning: failed to sign {p}: {exc}")


def run_post_build_smoke_test(
    archive_path: Path,
    archive_format: str,
    interpreter_rel: str,
    worker_rel: str,
    sample_pdf: Path | None = None,
) -> None:
    """Requirement 5: Extract into a clean tempdir and verify Ping RPC and offline PDF conversion."""
    print(f"[*] Running post-build smoke test from archive: {archive_path.name}...")
    with tempfile.TemporaryDirectory(prefix="inkdoc_smoke_") as clean_dir_str:
        clean_dir = Path(clean_dir_str)
        # Extract archive
        if archive_format == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(clean_dir)
        else:
            with tarfile.open(archive_path, "r:*") as tf:
                tf.extractall(clean_dir)

        extracted_python = clean_dir / interpreter_rel
        extracted_worker = clean_dir / worker_rel
        extracted_models = clean_dir / "models"

        if not extracted_python.is_file():
            raise RuntimeError(f"Extracted interpreter not found at {extracted_python}")
        if not extracted_worker.is_file():
            raise RuntimeError(f"Extracted worker script not found at {extracted_worker}")
        model_count, model_bytes = count_model_files(extracted_models)
        print(f"    [OK] Bundled Docling models present ({model_count} files, {model_bytes} bytes).")

        # Ensure POSIX execution permissions on Unix
        if sys.platform != "win32":
            extracted_python.chmod(0o755)

        offline_env = os.environ.copy()
        offline_env.update({
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "SCARF_NO_ANALYTICS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "DOCLING_ARTIFACTS_PATH": str(extracted_models),
        })

        # 1. Ping RPC test
        cmd = [str(extracted_python), "-I", "-s", "-S", str(extracted_worker)]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=offline_env,
        )
        ping_rpc = json.dumps({"action": "ping"}) + "\n"
        stdout_data, stderr_data = proc.communicate(input=ping_rpc, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"Post-build ping test failed with code {proc.returncode}: {stderr_data}")

        # Worker emits 'ready' line first, followed by pong
        lines = [ln.strip() for ln in stdout_data.strip().split("\n") if ln.strip()]
        last_res = json.loads(lines[-1]) if lines else {}
        if last_res.get("status") not in ("pong", "ok"):
            raise RuntimeError(f"Unexpected ping RPC response: {last_res} (stdout: {stdout_data})")
        print("    [OK] Post-build worker Ping RPC passed in clean extracted environment.")

        # 2. Offline PDF conversion test
        pdf_path = sample_pdf
        if not pdf_path or not pdf_path.is_file():
            pdf_path = clean_dir / "smoke_sample.pdf"
            pdf_path.write_bytes(MINIMAL_PDF_BYTES)

        out_md = clean_dir / "smoke_sample_out.md"
        convert_rpc = json.dumps({
            "action": "convert",
            "job_id": "smoke-test-1",
            "source_file": str(pdf_path),
            "output_file": str(out_md),
            "ocr": True,
            "table_structure": True,
        }) + "\n"

        proc2 = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=offline_env,
        )
        stdout_data2, stderr_data2 = proc2.communicate(input=convert_rpc, timeout=60)
        if proc2.returncode != 0:
            raise RuntimeError(f"Post-build offline PDF conversion failed with code {proc2.returncode}: {stderr_data2}")
        lines2 = [ln.strip() for ln in stdout_data2.strip().split("\n") if ln.strip()]
        last_res2 = json.loads(lines2[-1]) if lines2 else {}
        if last_res2.get("status") != "ok" or not out_md.is_file():
            raise RuntimeError(
                f"Post-build offline PDF conversion did not produce Markdown: {last_res2} (stdout: {stdout_data2})"
            )
        print(f"    [OK] Post-build worker conversion RPC executed (status: {last_res2.get('status')}).")


def build_pack(
    output_dir: Path,
    release_tag: str = DEFAULT_RELEASE_TAG,
    pack_version: str = "1.0.0",
    min_app_version: str = "1.0.0",
    run_test: bool = True,
    sample_pdf: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Assemble, verify, and package the isolated Docling engine pack."""
    platform_key = get_current_platform_key()
    print(f"[*] Building Docling pack for platform: {platform_key} (release: {release_tag})")

    output_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = REPO_ROOT / "build" / "pack_staging" / platform_key
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create a virtual environment with copies
    venv_dir = staging_dir / "env"
    print(f"[*] Creating isolated virtual environment at {venv_dir}...")
    subprocess.run([sys.executable, "-m", "venv", "--copies", str(venv_dir)], check=True)

    # Determine interpreter binary
    if sys.platform.startswith("win"):
        venv_python = venv_dir / "Scripts" / "python.exe"
        interp_rel = "env/Scripts/python.exe"
        archive_format = "zip"
        archive_name = f"docling-{platform_key}.zip"
    else:
        venv_python = venv_dir / "bin" / "python"
        interp_rel = "env/bin/python"
        archive_format = "tar.gz"
        archive_name = f"docling-{platform_key}.tar.gz"

    # 2. Upgrade pip
    print("[*] Upgrading pip in isolated environment...")
    subprocess.run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"], check=True)

    # 3. Install PyTorch with CPU-only wheel (Requirement 4)
    print("[*] Installing PyTorch (CPU-only index) to optimize size...")
    torch_cmd = [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--no-warn-script-location",
        "--extra-index-url",
        "https://download.pytorch.org/whl/cpu",
        "torch",
        "torchvision",
    ]
    subprocess.run(torch_cmd, check=True)

    # 4. Install docling into the isolated environment
    print("[*] Installing docling into isolated environment...")
    install_cmd = [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--no-warn-script-location",
        "--extra-index-url",
        "https://download.pytorch.org/whl/cpu",
        "docling",
    ]
    subprocess.run(install_cmd, check=True)

    # 5. Prefetch the default Docling model artifacts into the pack.  The worker
    # runs with networking disabled, so model downloads after installation are
    # not an option.
    models_dir = staging_dir / "models"
    model_tool = venv_dir / ("Scripts/docling-tools.exe" if sys.platform.startswith("win") else "bin/docling-tools")
    print(f"[*] Prefetching Docling models into {models_dir}...")
    subprocess.run([str(model_tool), "models", "download", "--output-dir", str(models_dir)], check=True)
    model_count, model_bytes = count_model_files(models_dir)
    print(f"    [OK] Bundled Docling models: {model_count} files, {model_bytes} bytes.")

    # 6. Copy worker script into staging root
    worker_src = REPO_ROOT / "app" / "core" / "engines" / "worker.py"
    worker_dst = staging_dir / "worker.py"
    shutil.copy2(worker_src, worker_dst)

    # 7. Strip unnecessary bloat (__pycache__, test files)
    print("[*] Cleaning cache files and tests from pack...")
    for cache_dir in staging_dir.rglob("__pycache__"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)

    # 8. macOS Ad-Hoc Re-signing (Requirement 5)
    if sys.platform == "darwin":
        resign_macos_binaries(staging_dir)

    # 9. Compute full tree file hashes before archiving
    print("[*] Computing cryptographic hashes for all pack files...")
    sha256_files = compute_tree_hashes(staging_dir)
    print(f"    [+] Indexed {len(sha256_files)} files.")

    # 10. Create archive
    archive_path = output_dir / archive_name
    print(f"[*] Packaging into {archive_path}...")
    if archive_format == "zip":
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in staging_dir.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(staging_dir).as_posix()
                    zf.write(p, rel)
    else:
        with tarfile.open(archive_path, "w:gz") as tf:
            for p in staging_dir.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(staging_dir).as_posix()
                    tf.add(p, arcname=rel)

    archive_sha256 = compute_sha256(archive_path)
    archive_size = archive_path.stat().st_size
    uncompressed_size = sum(p.stat().st_size for p in staging_dir.rglob("*") if p.is_file())

    print(f"[*] Pack created: {archive_path.name}")
    print(f"    - Archive size: {archive_size} bytes ({archive_size / (1024*1024):.2f} MB)")
    print(f"    - Uncompressed: {uncompressed_size} bytes ({uncompressed_size / (1024*1024):.2f} MB)")
    print(f"    - SHA-256: {archive_sha256}")

    # Enforce 1.9 GiB ceiling (Requirement 4)
    if archive_size > MAX_ALLOWED_PACK_SIZE:
        archive_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Archive size {archive_size} bytes ({archive_size / (1024*1024):.1f} MB) "
            f"exceeds maximum allowed ceiling of 1.9 GiB ({MAX_ALLOWED_PACK_SIZE} bytes)!"
        )

    pack_info = {
        "url": f"https://github.com/AbdoslamB/InkDoc/releases/download/{release_tag}/{archive_name}",
        "archive_format": archive_format,
        "sha256": archive_sha256,
        "size_bytes": archive_size,
        "uncompressed_size_bytes": uncompressed_size,
        "interpreter_path": interp_rel,
        "worker_script": "worker.py",
        "sha256_files": sha256_files,
    }

    # 11. Write per-platform manifest fragment (Requirement 10)
    fragment_data = {
        "manifest_version": "1.0.0",
        "pack_version": pack_version,
        "min_app_version": min_app_version,
        "release_tag": release_tag,
        "platform": platform_key,
        "supported_platforms": {
            platform_key: pack_info
        }
    }
    fragment_path = output_dir / f"{platform_key}.fragment.json"
    fragment_path.write_text(json.dumps(fragment_data, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] Manifest fragment saved to: {fragment_path}")

    # 12. Run post-build smoke test (Requirement 5)
    if run_test:
        run_post_build_smoke_test(
            archive_path=archive_path,
            archive_format=archive_format,
            interpreter_rel=interp_rel,
            worker_rel="worker.py",
            sample_pdf=sample_pdf,
        )

    return archive_path, pack_info


def main() -> None:
    parser = argparse.ArgumentParser(description="Build InkDoc Docling engine pack.")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "dist" / "packs")
    parser.add_argument("--release-tag", type=str, default=DEFAULT_RELEASE_TAG, help="Dedicated release tag (e.g. docling-pack-v2)")
    parser.add_argument("--pack-version", type=str, default="1.0.0", help="Pack version string")
    parser.add_argument("--min-app-version", type=str, default="1.0.0", help="Minimum required InkDoc version")
    parser.add_argument("--sample-pdf", type=Path, help="Path to sample PDF for smoke test")
    parser.add_argument("--no-test", action="store_true", help="Skip post-build smoke test")
    args = parser.parse_args()

    build_pack(
        output_dir=args.output_dir,
        release_tag=args.release_tag,
        pack_version=args.pack_version,
        min_app_version=args.min_app_version,
        run_test=not args.no_test,
        sample_pdf=args.sample_pdf,
    )


if __name__ == "__main__":
    main()
