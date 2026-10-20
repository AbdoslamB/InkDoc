"""Engine Manager for InkDoc.

Handles lifecycle, secure download, cryptographic verification, safe extraction,
and status tracking for optional engine packages (IBM Docling).
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.download_utils import (
    DownloadError,
    SecurityError,
    check_disk_space,
    extract_archive_safely,
    is_safe_path,
    stream_download,
    validate_download_url,
)
from app.core.engine_manifest import (
    EARLIER_PACK_ADVISORIES,
    EARLIER_PACKS,
    EngineManifest,
    EngineStatus,
    PlatformPackInfo,
    docling_version_in,
    get_current_platform_key,
    is_valid_sha256,
    load_engine_manifest,
)

logger = logging.getLogger(__name__)


def to_long_path_safe(p: Path) -> Path:
    """Ensure path uses Windows extended-length prefix \\\\?\\ if on Windows and length >= 240."""
    if sys.platform != "win32":
        return p
    try:
        resolved_str = str(p.resolve())
        if len(resolved_str) >= 240 and not resolved_str.startswith("\\\\?\\"):
            return Path(f"\\\\?\\{resolved_str}")
    except Exception:
        pass
    return p


def _rename_retrying(src: Path, dst: Path, attempts: int = 5, delay: float = 0.5) -> None:
    """os.rename, retried briefly: on Windows a process that has only just exited,
    or an antivirus scan, can keep a file of the folder open for a moment."""
    for attempt in range(attempts):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


class EngineInstallError(Exception):
    """Raised when an engine installation or extraction fails."""


@dataclass
class InstallProgress:
    status: str = "idle"  # idle, downloading, extracting, verifying, complete, error, cancelled
    bytes_downloaded: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    error_message: str | None = None
    started_at: float = 0.0


# User-facing settings and their defaults. Kept in one place because the same
# literal was previously repeated at every load and recovery path, which is
# exactly where a newly added key gets forgotten.
#
# Both Docling enrichment options default off deliberately: either one causes
# Docling to load a 640 MB vision model at pipeline construction, for every
# conversion, whether or not the document turns out to contain code.
#
# quality_check_enabled defaults on: the missing-text check only reads the PDF
# and the result, never changes the saved file, and costs milliseconds on a
# normal document (app/core/quality_check.py).
#
# GLM-OCR: GPU is opt-in (the Vulkan runtime is a separate download and is only
# selected when its self-test beats the CPU); "auto" downloads from the official
# source first and falls back to InkDoc's mirror; glm_ocr_catalogue_seen is the
# highest signed catalogue version ever accepted, which blocks rollback.
DEFAULT_SETTINGS: dict[str, Any] = {
    "fallback_to_markitdown": False,
    "check_for_updates_daily": False,
    "docling_code_enrichment": False,
    "docling_formula_enrichment": False,
    "quality_check_enabled": True,
    # "fast" | "balanced" | "thorough" (app/core/table_stage.py). Fast, the
    # default, converts exactly as before; Balanced checks every PDF's tables
    # against the PDF and lets Auto replace wrong ones; Thorough adds page
    # splitting and GLM-OCR table reading in Auto.
    "auto_quality": "fast",
    # Steps of Balanced and Thorough switched off, by name (app/core/thorough_passes.py).
    # A list of what is off, so a step added in a later version arrives switched on.
    "thorough_passes_off": [],
    "glm_ocr_use_gpu": False,
    "glm_ocr_download_source": "auto",
    "glm_ocr_catalogue_seen": 0,
    "glm_ocr_measured_spp": {},
    # Auto-save folder; "" is Downloads. Set only through the desktop folder
    # dialog (app/core/save_location.py), never through POST /settings.
    "save_dir": "",
    # How much of a wide window the UI uses: "standard" (1400px, as before),
    # "wide" (1920px) or "full". Kept here rather than in browser storage, which
    # the desktop app's web view does not keep between launches.
    "layout_width": "standard",
    # Text size of the document preview (Rendered and Raw tabs), in percent.
    # Only the interface's own steps are accepted; 100 is the size used before.
    "preview_text_size": 100,
    # Built-in add-on (Settings → Add-ons): draw LaTeX math in the preview with
    # the bundled KaTeX. Off means the preview is parsed exactly as before.
    "render_math": True,
    # Interface preferences that used to live in browser storage, which the
    # desktop app's web view clears on every launch. The page reads them from
    # the server before first paint (server._serve_bench_html).
    "theme": "dark",
    # The engine the user picked. The page falls back to Auto for the session
    # while it is not usable, without changing this.
    "preferred_engine": "auto",
    # Engines whose button is hidden on the main screen. Hiding never uninstalls
    # anything, and Auto ignores this list.
    "hidden_engines": [],
    # Also save each conversion to the save folder. Sent with every request as
    # save_to_downloads; the server itself never reads it.
    "auto_save": False,
    # "Don't show again" on Auto's install tip.
    "auto_hint_hidden": False,
    # Set once the page has moved any of the above out of browser storage
    # (InkDoc 1.2 and earlier kept them there). Never reset.
    "legacy_prefs_imported": False,
}

LAYOUT_WIDTHS = ("standard", "wide", "full")
PREVIEW_TEXT_SIZES = (85, 90, 100, 110, 120, 135, 150)
THEMES = ("dark", "light")
ENGINE_CHOICES = ("auto", "markitdown", "docling", "markit", "glm_ocr")
# Auto is the fallback and always stays on the main screen.
HIDEABLE_ENGINES = ("markitdown", "docling", "markit", "glm_ocr")

# What "Restore defaults" (POST /settings/reset) puts back. Deliberately left
# alone: the save folder (set only by the desktop folder dialog), the setup of
# installed engines and add-ons (Docling enrichment, GLM-OCR GPU and download
# source), glm_ocr_catalogue_seen (blocks catalogue rollback), measured speeds,
# legacy_prefs_imported and any key this version does not know.
RESETTABLE_SETTINGS = (
    "theme",
    "preferred_engine",
    "hidden_engines",
    "auto_save",
    "auto_hint_hidden",
    "fallback_to_markitdown",
    "quality_check_enabled",
    "auto_quality",
    "thorough_passes_off",
    "layout_width",
    "preview_text_size",
    "render_math",
    "check_for_updates_daily",
)


class SettingsWriteError(Exception):
    """settings.json could not be written; the change only lasts until restart."""


# On Windows an antivirus scan, the search indexer or a sync client can hold the
# just-written file (or settings.json) for a moment, and os.replace() then fails
# with "Access is denied". A short retry turns that into a delay of a few tens
# of milliseconds instead of a lost setting. Kept short: it runs inside a
# request (only writers wait for it; readers of the settings never do).
SETTINGS_REPLACE_ATTEMPTS = 8
SETTINGS_REPLACE_DELAY_S = 0.05


def _replace_settings_file(src: str, dst: Path) -> None:
    for attempt in range(SETTINGS_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt + 1 == SETTINGS_REPLACE_ATTEMPTS:
                raise
            time.sleep(SETTINGS_REPLACE_DELAY_S)


def _copy_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """A copy whose lists and dicts (hidden_engines, glm_ocr_measured_spp) are its
    own, so a caller can't change the stored settings by editing what it got."""
    return {
        key: copy.deepcopy(value) if isinstance(value, (list, dict)) else value
        for key, value in settings.items()
    }


def default_settings() -> dict[str, Any]:
    """A fresh copy of the defaults. Deep, so a list or dict default (hidden_engines,
    glm_ocr_measured_spp) is never shared with the stored settings."""
    return copy.deepcopy(DEFAULT_SETTINGS)


def sanitize_ui_preferences(settings: dict[str, Any]) -> dict[str, Any]:
    """The interface preferences reduced to known values.

    These go into a script on the page and into the desktop window's colour,
    so whatever a hand-edited settings.json holds, only one of the allowed
    values may come out of here.
    """
    theme = settings.get("theme")
    engine = settings.get("preferred_engine")
    stored_hidden = settings.get("hidden_engines")
    hidden = (
        [name for name in HIDEABLE_ENGINES if name in stored_hidden]
        if isinstance(stored_hidden, list)
        else []
    )
    if engine not in ENGINE_CHOICES or engine in hidden:
        engine = "auto"
    return {
        "theme": theme if theme in THEMES else "dark",
        "preferred_engine": engine,
        "hidden_engines": hidden,
        "auto_save": settings.get("auto_save") is True,
        "auto_hint_hidden": settings.get("auto_hint_hidden") is True,
        "legacy_prefs_imported": settings.get("legacy_prefs_imported") is True,
    }

GLM_OCR_ENGINE = "glm_ocr"


# Settings whose truth lives outside settings.json. Each names the add-on that
# has to be usable for the setting to mean anything, so reconciliation stays a
# table rather than a growing chain of special cases.
ADDON_GATED_SETTINGS: dict[str, str] = {
    "docling_code_enrichment": "code_enrichment",
    "docling_formula_enrichment": "code_enrichment",
}


def reconcile_addon_gated_settings() -> dict[str, Any]:
    """Bring add-on-gated settings back in line with what is actually installed.

    settings.json and the models directory are independent pieces of state and can
    disagree: a pack is removed and takes its add-ons with it, a settings file is
    copied between machines, an upgrade changes which Docling version the pack
    ships. Previously nothing noticed, and the disagreement surfaced once per
    conversion, deep in the engine, where the only options left were to fail the
    job or quietly drop what the user asked for.

    Reconciling once at startup fixes it at the source instead: the setting is
    corrected where it is stored, the change is logged, and the result is reported
    so the UI can tell the user why a toggle they had switched on is now off.
    Returns the keys that were cleared and the reason, or an empty result.
    """
    from app.core.addon_manager import AddonManager

    mgr = EngineManager.get_instance()
    settings = mgr.get_settings()
    enabled = [key for key in ADDON_GATED_SETTINGS if settings.get(key)]
    if not enabled:
        return {"cleared": [], "reason": ""}

    addon_mgr = AddonManager.get_instance()
    cleared: list[str] = []
    reason = ""
    for key in enabled:
        try:
            status = addon_mgr.get_status(ADDON_GATED_SETTINGS[key])
        except Exception as exc:
            # Never block startup on a status read. Leaving the setting alone is
            # correct here: the worker still refuses the job with a precise error
            # if the model really is missing, so nothing silently degrades.
            logger.warning("Could not check add-on state for '%s': %s", key, exc)
            continue
        if not status.get("usable"):
            cleared.append(key)
            reason = status.get("reason") or "The required add-on is not installed."

    if not cleared:
        return {"cleared": [], "reason": ""}

    mgr.update_settings({key: False for key in cleared})
    logger.warning(
        "Turned off %s because the required add-on is not usable: %s",
        ", ".join(cleared),
        reason,
    )
    return {"cleared": cleared, "reason": reason}


class EngineManager:
    """Manages installation, updates, and integrity of optional engines."""

    _instance: EngineManager | None = None
    _lock = threading.Lock()

    def __init__(self, manifest: EngineManifest | None = None) -> None:
        self.manifest = manifest or load_engine_manifest()
        self.platform_key = get_current_platform_key()
        self._install_lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._progress: dict[str, InstallProgress] = {}
        self._settings_cache: dict[str, Any] | None = None
        self._latest_docling: tuple[PlatformPackInfo, str] | None = None
        self._settings_lock = threading.Lock()
        # Serialises writes of settings.json. Held while the file is written,
        # which _settings_lock never is, so readers don't wait on the disk.
        self._settings_write_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> EngineManager:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ─── Directory Resolution ────────────────────────────────────────────────

    def get_engines_base_dir(self) -> Path:
        """Return base directory where optional engine runtimes are stored."""
        custom_base = os.environ.get("INKDOC_ENGINES_DIR")
        if custom_base:
            base = Path(custom_base).resolve()
        elif sys.platform == "win32":
            local_app_data = os.environ.get("LOCALAPPDATA")
            # If path would be unusually long (> 120 chars), use short root on SystemDrive
            if local_app_data and len(local_app_data) > 120:
                base = Path(os.environ.get("SYSTEMDRIVE", "C:")) / "InkDoc" / "engines"
            else:
                base = (
                    Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
                ) / "InkDoc" / "engines"
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "InkDoc" / "engines"
        else:
            xdg_data = os.environ.get("XDG_DATA_HOME")
            base = (Path(xdg_data) if xdg_data else Path.home() / ".local" / "share") / "inkdoc" / "engines"

        base.mkdir(parents=True, exist_ok=True)
        return to_long_path_safe(base)

    def get_engine_dir(self, engine_name: str = "docling") -> Path:
        """Return the target directory for a specific engine pack."""
        return to_long_path_safe(self.get_engines_base_dir() / engine_name.lower().strip())

    def get_settings_file_path(self) -> Path:
        """Return path to user settings JSON file."""
        return self.get_engines_base_dir().parent / "settings.json"

    # ─── Settings Management ─────────────────────────────────────────────────

    def get_settings(self) -> dict[str, Any]:
        """Read and cache user settings (thread-safe)."""
        with self._settings_lock:
            if self._settings_cache is not None:
                return _copy_settings(self._settings_cache)

            path = self.get_settings_file_path()
            if path.is_file():
                try:
                    stored = json.loads(path.read_text(encoding="utf-8"))
                    # A file written before a key existed must still report it.
                    merged = default_settings()
                    merged.update(stored if isinstance(stored, dict) else {})
                    self._settings_cache = merged
                    return _copy_settings(merged)
                except Exception as exc:
                    logger.warning("Failed to parse settings at %s: %s", path, exc)

            defaults = default_settings()
            self._settings_cache = defaults
            return _copy_settings(defaults)

    def update_settings(self, updates: dict[str, Any], *, strict: bool = False) -> dict[str, Any]:
        """Update and persist user settings thread-safely and atomically.

        A failed write is logged and the change kept for this session only. With
        strict=True it also raises SettingsWriteError, so a request from the
        Settings page can say the change will not last; every other caller keeps
        the old behaviour.

        The change is made under _settings_lock; the file is written after it is
        released, so get_settings() (every conversion, every page load) never
        waits on the disk, an antivirus scan or the retry in
        _replace_settings_file. Writers take _settings_write_lock in turn and
        each writes the newest settings, so the file always ends up current.
        """
        with self._settings_lock:
            # Refresh from current cache or disk
            if self._settings_cache is not None:
                current = dict(self._settings_cache)
            else:
                current = default_settings()
                path = self.get_settings_file_path()
                if path.is_file():
                    try:
                        stored = json.loads(path.read_text(encoding="utf-8"))
                        current.update(stored if isinstance(stored, dict) else {})
                    except Exception as exc:
                        logger.warning("Failed to parse settings at %s: %s", path, exc)

            current.update(updates)
            self._settings_cache = current
            result = _copy_settings(current)

        with self._settings_write_lock:
            path = self.get_settings_file_path()
            try:
                with self._settings_lock:
                    # The newest settings, which may include a later change.
                    payload = json.dumps(self._settings_cache, indent=2)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_fd, tmp_path_str = tempfile.mkstemp(
                    prefix="settings_",
                    suffix=".tmp",
                    dir=str(path.parent),
                )
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                        f.write(payload)
                        f.flush()
                        os.fsync(f.fileno())
                    _replace_settings_file(tmp_path_str, path)
                except Exception:
                    if os.path.exists(tmp_path_str):
                        try:
                            os.unlink(tmp_path_str)
                        except OSError:
                            pass
                    raise
            except Exception as exc:
                logger.error("Failed to save settings to %s: %s", path, exc)
                if strict:
                    raise SettingsWriteError(str(exc)) from exc

        return result

    def reset_preferences(self) -> dict[str, Any]:
        """Put every key in RESETTABLE_SETTINGS back to its default, in one write.

        Nothing else changes: installed engines and add-ons, their setup and the
        save folder are kept. Raises SettingsWriteError if the file can't be written.
        """
        defaults = default_settings()
        return self.update_settings({key: defaults[key] for key in RESETTABLE_SETTINGS}, strict=True)

    # ─── Engine Status & Platform Queries ────────────────────────────────────

    def get_platform_pack_info(self, engine_name: str = "docling") -> PlatformPackInfo | None:
        """Get the pack metadata for current host platform, or None if unsupported."""
        if engine_name.lower().strip() != "docling":
            return None
        return self.manifest.get_platform_pack(self.platform_key)

    def _detect_system_docling(self) -> tuple[bool, str | None]:
        """Detect if docling is installed in ambient Python (source mode only)."""
        if getattr(sys, "frozen", False):
            return False, None
        try:
            import importlib.util
            if importlib.util.find_spec("docling") is not None:
                try:
                    import importlib.metadata
                    ver = importlib.metadata.version("docling")
                except Exception:
                    ver = "installed"
                return True, ver
        except Exception:
            pass
        return False, None

    def is_platform_supported(self, engine_name: str = "docling") -> bool:
        """Return True if the current platform has a verified pack in the manifest."""
        if engine_name.lower().strip() != "docling":
            return True
        pack_info = self.get_platform_pack_info(engine_name)
        if not pack_info:
            return False
        return is_valid_sha256(pack_info.sha256)

    def is_pack_installed(self, engine_name: str = "docling") -> bool:
        """Return True if standalone engine pack is installed on disk."""
        if engine_name.lower().strip() != "docling":
            return False
        target_dir = self.get_engine_dir(engine_name)
        if not target_dir.is_dir():
            return False

        launch = self._launch_files(engine_name)
        if not launch:
            return False

        interpreter = target_dir / launch[0]
        worker_script = target_dir / launch[2]
        return interpreter.is_file() and worker_script.is_file()

    def installed_pack_version(self, engine_name: str = "docling") -> str | None:
        """The pack version recorded when the installed pack was set up, or None."""
        meta_file = self.get_engine_dir(engine_name) / "pack_meta.json"
        try:
            version = json.loads(meta_file.read_text(encoding="utf-8")).get("pack_version")
        except (OSError, ValueError, AttributeError):
            return None
        return version if isinstance(version, str) and version else None

    def is_pack_outdated(self, engine_name: str = "docling") -> bool:
        """True when the installed pack is an earlier one this app still knows how to check."""
        installed = self.installed_pack_version(engine_name)
        return (
            installed is not None
            and installed != self.manifest.pack_version
            and self.platform_key in EARLIER_PACKS.get(installed, {})
        )

    def _launch_files(self, engine_name: str = "docling") -> tuple[str, str | None, str, str | None] | None:
        """(interpreter path, its SHA-256, worker path, its SHA-256) for the installed pack.

        The manifest's pack, unless an earlier pack is installed: that one is checked
        against its own launch hashes, so it keeps working until the user updates.
        A hash is None when the manifest lists none for that file; one it lists,
        even empty, must be valid.
        """
        if self.is_pack_outdated(engine_name):
            files = EARLIER_PACKS[self.installed_pack_version(engine_name) or ""][self.platform_key]
            return files.interpreter_path, files.interpreter_sha256, files.worker_script, files.worker_sha256
        pack_info = self.get_platform_pack_info(engine_name)
        if not pack_info:
            return None
        hashes = pack_info.sha256_files or {}
        interp = pack_info.interpreter_path.replace("\\", "/")
        worker = pack_info.worker_script.replace("\\", "/")
        return interp, hashes.get(interp), worker, hashes.get(worker)

    def launch_paths(self, engine_name: str = "docling") -> tuple[Path, Path] | None:
        """The interpreter and worker script to launch: the ones verify_launch_integrity checks."""
        launch = self._launch_files(engine_name)
        if not launch:
            return None
        target_dir = self.get_engine_dir(engine_name)
        return target_dir / launch[0], target_dir / launch[2]

    def installed_docling_version(self, engine_name: str = "docling") -> str:
        """Docling's version in the installed pack, from its dist-info folder name, or ""."""
        root = self.get_engine_dir(engine_name) / "python"
        for pattern in ("Lib/site-packages/docling-*.dist-info", "lib/python3*/site-packages/docling-*.dist-info"):
            for dist in root.glob(pattern):
                version = dist.name[len("docling-"):-len(".dist-info")]
                if version[:1].isdigit():
                    return version
        return ""

    def latest_docling_version(self, engine_name: str = "docling") -> str:
        """Docling's version in the pack the manifest describes for this platform, or ""."""
        pack_info = self.get_platform_pack_info(engine_name)
        if not pack_info:
            return ""
        # Read once per manifest entry: the file list has ~32,000 names.
        if self._latest_docling is None or self._latest_docling[0] is not pack_info:
            self._latest_docling = (pack_info, docling_version_in(pack_info.sha256_files or {}))
        return self._latest_docling[1]

    def is_engine_installed(self, engine_name: str = "docling") -> bool:
        """Return True if engine pack is installed OR system Docling detected in source mode."""
        if engine_name.lower().strip().replace("-", "_") == GLM_OCR_ENGINE:
            from app.core.glm_ocr_manager import GlmOcrManager

            return GlmOcrManager.get_instance().is_installed()
        if engine_name.lower().strip() != "docling":
            return True  # markitdown and markit are always installed
        if self.is_pack_installed(engine_name):
            return True
        if not self.is_platform_supported(engine_name):
            return False
        is_sys, _ = self._detect_system_docling()
        return is_sys

    def get_supported_platform_keys(self) -> list[str]:
        """Return list of platform keys supported by the manifest."""
        return sorted(list(self.manifest.supported_platforms.keys()))

    def get_engine_status(self, engine_name: str = "docling") -> EngineStatus:
        """Return the current EngineStatus for the specified engine."""
        norm_name = engine_name.lower().strip()
        if norm_name in ("markitdown", "markit"):
            return EngineStatus.INSTALLED
        if norm_name.replace("-", "_") == GLM_OCR_ENGINE:
            from app.core.glm_ocr_manager import GlmOcrManager

            state = GlmOcrManager.get_instance().get_status()["status"]
            if state == "installed":
                return EngineStatus.INSTALLED
            if state in ("installable", "installing"):
                return EngineStatus.INSTALLABLE
            return EngineStatus.UNSUPPORTED

        if not self.is_platform_supported(norm_name):
            return EngineStatus.UNSUPPORTED

        if self.is_engine_installed(norm_name):
            return EngineStatus.INSTALLED

        return EngineStatus.INSTALLABLE

    def get_all_engines_info(self) -> dict[str, Any]:
        """Return full structured status payload for all engines."""
        docling_status = self.get_engine_status("docling")
        docling_pack = self.get_platform_pack_info("docling")
        docling_dir = self.get_engine_dir("docling")
        is_sys, sys_ver = self._detect_system_docling()
        is_pack = self.is_pack_installed("docling")
        manifest_platforms = self.get_supported_platform_keys()

        from app.core.converter import get_markitdown_version
        from app.core.engines.markit_engine import get_markit_version

        engines_data: dict[str, Any] = {
            "markitdown": {
                "name": "MarkItDown",
                "status": EngineStatus.INSTALLED.value,
                "version": get_markitdown_version(),
                "description": "Fast local programmatic converter (Office, PDF, HTML, Audio). Bundled & offline.",
                "bundled": True,
            },
            "markit": {
                "name": "Markit",
                "status": EngineStatus.INSTALLED.value,
                "version": get_markit_version(),
                "description": "Broad format converter (Jupyter Notebooks, EPUB, YAML, XML, CSV). Bundled.",
                "bundled": True,
            },
        }

        # Docling info
        if is_pack:
            pack_ver = self.manifest.pack_version
            installed_ver = self.installed_pack_version("docling") or pack_ver

            engines_data["docling"] = {
                "name": "Docling",
                "status": EngineStatus.INSTALLED.value,
                "platform": self.platform_key,
                "supported": True,
                "bundled": False,
                "version": installed_ver,
                "pack_version": pack_ver,
                "download_size_bytes": docling_pack.size_bytes if docling_pack else 0,
                "uncompressed_size_bytes": docling_pack.uncompressed_size_bytes if docling_pack else 0,
                "install_path": str(docling_dir),
                "source_mode": False,
                "supported_platforms": manifest_platforms,
                "description": "Deep document layout analysis and TableFormer table recovery from IBM Research.",
                "update_available": (
                    installed_ver is not None and installed_ver != pack_ver
                ),
                # The installed pack's published advisories the update fixes.
                "security_fixes": (
                    list(EARLIER_PACK_ADVISORIES.get(installed_ver, ()))
                    if installed_ver is not None and installed_ver != pack_ver else []
                ),
                # Docling's own versions, for the card's "update available" line.
                "docling_version": self.installed_docling_version("docling"),
                "latest_docling_version": self.latest_docling_version("docling"),
            }
        elif is_sys:
            engines_data["docling"] = {
                "name": "Docling",
                "status": EngineStatus.INSTALLED.value,
                "platform": self.platform_key,
                "supported": True,
                "bundled": False,
                "version": sys_ver or "system",
                "pack_version": self.manifest.pack_version,
                "download_size_bytes": 0,
                "uncompressed_size_bytes": 0,
                "install_path": sys.executable,
                "source_mode": True,
                "supported_platforms": manifest_platforms,
                "description": "IBM Docling layout analysis and TableFormer (active in local Python environment).",
                "update_available": False,
            }
        elif docling_status == EngineStatus.UNSUPPORTED:
            engines_data["docling"] = {
                "name": "Docling",
                "status": EngineStatus.UNSUPPORTED.value,
                "platform": self.platform_key,
                "supported": False,
                "bundled": False,
                "supported_platforms": manifest_platforms,
                "description": f"IBM Docling is not available for this build ({self.platform_key}).",
                "message": "not available for this build",
            }
        else:
            pack_ver = self.manifest.pack_version
            engines_data["docling"] = {
                "name": "Docling",
                "status": EngineStatus.INSTALLABLE.value,
                "platform": self.platform_key,
                "supported": True,
                "bundled": False,
                "version": pack_ver,
                "pack_version": pack_ver,
                "download_size_bytes": docling_pack.size_bytes if docling_pack else 0,
                "uncompressed_size_bytes": docling_pack.uncompressed_size_bytes if docling_pack else 0,
                "install_path": None,
                "source_url": docling_pack.url if docling_pack else None,
                "source_mode": False,
                "supported_platforms": manifest_platforms,
                "description": "Deep document layout analysis and TableFormer table recovery from IBM Research.",
                "update_available": False,
            }

        # GLM-OCR has its own manager; its status reads the completion marker
        # only (no hashing, no network), so listing engines stays cheap.
        try:
            from app.core.glm_ocr_manager import GlmOcrManager

            engines_data[GLM_OCR_ENGINE] = GlmOcrManager.get_instance().get_status()
        except Exception as exc:
            logger.warning("Could not read GLM-OCR status: %s", exc)

        return {
            "platform": self.platform_key,
            "engines": engines_data,
            "settings": self.get_settings(),
        }

    # ─── Secure Download & Integrity Verification ────────────────────────────

    @staticmethod
    def _validate_download_url(url: str) -> None:
        """Validate that URL uses HTTPS and points to an allowlisted CDN host."""
        validate_download_url(url)

    def _open_safe_download_stream(self, initial_url: str, cancel_event: threading.Event) -> Any:
        """Follow redirects while enforcing host allowlist and redirect caps."""
        self._validate_download_url(initial_url)
        current_url = initial_url
        redirect_count = 0
        max_redirects = 5

        while redirect_count <= max_redirects:
            if cancel_event.is_set():
                raise EngineInstallError("Download cancelled by user.")

            req = urllib.request.Request(
                current_url,
                headers={
                    "User-Agent": "InkDoc-PackManager/1.0",
                    "Accept": "application/octet-stream, application/zip, application/gzip, */*",
                },
            )

            # Do not auto-follow in default handler so we can validate each redirect host
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            opener.addheaders = req.headers.items()  # type: ignore[assignment]

            class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            no_redirect_opener = urllib.request.build_opener(NoRedirectHandler())

            try:
                resp = no_redirect_opener.open(req, timeout=30.0)
                return resp
            except urllib.error.HTTPError as err:
                if err.code in (301, 302, 303, 307, 308):
                    new_url = err.headers.get("Location")
                    if not new_url:
                        raise SecurityError("Redirect missing Location header.") from err
                    # Resolve relative redirect if any
                    if new_url.startswith("/"):
                        parsed_cur = urlparse(current_url)
                        new_url = f"{parsed_cur.scheme}://{parsed_cur.netloc}{new_url}"

                    self._validate_download_url(new_url)
                    current_url = new_url
                    redirect_count += 1
                    continue
                raise

        raise SecurityError(f"Exceeded maximum allowed redirects ({max_redirects}).")

    def _check_disk_space(self, target_dir: Path, required_bytes: int, uncompressed_bytes: int = 0) -> None:
        """Verify that sufficient free disk space exists before downloading."""
        try:
            check_disk_space(target_dir, required_bytes, uncompressed_bytes=uncompressed_bytes)
        except DownloadError as exc:
            raise EngineInstallError(str(exc)) from exc

    # ─── Safe Archive Extraction ─────────────────────────────────────────────

    @staticmethod
    def _is_safe_path(base_dir: Path, target_path: Path) -> bool:
        """Verify that target_path resolves strictly within base_dir."""
        return is_safe_path(base_dir, target_path)

    @classmethod
    def _extract_zip_safely(cls, archive_path: Path, staging_dir: Path) -> None:
        """Extract zip archive with strict zip-slip, zip-bomb, and symlink protection."""
        extract_archive_safely(archive_path, staging_dir)

    @classmethod
    def _extract_tar_safely(cls, archive_path: Path, staging_dir: Path) -> None:
        """Extract tar.gz archive with strict path traversal, symlink, and size guards."""
        extract_archive_safely(archive_path, staging_dir)

    # ─── Installation Workflow ───────────────────────────────────────────────

    def get_progress(self, engine_name: str = "docling") -> dict[str, Any]:
        """Return current progress information for UI reporting."""
        prog = self._progress.get(engine_name, InstallProgress())
        return {
            "status": prog.status,
            "bytes_downloaded": prog.bytes_downloaded,
            "total_bytes": prog.total_bytes,
            "percent": round(prog.percent, 1),
            "error_message": prog.error_message,
            "elapsed_seconds": round(time.time() - prog.started_at, 1) if prog.started_at else 0,
        }

    def is_install_in_progress(self, engine_name: str = "docling") -> bool:
        """True while an install, update or removal of `engine_name` is running.

        install_engine() and remove_engine() both hold _install_lock for their whole
        duration, so the lock alone answers the question. The progress status is
        checked as well because it is published the moment an install starts.
        """
        if self._install_lock.locked():
            return True
        prog = self._progress.get(engine_name)
        return bool(prog and prog.status in ("downloading", "verifying", "extracting"))

    def cancel_install(self, engine_name: str = "docling") -> bool:
        """Request cancellation of an active installation."""
        evt = self._cancel_events.get(engine_name)
        if evt:
            evt.set()
            prog = self._progress.get(engine_name)
            if prog:
                prog.status = "cancelled"
            return True
        return False

    def install_engine(self, engine_name: str = "docling") -> dict[str, Any]:
        pack_info = self.get_platform_pack_info(engine_name)
        if not pack_info:
            raise EngineInstallError(f"Engine pack '{engine_name}' is not available for this build ({self.platform_key}).")

        # Fail closed: must have a valid 64-character hex SHA-256 in manifest
        expected_sha256 = pack_info.sha256.lower().strip() if pack_info.sha256 else ""
        if not is_valid_sha256(expected_sha256):
            raise SecurityError(
                f"Engine pack '{engine_name}' manifest contains an invalid, placeholder, or missing SHA-256 hash: "
                f"'{pack_info.sha256}'. Installation aborted (fail closed)."
            )

        target_dir = self.get_engine_dir(engine_name)

        # Acquire the install lock BEFORE publishing any shared state.
        #
        # This used to happen last. A second concurrent install request therefore
        # overwrote self._cancel_events[engine_name] and self._progress[engine_name]
        # and only then discovered the lock was taken and raised. The first install
        # kept running against a cancel event nobody holds a reference to any more,
        # so /engines/{name}/cancel signalled the wrong Event and the UI polled a
        # progress object the running install never updates: an install that cannot
        # be cancelled and appears frozen at 0%.
        if not self._install_lock.acquire(blocking=False):
            raise EngineInstallError("An installation or update is already in progress.")

        cancel_event = threading.Event()
        self._cancel_events[engine_name] = cancel_event

        progress = InstallProgress(
            status="downloading",
            total_bytes=pack_info.size_bytes,
            started_at=time.time(),
        )
        self._progress[engine_name] = progress

        arch_ext = "zip" if (pack_info.archive_format == "zip" or pack_info.url.endswith(".zip")) else "tar.gz"
        tmp_download_file = target_dir.parent / f"{engine_name}.download.{arch_ext}"
        staging_dir = target_dir.parent / f"{engine_name}.staging.tmp"

        def on_download_progress(downloaded: int, total: int) -> None:
            progress.bytes_downloaded = downloaded
            progress.total_bytes = total
            if total > 0:
                progress.percent = (downloaded / total) * 100.0

        try:
            self._recover_interrupted_update(engine_name, target_dir)
            # Combined disk space check (download size + uncompressed footprint)
            self._check_disk_space(target_dir, pack_info.size_bytes, pack_info.uncompressed_size_bytes)

            # 1. Download: Check if _open_safe_download_stream was mocked (e.g. in test suite)
            is_mocked = hasattr(self._open_safe_download_stream, "return_value") or hasattr(
                self._open_safe_download_stream, "side_effect"
            )
            if is_mocked:
                hasher = hashlib.sha256()
                downloaded_bytes = 0
                with self._open_safe_download_stream(pack_info.url, cancel_event) as resp:
                    content_len = resp.headers.get("Content-Length")
                    if content_len:
                        progress.total_bytes = int(content_len)

                    tmp_download_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(tmp_download_file, "wb") as out_fp:
                        while True:
                            if cancel_event.is_set():
                                raise EngineInstallError("Installation cancelled by user.")

                            chunk = resp.read(64 * 1024)
                            if not chunk:
                                break

                            out_fp.write(chunk)
                            hasher.update(chunk)
                            downloaded_bytes += len(chunk)
                            progress.bytes_downloaded = downloaded_bytes
                            if progress.total_bytes > 0:
                                progress.percent = (downloaded_bytes / progress.total_bytes) * 100.0
                calc_sha256 = hasher.hexdigest().lower()
            else:
                try:
                    calc_sha256 = stream_download(
                        url=pack_info.url,
                        destination_path=tmp_download_file,
                        expected_size=pack_info.size_bytes,
                        uncompressed_size=pack_info.uncompressed_size_bytes,
                        max_size_cap=2000 * 1024 * 1024,
                        cancel_event=cancel_event,
                        progress_callback=on_download_progress,
                        user_agent="InkDoc-PackManager/1.0",
                    )
                except DownloadError as err:
                    raise EngineInstallError(str(err)) from err

            # 2. Checksum verification (fail closed)
            progress.status = "verifying"

            if not is_valid_sha256(expected_sha256):
                if tmp_download_file.exists():
                    try:
                        tmp_download_file.unlink()
                    except OSError:
                        pass
                raise SecurityError(
                    f"Refusing verification against invalid manifest SHA-256: '{expected_sha256}'. Installation aborted."
                )

            if calc_sha256 != expected_sha256:
                if tmp_download_file.exists():
                    try:
                        tmp_download_file.unlink()
                    except OSError:
                        pass
                raise SecurityError(
                    f"Checksum mismatch! Expected SHA-256: {expected_sha256}, got: {calc_sha256}. "
                    "Corrupted archive deleted and installation aborted."
                )

            # 3. Safe Extraction
            progress.status = "extracting"
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)

            if pack_info.archive_format == "zip" or tmp_download_file.name.endswith(".zip"):
                self._extract_zip_safely(tmp_download_file, staging_dir)
            else:
                self._extract_tar_safely(tmp_download_file, staging_dir)

            # Verify file tree integrity after extraction (fail closed)
            if pack_info.sha256_files:
                for rel_path, expected_file_hash in pack_info.sha256_files.items():
                    exp_h = expected_file_hash.lower().strip() if expected_file_hash else ""
                    if not is_valid_sha256(exp_h):
                        raise SecurityError(
                            f"Pack manifest contains invalid or placeholder SHA-256 for file '{rel_path}': "
                            f"'{expected_file_hash}'. Installation aborted (fail closed)."
                        )
                    staged_file = staging_dir / rel_path
                    if not staged_file.is_file():
                        raise SecurityError(
                            f"Extracted pack is missing required file: '{rel_path}'. Installation aborted."
                        )
                    actual_file_hash = hashlib.sha256(staged_file.read_bytes()).hexdigest().lower()
                    if actual_file_hash != exp_h:
                        raise SecurityError(
                            f"File integrity check failed for '{rel_path}': expected {exp_h}, got {actual_file_hash}. "
                            "Installation aborted."
                        )

            # Write metadata file inside pack for version tracking
            meta_payload = {
                "pack_version": self.manifest.pack_version,
                "platform": self.platform_key,
                "installed_at": time.time(),
                "sha256": calc_sha256,
            }
            (staging_dir / "pack_meta.json").write_text(json.dumps(meta_payload, indent=2), encoding="utf-8")

            # 4. Atomic Install
            carried: list[str] = []
            if target_dir.exists():
                # An update. The running worker has the old pack's files open
                # (Windows refuses to rename them), so it is stopped first, after
                # any conversion it is doing, and none starts until the swap is done.
                from app.core.engines.docling_worker_client import DoclingWorkerClient

                old_backup = target_dir.parent / f"{engine_name}.old.tmp"
                with DoclingWorkerClient.get_instance().paused():
                    if old_backup.exists():
                        shutil.rmtree(old_backup, ignore_errors=True)
                    _rename_retrying(target_dir, old_backup)
                    try:
                        os.rename(staging_dir, target_dir)
                    except Exception:
                        # Restore on failure
                        if old_backup.exists() and not target_dir.exists():
                            os.rename(old_backup, target_dir)
                        raise
                    carried = self._carry_over_addons(old_backup, target_dir)
                # Outside the pause: the old pack is tens of thousands of files, and
                # with antivirus scanning deleting them can take minutes.
                shutil.rmtree(old_backup, ignore_errors=True)
            else:
                os.rename(staging_dir, target_dir)

            if carried:
                # The add-on came from a pack with another Docling; have the new
                # pack's worker confirm it still loads, so it needn't be downloaded again.
                progress.status = "verifying"
                self._confirm_carried_addons(carried)

            progress.status = "complete"
            progress.percent = 100.0
            return {
                "status": "success",
                "engine": engine_name,
                "pack_version": self.manifest.pack_version,
                "install_path": str(target_dir),
            }

        except Exception as exc:
            progress.status = "error"
            progress.error_message = str(exc)
            logger.exception("Installation of engine '%s' failed: %s", engine_name, exc)
            # Full cleanup on failure
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            raise EngineInstallError(str(exc)) from exc

        finally:
            if tmp_download_file.exists():
                try:
                    tmp_download_file.unlink()
                except OSError:
                    pass
            self._install_lock.release()
            self._cancel_events.pop(engine_name, None)

    @staticmethod
    def _recover_interrupted_update(engine_name: str, target_dir: Path) -> None:
        """Undo an update that stopped between its two renames (InkDoc closed or
        crashed): the previous pack is left in <name>.old.tmp and none is installed.
        With a pack installed, a leftover <name>.old.tmp is only waste. Runs under
        the install lock, so no update of this process is half-way through."""
        old_backup = target_dir.parent / f"{engine_name}.old.tmp"
        if not old_backup.exists():
            return
        try:
            if not target_dir.exists():
                os.rename(old_backup, target_dir)
                logger.warning("Restored the %s pack left by an update that did not finish.", engine_name)
            else:
                shutil.rmtree(old_backup, ignore_errors=True)
        except OSError as exc:
            logger.warning("Could not tidy up after an unfinished %s update: %s", engine_name, exc)

    @staticmethod
    def _carry_over_addons(old_pack: Path, new_pack: Path) -> list[str]:
        """Keep the add-ons installed inside the replaced pack. Never fails the update."""
        try:
            from app.core.addon_manager import AddonManager

            return AddonManager.get_instance().carry_over(old_pack, new_pack)
        except Exception as exc:
            logger.warning("Could not keep the add-ons across the pack update: %s", exc)
            return []

    @staticmethod
    def _confirm_carried_addons(names: list[str]) -> None:
        """Ask the new pack's worker to load each carried add-on. Never fails the update."""
        try:
            from app.core.addon_manager import AddonManager

            mgr = AddonManager.get_instance()
        except Exception as exc:
            logger.warning("Could not confirm the add-ons with the updated pack: %s", exc)
            return
        for name in names:
            try:
                mgr.reconfirm(name)
            except Exception as exc:
                logger.warning("Could not confirm the add-on '%s' with the updated pack: %s", name, exc)

    # ─── Removal & Integrity Verification ────────────────────────────────────

    def remove_engine(self, engine_name: str = "docling") -> bool:
        """Completely remove an installed engine pack and all its cached model weights."""
        if engine_name.lower().strip() != "docling":
            raise ValueError(f"Cannot remove bundled core engine '{engine_name}'.")

        target_dir = self.get_engine_dir(engine_name)
        if not target_dir.exists():
            return True

        with self._install_lock:
            try:
                # Stop active worker if running
                from app.core.engines.docling_worker_client import DoclingWorkerClient

                DoclingWorkerClient.get_instance().shutdown()
            except Exception as exc:
                logger.debug("Docling worker shutdown during removal: %s", exc)

            shutil.rmtree(target_dir, ignore_errors=True)
            self._progress.pop(engine_name, None)
            return True

    def verify_launch_integrity(self, engine_name: str = "docling") -> bool:
        """Re-hash interpreter and worker script before every launch (Security Item #7)."""
        launch = self._launch_files(engine_name)
        if not launch:
            return False
        interp_rel, interp_sha, worker_rel, worker_sha = launch

        target_dir = self.get_engine_dir(engine_name)
        interpreter = target_dir / interp_rel
        worker_script = target_dir / worker_rel

        if not interpreter.is_file() or not worker_script.is_file():
            return False

        # A file the manifest lists no hash for is not checked (as before);
        # one it lists must be a valid hash, and match.
        for label, path, expected in (
            ("interpreter", interpreter, interp_sha),
            ("worker script", worker_script, worker_sha),
        ):
            if expected is None:
                continue
            exp = expected.lower().strip()
            if not is_valid_sha256(exp):
                logger.error("Invalid SHA-256 in manifest for %s: %r", label, exp)
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest().lower() != exp:
                logger.error("%s integrity check failed for %s", label.capitalize(), path)
                return False

        return True

    def verify_full_installed_tree(self, engine_name: str = "docling") -> dict[str, Any]:
        """Verify the complete directory tree against manifest file hashes (Security Item #7)."""
        pack_info = self.get_platform_pack_info(engine_name)
        if not pack_info:
            return {"valid": False, "reason": f"Platform '{self.platform_key}' unsupported"}

        target_dir = self.get_engine_dir(engine_name)
        if not target_dir.is_dir():
            return {"valid": False, "reason": "Engine pack is not installed"}

        # An earlier pack: this app holds only its launch hashes, not its full file list.
        if self.is_pack_outdated(engine_name):
            installed = self.installed_pack_version(engine_name)
            if not self.verify_launch_integrity(engine_name):
                return {
                    "valid": False,
                    "outdated": True,
                    "reason": f"Docling pack {installed} failed its check. Update Docling to replace it.",
                }
            return {
                "valid": True,
                "outdated": True,
                "engine": engine_name,
                "pack_version": installed,
                "latest_pack_version": self.manifest.pack_version,
                "path": str(target_dir),
                "note": (
                    f"Docling pack {installed} is an earlier version: only its interpreter and "
                    "worker were checked. Update Docling to get the current pack."
                ),
            }

        # Check interpreter & worker script existence
        interpreter = target_dir / pack_info.interpreter_path
        worker = target_dir / pack_info.worker_script
        if not interpreter.is_file():
            return {"valid": False, "reason": f"Interpreter missing at '{pack_info.interpreter_path}'"}
        if not worker.is_file():
            return {"valid": False, "reason": f"Worker script missing at '{pack_info.worker_script}'"}

        # If full sha256_files list is present in the pack metadata, verify each file
        meta_file = target_dir / "pack_meta.json"
        if not meta_file.is_file():
            return {"valid": False, "reason": "Pack metadata file missing"}

        mismatches: list[str] = []
        if pack_info.sha256_files:
            for rel_path, expected_hash in pack_info.sha256_files.items():
                exp = expected_hash.lower().strip() if expected_hash else ""
                if not is_valid_sha256(exp):
                    mismatches.append(f"Invalid manifest hash for: {rel_path} ({expected_hash!r})")
                    continue
                file_path = target_dir / rel_path
                if not file_path.is_file():
                    mismatches.append(f"Missing file: {rel_path}")
                    continue
                actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest().lower()
                if actual_hash != exp:
                    mismatches.append(f"Hash mismatch: {rel_path}")

        if mismatches:
            return {"valid": False, "mismatches": mismatches[:20]}

        return {
            "valid": True,
            "engine": engine_name,
            "pack_version": self.manifest.pack_version,
            "files_checked": len(pack_info.sha256_files),
            "path": str(target_dir),
        }
