"""LICENSES/docling-pack-packages.md must describe the pack manifest.json names.

The first test is a statement about today's files, like test_build_addon's pin
check: when manifest.json moves to a new pack it fails until
`python scripts/pack_license_list.py --write` has been run and any new package's
row written by hand.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import pack_license_list as pll


def test_the_list_matches_the_shipped_pack():
    manifest = json.loads(pll.MANIFEST.read_text(encoding="utf-8"))
    problems, _ = pll.compare(manifest, pll.LIST.read_text(encoding="utf-8"))
    assert problems == []


LIST = """# Docling engine pack

InkDoc publishes it on [`docling-pack-v1`](https://github.com/AbdoslamB/InkDoc/releases/tag/docling-pack-v1).

This list was generated on 2026-10-01 from the per-file manifest (pack version 1).

| Package | Version | License (PyPI metadata) | Platforms | License file in pack |
| :--- | :--- | :--- | :--- | :--- |
| [docling](https://example.org) | 2.130.0 | MIT | Linux, Windows, macOS | supplement |
| [torch](https://pytorch.org) | 2.14.0 / 2.14.0+cpu | BSD | Linux, Windows, macOS | yes |
| gone_pkg | 1.0 | MIT | Linux, Windows, macOS | yes |
"""


def _manifest(packages: dict[str, dict[str, str]]) -> dict:
    platforms: dict[str, dict] = {}
    for name, per_platform in packages.items():
        for platform, version in per_platform.items():
            entry = platforms.setdefault(platform, {
                "url": f"https://github.com/AbdoslamB/InkDoc/releases/download/docling-pack-v2/docling-{platform}.zip",
                "sha256_files": {},
            })
            entry["sha256_files"][f"python/Lib/site-packages/{name}-{version}.dist-info/RECORD"] = "x"
    return {"pack_version": "2.0.0", "supported_platforms": platforms}


ALL = ("linux-x86_64", "windows-x86_64", "macos-arm64")


def test_a_new_pack_is_reported_and_written():
    manifest = _manifest({
        "docling": {p: "2.140.0" for p in ALL},
        "torch": {"linux-x86_64": "2.14.0+cpu", "windows-x86_64": "2.14.0+cpu", "macos-arm64": "2.14.0"},
        "new_pkg": {"windows-x86_64": "0.1"},
    })
    problems, updated = pll.compare(manifest, LIST)

    assert "docling: list says 2.130.0 (Linux, Windows, macOS), the pack has 2.140.0 (Linux, Windows, macOS)" in problems
    assert "gone_pkg: listed, but not in the pack" in problems
    assert any(p.startswith("new_pkg 0.1 (Windows): in the pack, not listed") for p in problems)
    assert not any(p.startswith("torch") for p in problems)

    assert "| [docling](https://example.org) | 2.140.0 | MIT | Linux, Windows, macOS | supplement |" in updated
    assert "releases/tag/docling-pack-v2" in updated and "`docling-pack-v2`" in updated
    assert "(pack version 2)" in updated
    assert "2026-10-01" not in updated
    # Rows are never added or removed: those need a person to read the license.
    assert "gone_pkg" in updated and "new_pkg" not in updated
