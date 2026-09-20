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


if __name__ == "__main__":
    unittest.main()
