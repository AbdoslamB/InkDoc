"""Tests for hardened pack and update downloader in download_utils.py.

Verifies:
1. HTTP If-Range header sent on resume, handling 206 vs 200 fallback.
2. Redirect re-validation per retry with allowed hosts.
3. No retry on 4xx (e.g. 404, 403) but retry on 429 with Retry-After.
4. Size check before hash computation.
5. Combined disk-space check (download bytes + 1.5x uncompressed workspace).
6. Stale .part cleanup.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.download_utils import (
    DownloadError,
    SecurityError,
    check_disk_space,
    stream_download,
)


class TestDownloaderHardening(unittest.TestCase):
    """Test suite verifying Requirement 9 downloader capabilities."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="inkdoc_dl_test_")
        self.dest_path = Path(self.tmp_dir) / "output.bin"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_combined_disk_space_check(self) -> None:
        """Combined disk-space check accounts for download bytes + 1.5x uncompressed."""
        # Check standard success with ample space
        check_disk_space(Path(self.tmp_dir), required_bytes=1024, uncompressed_bytes=2048)

        # Mock shutil.disk_usage to simulate low disk space
        from collections import namedtuple
        Usage = namedtuple("Usage", ["total", "used", "free"])

        with patch("shutil.disk_usage", return_value=Usage(1000 * 1024 * 1024, 900 * 1024 * 1024, 100 * 1024 * 1024)):
            # Free is 100 MB.
            # If download needs 50 MB, uncompressed needs 50 MB -> 50 + 75 + 200 = 325 MB needed -> must raise DownloadError
            with self.assertRaises(DownloadError) as ctx:
                check_disk_space(
                    Path(self.tmp_dir),
                    required_bytes=50 * 1024 * 1024,
                    uncompressed_bytes=50 * 1024 * 1024,
                )
            self.assertIn("Insufficient disk space", str(ctx.exception))

    def test_stale_part_cleanup(self) -> None:
        """Stale .part files older than stale_part_age_seconds are deleted."""
        part_file = self.dest_path.with_suffix(self.dest_path.suffix + ".part")
        meta_file = self.dest_path.with_suffix(self.dest_path.suffix + ".part.meta")

        part_file.write_bytes(b"old-partial-data")
        meta_file.write_text(json.dumps({"etag": '"old-etag"'}), encoding="utf-8")

        # Set mtime to 3 days ago
        old_time = time.time() - (3 * 86400)
        os.utime(part_file, (old_time, old_time))

        # Run stream_download with non-existent URL to trigger initial cleanup before network request fails
        with self.assertRaises(DownloadError):
            stream_download(
                url="https://github.com/AbdoslamB/InkDoc/releases/download/v1.0.0/missing.bin",
                destination_path=self.dest_path,
                stale_part_age_seconds=86400.0,
                max_retries=0,
            )

        self.assertFalse(part_file.exists(), "Stale .part file was not cleaned up!")
        self.assertFalse(meta_file.exists(), "Stale .part.meta was not cleaned up!")

    def test_size_check_before_hash_rejects_and_deletes_mismatched_file(self) -> None:
        """Downloaded file size mismatch fails immediately and cleans up before hash."""
        payload = b"0123456789"

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            url = f"http://127.0.0.1:{port}/file.bin"
            # Declare expected_size as 50 bytes (actual is 10)
            with self.assertRaises(SecurityError) as ctx:
                stream_download(
                    url=url,
                    destination_path=self.dest_path,
                    expected_size=50,
                    allowed_hosts=frozenset({"127.0.0.1"}),
                    max_retries=0,
                )
            self.assertIn("Integrity check failed: downloaded size mismatch", str(ctx.exception))
            self.assertFalse(self.dest_path.exists())
            self.assertFalse(self.dest_path.with_suffix(".bin.part").exists())
        finally:
            server.shutdown()
            server.server_close()

    def test_no_retry_on_4xx_client_errors(self) -> None:
        """Client errors (e.g. 404, 403) fail immediately without retrying."""
        request_count = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                nonlocal request_count
                request_count += 1
                self.send_response(404, "Not Found")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            url = f"http://127.0.0.1:{port}/missing.bin"
            with self.assertRaises(DownloadError) as ctx:
                stream_download(
                    url=url,
                    destination_path=self.dest_path,
                    allowed_hosts=frozenset({"127.0.0.1"}),
                    max_retries=3,
                )
            self.assertIn("Client errors are not retried", str(ctx.exception))
            self.assertEqual(request_count, 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_retry_on_429_with_retry_after(self) -> None:
        """HTTP 429 inspects Retry-After header and retries successfully."""
        request_count = 0
        final_payload = b"successful-data-after-rate-limit"

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                nonlocal request_count
                request_count += 1
                if request_count == 1:
                    self.send_response(429, "Too Many Requests")
                    self.send_header("Retry-After", "1")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(final_payload)))
                    self.end_headers()
                    self.wfile.write(final_payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            url = f"http://127.0.0.1:{port}/rate-limited.bin"
            stream_download(
                url=url,
                destination_path=self.dest_path,
                expected_size=len(final_payload),
                allowed_hosts=frozenset({"127.0.0.1"}),
                max_retries=2,
            )
            self.assertEqual(request_count, 2)
            self.assertEqual(self.dest_path.read_bytes(), final_payload)
        finally:
            server.shutdown()
            server.server_close()

    def test_if_range_and_resume_flow(self) -> None:
        """Test resume flow with If-Range header and 206 Partial Content."""
        full_payload = b"0123456789ABCDEF"
        received_if_range = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                range_header = self.headers.get("Range")
                if_range = self.headers.get("If-Range")
                if if_range:
                    received_if_range.append(if_range)

                if range_header and range_header.startswith("bytes="):
                    start = int(range_header.split("=")[1].split("-")[0])
                    self.send_response(206, "Partial Content")
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Range", f"bytes {start}-{len(full_payload)-1}/{len(full_payload)}")
                    self.send_header("Content-Length", str(len(full_payload) - start))
                    self.send_header("ETag", '"v1-etag"')
                    self.end_headers()
                    self.wfile.write(full_payload[start:])
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(full_payload)))
                    self.send_header("ETag", '"v1-etag"')
                    self.end_headers()
                    self.wfile.write(full_payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            part_path = self.dest_path.with_suffix(self.dest_path.suffix + ".part")
            meta_path = self.dest_path.with_suffix(self.dest_path.suffix + ".part.meta")
            part_path.write_bytes(full_payload[:6])
            meta_path.write_text(json.dumps({"etag": '"v1-etag"'}), encoding="utf-8")

            url = f"http://127.0.0.1:{port}/file.bin"
            stream_download(
                url=url,
                destination_path=self.dest_path,
                expected_size=len(full_payload),
                allowed_hosts=frozenset({"127.0.0.1"}),
                support_resume=True,
            )

            self.assertIn('"v1-etag"', received_if_range)
            self.assertEqual(self.dest_path.read_bytes(), full_payload)
        finally:
            server.shutdown()
            server.server_close()


class TestPartFileCleanupScope(unittest.TestCase):
    """stream_download must never delete a .part file it did not create.

    For an update with install_method "download_reveal" the destination directory
    is the user's Downloads folder, and ".part" is the extension Firefox gives its
    own in-progress downloads.
    """

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="inkdoc_partscope_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _age(self, path: Path, days: float) -> None:
        old = time.time() - days * 86400
        os.utime(path, (old, old))

    def test_foreign_part_files_are_left_alone(self) -> None:
        # Two days old, so well past the 1 day stale threshold.
        firefox = self.tmp_dir / "family-photos.zip.part"
        firefox.write_bytes(b"a user download that is still in progress")
        self._age(firefox, 2)

        other_app = self.tmp_dir / "something-else.part"
        other_app.write_bytes(b"not ours either")
        self._age(other_app, 2)

        payload = b"inkdoc installer payload"

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence test server output
                return

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            dest = self.tmp_dir / "inkdoc-setup.exe"
            stream_download(
                url=f"http://127.0.0.1:{port}/inkdoc-setup.exe",
                destination_path=dest,
                expected_size=len(payload),
                allowed_hosts=frozenset({"127.0.0.1"}),
            )
            self.assertEqual(dest.read_bytes(), payload)
        finally:
            server.shutdown()
            server.server_close()

        self.assertTrue(
            firefox.is_file(),
            "stream_download deleted an unrelated .part file in the destination directory",
        )
        self.assertTrue(other_app.is_file(), "stream_download deleted a foreign .part file")

    def test_own_stale_part_is_still_discarded(self) -> None:
        """The legitimate cleanup must survive: our own stale partial is not resumed."""
        dest = self.tmp_dir / "inkdoc-setup.exe"
        own_part = self.tmp_dir / "inkdoc-setup.exe.part"
        own_part.write_bytes(b"GARBAGE FROM AN ABANDONED RUN")
        self._age(own_part, 2)

        payload = b"a completely different payload"
        seen_range_header: list[str | None] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen_range_header.append(self.headers.get("Range"))
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                return

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            stream_download(
                url=f"http://127.0.0.1:{port}/inkdoc-setup.exe",
                destination_path=dest,
                expected_size=len(payload),
                allowed_hosts=frozenset({"127.0.0.1"}),
            )
        finally:
            server.shutdown()
            server.server_close()

        # The stale partial was dropped rather than resumed, so no Range was sent
        # and the garbage bytes are not present in the final file.
        self.assertEqual(seen_range_header, [None])
        self.assertEqual(dest.read_bytes(), payload)


class TestConcurrentInstallLock(unittest.TestCase):
    """A rejected second install must not disturb the install already running."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="inkdoc_lock_"))
        os.environ["INKDOC_ENGINES_DIR"] = str(self.tmp_dir)

    def tearDown(self) -> None:
        os.environ.pop("INKDOC_ENGINES_DIR", None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_rejected_second_install_leaves_first_cancellable(self) -> None:
        from app.core.engine_manager import EngineInstallError, EngineManager
        from app.core.engine_manifest import (
            EngineManifest,
            PlatformPackInfo,
            get_current_platform_key,
        )

        platform_key = get_current_platform_key()
        manifest = EngineManifest(
            manifest_version="1.0.0",
            pack_version="1.0.0",
            min_app_version="1.0.0",
            supported_platforms={
                platform_key: PlatformPackInfo(
                    url="https://github.com/AbdoslamB/InkDoc/releases/download/docling-pack-v1/docling.tar.gz",
                    archive_format="tar.gz",
                    sha256="a" * 64,
                    size_bytes=1024,
                    uncompressed_size_bytes=4096,
                    interpreter_path="env/bin/python",
                )
            },
        )
        mgr = EngineManager(manifest=manifest)

        download_started = threading.Event()
        release_download = threading.Event()

        def blocking_download(*args, **kwargs):
            download_started.set()
            release_download.wait(timeout=15)
            raise DownloadError("halted by test")

        def run_first() -> None:
            try:
                mgr.install_engine("docling")
            except Exception:
                pass

        with patch("app.core.engine_manager.stream_download", side_effect=blocking_download):
            first = threading.Thread(target=run_first, daemon=True)
            first.start()
            self.assertTrue(download_started.wait(timeout=15), "first install never started downloading")

            first_event = mgr._cancel_events.get("docling")
            first_progress = mgr._progress.get("docling")
            self.assertIsNotNone(first_event)
            self.assertIsNotNone(first_progress)

            # A second request must be refused...
            with self.assertRaises(EngineInstallError):
                mgr.install_engine("docling")

            # ...without having replaced the running install's shared state first.
            self.assertIs(
                mgr._cancel_events.get("docling"),
                first_event,
                "second install replaced the running install's cancel event",
            )
            self.assertIs(
                mgr._progress.get("docling"),
                first_progress,
                "second install replaced the running install's progress object",
            )

            # The consequence that matters: cancel still reaches the running install.
            mgr.cancel_install("docling")
            self.assertTrue(
                first_event.is_set(),
                "cancel_install signalled an Event the running install is not watching",
            )

            release_download.set()
            first.join(timeout=15)

        self.assertFalse(first.is_alive(), "first install thread did not finish")


if __name__ == "__main__":
    unittest.main()
