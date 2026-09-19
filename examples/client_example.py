"""Example client script for InkDoc Local API.

Demonstrates how to programmatically interact with the local API server:
- Health check
- Converting a local file
- Converting a webpage URL
- Batch converting multiple files
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import requests

API_BASE_URL = "http://localhost:13118/InkDoc"


def check_health() -> bool:
    """Verify that the local API server is up and running."""
    try:
        resp = requests.get(f"{API_BASE_URL}/health", timeout=3)
        if resp.status_code == 200:
            print("[OK] Server is healthy:", resp.json())
            return True
        print(f"[ERROR] Unexpected status {resp.status_code}: {resp.text}")
        return False
    except requests.exceptions.ConnectionError:
        print("[ERROR] Could not connect to API server at", API_BASE_URL)
        print("  Make sure the server is running (e.g. python main.py --headless)")
        return False


def convert_file(file_path: str | Path, save_to_downloads: bool = False) -> str:
    """Send a local file to the API server and get Markdown back."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    print(f"\nConverting file: {path.name} ...")
    with open(path, "rb") as f:
        files = {"file": (path.name, f)}
        params = {"save_to_downloads": str(save_to_downloads).lower()}
        resp = requests.post(f"{API_BASE_URL}/convert/file", files=files, params=params)

    if resp.status_code != 200:
        raise RuntimeError(f"Conversion error {resp.status_code}: {resp.text}")

    return resp.text


def convert_url(url: str, save_to_downloads: bool = False) -> str:
    """Send a URL to the API server and get Markdown back."""
    print(f"\nConverting URL: {url} ...")
    payload = {
        "url": url,
        "save_to_downloads": save_to_downloads,
    }
    resp = requests.post(f"{API_BASE_URL}/convert/url", json=payload)

    if resp.status_code != 200:
        raise RuntimeError(f"URL conversion error {resp.status_code}: {resp.text}")

    return resp.text


def batch_convert(file_paths: list[str | Path]) -> dict:
    """Send multiple files for batch conversion."""
    print(f"\nBatch converting {len(file_paths)} files...")
    with contextlib.ExitStack() as stack:
        files = []
        for p in file_paths:
            path = Path(p)
            f = stack.enter_context(open(path, "rb"))
            files.append(("files", (path.name, f)))

        resp = requests.post(f"{API_BASE_URL}/convert/batch", files=files)
        if resp.status_code != 200:
            raise RuntimeError(f"Batch conversion error {resp.status_code}: {resp.text}")
        return resp.json()


if __name__ == "__main__":
    if not check_health():
        sys.exit(1)

    # Example 1: Convert a simple test file
    sample_file = Path(__file__).resolve().parent / "sample.txt"
    sample_file.write_text("Hello from MarkItDown Local API!\nThis is a quick test.", encoding="utf-8")

    try:
        md_content = convert_file(sample_file)
        print("\n--- Converted Markdown Result ---")
        print(md_content.strip())
        print("---------------------------------")
    finally:
        if sample_file.exists():
            sample_file.unlink()
