"""Bundled third-party UI files match app/ui/vendor/VENDOR.json exactly.

Every file the page can load from /static/vendor is pinned here by SHA-256,
so a changed, missing or unlisted file fails CI instead of shipping. The
versions must also agree with the files' own headers and with
THIRD_PARTY_NOTICES.md, which keeps the notices in step with what ships.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VENDOR = REPO / "app" / "ui" / "vendor"
MANIFEST = VENDOR / "VENDOR.json"


def _libraries() -> list[dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["libraries"]


def _listed() -> dict[str, str]:
    files: dict[str, str] = {}
    for lib in _libraries():
        for rel, digest in lib["files"].items():
            assert rel not in files, f"{rel} is listed twice"
            files[rel] = digest
    return files


def test_every_listed_file_matches_its_hash():
    for rel, digest in _listed().items():
        path = VENDOR / rel
        assert path.is_file(), f"{rel} is listed in VENDOR.json but missing"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == digest, f"{rel} changed: {actual} != {digest}"


def test_no_unlisted_files():
    on_disk = {
        p.relative_to(VENDOR).as_posix()
        for p in VENDOR.rglob("*")
        if p.is_file() and p != MANIFEST
    }
    unlisted = sorted(on_disk - set(_listed()))
    assert not unlisted, f"files in app/ui/vendor not in VENDOR.json: {unlisted}"


def test_each_library_is_described_and_licensed():
    for lib in _libraries():
        for key in ("name", "version", "license", "source", "files"):
            assert lib.get(key), f"{lib.get('name')}: {key} is empty"
        assert (REPO / lib["licence_file"]).is_file(), f"{lib['name']}: {lib['licence_file']} missing"
        integrity = lib.get("upstream_integrity")
        assert integrity is None or integrity.startswith("sha512-"), lib["name"]


def test_versions_match_file_headers_and_notices():
    notices = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    header_file = {
        "DOMPurify": "purify.min.js",
        "marked": "marked.min.js",
        "highlight.js": "highlight.min.js",
    }
    for lib in _libraries():
        name, version = lib["name"], lib["version"]
        assert f"{name} {version}" in notices, f"THIRD_PARTY_NOTICES.md does not list {name} {version}"
        if name in header_file:
            head = (VENDOR / header_file[name]).read_bytes()[:400].decode("utf-8", "replace")
            # "v15.0.12" has no word boundary before the 1, so match on digits instead.
            assert re.search(rf"(?<![\d.]){re.escape(version)}(?![\d.])", head), f"{name} header does not say {version}"


def test_katex_ships_only_woff2_and_every_font_it_needs():
    css = (VENDOR / "katex" / "katex.min.css").read_text(encoding="utf-8")
    needed = set(re.findall(r"url\((fonts/[^)]+\.woff2)\)", css))
    assert len(needed) == 20
    shipped = {p.relative_to(VENDOR / "katex").as_posix() for p in (VENDOR / "katex" / "fonts").iterdir()}
    assert shipped == needed, f"fonts differ: extra {shipped - needed}, missing {needed - shipped}"


def test_vendor_files_are_never_line_ending_converted():
    rules = (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "app/ui/vendor/** -text" in rules
