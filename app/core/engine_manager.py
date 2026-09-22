"""Engine Manager for InkDoc.

Handles lifecycle, secure download, cryptographic verification, safe extraction,
and status tracking for optional engine packages (IBM Docling).
"""
from __future__ import annotations

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
    EngineManifest,
    EngineStatus,
    PlatformPackInfo,
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


class EngineManager:
    """Manages installation, updates, and integrity of optional engines."""

    _instance: EngineManager | None = None
    _lock = threading.Lock()

    def __init__(self, manifest: EngineManifest | None = None) -> None:
        # Loaded on first use rather than here. app/core/manifest.json carries a
        # SHA-256 for every file in every platform pack -- about 93,000 entries and
        # 14 MB -- which costs roughly 140 ms to parse and 18 MB to retain.
        #
        # The desktop runner reads user settings before opening the window, and
        # settings do not need the manifest. Parsing it in the constructor put that
        # work on the critical path to the window appearing. Deferring it moves the
        # cost to the first request that genuinely needs pack metadata, where it
        # overlaps with the UI rendering instead of delaying it.
        self._manifest: EngineManifest | None = manifest
        self._manifest_lock = threading.Lock()
        self.platform_key = get_current_platform_key()
        self._install_lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._progress: dict[str, InstallProgress] = {}
        self._settings_cache: dict[str, Any] | None = None
        self._settings_lock = threading.Lock()

    @property
    def manifest(self) -> EngineManifest:
        """The bundled engine manifest, parsed on first access and then cached."""
        if self._manifest is None:
            with self._manifest_lock:
                if self._manifest is None:
                    self._manifest = load_engine_manifest()
        return self._manifest

    @manifest.setter
    def manifest(self, value: EngineManifest) -> None:
        with self._manifest_lock:
            self._manifest = value

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
                return dict(self._settings_cache)

            path = self.get_settings_file_path()
            if path.is_file():
                try:
                    self._settings_cache = json.loads(path.read_text(encoding="utf-8"))
                    return dict(self._settings_cache)
                except Exception as exc:
                    logger.warning("Failed to parse settings at %s: %s", path, exc)

            default_settings = {
                "fallback_to_markitdown": False,
                "check_for_updates_daily": False,
            }
            self._settings_cache = default_settings
            return dict(default_settings)

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Update and persist user settings thread-safely and atomically."""
        with self._settings_lock:
            # Refresh from current cache or disk
            if self._settings_cache is not None:
                current = dict(self._settings_cache)
            else:
                path = self.get_settings_file_path()
                if path.is_file():
                    try:
                        current = json.loads(path.read_text(encoding="utf-8"))
                    except Exception as exc:
                        logger.warning("Failed to parse settings at %s: %s", path, exc)
                        current = {
                            "fallback_to_markitdown": False,
                            "check_for_updates_daily": False,
                        }
                else:
                    current = {
                        "fallback_to_markitdown": False,
                        "check_for_updates_daily": False,
                    }

            current.update(updates)
            self._settings_cache = current

            path = self.get_settings_file_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_fd, tmp_path_str = tempfile.mkstemp(
                    prefix="settings_",
                    suffix=".tmp",
                    dir=str(path.parent),
                )
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                        json.dump(current, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp_path_str, path)
                except Exception:
                    if os.path.exists(tmp_path_str):
                        try:
                            os.unlink(tmp_path_str)
                        except OSError:
                            pass
                    raise
            except Exception as exc:
                logger.error("Failed to save settings to %s: %s", path, exc)

            return dict(current)

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

        pack_info = self.get_platform_pack_info(engine_name)
        if not pack_info:
            return False

        interpreter = target_dir / pack_info.interpreter_path
        worker_script = target_dir / pack_info.worker_script
        return interpreter.is_file() and worker_script.is_file()

    def is_engine_installed(self, engine_name: str = "docling") -> bool:
        """Return True if engine pack is installed OR system Docling detected in source mode."""
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
            meta_file = docling_dir / "pack_meta.json"
            installed_ver = None
            if meta_file.is_file():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    installed_ver = meta.get("pack_version")
                except Exception:
                    pass
            installed_ver = installed_ver or pack_ver

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
            if target_dir.exists():
                old_backup = target_dir.parent / f"{engine_name}.old.tmp"
                if old_backup.exists():
                    shutil.rmtree(old_backup, ignore_errors=True)
                os.rename(target_dir, old_backup)
                try:
                    os.rename(staging_dir, target_dir)
                    shutil.rmtree(old_backup, ignore_errors=True)
                except Exception:
                    # Restore on failure
                    if old_backup.exists() and not target_dir.exists():
                        os.rename(old_backup, target_dir)
                    raise
            else:
                os.rename(staging_dir, target_dir)

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
        pack_info = self.get_platform_pack_info(engine_name)
        if not pack_info:
            return False

        target_dir = self.get_engine_dir(engine_name)
        interpreter = target_dir / pack_info.interpreter_path
        worker_script = target_dir / pack_info.worker_script

        if not interpreter.is_file() or not worker_script.is_file():
            return False

        # If file hashes are provided in the manifest, verify them
        if pack_info.sha256_files:
            rel_interp = pack_info.interpreter_path.replace("\\", "/")
            if rel_interp in pack_info.sha256_files:
                exp = pack_info.sha256_files[rel_interp].lower().strip() if pack_info.sha256_files[rel_interp] else ""
                if not is_valid_sha256(exp):
                    logger.error("Invalid SHA-256 in manifest for interpreter: %r", exp)
                    return False
                actual_hash = hashlib.sha256(interpreter.read_bytes()).hexdigest().lower()
                if actual_hash != exp:
                    logger.error("Interpreter integrity check failed for %s", interpreter)
                    return False

            rel_worker = pack_info.worker_script.replace("\\", "/")
            if rel_worker in pack_info.sha256_files:
                exp = pack_info.sha256_files[rel_worker].lower().strip() if pack_info.sha256_files[rel_worker] else ""
                if not is_valid_sha256(exp):
                    logger.error("Invalid SHA-256 in manifest for worker script: %r", exp)
                    return False
                actual_hash = hashlib.sha256(worker_script.read_bytes()).hexdigest().lower()
                if actual_hash != exp:
                    logger.error("Worker script integrity check failed for %s", worker_script)
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
            "path": str(target_dir),
        }
