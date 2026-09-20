"""End-to-End integration test harness for InkDoc update system.

Verifies complete lifecycle with a local mock HTTP server and TEST Ed25519 keypair:
1. Successful end-to-end check, streaming download, and SHA-256 verification
2. Missing manifest (HTTP 404) returns distinct error (Requirement B)
3. Envelope payload tampering rejected (Requirement A)
4. Wrong-repository download URL rejected (Requirement C)
5. Redirect hop to disallowed host blocked by StrictRedirectHandler (Requirement C)
6. Truncated or corrupted download integrity rejection (Requirement C)
7. Frozen build override protection (Requirement H)
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Configure thread pool limits before scientific/native libraries load
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cryptography.hazmat.primitives.asymmetric import ed25519

from app.core.download_utils import SecurityError
from app.core.update_manager import (
    OFFICIAL_UPDATE_MANIFEST_URL,
    UpdateManager,
    UpdateState,
)
from scripts.sign_manifest import build_envelope


class MockUpdateHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Mock HTTP handler serving simulated release manifests and download payloads."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # Suppress console log spam during tests

    def do_GET(self) -> None:  # noqa: N802
        routes = getattr(self.server, "routes", {})
        path = self.path.split("?")[0]

        if path in routes:
            handler_func = routes[path]
            handler_func(self)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"404 Not Found")


class TestUpdateE2EHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Generate ephemeral test Ed25519 keypair
        cls.private_key = ed25519.Ed25519PrivateKey.generate()
        cls.public_key = cls.private_key.public_key()

        # Spin up local test HTTP server on loopback ephemeral port
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), MockUpdateHTTPHandler)
        cls.port = cls.server.server_address[1]
        cls.server.routes = {}  # type: ignore[attr-defined]

        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self.server.routes = {}  # type: ignore[attr-defined]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="inkdoc_e2e_"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_e2e_successful_update_check_and_download(self) -> None:
        """Requirement 1 & C: Check, stream download, and verify SHA-256 successfully."""
        asset_content = b"GENUINE_INKDOC_V2_EXECUTABLE_BINARY_DATA" * 50
        asset_hash = hashlib.sha256(asset_content).hexdigest().lower()
        asset_size = len(asset_content)

        manifest_data = {
            "version": "2.0.0",
            "issued_at": "2026-09-19T12:00:00Z",
            "html_url": "https://github.com/AbdoslamB/InkDoc/releases/tag/v2.0.0",
            "notes": "## What's Changed\n* Added verified in-app updater\n* Enhanced PDF table layouts",
            "assets": {
                "windows-x86_64-installer": {
                    "filename": "inkdoc-setup.exe",
                    "url": f"http://127.0.0.1:{self.port}/abdoslamb/inkdoc/releases/download/v2.0.0/inkdoc-setup.exe",
                    "sha256": asset_hash,
                    "size_bytes": asset_size,
                    "install_method": "inno_silent",
                }
            },
        }

        envelope = build_envelope(manifest_data, self.private_key)
        envelope_bytes = json.dumps(envelope).encode("utf-8")

        # Configure routes on test server
        def serve_manifest(handler: http.server.BaseHTTPRequestHandler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(envelope_bytes)))
            handler.end_headers()
            handler.wfile.write(envelope_bytes)

        def serve_asset(handler: http.server.BaseHTTPRequestHandler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Length", str(len(asset_content)))
            handler.end_headers()
            handler.wfile.write(asset_content)

        manifest_path = "/releases/latest/download/inkdoc-update-manifest.json"
        asset_path = "/abdoslamb/inkdoc/releases/download/v2.0.0/inkdoc-setup.exe"

        self.server.routes[manifest_path] = serve_manifest  # type: ignore[attr-defined]
        self.server.routes[asset_path] = serve_asset        # type: ignore[attr-defined]

        manifest_url = f"http://127.0.0.1:{self.port}{manifest_path}"
        mgr = UpdateManager(
            manifest_url_override=manifest_url,
            test_public_keys=[self.public_key],
            is_desktop_runner=True,
        )
        mgr._platform_key = "windows-x86_64-installer"

        # 1. Check for updates
        check_res = mgr.check_for_updates(force=True)
        self.assertEqual(check_res["state"], UpdateState.AVAILABLE.value)
        self.assertEqual(check_res["latest_version"], "2.0.0")
        self.assertEqual(check_res["asset_name"], "inkdoc-setup.exe")

        # 2. Download update
        mgr.download_update()

        # Wait for background streaming thread to finish
        for _ in range(50):
            status = mgr.get_status()
            if status["state"] in (UpdateState.READY_TO_INSTALL.value, UpdateState.ERROR.value):
                break
            time.sleep(0.08)

        final_status = mgr.get_status()
        self.assertEqual(final_status["state"], UpdateState.READY_TO_INSTALL.value)
        self.assertTrue(final_status["can_apply"])
        self.assertIsNotNone(final_status["staged_path"])

        # Staged file must exist and match hash
        staged = Path(final_status["staged_path"])
        self.assertTrue(staged.is_file())
        self.assertEqual(hashlib.sha256(staged.read_bytes()).hexdigest().lower(), asset_hash)

    def test_missing_manifest_404_shows_distinct_error_not_up_to_date(self) -> None:
        """Requirement B: Missing 404 manifest must never show 'up to date'."""
        manifest_url = f"http://127.0.0.1:{self.port}/nonexistent-manifest.json"
        mgr = UpdateManager(
            manifest_url_override=manifest_url,
            test_public_keys=[self.public_key],
        )

        res = mgr.check_for_updates(force=True)
        self.assertEqual(res["state"], UpdateState.ERROR.value)
        self.assertIn("HTTP 404", res["error"])
        self.assertNotEqual(res["state"], UpdateState.UP_TO_DATE.value)

    def test_envelope_payload_tampering_rejected(self) -> None:
        """Requirement A: Verify signature over exact received bytes before parsing."""
        manifest_data = {
            "version": "2.1.0",
            "issued_at": "2026-09-19T12:00:00Z",
            "assets": {},
        }
        envelope = build_envelope(manifest_data, self.private_key)

        # Attacker modifies the payload base64 string
        tampered_manifest = {
            "version": "9.9.9",
            "issued_at": "2026-09-19T12:00:00Z",
            "assets": {},
        }
        envelope["payload"] = base64.b64encode(json.dumps(tampered_manifest).encode("utf-8")).decode("ascii")
        tampered_bytes = json.dumps(envelope).encode("utf-8")

        manifest_path = "/tampered-manifest.json"
        self.server.routes[manifest_path] = lambda h: (  # type: ignore[attr-defined]
            h.send_response(200),
            h.send_header("Content-Type", "application/json"),
            h.end_headers(),
            h.wfile.write(tampered_bytes),
        )

        mgr = UpdateManager(
            manifest_url_override=f"http://127.0.0.1:{self.port}{manifest_path}",
            test_public_keys=[self.public_key],
        )

        res = mgr.check_for_updates(force=True)
        self.assertEqual(res["state"], UpdateState.ERROR.value)
        self.assertIn("Cryptographic signature verification failed", res["error"])

    def test_disallowed_repo_url_rejected(self) -> None:
        """Requirement C: Asset URL path must sit under official repository release path."""
        manifest_data = {
            "version": "2.0.0",
            "issued_at": "2026-09-19T12:00:00Z",
            "assets": {
                "windows-x86_64-installer": {
                    "filename": "inkdoc-setup.exe",
                    # Disallowed repository path
                    "url": "https://github.com/AttackerUser/MaliciousFork/releases/download/v2.0.0/inkdoc-setup.exe",
                    "sha256": "a" * 64,
                    "size_bytes": 1000,
                }
            },
        }
        envelope = build_envelope(manifest_data, self.private_key)
        envelope_bytes = json.dumps(envelope).encode("utf-8")

        manifest_path = "/wrong-repo-manifest.json"
        self.server.routes[manifest_path] = lambda h: (  # type: ignore[attr-defined]
            h.send_response(200),
            h.send_header("Content-Type", "application/json"),
            h.end_headers(),
            h.wfile.write(envelope_bytes),
        )

        mgr = UpdateManager(
            manifest_url_override=f"http://127.0.0.1:{self.port}{manifest_path}",
            test_public_keys=[self.public_key],
        )
        res = mgr.check_for_updates(force=True)
        self.assertEqual(res["state"], UpdateState.ERROR.value)
        self.assertIn("official repository prefix", res["error"])

    def test_redirect_hop_to_disallowed_host_blocked(self) -> None:
        """Requirement C: StrictRedirectHandler must validate every single redirect hop."""
        def serve_redirect(handler: http.server.BaseHTTPRequestHandler) -> None:
            # Attempt redirect to untrusted host
            handler.send_response(302)
            handler.send_header("Location", "https://untrusted-malicious-cdn.com/trojan.exe")
            handler.end_headers()

        redir_path = "/redirect-to-untrusted"
        self.server.routes[redir_path] = serve_redirect  # type: ignore[attr-defined]

        dest_file = self.temp_dir / "target.exe"
        from app.core.download_utils import stream_download
        with self.assertRaises(SecurityError) as ctx:
            stream_download(
                url=f"http://127.0.0.1:{self.port}{redir_path}",
                destination_path=dest_file,
                allowed_hosts=frozenset({"127.0.0.1"}),
            )
        self.assertIn("not in the trusted CDN allowlist", str(ctx.exception))

    def test_truncated_download_rejected(self) -> None:
        """Requirement C: Download with fewer bytes or SHA-256 mismatch is rejected and removed."""
        truncated_content = b"ONLY_FIRST_HALF_OF_FILE"
        expected_full_content = b"ONLY_FIRST_HALF_OF_FILE_AND_THE_REST"
        expected_hash = hashlib.sha256(expected_full_content).hexdigest().lower()

        manifest_data = {
            "version": "2.0.0",
            "issued_at": "2026-09-19T12:00:00Z",
            "assets": {
                "windows-x86_64-installer": {
                    "filename": "inkdoc-setup.exe",
                    "url": f"http://127.0.0.1:{self.port}/abdoslamb/inkdoc/releases/download/v2.0.0/inkdoc-setup.exe",
                    "sha256": expected_hash,
                    "size_bytes": len(expected_full_content),
                    "install_method": "inno_silent",
                }
            },
        }
        envelope = build_envelope(manifest_data, self.private_key)
        envelope_bytes = json.dumps(envelope).encode("utf-8")

        self.server.routes["/manifest.json"] = lambda h: (  # type: ignore[attr-defined]
            h.send_response(200),
            h.send_header("Content-Type", "application/json"),
            h.end_headers(),
            h.wfile.write(envelope_bytes),
        )

        self.server.routes["/abdoslamb/inkdoc/releases/download/v2.0.0/inkdoc-setup.exe"] = lambda h: (  # type: ignore[attr-defined]
            h.send_response(200),
            h.send_header("Content-Type", "application/octet-stream"),
            h.send_header("Content-Length", str(len(truncated_content))),
            h.end_headers(),
            h.wfile.write(truncated_content),
        )

        mgr = UpdateManager(
            manifest_url_override=f"http://127.0.0.1:{self.port}/manifest.json",
            test_public_keys=[self.public_key],
        )
        mgr._platform_key = "windows-x86_64-installer"

        mgr.check_for_updates(force=True)
        mgr.download_update()

        for _ in range(50):
            st = mgr.get_status()
            if st["state"] in (UpdateState.READY_TO_INSTALL.value, UpdateState.ERROR.value):
                break
            time.sleep(0.08)

        st = mgr.get_status()
        self.assertEqual(st["state"], UpdateState.ERROR.value)
        self.assertIn("Integrity check failed", st["error"])

    def test_frozen_build_strictly_ignores_overrides(self) -> None:
        """Requirement H: In frozen builds, no env var or flag may redirect the update URL or key."""
        with patch.object(sys, "frozen", True, create=True):
            mgr = UpdateManager(
                manifest_url_override="http://malicious-local.test/manifest.json",
                test_public_keys=[self.public_key],
            )
            # Overrides must be completely ignored in frozen builds
            self.assertEqual(mgr._manifest_url, OFFICIAL_UPDATE_MANIFEST_URL)
            self.assertFalse(mgr._test_mode)


if __name__ == "__main__":
    unittest.main()
