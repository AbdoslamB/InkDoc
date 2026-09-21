"""Unit tests for UpdateManager lifecycle, state transitions, and security checks."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cryptography.hazmat.primitives.asymmetric import ed25519

from app.core.update_manager import UpdateManager, UpdateState, get_current_platform_key
from scripts.sign_manifest import build_envelope


class TestUpdateManager(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_inkdoc_mgr_"))
        self.private_key = ed25519.Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()

        # Dummy asset file
        self.fake_installer = self.temp_dir / "inkdoc-setup.exe"
        self.fake_installer.write_bytes(b"FAKE_EXE_CONTENT_FOR_TESTING")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_platform_key_detection(self) -> None:
        key, supported, msg = get_current_platform_key()
        self.assertIsInstance(key, str)
        self.assertIsInstance(supported, bool)
        if not supported:
            self.assertIsNotNone(msg)

    def test_intel_mac_compatibility_message(self) -> None:
        with patch("sys.platform", "darwin"), patch("platform.machine", return_value="x86_64"):
            key, supported, msg = get_current_platform_key()
            self.assertEqual(key, "macos-intel-unsupported")
            self.assertFalse(supported)
            self.assertIn("Intel macOS", msg)

    def test_windows_arm64_unsupported(self) -> None:
        with patch("sys.platform", "win32"), patch("platform.machine", return_value="arm64"):
            key, supported, msg = get_current_platform_key()
            self.assertEqual(key, "windows-unsupported")
            self.assertFalse(supported)
            self.assertIn("ARM64", msg)

    def test_inno_setup_vs_portable_detection(self) -> None:
        # In frozen mode on Windows: presence of unins000.exe determines installer vs portable
        with patch("sys.platform", "win32"), patch("platform.machine", return_value="AMD64"), \
             patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", str(self.temp_dir / "inkdoc.exe")):
            # Without unins000.exe -> portable
            key, supported, _ = get_current_platform_key()
            self.assertEqual(key, "windows-x86_64-portable")

            # With unins000.exe -> installer
            (self.temp_dir / "unins000.exe").touch()
            key, supported, _ = get_current_platform_key()
            self.assertEqual(key, "windows-x86_64-installer")

    def test_check_for_updates_available_and_up_to_date(self) -> None:
        mgr = UpdateManager(
            manifest_url_override="https://github.com/AbdoslamB/InkDoc/releases/latest/download/inkdoc-update-manifest.json",
            test_public_keys=[self.public_key],
        )

        manifest_data = {
            "version": "2.0.0",
            "issued_at": "2026-09-19T00:00:00Z",
            "html_url": "https://github.com/AbdoslamB/InkDoc/releases/tag/v2.0.0",
            "notes": "Version 2.0 release notes",
            "assets": {
                mgr._platform_key: {
                    "filename": "inkdoc-setup.exe",
                    "url": "https://github.com/AbdoslamB/InkDoc/releases/download/v2.0.0/inkdoc-setup.exe",
                    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "size_bytes": 1024,
                    "install_method": "inno_silent",
                }
            },
        }

        envelope = build_envelope(manifest_data, self.private_key)
        envelope_bytes = json.dumps(envelope).encode("utf-8")

        with patch.object(mgr, "_fetch_manifest_bytes", return_value=envelope_bytes):
            status = mgr.check_for_updates(force=True)
            self.assertEqual(status["state"], UpdateState.AVAILABLE.value)
            self.assertEqual(status["latest_version"], "2.0.0")
            self.assertEqual(status["asset_name"], "inkdoc-setup.exe")

        # Now test when installed version is same or higher -> UP_TO_DATE
        with patch("app.core.update_manager.get_version", return_value="2.0.0"), patch.object(
            mgr, "_fetch_manifest_bytes", return_value=envelope_bytes
        ):
            status = mgr.check_for_updates(force=True)
            self.assertEqual(status["state"], UpdateState.UP_TO_DATE.value)

    def test_check_404_error_does_not_show_up_to_date(self) -> None:
        """Requirement B: A 404 manifest means 'couldn't check' and must never display 'up to date'."""
        mgr = UpdateManager(
            manifest_url_override="https://github.com/AbdoslamB/InkDoc/releases/latest/download/inkdoc-update-manifest.json",
            test_public_keys=[self.public_key],
        )

        import urllib.error
        err_404 = urllib.error.HTTPError(
            url="https://github.com/.../inkdoc-update-manifest.json",
            code=404,
            msg="Not Found",
            hdrs=MagicMock(),
            fp=None,
        )

        with patch.object(mgr, "_fetch_manifest_bytes", side_effect=err_404):
            status = mgr.check_for_updates(force=True)
            self.assertEqual(status["state"], UpdateState.ERROR.value)
            self.assertEqual(
                status["error"],
                "No signed update information has been published for the latest release yet.",
            )
            # Must not read as a fault on the user's machine.
            self.assertNotIn("failed", status["error"].lower())
            self.assertNotEqual(status["state"], UpdateState.UP_TO_DATE.value)

    def test_failed_check_discards_previously_verified_asset(self) -> None:
        """A failing check must not leave the previous check's asset usable.

        download_update() accepts the ERROR state so a failed *download* can be
        retried. The verified asset was only ever assigned on success and never
        cleared, so after a good check followed by a failing one the manager still
        held the older manifest's asset and would happily download it.
        """
        import urllib.error

        mgr = UpdateManager(
            manifest_url_override="https://github.com/AbdoslamB/InkDoc/releases/latest/download/inkdoc-update-manifest.json",
            test_public_keys=[self.public_key],
        )

        manifest_data = {
            "version": "2.0.0",
            "issued_at": "2026-09-19T00:00:00Z",
            "assets": {
                mgr._platform_key: {
                    "filename": "inkdoc-setup.exe",
                    "url": "https://github.com/AbdoslamB/InkDoc/releases/download/v2.0.0/inkdoc-setup.exe",
                    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "size_bytes": 1024,
                    "install_method": "inno_silent",
                }
            },
        }
        envelope_bytes = json.dumps(build_envelope(manifest_data, self.private_key)).encode("utf-8")

        # 1. A good check arms the manager with a verified asset.
        with patch.object(mgr, "_fetch_manifest_bytes", return_value=envelope_bytes):
            self.assertEqual(mgr.check_for_updates(force=True)["state"], UpdateState.AVAILABLE.value)
        self.assertIsNotNone(mgr._verified_target_asset)

        # 2. The server stops serving a manifest.
        err_404 = urllib.error.HTTPError(
            url="https://github.com/.../inkdoc-update-manifest.json",
            code=404,
            msg="Not Found",
            hdrs=MagicMock(),
            fp=None,
        )
        with patch.object(mgr, "_fetch_manifest_bytes", side_effect=err_404):
            self.assertEqual(mgr.check_for_updates(force=True)["state"], UpdateState.ERROR.value)

        # 3. The stale asset must be gone and the download refused, not started.
        self.assertIsNone(mgr._verified_target_asset)
        self.assertIsNone(mgr._verified_manifest)
        with self.assertRaises(ValueError) as ctx:
            mgr.download_update()
        self.assertIn("Check for updates first", str(ctx.exception))

    def test_failed_download_can_still_be_retried(self) -> None:
        """Clearing state on a failed *check* must not break retrying a failed *download*.

        No new check runs between the failure and the retry, so the asset verified
        by the last successful check is still legitimately in hand.
        """
        mgr = UpdateManager(
            manifest_url_override="https://github.com/AbdoslamB/InkDoc/releases/latest/download/inkdoc-update-manifest.json",
            test_public_keys=[self.public_key],
        )

        manifest_data = {
            "version": "2.0.0",
            "issued_at": "2026-09-19T00:00:00Z",
            "assets": {
                mgr._platform_key: {
                    "filename": "inkdoc-setup.exe",
                    "url": "https://github.com/AbdoslamB/InkDoc/releases/download/v2.0.0/inkdoc-setup.exe",
                    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "size_bytes": 1024,
                    "install_method": "inno_silent",
                }
            },
        }
        envelope_bytes = json.dumps(build_envelope(manifest_data, self.private_key)).encode("utf-8")

        with patch.object(mgr, "_fetch_manifest_bytes", return_value=envelope_bytes):
            mgr.check_for_updates(force=True)

        # Simulate a download that failed and left the manager in ERROR.
        mgr._status.state = UpdateState.ERROR.value
        mgr._status.error = "Network error after 3 retries"

        with patch.object(mgr, "_run_download") as fake_download:
            mgr.download_update()
            if mgr._download_thread:
                mgr._download_thread.join(timeout=5)
            self.assertTrue(fake_download.called, "retry after a failed download was refused")

    def test_apply_pre_execution_hash_check_and_abort_on_tamper(self) -> None:
        """Requirement D: Re-hash staged installer immediately before launching it. Abort if tampered."""
        mgr = UpdateManager(test_public_keys=[self.public_key])
        mgr._status.state = UpdateState.READY_TO_INSTALL.value
        mgr._status.can_apply = True
        mgr._status.install_method = "inno_silent"

        test_file = self.temp_dir / "staged-setup.exe"
        test_file.write_bytes(b"LEGITIMATE_CONTENT")
        import hashlib
        legit_hash = hashlib.sha256(b"LEGITIMATE_CONTENT").hexdigest().lower()

        mgr._staged_file_path = test_file
        mgr._verified_target_asset = {
            "sha256": legit_hash,
            "filename": "staged-setup.exe",
        }

        # Tamper with file before apply
        test_file.write_bytes(b"MALICIOUS_TAMPERED_CONTENT")

        from app.core.download_utils import SecurityError
        with self.assertRaises(SecurityError):
            mgr.apply_update()

        self.assertEqual(mgr.get_status()["state"], UpdateState.ERROR.value)
        self.assertIn("Pre-execution integrity verification failed", mgr.get_status()["error"])
        # Verify file was deleted on tamper detection
        self.assertFalse(test_file.exists())


if __name__ == "__main__":
    unittest.main()
