"""Engine Manifest and Platform Discovery for InkDoc.

Implements data-driven platform discovery and status determination.
A platform is supported if and only if it has a verified entry in the bundled manifest.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def is_valid_sha256(val: str | None) -> bool:
    """Return True if val is a valid 64-character hex SHA-256 string."""
    if not val or not isinstance(val, str):
        return False
    cleaned = val.strip()
    if cleaned.lower().startswith("placeholder_"):
        return False
    return bool(_SHA256_HEX_RE.match(cleaned))


class EngineStatus(str, Enum):
    INSTALLED = "installed"
    INSTALLABLE = "installable"
    UNSUPPORTED = "unsupported"


@dataclass
class PlatformPackInfo:
    url: str
    archive_format: str  # "zip" or "tar.gz"
    sha256: str
    size_bytes: int
    uncompressed_size_bytes: int
    interpreter_path: str
    worker_script: str = "worker.py"
    sha256_files: dict[str, str] = field(default_factory=dict)


@dataclass
class EngineManifest:
    manifest_version: str
    pack_version: str
    min_app_version: str
    supported_platforms: dict[str, PlatformPackInfo] = field(default_factory=dict)

    def get_platform_pack(self, platform_key: str) -> PlatformPackInfo | None:
        return self.supported_platforms.get(platform_key)


def get_current_platform_key() -> str:
    """Determine normalized (os, arch) key for the current host environment.

    Examples: 'windows-x86_64', 'linux-x86_64', 'macos-arm64', 'macos-x86_64', 'windows-arm64'.
    """
    raw_os = sys.platform.lower()
    if raw_os.startswith("win"):
        os_name = "windows"
    elif raw_os.startswith("linux"):
        os_name = "linux"
    elif raw_os.startswith("darwin"):
        os_name = "macos"
    else:
        os_name = raw_os

    raw_arch = platform.machine().lower()

    # Normalization
    if raw_arch in ("amd64", "x86_64", "x64"):
        arch_name = "x86_64"
    elif raw_arch in ("arm64", "aarch64"):
        arch_name = "arm64"
    elif raw_arch in ("i386", "i686", "x86"):
        arch_name = "x86"
    else:
        arch_name = raw_arch

    # Detect Windows-on-ARM64 running x64 application under emulation
    if os_name == "windows":
        arch_w6432 = os.environ.get("PROCESSOR_ARCHITEW6432", "").lower()
        if arch_w6432 in ("arm64", "aarch64") and arch_name == "x86_64":
            # Running x64 emulation on Windows 11 ARM
            pass

    return f"{os_name}-{arch_name}"


def _locate_bundled_manifest_path() -> Path:
    """Locate the build-time bundled manifest.json across development and frozen environments."""
    # 1. Check frozen PyInstaller bundle
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundle_dir = Path(sys._MEIPASS)
        for candidate in [
            bundle_dir / "app" / "core" / "manifest.json",
            bundle_dir / "manifest.json",
        ]:
            if candidate.is_file():
                return candidate

    # 2. Check next to current module in development or onedir
    local_path = Path(__file__).resolve().parent / "manifest.json"
    if local_path.is_file():
        return local_path

    # 3. Check application root
    app_root = Path(__file__).resolve().parent.parent.parent / "manifest.json"
    if app_root.is_file():
        return app_root

    return local_path


def load_engine_manifest(manifest_path: Path | None = None) -> EngineManifest:
    """Load and parse the bundled EngineManifest from disk."""
    path = manifest_path or _locate_bundled_manifest_path()
    if not path.is_file():
        logger.warning("Engine manifest not found at %s. Defaulting to empty manifest.", path)
        return EngineManifest(manifest_version="1.0.0", pack_version="1.0.0", min_app_version="1.0.0")

    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        platforms: dict[str, PlatformPackInfo] = {}
        for plat_key, plat_data in data.get("supported_platforms", {}).items():
            platforms[plat_key] = PlatformPackInfo(
                url=plat_data.get("url", ""),
                archive_format=plat_data.get("archive_format", "zip"),
                sha256=plat_data.get("sha256", ""),
                size_bytes=int(plat_data.get("size_bytes", 0)),
                uncompressed_size_bytes=int(plat_data.get("uncompressed_size_bytes", 0)),
                interpreter_path=plat_data.get("interpreter_path", ""),
                worker_script=plat_data.get("worker_script", "worker.py"),
                sha256_files=plat_data.get("sha256_files", {}),
            )

        return EngineManifest(
            manifest_version=data.get("manifest_version", "1.0.0"),
            pack_version=data.get("pack_version", "1.0.0"),
            min_app_version=data.get("min_app_version", "1.0.0"),
            supported_platforms=platforms,
        )
    except Exception as exc:
        logger.exception("Failed to parse engine manifest at %s: %s", path, exc)
        return EngineManifest(manifest_version="1.0.0", pack_version="1.0.0", min_app_version="1.0.0")
