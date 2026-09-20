"""Integration and endpoint tests for InkDoc Local API server."""
from __future__ import annotations

import io
import os
import re
import sys
import threading
from pathlib import Path

# Configure thread pool limits before scientific/native libraries load
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

import app.server.server as server_module
from app.server.server import app, set_update_applying

client = TestClient(app)


def test_root():
    response = client.get("/")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    assert data["app"] == "InkDoc Local API"
    assert data["status"] == "running"
    print("[OK] test_root passed")


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "markitdown_version" in data
    print("[OK] test_health passed")


def test_extensions():
    response = client.get("/extensions")
    assert response.status_code == 200
    data = response.json()
    assert ".pdf" in data["extensions"]
    assert ".docx" in data["extensions"]
    assert ".xlsx" in data["extensions"]
    print("[OK] test_extensions passed")


def test_convert_file():
    # Test text file conversion
    sample_content = b"# Test Document\n\nThis is a sample markdown test."
    file_payload = {"file": ("test.md", io.BytesIO(sample_content), "text/markdown")}
    response = client.post("/convert/file", files=file_payload)
    assert response.status_code == 200, f"Convert failed: {response.text}"
    assert "Test Document" in response.text
    print("[OK] test_convert_file (raw text) passed")

    # Test JSON response format
    file_payload = {"file": ("test2.txt", io.BytesIO(b"Simple plain text content"), "text/plain")}
    response = client.post("/convert/file?response_format=json", files=file_payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["filename"] == "test2.txt"
    assert "Simple plain text content" in data["markdown"]
    print("[OK] test_convert_file (json format) passed")


def test_audio_conversion_unavailable_is_not_500(monkeypatch):
    def raise_audio_failure(*_args):
        raise RuntimeError("FileConversionException: AudioConverter threw UnknownValueError")

    monkeypatch.setattr(server_module, "_perform_conversion", raise_audio_failure)
    response = client.post(
        "/convert/file?response_format=json",
        files={"file": ("sample.wav", io.BytesIO(b"RIFF"), "audio/x-wav")},
    )
    assert response.status_code == 422
    assert "Audio transcription unavailable" in response.json()["detail"]
    print("[OK] test_audio_conversion_unavailable_is_not_500 passed")


def test_convert_batch():
    files = [
        ("files", ("doc1.txt", io.BytesIO(b"Doc 1 contents"), "text/plain")),
        ("files", ("doc2.txt", io.BytesIO(b"Doc 2 contents"), "text/plain")),
    ]
    response = client.post("/convert/batch", files=files)
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert "doc1.txt" in data["results"]
    assert "doc2.txt" in data["results"]
    assert data["results"]["doc1.txt"]["success"] is True
    assert data["results"]["doc2.txt"]["success"] is True
    print("[OK] test_convert_batch passed")


def test_web_routes():
    # Test primary /InkDoc endpoint
    resp_inkdoc = client.get("/InkDoc")
    assert resp_inkdoc.status_code == 200, f"Expected 200 for /InkDoc, got {resp_inkdoc.status_code}"
    assert "InkDoc" in resp_inkdoc.text

    # Verify Content-Security-Policy header and dynamic nonce matching (Requirement M)
    csp = resp_inkdoc.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'nonce-" in csp
    assert "frame-ancestors 'none'" in csp

    # Extract nonce from CSP header
    match = re.search(r"'nonce-([a-f0-9]+)'", csp)
    assert match is not None, f"Expected nonce in CSP header, got: {csp}"
    nonce = match.group(1)

    # Verify script tag in HTML uses the exact matching nonce
    assert f'nonce="{nonce}"' in resp_inkdoc.text
    assert 'id="inkdoc-session-token"' in resp_inkdoc.text

    # Verify security hardening headers
    assert resp_inkdoc.headers.get("x-content-type-options") == "nosniff"
    assert resp_inkdoc.headers.get("x-frame-options") == "DENY"
    assert resp_inkdoc.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

    # Test backward-compatible alias /MarkItDown
    resp_alias = client.get("/MarkItDown")
    assert resp_alias.status_code == 200, f"Expected 200 for /MarkItDown alias, got {resp_alias.status_code}"
    print("[OK] test_web_routes passed")


def test_convert_url_ssrf_protection():
    """Verify that /convert/url rejects SSRF, local file, and metadata endpoints."""
    prohibited_urls = [
        "file:///etc/passwd",
        "file://C:/Windows/win.ini",
        "data:text/plain;base64,SGVsbG8sIFdvcmxkIQ==",
        "http://127.0.0.1:8000",
        "http://localhost:13118",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/admin",
        "http://10.0.0.1/status",
        "ftp://ftp.example.com/secret",
        "javascript:alert(1)",
    ]
    for prohibited in prohibited_urls:
        resp = client.post("/convert/url", json={"url": prohibited})
        assert resp.status_code == 400, (
            f"Expected 400 for prohibited URL '{prohibited}', got {resp.status_code}: {resp.text}"
        )
        assert "prohibited" in resp.text.lower() or "invalid" in resp.text.lower()

    print("[OK] test_convert_url_ssrf_protection passed")


def test_cors_restriction():
    """Verify CORS middleware blocks external origins and allows loopback origins."""
    # Prohibited external origin (e.g. malicious website)
    resp_external = client.options(
        "/health",
        headers={
            "Origin": "https://evil.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp_external.headers.get("access-control-allow-origin") != "*"
    assert resp_external.headers.get("access-control-allow-origin") != "https://evil.com"

    # Permitted loopback origins (must match exact bound port 13118)
    for origin in ("http://127.0.0.1:13118", "http://localhost:13118"):
        resp_local = client.options(
            "/health",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp_local.headers.get("access-control-allow-origin") == origin, (
            f"Expected allow-origin for '{origin}', got {resp_local.headers.get('access-control-allow-origin')}"
        )

    # Foreign localhost ports must NOT be permitted (DNS rebinding / local port isolation)
    for foreign_origin in ("http://127.0.0.1:54321", "http://localhost:3000"):
        resp_foreign = client.options(
            "/health",
            headers={
                "Origin": foreign_origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp_foreign.headers.get("access-control-allow-origin") is None, (
            f"Foreign origin '{foreign_origin}' should not be allowed by CORS"
        )

    print("[OK] test_cors_restriction passed")


def test_batch_size_limit():
    """Verify that /convert/batch rejects requests exceeding the 50-file limit."""
    too_many_files = [
        ("files", (f"doc_{i}.txt", io.BytesIO(b"sample text"), "text/plain"))
        for i in range(51)
    ]
    resp = client.post("/convert/batch", files=too_many_files)
    assert resp.status_code == 400, f"Expected 400 for 51 files, got {resp.status_code}"
    assert "exceeds maximum limit" in resp.text
    print("[OK] test_batch_size_limit passed")


def test_error_sanitization():
    """Verify that conversion errors do not leak internal filesystem paths or stack traces."""
    from app.server.server import sanitize_conversion_error

    # Windows path disclosure test
    err_windows = RuntimeError(r"Internal parser crashed at C:\Users\SensitiveUser\Documents\secret\file.bin")
    sanitized_win = sanitize_conversion_error(err_windows, context_name="test.bin")
    assert r"C:\Users\SensitiveUser" not in sanitized_win
    assert "[path]" in sanitized_win or "internal" in sanitized_win.lower()

    # Unix path disclosure test
    err_unix = RuntimeError("Failed opening /home/sensitive_user/.config/private/file.bin")
    sanitized_unix = sanitize_conversion_error(err_unix, context_name="test.bin")
    assert "/home/sensitive_user" not in sanitized_unix

    # Unsupported format message test (preserves keywords for classifyError in UI)
    err_unsupported = ValueError("unsupported format for document")
    sanitized_fmt = sanitize_conversion_error(err_unsupported, context_name="sample.xyz")
    assert "unsupported" in sanitized_fmt.lower()

    print("[OK] test_error_sanitization passed")


def test_upload_size_limit():
    """Verify that uploads exceeding MAX_FILE_SIZE_BYTES return HTTP 413."""
    import app.server.server as srv
    original_max = srv.MAX_FILE_SIZE_BYTES
    try:
        # Set threshold to 10 bytes for unit testing
        srv.MAX_FILE_SIZE_BYTES = 10
        payload = {"file": ("big.txt", io.BytesIO(b"This text is definitely longer than 10 bytes"), "text/plain")}
        resp = client.post("/convert/file", files=payload)
        assert resp.status_code == 413, f"Expected 413, got {resp.status_code}: {resp.text}"
        assert "exceeds maximum allowed size" in resp.text
    finally:
        srv.MAX_FILE_SIZE_BYTES = original_max
    print("[OK] test_upload_size_limit passed")


def test_plugin_permission_guard():
    """Verify that enable_plugins=True returns HTTP 403 when not permitted on server."""
    # File conversion with plugins
    file_payload = {"file": ("test.txt", io.BytesIO(b"content"), "text/plain")}
    resp_file = client.post("/convert/file?enable_plugins=true", files=file_payload)
    assert resp_file.status_code == 403, f"Expected 403, got {resp_file.status_code}: {resp_file.text}"
    assert "plugins are disabled" in resp_file.text.lower()

    # URL conversion with plugins
    resp_url = client.post(
        "/convert/url",
        json={"url": "https://github.com", "enable_plugins": True},
    )
    assert resp_url.status_code == 403, f"Expected 403, got {resp_url.status_code}: {resp_url.text}"
    assert "plugins are disabled" in resp_url.text.lower()

    print("[OK] test_plugin_permission_guard passed")


def test_epub_zip_bomb_protection():
    """Verify that EPUB conversion catches oversized files / zip bomb conditions."""
    import tempfile
    import zipfile

    import app.core.engines.markit_engine as me

    original_limit = me.MAX_EPUB_ENTRY_SIZE
    me.MAX_EPUB_ENTRY_SIZE = 10  # Lower limit to 10 bytes for unit test

    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        tmp_epub = Path(tmp.name)

    try:
        with zipfile.ZipFile(tmp_epub, "w") as z:
            z.writestr("chapter1.xhtml", b"<html><body>This content is longer than ten bytes.</body></html>")

        try:
            me._convert_epub(tmp_epub)
            raise AssertionError("Expected _convert_epub to raise RuntimeError for oversized entry")
        except RuntimeError as exc:
            assert "exceeds safe size limit" in str(exc)
    finally:
        me.MAX_EPUB_ENTRY_SIZE = original_limit
        if tmp_epub.exists():
            tmp_epub.unlink()

    print("[OK] test_epub_zip_bomb_protection passed")


def test_server_verification_check():
    """Verify verify_inkdoc_server returns False for closed or non-InkDoc ports."""
    from app.desktop.runner import verify_inkdoc_server

    # Non-existent server port
    assert verify_inkdoc_server("127.0.0.1", 1) is False
    print("[OK] test_server_verification_check passed")


def test_desktop_runner_gui_mode_stdio_protection():
    """Verify runner and EmbeddedServer don't crash when sys.stdout/stderr are None (PyInstaller windowed mode)."""
    from app.desktop.runner import EmbeddedServer

    old_stdout, old_stderr, old_stdin = sys.stdout, sys.stderr, sys.stdin
    try:
        sys.stdout = None
        sys.stderr = None
        sys.stdin = None

        server = EmbeddedServer("127.0.0.1", 13192)
        assert server.server is not None
        assert server.server.config is not None
    finally:
        sys.stdout, sys.stderr, sys.stdin = old_stdout, old_stderr, old_stdin

    print("[OK] test_desktop_runner_gui_mode_stdio_protection passed")


def test_conversion_offloaded_from_event_loop():
    """Verify conversion does not block asyncio event loop and runs off main thread."""
    import asyncio
    import time
    from unittest.mock import patch

    import httpx

    worker_threads = []

    def mock_convert(*args, **kwargs):
        worker_threads.append(threading.current_thread().name)
        time.sleep(0.3)
        return "sample markdown"

    async def run_check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:13118"
        ) as client:
            with patch("app.server.server.convert_item", side_effect=mock_convert):
                conv_task = asyncio.create_task(
                    client.post("/InkDoc/convert/file", files={"file": ("test.txt", b"hello")})
                )
                await asyncio.sleep(0.05)
                h_t0 = time.time()
                h_resp = await client.get("/health")
                h_dur = time.time() - h_t0
                await conv_task
                assert h_resp.status_code == 200
                assert h_dur < 0.2, f"Health check stalled ({h_dur:.3f}s); event loop was blocked"
                assert len(worker_threads) == 1
                assert "MainThread" not in worker_threads[0]

    asyncio.run(run_check())
    print("[OK] test_conversion_offloaded_from_event_loop passed")


def test_conversion_concurrency_cap():
    """Verify concurrency cap limits simultaneous active conversions."""
    import asyncio
    import time
    from unittest.mock import patch

    import httpx

    from app.server.server import get_active_conversions_count

    max_concurrent = 0

    def slow_convert(*args, **kwargs):
        nonlocal max_concurrent
        curr = get_active_conversions_count()
        if curr > max_concurrent:
            max_concurrent = curr
        time.sleep(0.2)
        return "markdown output"

    async def run_cap():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:13118"
        ) as client:
            with patch("app.server.server.convert_item", side_effect=slow_convert):
                tasks = [
                    asyncio.create_task(
                        client.post("/InkDoc/convert/file", files={"file": (f"test{i}.txt", b"content")})
                    )
                    for i in range(4)
                ]
                await asyncio.gather(*tasks)
                assert max_concurrent <= 2, f"Concurrency cap exceeded: {max_concurrent}"

    asyncio.run(run_cap())
    print("[OK] test_conversion_concurrency_cap passed")


def test_thread_safe_atomic_settings():
    """Verify thread-safe concurrent settings updates and atomic file replacement."""
    import json
    import tempfile

    from app.core.engine_manager import EngineManager

    with tempfile.TemporaryDirectory() as td:
        em = EngineManager()
        settings_file = Path(td) / "settings.json"
        em.get_settings_file_path = lambda: settings_file

        def worker(i):
            em.update_settings({f"key_{i}": i, "counter": i})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = em.get_settings()
        disk_content = json.loads(settings_file.read_text(encoding="utf-8"))
        for i in range(15):
            assert f"key_{i}" in final
            assert f"key_{i}" in disk_content

    print("[OK] test_thread_safe_atomic_settings passed")


def test_batch_conversion_options_isolation():
    """Verify that fallback flags in ConversionOptions do not leak between batch items (H-6)."""
    from unittest.mock import patch

    def mock_convert_item(item, options):
        # First file triggers fallback
        if "doc1.txt" in item.source or item.display_name == "doc1.txt":
            options.fallback_occurred = True
            options.fallback_reason = "Docling engine unavailable"
            return "doc1 content via fallback"
        # Second file succeeds without fallback
        return "doc2 content without fallback"

    files = [
        ("files", ("doc1.txt", io.BytesIO(b"Doc 1 contents"), "text/plain")),
        ("files", ("doc2.txt", io.BytesIO(b"Doc 2 contents"), "text/plain")),
    ]
    with (
        patch("app.server.server._guard_engine_availability", return_value=None),
        patch("app.server.server.convert_item", side_effect=mock_convert_item),
    ):
        response = client.post("/convert/batch?engine=docling", files=files)
        assert response.status_code == 200
        data = response.json()
        assert data["results"]["doc1.txt"]["fallback"] is True
        assert data["results"]["doc1.txt"]["engine_used"] == "markitdown"
        # doc2 MUST NOT have fallback=True leaked from doc1!
        assert data["results"]["doc2.txt"]["fallback"] is False, (
            f"Fallback leaked to doc2: {data['results']['doc2.txt']}"
        )
        assert data["results"]["doc2.txt"]["engine_used"] == "docling"

    print("[OK] test_batch_conversion_options_isolation passed")


def test_conversions_rejected_when_update_applying():
    """Verify that when an update is actively applying, all conversion endpoints reject requests (H-9)."""
    set_update_applying(True)
    try:
        # 1. /convert/file
        resp_file = client.post(
            "/convert/file",
            files={"file": ("test.txt", io.BytesIO(b"Hello world"), "text/plain")},
        )
        assert resp_file.status_code == 409
        assert "update in progress" in resp_file.json()["detail"].lower()

        # 2. /convert/url
        resp_url = client.post(
            "/convert/url",
            json={"url": "https://example.com/test"},
        )
        assert resp_url.status_code == 409
        assert "update in progress" in resp_url.json()["detail"].lower()

        # 3. /convert/batch
        resp_batch = client.post(
            "/convert/batch",
            files=[("files", ("test.txt", io.BytesIO(b"Hello world"), "text/plain"))],
        )
        assert resp_batch.status_code == 409
        assert "update in progress" in resp_batch.json()["detail"].lower()
    finally:
        set_update_applying(False)

    # After reset, file conversion succeeds again
    resp_ok = client.post(
        "/convert/file",
        files={"file": ("test.txt", io.BytesIO(b"# Restored\nWorking"), "text/plain")},
    )
    assert resp_ok.status_code == 200
    print("[OK] test_conversions_rejected_when_update_applying passed")


if __name__ == "__main__":
    print("Running API Server integration tests...")
    test_root()
    test_health()
    test_extensions()
    test_convert_file()
    test_convert_batch()
    test_web_routes()
    test_convert_url_ssrf_protection()
    test_cors_restriction()
    test_batch_size_limit()
    test_error_sanitization()
    test_upload_size_limit()
    test_plugin_permission_guard()
    test_epub_zip_bomb_protection()
    test_server_verification_check()
    test_desktop_runner_gui_mode_stdio_protection()
    test_conversion_offloaded_from_event_loop()
    test_conversion_concurrency_cap()
    test_thread_safe_atomic_settings()
    test_batch_conversion_options_isolation()
    test_conversions_rejected_when_update_applying()
    print("\nALL API TESTS PASSED!")




