#!/usr/bin/env python3
"""Canonical version writer for InkDoc releases and builds.

Validates semantic version tags, updates app/_version.py with verified template
expansion, and verifies get_version() returns the expected version string.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Semantic Versioning regex (accepts v1.2.3, v1.2.3-rc1, v1.2.3+build, etc.)
SEMVER_TAG_REGEX = re.compile(
    r"^v(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)$"
)

VERSION_FILE_TEMPLATE = '''"""Canonical version definition for InkDoc.

Single source of truth for application version across desktop runner,
embedded FastAPI server, and updater.
"""
from __future__ import annotations

# CI updates this value before packaging. Default fallback for development checkouts.
__version__ = "{version}"


def get_version() -> str:
    """Return the current InkDoc version string."""
    return globals().get("__version__", "0.0.0+unknown")
'''


def get_git_short_sha() -> str:
    """Return short git commit SHA or fallback to dev."""
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if output:
            return output
    except Exception:
        pass
    return "dev"


def resolve_version_string(raw_tag: str | None, is_dispatch: bool = False, sha: str | None = None) -> str:
    """Resolve raw tag or dispatch flag into canonical version string.

    Rules:
    - Release tags must match v<semver> (e.g. v1.0.2, v1.2.3-rc1). The 'v' is stripped.
    - Manual dispatch or dev builds resolve to 0.0.0+dev.<short-sha>.
    - Anything else raises ValueError.
    """
    if is_dispatch or (raw_tag and raw_tag.lower() in ("dispatch", "dev", "manual")):
        commit_sha = sha or get_git_short_sha()
        return f"0.0.0+dev.{commit_sha}"

    if not raw_tag or not raw_tag.strip():
        raise ValueError("Version tag cannot be empty. Expected v<semver> (e.g. v1.2.3).")

    cleaned = raw_tag.strip()
    match = SEMVER_TAG_REGEX.match(cleaned)
    if not match:
        raise ValueError(
            f"Invalid version tag '{raw_tag}'. Must strictly match v<semver> "
            f"(e.g. v1.2.3, v1.2.3-rc1, v1.0.0-beta.1). Non-tag builds must use --dispatch."
        )

    return match.group("version")


def write_version_file(version: str, target_file: Path) -> None:
    """Write canonical version file with verified variable expansion."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    content = VERSION_FILE_TEMPLATE.format(version=version)
    target_file.write_text(content, encoding="utf-8")


def verify_written_version(version: str, target_file: Path) -> None:
    """Import get_version() from target file and assert it matches expected version."""
    spec = importlib.util.spec_from_file_location("inkdoc_version_check", str(target_file))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {target_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "get_version"):
        raise AttributeError(f"{target_file} is missing 'get_version()' function")

    actual_version = module.get_version()
    if actual_version != version:
        raise AssertionError(
            f"Version verification failed in {target_file}: expected '{version}', got '{actual_version}'"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write canonical version file for InkDoc builds.")
    parser.add_argument("tag", nargs="?", default=None, help="Raw version tag (e.g. v1.0.2).")
    parser.add_argument("--dispatch", action="store_true", help="Build for manual workflow dispatch or dev.")
    parser.add_argument("--sha", default=None, help="Short commit SHA for dispatch builds.")
    parser.add_argument(
        "--target",
        type=Path,
        default=REPO_ROOT / "app" / "_version.py",
        help="Target version file path (default: app/_version.py).",
    )

    args = parser.parse_args(argv)

    raw_tag = args.tag or os.environ.get("GITHUB_REF_NAME")
    try:
        resolved = resolve_version_string(raw_tag, is_dispatch=args.dispatch, sha=args.sha)
    except ValueError as err:
        print(f"::error::{err}", file=sys.stderr)
        return 1

    write_version_file(resolved, args.target)
    verify_written_version(resolved, args.target)
    print(f"Successfully wrote and verified InkDoc version '{resolved}' in {args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
