#!/usr/bin/env python3
"""Print one version's release notes, taken from CHANGELOG.md.

Usage:
    python scripts/changelog_notes.py v1.3.0 [--changelog CHANGELOG.md]

The release page (release.yml) and the signed update manifest
(sign_manifest.py) both use this section, so the notes the app shows under
Settings -> Updates are the CHANGELOG's own entries. GitHub's generated notes
were used before: with few pull requests they are little more than a
"Full Changelog" link, which is all the app could show.

Exits 1, printing nothing, when the CHANGELOG has no section for the version.
Standard library only: release.yml runs it on a bare runner.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# A link reference definition ("[1.2.0]: https://..."), as at the foot of the file.
_LINK_DEFINITION = re.compile(r"^\[[^\]]+\]:\s+\S+\s*$")


def changelog_section(text: str, version: str) -> str | None:
    """The body of `## [version]` (without the heading), or None if absent or empty.

    Ends at the next `## ` heading. The comparison links at the foot of the file
    are left out, so the oldest section doesn't carry them.
    """
    version = version.strip().lstrip("vV")
    if not version:
        return None
    heading = re.compile(rf"^## \[{re.escape(version)}\](?:\s|$)")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body: list[str] | None = None
    for line in lines:
        if body is None:
            if heading.match(line):
                body = []
            continue
        if line.startswith("## "):
            break
        if _LINK_DEFINITION.match(line):
            continue
        body.append(line.rstrip())
    if body is None:
        return None
    section = "\n".join(body).strip("\n")
    return section if section.strip() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version", help="Version or tag, e.g. 1.3.0 or v1.3.0")
    parser.add_argument("--changelog", type=Path, default=REPO_ROOT / "CHANGELOG.md")
    args = parser.parse_args(argv)

    try:
        text = args.changelog.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Can't read {args.changelog}: {exc}", file=sys.stderr)
        return 1
    section = changelog_section(text, args.version)
    if section is None:
        print(f"{args.changelog.name} has no section for {args.version}.", file=sys.stderr)
        return 1
    # Bytes, so the console's code page can't mangle or refuse the text.
    sys.stdout.buffer.write((section + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
