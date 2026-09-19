"""Integration and endpoint tests for InkDoc Local API server."""
from __future__ import annotations

import io
import sys
from pathlib import Path

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

from app.server.server import app

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

    # Permitted loopback origins
    for origin in ("http://127.0.0.1:13118", "http://localhost:13118", "http://127.0.0.1:54321"):
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
    print("\nALL API TESTS PASSED!")



