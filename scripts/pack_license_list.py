#!/usr/bin/env python3
"""Keep LICENSES/docling-pack-packages.md in step with the pack in app/core/manifest.json.

The list names every Python package in the Docling engine pack with its version,
license and platforms. It was written from the manifest's per-file hashes, which
name each package's dist-info folder, so it goes stale the moment manifest.json
moves to a new pack.

    python scripts/pack_license_list.py           # report differences, exit 1 if any
    python scripts/pack_license_list.py --write   # update versions, platforms and the pack tag

--write changes only what the manifest states: each row's Version and Platforms,
and the release tag, pack version and date in the introduction. A package added
to or removed from the pack is reported, never written: its license has to be
read (PyPI metadata and the files in its dist-info) by a person.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "app" / "core" / "manifest.json"
LIST = REPO_ROOT / "LICENSES" / "docling-pack-packages.md"

PLATFORM_LABELS = {"linux-x86_64": "Linux", "windows-x86_64": "Windows", "macos-arm64": "macOS"}
LABEL_ORDER = ["Linux", "Windows", "macOS"]

_DIST_INFO = re.compile(r"site-packages/([^/]+)-([^-/]+)\.dist-info/")
_ROW = re.compile(r"^\| (?P<name>\[[^\]]+\]\([^)]*\)|[^|]+?) \| (?P<version>[^|]+?) \| (?P<license>[^|]+?) "
                  r"\| (?P<platforms>[^|]+?) \| (?P<file>[^|]+?) \|$")


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def pack_packages(manifest: dict) -> dict[str, dict[str, str]]:
    """{normalised name: {platform: version}} from the dist-info folders the manifest names."""
    found: dict[str, dict[str, str]] = {}
    for platform, info in (manifest.get("supported_platforms") or {}).items():
        for path in info.get("sha256_files") or {}:
            match = _DIST_INFO.search(path)
            if match:
                found.setdefault(normalise(match.group(1)), {})[platform] = match.group(2)
    return found


def cells_for(versions: dict[str, str]) -> tuple[str, str]:
    """The Version and Platforms cells for one package, as the list writes them."""
    version = " / ".join(sorted(set(versions.values())))
    labels = {PLATFORM_LABELS.get(p, p) for p in versions}
    platforms = ", ".join([lab for lab in LABEL_ORDER if lab in labels] + sorted(labels - set(LABEL_ORDER)))
    return version, platforms


def _row_name(cell: str) -> str:
    match = re.match(r"\[([^\]]+)\]", cell)
    return normalise(match.group(1) if match else cell.strip())


def compare(manifest: dict, text: str) -> tuple[list[str], str]:
    """(problems, the list with versions, platforms and the introduction brought up to date)."""
    packages = pack_packages(manifest)
    problems: list[str] = []
    listed: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        row = _ROW.match(line)
        name = _row_name(row.group("name")) if row else ""
        if not row or name in ("package",) or set(row.group("version")) <= set(":- "):
            out.append(line)
            continue
        listed.add(name)
        if name not in packages:
            problems.append(f"{name}: listed, but not in the pack")
            out.append(line)
            continue
        version, platforms = cells_for(packages[name])
        if (row.group("version"), row.group("platforms")) != (version, platforms):
            problems.append(
                f"{name}: list says {row.group('version')} ({row.group('platforms')}), "
                f"the pack has {version} ({platforms})"
            )
            line = (f"| {row.group('name')} | {version} | {row.group('license')} | {platforms} "
                    f"| {row.group('file')} |")
        out.append(line)
    for name in sorted(set(packages) - listed):
        version, platforms = cells_for(packages[name])
        problems.append(f"{name} {version} ({platforms}): in the pack, not listed; read its license and add it")

    updated = "\n".join(out) + ("\n" if text.endswith("\n") else "")
    tag = _release_tag(manifest)
    if tag:
        new = re.sub(r"\[`docling-pack-v\d+`\]\(https://github\.com/AbdoslamB/InkDoc/releases/tag/docling-pack-v\d+\)",
                     f"[`{tag}`](https://github.com/AbdoslamB/InkDoc/releases/tag/{tag})", updated)
        if new != updated:
            problems.append(f"the introduction names another pack release than {tag}")
            updated = new
    major = str(manifest.get("pack_version", "")).split(".")[0]
    new = re.sub(r"\(pack version \d+\)", f"(pack version {major})", updated)
    if major and new != updated:
        problems.append(f"the introduction names another pack version than {major}")
        updated = re.sub(r"generated on \d{4}-\d{2}-\d{2}", f"generated on {datetime.date.today():%Y-%m-%d}", new)
    return problems, updated


def _release_tag(manifest: dict) -> str:
    for info in (manifest.get("supported_platforms") or {}).values():
        match = re.search(r"/releases/download/(docling-pack-v\d+)/", info.get("url", ""))
        if match:
            return match.group(1)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="update the list from the manifest")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    text = LIST.read_text(encoding="utf-8")
    problems, updated = compare(manifest, text)
    for problem in problems:
        print(f"[!] {problem}")
    if args.write and updated != text:
        LIST.write_text(updated, encoding="utf-8")
        print(f"[OK] Updated {LIST.relative_to(REPO_ROOT)}. Packages reported as not listed still need a row.")
    elif not problems:
        print(f"[OK] {LIST.relative_to(REPO_ROOT)} matches the pack in {MANIFEST.relative_to(REPO_ROOT)}.")
    unfixable = [p for p in problems if "not listed" in p or "not in the pack" in p]
    return 1 if (problems and not args.write) or unfixable else 0


if __name__ == "__main__":
    sys.exit(main())
