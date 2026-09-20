"""Check that release dependencies resolve from the build virtual environment."""

from __future__ import annotations

import argparse
import importlib
import os
from collections.abc import Iterable
from pathlib import Path

REQUIRED_PACKAGES = (
    "markitdown",
    "cryptography",
    "yaml",
    "pydantic",
    "fastapi",
    "uvicorn",
    "requests",
    "webview",
)
OPTIONAL_PACKAGES = ("httpx", "PIL", "numpy")


def check_packages(venv: Path, packages: Iterable[str], optional: Iterable[str]) -> int:
    """Print all package resolutions and return non-zero for shadowed packages."""
    resolved: list[tuple[str, str]] = []
    shadowed: list[tuple[str, str]] = []

    for name in packages:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            resolved.append((name, "not installed"))
            continue
        path = Path(module.__file__).resolve()
        path_text = str(path)
        resolved.append((name, path_text))
        if not path.is_relative_to(venv):
            shadowed.append((name, path_text))

    for name, path in resolved:
        print(f"{name}: {path}")

    yaml_path = dict(resolved).get("yaml")
    if yaml_path is not None:
        print(f"PyInstaller yaml source: {yaml_path}")

    if shadowed:
        details = "; ".join(f"{name}: {path}" for name, path in shadowed)
        print(f"Packages shadowing the venv: {details}")
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venv", type=Path, default=None)
    parser.add_argument("--packages", nargs="+", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    venv = (args.venv or Path(os.environ["VIRTUAL_ENV"])).resolve()
    packages = args.packages or [*REQUIRED_PACKAGES, *OPTIONAL_PACKAGES]
    return check_packages(venv, packages, OPTIONAL_PACKAGES)


if __name__ == "__main__":
    raise SystemExit(main())
