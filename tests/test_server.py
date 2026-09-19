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


if __name__ == "__main__":
    print("Running API Server integration tests...")
    test_root()
    test_health()
    test_extensions()
    test_convert_file()
    test_convert_batch()
    test_web_routes()
    print("\nALL API TESTS PASSED!")
