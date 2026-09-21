"""Update manager and lifecycle controller for InkDoc.

Orchestrates:
- On-demand and optional daily update checks via cryptographic envelope verification
- Platform asset resolution (Windows Inno installer, Windows portable, macOS ARM64, Linux x86_64)
- Intel Mac detection and explicit "no compatible build" notification
- Safe background streaming with Range resume, disk space checks, and SHA-256 verification
- Windows installer silent update execution (/VERYSILENT /SUPPRESSMSGBOXES /NORESTART)
- Pre-execution SHA-256 re-hashing
- Portable/macOS/Linux verified download and "reveal in folder" workflow
- Rejection of apply requests when running from source, headless, or browser tab
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from app._version import get_version
from app.core.converter import get_downloads_dir
from app.core.download_utils import (
    ALLOWED_DOWNLOAD_HOSTS,
    DownloadError,
    SecurityError,
    StrictRedirectHandler,
    stream_download,
)
from app.core.update_verifier import (
    DowngradeError,
    UpdateVerificationError,
    get_official_public_keys,
    verify_envelope_bytes,
)

logger = logging.getLogger("inkdoc.update_manager")

# Official GitHub Releases manifest endpoint
OFFICIAL_UPDATE_MANIFEST_URL = (
    "https://github.com/AbdoslamB/InkDoc/releases/latest/download/inkdoc-update-manifest.json"
)

# Minimum check interval to avoid rate limiting unless forced (1 hour)
MIN_CHECK_INTERVAL_SECONDS = 3600

# Freeze-attack residual risk note:
# Manifests use `issued_at` without a hard expiration to allow offline builds and long-lived
# version checks. An attacker with MITM/CDN control cannot downgrade an installed version
# because `is_version_newer()` strictly rejects remote_version <= installed_version.
# The only residual risk is a freeze attack where an attacker serves an older valid manifest,
# preventing detection of newer releases. The frequency cap and HTTPS validation mitigate this.


class UpdateState(str, Enum):
    IDLE = "idle"
    CHECKING = "checking"
    AVAILABLE = "available"
    UP_TO_DATE = "up_to_date"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    READY_TO_INSTALL = "ready_to_install"
    READY_TO_REVEAL = "ready_to_reveal"
    APPLYING = "applying"
    ERROR = "error"


@dataclass
class UpdateStatus:
    state: str = UpdateState.IDLE.value
    current_version: str = "0.0.0+dev"
    latest_version: str | None = None
    release_notes: str | None = None
    release_date: str | None = None
    release_url: str | None = None
    asset_name: str | None = None
    asset_size: int | None = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    progress_percent: float = 0.0
    staged_path: str | None = None
    revealed_path: str | None = None
    install_method: str | None = None  # "inno_silent" or "download_reveal"
    can_apply: bool = False
    error: str | None = None
    last_checked: str | None = None
    platform_key: str = "unknown"
    platform_supported: bool = True
    compatibility_message: str | None = None


def get_current_platform_key() -> tuple[str, bool, str | None]:
    """Identify the target release asset key for this host system.

    Returns:
        (platform_key, is_supported, compatibility_message)
    """
    sys_plat = sys.platform
    mach = platform.machine().lower()

    if sys_plat == "win32":
        if mach not in ("amd64", "x86_64", "x64"):
            return (
                "windows-unsupported",
                False,
                "Windows on ARM64 is not supported by packaged releases. Please run from source.",
            )

        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).parent
            # Inno Setup creates unins000.exe in the installation root
            if (exe_dir / "unins000.exe").is_file():
                return ("windows-x86_64-installer", True, None)
            return ("windows-x86_64-portable", True, None)
        else:
            # Source mode (development/testing)
            return ("windows-x86_64-installer", True, None)

    elif sys_plat == "darwin":
        if mach in ("arm64", "aarch64"):
            return ("macos-arm64", True, None)
        else:
            return (
                "macos-intel-unsupported",
                False,
                "No compatible build for Intel macOS. InkDoc packaged releases require Apple Silicon (ARM64).",
            )

    elif sys_plat.startswith("linux"):
        if mach in ("x86_64", "amd64"):
            return ("linux-x86_64", True, None)
        else:
            return (
                "linux-unsupported",
                False,
                f"Linux architecture '{mach}' is not supported by packaged releases.",
            )

    return ("unsupported", False, f"Operating system '{sys_plat}' is not supported.")


def get_staging_directory() -> Path:
    """Return local update staging directory."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            base = Path(local_app_data) / "InkDoc"
        else:
            base = Path.home() / "AppData" / "Local" / "InkDoc"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "InkDoc"
    else:
        cache_home = os.environ.get("XDG_CACHE_HOME")
        base = Path(cache_home) / "inkdoc" if cache_home else Path.home() / ".cache" / "inkdoc"

    staging = base / "update_staging"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


class UpdateManager:
    """Thread-safe update orchestrator for InkDoc."""

    def __init__(
        self,
        manifest_url_override: str | None = None,
        test_public_keys: list[Any] | None = None,
        is_desktop_runner: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._download_thread: threading.Thread | None = None
        self._last_check_timestamp: float = 0.0

        # In frozen builds, overrides are strictly prohibited (Requirement H)
        is_frozen = getattr(sys, "frozen", False)
        if is_frozen:
            self._manifest_url = OFFICIAL_UPDATE_MANIFEST_URL
            self._trusted_keys = get_official_public_keys()
            self._test_mode = False
        else:
            self._manifest_url = manifest_url_override or OFFICIAL_UPDATE_MANIFEST_URL
            self._trusted_keys = test_public_keys or get_official_public_keys()
            self._test_mode = bool(manifest_url_override or test_public_keys)

        self._is_desktop_runner = is_desktop_runner
        self._conversion_active_checker: Callable[[], bool] | None = None

        # Verified manifest state held strictly server-side (Requirement D)
        self._verified_manifest: dict[str, Any] | None = None
        self._verified_target_asset: dict[str, Any] | None = None
        self._staged_file_path: Path | None = None

        # Platform compatibility
        platform_key, supported, compat_msg = get_current_platform_key()
        self._platform_key = platform_key
        self._platform_supported = supported
        self._compat_msg = compat_msg

        self._status = UpdateStatus(
            current_version=get_version(),
            platform_key=platform_key,
            platform_supported=supported,
            compatibility_message=compat_msg,
        )

    def set_conversion_active_checker(self, checker: Callable[[], bool]) -> None:
        """Register a callback returning True if any document conversion is running."""
        self._conversion_active_checker = checker

    def set_desktop_runner(self, is_runner: bool) -> None:
        """Set whether the app is executing inside the native pywebview desktop runner."""
        self._is_desktop_runner = is_runner
        self._update_can_apply()

    def get_status(self) -> dict[str, Any]:
        """Return a snapshot of current update status."""
        with self._lock:
            self._status.current_version = get_version()
            self._update_can_apply()
            return asdict(self._status)

    def _update_can_apply(self) -> None:
        """Evaluate if the staged update can be applied in-place."""
        is_frozen = getattr(sys, "frozen", False)
        is_ready = self._status.state == UpdateState.READY_TO_INSTALL.value
        is_inno = self._status.install_method == "inno_silent"

        # Apply is strictly refused if not frozen, not inno, or not launched via desktop runner
        # (Requirement D: "apply is refused server-side unless launched by the desktop runner
        # (not headless, not a browser tab, not source mode).")
        # In test mode when running unit tests, we allow testing execution logic if specified.
        if self._test_mode:
            self._status.can_apply = is_ready and is_inno
        else:
            self._status.can_apply = is_ready and is_inno and is_frozen and self._is_desktop_runner

    def check_for_updates(self, force: bool = True) -> dict[str, Any]:
        """Fetch, cryptographically verify, and evaluate the update manifest.

        Args:
            force: If False, enforces MIN_CHECK_INTERVAL_SECONDS frequency cap.
        """
        now = time.time()
        with self._lock:
            if not force and (now - self._last_check_timestamp) < MIN_CHECK_INTERVAL_SECONDS:
                return asdict(self._status)

            if self._status.state == UpdateState.DOWNLOADING.value:
                return asdict(self._status)

            self._status.state = UpdateState.CHECKING.value
            self._status.error = None

            # Discard the previous check's verified result before starting a new one.
            # These were only ever assigned on success and never cleared, so after a
            # successful check followed by a failing one (404, network error, or a
            # signature that no longer verifies) the manager still held the older
            # manifest's asset. download_update() accepts the ERROR state, so the UI
            # would then download an asset from a manifest the server has since
            # stopped vouching for. Clearing here also makes download_update() fail
            # closed with "Check for updates first" instead of acting on stale data.
            self._verified_manifest = None
            self._verified_target_asset = None

        installed_ver = get_version()
        if not installed_ver or installed_ver == "0.0.0+unknown" or "unknown" in installed_ver:
            logger.error(
                "LOUD ERROR: Update check refused because application version is unknown (%s)",
                installed_ver,
            )
            with self._lock:
                self._status.state = UpdateState.ERROR.value
                self._status.error = f"Update check refused: application version is unknown ({installed_ver})"
                return asdict(self._status)

        try:
            raw_manifest_bytes = self._fetch_manifest_bytes()

            # Cryptographically verify envelope BEFORE parsing
            manifest = verify_envelope_bytes(
                envelope_raw=raw_manifest_bytes,
                installed_version=installed_ver,
                trusted_public_keys=self._trusted_keys,
            )

            with self._lock:
                self._verified_manifest = manifest
                self._last_check_timestamp = now
                self._status.last_checked = datetime.now(timezone.utc).isoformat()
                self._status.latest_version = manifest["version"]
                # Accept either spelling. scripts/sign_manifest.py emitted
                # release_notes/release_url while this read notes/html_url, so the
                # notes panel and release link would have come up empty on the first
                # signed release. The signer now emits notes/html_url; the fallbacks
                # keep a manifest produced by an older signer from silently losing
                # both fields, since these are display-only and default to empty.
                self._status.release_notes = manifest.get("notes") or manifest.get("release_notes") or ""
                self._status.release_date = manifest.get("issued_at")
                self._status.release_url = manifest.get("html_url") or manifest.get("release_url")

                # Platform compatibility check (Requirement 4)
                if not self._platform_supported:
                    self._status.state = UpdateState.ERROR.value
                    self._status.error = self._compat_msg
                    return asdict(self._status)

                # Look up target asset for this platform
                assets = manifest.get("assets", {})
                target_asset = assets.get(self._platform_key)

                if not target_asset:
                    self._status.state = UpdateState.ERROR.value
                    self._status.error = (
                        f"Release v{manifest['version']} does not provide an asset for '{self._platform_key}'."
                    )
                    return asdict(self._status)

                self._verified_target_asset = target_asset
                self._status.asset_name = target_asset.get("filename")
                self._status.asset_size = target_asset.get("size_bytes")
                self._status.install_method = target_asset.get("install_method", "download_reveal")
                self._status.state = UpdateState.AVAILABLE.value
                self._update_can_apply()
                return asdict(self._status)

        except DowngradeError:
            with self._lock:
                self._last_check_timestamp = now
                self._status.last_checked = datetime.now(timezone.utc).isoformat()
                self._status.state = UpdateState.UP_TO_DATE.value
                self._status.error = None
                return asdict(self._status)

        except urllib.error.HTTPError as err:
            with self._lock:
                self._status.state = UpdateState.ERROR.value
                # Requirement B: "A missing or 404 manifest means 'couldn't check' and must never display 'up to date'."
                if err.code == 404:
                    self._status.error = "Update check failed: release manifest not found on server (HTTP 404)."
                else:
                    self._status.error = f"Update check failed: server returned HTTP {err.code}."
                return asdict(self._status)

        except urllib.error.URLError as err:
            with self._lock:
                self._status.state = UpdateState.ERROR.value
                self._status.error = f"Update check failed: network connection error ({err.reason})."
                return asdict(self._status)

        except UpdateVerificationError as err:
            with self._lock:
                self._status.state = UpdateState.ERROR.value
                self._status.error = f"Update verification failed: {err}"
                return asdict(self._status)

        except Exception as err:
            logger.exception("Unexpected error during update check")
            with self._lock:
                self._status.state = UpdateState.ERROR.value
                self._status.error = f"Update check error: {err}"
                return asdict(self._status)

    def _fetch_manifest_bytes(self) -> bytes:
        """Download raw envelope bytes from manifest URL with redirect validation."""
        url = self._manifest_url
        # If running unit test on localhost, allow localhost host
        allowed = set(ALLOWED_DOWNLOAD_HOSTS)
        if self._test_mode:
            allowed.add("127.0.0.1")
            allowed.add("localhost")

        parsed = urllib.parse.urlparse(url)
        if not self._test_mode and parsed.scheme.lower() != "https":
            raise SecurityError("Insecure manifest URL protocol. HTTPS is required.")

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "InkDoc-Updater",
                "Accept": "application/json, */*",
            },
        )
        opener = urllib.request.build_opener(StrictRedirectHandler(frozenset(allowed)))
        with opener.open(req, timeout=15.0) as resp:
            return resp.read()

    def download_update(self) -> dict[str, Any]:
        """Start downloading the verified update asset in a background thread."""
        installed_ver = get_version()
        if not installed_ver or installed_ver == "0.0.0+unknown" or "unknown" in installed_ver:
            logger.error(
                "LOUD ERROR: Update download refused because application version is unknown (%s)",
                installed_ver,
            )
            with self._lock:
                self._status.state = UpdateState.ERROR.value
                self._status.error = f"Update download refused: application version is unknown ({installed_ver})"
            raise ValueError(f"Update download refused: application version is unknown ({installed_ver})")

        with self._lock:
            if self._status.state == UpdateState.DOWNLOADING.value:
                return asdict(self._status)

            if not self._verified_target_asset or self._status.state not in (
                UpdateState.AVAILABLE.value,
                UpdateState.ERROR.value,
            ):
                raise ValueError("No verified update asset available to download. Check for updates first.")

            self._cancel_event.clear()
            self._status.state = UpdateState.DOWNLOADING.value
            self._status.downloaded_bytes = 0
            self._status.progress_percent = 0.0
            self._status.error = None

            asset_info = dict(self._verified_target_asset)

        self._download_thread = threading.Thread(
            target=self._run_download,
            args=(asset_info,),
            daemon=True,
            name="InkDocUpdateDownloader",
        )
        self._download_thread.start()
        return self.get_status()

    def _run_download(self, asset: dict[str, Any]) -> None:
        """Worker thread executing streaming download, resume, and hash verification."""
        url = asset["url"]
        expected_hash = asset["sha256"].lower()
        size_bytes = asset.get("size_bytes", 0)
        filename = asset.get("filename") or "inkdoc-update"
        install_method = asset.get("install_method", "download_reveal")

        # Determine download target destination
        if install_method == "inno_silent":
            staging_dir = get_staging_directory()
            target_path = staging_dir / filename
        else:
            downloads_dir = get_downloads_dir()
            target_path = downloads_dir / filename

        allowed = set(ALLOWED_DOWNLOAD_HOSTS)
        if self._test_mode:
            allowed.add("127.0.0.1")
            allowed.add("localhost")

        def on_progress(downloaded: int, total: int) -> None:
            with self._lock:
                self._status.downloaded_bytes = downloaded
                self._status.total_bytes = total
                if total > 0:
                    self._status.progress_percent = round((downloaded / total) * 100, 1)

        try:
            computed_sha256 = stream_download(
                url=url,
                destination_path=target_path,
                expected_size=size_bytes,
                max_size_cap=600 * 1024 * 1024,  # 600 MB cap
                cancel_event=self._cancel_event,
                progress_callback=on_progress,
                user_agent="InkDoc-Updater",
                allowed_hosts=frozenset(allowed),
            )

            # Strict SHA-256 verification (Requirement C)
            with self._lock:
                self._status.state = UpdateState.VERIFYING.value

            if computed_sha256 != expected_hash:
                if target_path.exists():
                    target_path.unlink()
                raise SecurityError(
                    f"Integrity check failed: computed SHA-256 ({computed_sha256}) did not match manifest ({expected_hash})."
                )

            with self._lock:
                self._staged_file_path = target_path
                if install_method == "inno_silent":
                    self._status.state = UpdateState.READY_TO_INSTALL.value
                    self._status.staged_path = str(target_path)
                else:
                    self._status.state = UpdateState.READY_TO_REVEAL.value
                    self._status.revealed_path = str(target_path)

                self._update_can_apply()

        except DownloadError as err:
            logger.warning(f"Download failed: {err}")
            with self._lock:
                self._status.state = UpdateState.ERROR.value
                self._status.error = str(err)
        except SecurityError as err:
            logger.error(f"Security error during update download: {err}")
            with self._lock:
                self._status.state = UpdateState.ERROR.value
                self._status.error = str(err)
        except Exception as err:
            logger.exception("Unexpected error during update download")
            with self._lock:
                self._status.state = UpdateState.ERROR.value
                self._status.error = f"Download error: {err}"

    def cancel_download(self) -> dict[str, Any]:
        """Cancel an ongoing download."""
        self._cancel_event.set()
        with self._lock:
            if self._status.state == UpdateState.DOWNLOADING.value:
                self._status.state = UpdateState.AVAILABLE.value
                self._status.error = "Download cancelled."
        return self.get_status()

    def reveal_downloaded_file(self) -> bool:
        """Open system file manager highlighting the downloaded release asset."""
        with self._lock:
            path_str = self._status.revealed_path or (
                str(self._staged_file_path) if self._staged_file_path else None
            )

        if not path_str or not Path(path_str).exists():
            return False

        path = Path(path_str).resolve()
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer.exe", f"/select,{path}"])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
            return True
        except Exception as err:
            logger.error(f"Failed to reveal file: {err}")
            return False

    def apply_update(
        self,
        shutdown_callback: Callable[[], None] | None = None,
        exit_process: bool = True,
    ) -> dict[str, Any]:
        """Apply verified staged update.

        Guarantees:
        1. Refused if active conversions are running (returns 409).
        2. Refused if not launched by desktop runner in packaged mode (Requirement D).
        3. Staged installer is cryptographically re-hashed immediately prior to launch.
        4. Inno Setup is spawned detached with /VERYSILENT /SUPPRESSMSGBOXES /NORESTART.
        5. The current process triggers clean shutdown to release port 13118 and WebView2 locks.
        """
        with self._lock:
            if self._status.state != UpdateState.READY_TO_INSTALL.value:
                raise ValueError("No update ready to install.")

            if not self._status.can_apply:
                raise PermissionError(
                    "Applying updates in-app is only permitted in the packaged desktop application."
                )

            # Check active document conversions
            if self._conversion_active_checker and self._conversion_active_checker():
                raise RuntimeError(
                    "Cannot apply update while document conversions are in progress. Please wait."
                )

            if not self._staged_file_path or not self._staged_file_path.is_file():
                raise FileNotFoundError("Staged installer file not found on disk.")

            if not self._verified_target_asset:
                raise SecurityError("No verified manifest asset associated with staged file.")

            expected_sha256 = self._verified_target_asset["sha256"].lower()
            installer_path = self._staged_file_path
            self._status.state = UpdateState.APPLYING.value

        # Requirement D: "Re-hash the staged installer immediately before launching it."
        hasher = hashlib.sha256()
        with open(installer_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        current_hash = hasher.hexdigest().lower()

        if current_hash != expected_sha256:
            # Staged file was altered or corrupted on disk!
            try:
                installer_path.unlink()
            except OSError:
                pass
            with self._lock:
                self._status.state = UpdateState.ERROR.value
                self._status.error = "Pre-execution integrity verification failed: hash mismatch."
            raise SecurityError("Pre-execution integrity verification failed. Update aborted.")

        logger.info(f"Launching verified installer: {installer_path}")

        # Spawn detached installer:
        # Inno Setup flags: /VERYSILENT (no UI) /SUPPRESSMSGBOXES (no prompts) /NORESTART (no system reboot)
        if sys.platform == "win32":
            creationflags = 0
            if hasattr(subprocess, "DETACHED_PROCESS") and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

            # /RESTARTAPP=1 is an InkDoc-specific parameter read by installer.iss.
            # Inno skips [Run] entries flagged "postinstall" under /VERYSILENT, so
            # without this the update installs and the app never comes back. The
            # installer only honours it when the install is silent, so a plain
            # unattended install still does not launch a GUI.
            subprocess.Popen(
                [
                    str(installer_path),
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    "/RESTARTAPP=1",
                ],
                creationflags=creationflags,
                close_fds=True,
            )

        # Requirement D: "The old process must fully exit (port 13118 and the WebView2 profile lock) before relaunch."
        if exit_process:
            def _delayed_exit() -> None:
                time.sleep(0.8)
                if shutdown_callback:
                    try:
                        shutdown_callback()
                    except Exception as exc:
                        logger.warning(f"Error executing shutdown callback: {exc}")
                # Wait up to 2.0 seconds for graceful shutdown before fallback exit
                time.sleep(2.0)
                os._exit(0)

            threading.Thread(target=_delayed_exit, daemon=True, name="InkDocAppExit").start()

        return {
            "status": "applying",
            "message": "Update launched successfully. InkDoc is restarting...",
        }
