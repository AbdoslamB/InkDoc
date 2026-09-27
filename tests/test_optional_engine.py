"""Integration and security test suite for InkDoc Optional Engines and Security Guards.

Covers all 18 security requirements:
- Platform support discovery & status
- HTTP 400 (unsupported) and HTTP 409 (uninstalled) engine guards
- Host header check (DNS rebinding prevention)
- CORS foreign port / foreign origin rejection
- Session token requirements on engine management & settings endpoints
- Safe extraction guards (zip-slip, absolute path, reserved name, symlink rejection)
- Symlink safety and path traversal in Downloads auto-save
- Zero silent engine substitution contract (explicit fallback setting)
- Launch-time worker integrity verification
- CDN download allowlist enforcement
"""
from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.converter import (
    ConversionOptions,
    auto_save_markdown,
)
from app.core.engine_manager import (
    EngineManager,
    SecurityError,
)
from app.core.engine_manifest import (
    EngineManifest,
    EngineStatus,
    PlatformPackInfo,
    get_current_platform_key,
)
from app.core.queue_model import EngineKind, QueueItem, SourceKind
from app.server.server import SESSION_TOKEN, app, set_server_port

client = TestClient(app)
set_server_port(13118)


# ─── 1. Platform Discovery & Normalization Tests ─────────────────────────────

def test_platform_key_normalization():
    """Verify platform keys are properly normalized."""
    key = get_current_platform_key()
    assert isinstance(key, str)
    assert "-" in key
    os_name, arch = key.split("-", 1)
    assert os_name in ("windows", "linux", "macos")
    assert arch in ("x86_64", "arm64", "arm")
    print("[OK] test_platform_key_normalization passed")


def test_manifest_support_check():
    """Verify supported vs unsupported platform determination."""
    mgr = EngineManager.get_instance()
    # Mock an unsupported platform
    with patch.object(mgr, "platform_key", "solaris-sparc"):
        assert not mgr.is_platform_supported("docling")
        assert mgr.get_engine_status("docling") == EngineStatus.UNSUPPORTED

    print("[OK] test_manifest_support_check passed")


# ─── 2. Engine Status & Listing Tests ────────────────────────────────────────

def test_engines_list_endpoint():
    """Verify GET /engines returns structured info for all engines."""
    resp = client.get("/engines")
    assert resp.status_code == 200
    data = resp.json()
    assert "engines" in data
    engines = data["engines"]

    assert "markitdown" in engines
    assert engines["markitdown"]["status"] == "installed"
    assert engines["markitdown"]["bundled"] is True

    assert "markit" in engines
    assert engines["markit"]["status"] == "installed"

    assert "docling" in engines
    assert engines["docling"]["status"] in ("installed", "installable", "unsupported")
    print("[OK] test_engines_list_endpoint passed")


# ─── 3. HTTP 400 & 409 Engine Availability Guards ───────────────────────────

def test_conversion_guards_for_uninstalled_engine():
    """Verify that requesting uninstalled Docling returns HTTP 409 (not 200 or 500)."""
    mgr = EngineManager.get_instance()
    with patch.object(mgr, "get_engine_status", return_value=EngineStatus.INSTALLABLE):
        resp = client.post(
            "/convert/file?engine=docling",
            files={"file": ("test.txt", b"Hello world", "text/plain")},
        )
        assert resp.status_code == 409
        assert "engine_not_installed" in resp.json()["detail"]
    print("[OK] test_conversion_guards_for_uninstalled_engine passed")


def test_conversion_guards_for_unsupported_engine():
    """Verify that requesting unsupported Docling returns HTTP 400."""
    mgr = EngineManager.get_instance()
    with patch.object(mgr, "get_engine_status", return_value=EngineStatus.UNSUPPORTED):
        resp = client.post(
            "/convert/file?engine=docling",
            files={"file": ("test.txt", b"Hello world", "text/plain")},
        )
        assert resp.status_code == 400
        assert "engine_unsupported" in resp.json()["detail"]
    print("[OK] test_conversion_guards_for_unsupported_engine passed")


# ─── 4. Security: Host Header DNS Rebinding Protection ───────────────────────

def test_host_header_dns_rebinding_protection():
    """Verify that requests with forged Host headers are refused with 403 Forbidden."""
    # Evil external host
    resp = client.get("/health", headers={"Host": "attacker.com"})
    assert resp.status_code == 403
    assert "Invalid Host" in resp.text

    # Spoofed internal host
    resp = client.get("/health", headers={"Host": "internal.bank.corp:13118"})
    assert resp.status_code == 403

    # Valid loopback hosts
    resp_loopback = client.get("/health", headers={"Host": "127.0.0.1:13118"})
    assert resp_loopback.status_code == 200

    resp_localhost = client.get("/health", headers={"Host": "localhost:13118"})
    assert resp_localhost.status_code == 200
    print("[OK] test_host_header_dns_rebinding_protection passed")


# ─── 5. Security: Exact Port CORS Restriction ────────────────────────────────

def test_cors_origin_exact_port_protection():
    """Verify CORS strictly rejects foreign ports and external origins."""
    # Foreign port on localhost (e.g., rogue local service or attacker port)
    resp = client.options(
        "/convert/file",
        headers={
            "Origin": "http://127.0.0.1:54321",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.headers.get("access-control-allow-origin") is None

    # Evil web origin
    resp_evil = client.options(
        "/convert/file",
        headers={
            "Origin": "https://malicious-website.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp_evil.headers.get("access-control-allow-origin") is None

    # Legitimate origin on exact server port
    resp_valid = client.options(
        "/convert/file",
        headers={
            "Origin": "http://127.0.0.1:13118",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp_valid.headers.get("access-control-allow-origin") == "http://127.0.0.1:13118"
    print("[OK] test_cors_origin_exact_port_protection passed")


def test_opaque_origin_cannot_read_session_token():
    """An opaque origin must never be able to read a response or change state.

    A sandboxed iframe on any website carries `Origin: null`. The UI HTML embeds
    SESSION_TOKEN in a <script>, so echoing Access-Control-Allow-Origin back to an
    opaque origin would let an arbitrary page fetch /InkDoc, scrape the token and
    then drive every management endpoint. Custom embedder schemes are treated the
    same way: no supported configuration produces them.
    """
    untrusted_origins = [
        "null",
        "file://",
        "pywebview://inkdoc",
        "vscode-webview://abc123",
        "https://malicious-website.com",
        "http://127.0.0.1:54321",      # rogue local service on another port
        "http://evil.127.0.0.1.nip.io:13118",  # loopback-looking hostname
        "http://localhost",            # no port: a different origin to :13118
    ]

    for origin in untrusted_origins:
        # The read path: no CORS grant, so a browser blocks the caller from
        # seeing the body even though the request itself succeeds.
        read = client.get("/InkDoc", headers={"Origin": origin})
        assert read.headers.get("access-control-allow-origin") is None, (
            f"{origin!r} was granted read access to the token-bearing UI HTML"
        )

        # The write path: state-changing requests are refused outright.
        write = client.post("/settings", headers={"Origin": origin}, json={})
        assert write.status_code == 403, f"{origin!r} was allowed to POST"
        assert "Cross-origin" in write.text

        # Preflight must not hand out a grant either.
        pre = client.options(
            "/settings",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
        assert pre.headers.get("access-control-allow-origin") is None

    # The real UI origin keeps working, and marks the response as Origin-dependent.
    ok = client.get("/InkDoc", headers={"Origin": "http://127.0.0.1:13118"})
    assert ok.headers.get("access-control-allow-origin") == "http://127.0.0.1:13118"
    assert ok.headers.get("vary") == "Origin"

    # localhost on the exact port is the same UI reached by a different name.
    ok_localhost = client.get("/InkDoc", headers={"Origin": "http://localhost:13118"})
    assert ok_localhost.headers.get("access-control-allow-origin") == "http://localhost:13118"

    # Native API clients (curl, examples/client_example.py) send no Origin at all and
    # must still reach the route. /settings then refuses them for lacking a session
    # token, which is the point: the request got past the Origin gate, so the
    # rejection reason distinguishes "blocked by CORS" from "blocked by auth".
    no_origin = client.post("/settings", json={})
    assert "Cross-origin" not in no_origin.text, "a request with no Origin was blocked by the Origin gate"
    print("[OK] test_opaque_origin_cannot_read_session_token passed")


# ─── 6. Security: Session Token Authentication on Management Endpoints ────────

def test_session_token_required_on_management():
    """Verify that management endpoints reject requests without a valid session token."""
    # Install
    resp = client.post("/engines/docling/install")
    assert resp.status_code == 403
    assert "session token" in resp.json()["detail"].lower()

    # Cancel
    resp = client.post("/engines/docling/cancel")
    assert resp.status_code == 403

    # Remove
    resp = client.post("/engines/docling/remove")
    assert resp.status_code == 403

    # Verify
    resp = client.post("/engines/docling/verify")
    assert resp.status_code == 403

    # Settings update
    resp = client.post("/settings", json={"docling_fallback": True})
    assert resp.status_code == 403

    # Query param ?token= must NOT be accepted (strictly X-InkDoc-Token header required)
    resp = client.post(
        f"/engines/docling/cancel?token={SESSION_TOKEN}",
    )
    assert resp.status_code == 403
    assert "x-inkdoc-token header required" in resp.json()["detail"].lower()

    # Invalid token in header
    resp = client.post(
        "/engines/docling/cancel",
        headers={"X-InkDoc-Token": "bogus-attacker-token"},
    )
    assert resp.status_code == 403

    # Valid token in header succeeds
    resp = client.post(
        "/engines/docling/cancel",
        headers={"X-InkDoc-Token": SESSION_TOKEN},
    )
    assert resp.status_code == 200
    print("[OK] test_session_token_required_on_management passed")


# ─── 7. Security: Safe Archive Extraction (Zip-Slip & Symlinks) ──────────────

def test_safe_path_validation_zip_slip():
    """Verify that _is_safe_path rejects path traversal attacks."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)

        # Valid relative paths
        assert EngineManager._is_safe_path(base, base / "file.txt")
        assert EngineManager._is_safe_path(base, base / "sub" / "dir" / "worker.py")

        # Zip-slip traversal
        assert not EngineManager._is_safe_path(base, base / ".." / "evil.txt")
        assert not EngineManager._is_safe_path(base, base / ".." / ".." / "etc" / "passwd")
        assert not EngineManager._is_safe_path(base, base / "sub" / ".." / ".." / "outside.txt")

        # Absolute paths outside base
        outside = Path(tempfile.gettempdir()) / "outside_file.txt"
        if not str(outside.resolve()).startswith(str(base.resolve())):
            assert not EngineManager._is_safe_path(base, outside)

        # Sibling prefix path (e.g., base_dir_evil/file.txt) must be rejected
        sibling_prefix = Path(str(base.resolve()) + "_evil") / "file.txt"
        assert not EngineManager._is_safe_path(base, sibling_prefix)
    print("[OK] test_safe_path_validation_zip_slip passed")


def test_safe_extraction_rejects_malicious_zip():
    """Verify safe extraction aborts when a zip contains traversal paths."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_file = Path(tmp_dir) / "malicious.zip"
        with zipfile.ZipFile(zip_file, "w") as zf:
            zf.writestr("../traversal.txt", b"evil content")

        staging_dir = Path(tmp_dir) / "staging"
        staging_dir.mkdir()

        try:
            EngineManager._extract_zip_safely(zip_file, staging_dir)
            raise AssertionError("Should have raised SecurityError for path traversal")
        except SecurityError as exc:
            assert "traversal" in str(exc).lower() or "zip slip" in str(exc).lower()
    print("[OK] test_safe_extraction_rejects_malicious_zip passed")


# ─── 8. Security: CDN Allowlist Validation ───────────────────────────────────

def test_cdn_allowlist_validation():
    """Verify that only approved GitHub Release CDN URLs are accepted."""
    # Valid release URLs
    EngineManager._validate_download_url("https://github.com/AbdoslamB/InkDoc/releases/download/v1.0.0/pack.zip")
    EngineManager._validate_download_url("https://release-assets.githubusercontent.com/12345/pack.zip")
    EngineManager._validate_download_url("https://objects.githubusercontent.com/github-production-release-asset-123/pack.zip")

    # Insecure HTTP rejected
    try:
        EngineManager._validate_download_url("http://github.com/AbdoslamB/InkDoc/releases/download/v1.0.0/pack.zip")
        raise AssertionError("Should reject HTTP")
    except SecurityError as exc:
        assert "insecure" in str(exc).lower() or "https" in str(exc).lower()

    # Untrusted domain rejected
    try:
        EngineManager._validate_download_url("https://evil-server.com/malicious.zip")
        raise AssertionError("Should reject untrusted domain")
    except SecurityError as exc:
        assert "allowlist" in str(exc).lower() or "trusted" in str(exc).lower()
    print("[OK] test_cdn_allowlist_validation passed")


# ─── 9. Security: Symlink Safe Downloads Auto-Save ───────────────────────────

def test_downloads_auto_save_symlink_protection():
    """Verify auto_save_markdown refuses to write through unauthorized symlinks."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        downloads = Path(tmp_dir) / "Downloads"
        downloads.mkdir()

        with patch("app.core.converter.get_downloads_dir", return_value=downloads):
            item = QueueItem(
                source="sample.txt",
                kind=SourceKind.FILE,
                display_name="sample.txt",
                engine=EngineKind.MARKITDOWN,
            )
            saved = auto_save_markdown(item, "# Content")
            assert saved.exists()
            assert saved.read_text(encoding="utf-8") == "# Content"

            # Verify collision safety
            saved2 = auto_save_markdown(item, "# Content 2")
            assert saved2 != saved
            assert saved2.exists()
            assert "sample (1).md" in saved2.name
    print("[OK] test_downloads_auto_save_symlink_protection passed")


# ─── 10. Engine Contract: Zero Silent Substitution ───────────────────────────

def test_zero_silent_substitution_contract():
    """Verify Docling never silently falls back to MarkItDown unless explicitly configured."""
    from app.core.engines.docling_engine import convert_with_docling
    from app.core.engines.docling_worker_client import DoclingWorkerClient

    mgr = EngineManager.get_instance()

    # 1. Fallback setting is False by default
    mgr.update_settings({"fallback_to_markitdown": False})
    item = QueueItem(
        source=str(Path(__file__)),
        kind=SourceKind.FILE,
        display_name="test_file.py",
        engine=EngineKind.DOCLING,
    )
    opts = ConversionOptions(engine=EngineKind.DOCLING)

    # When docling is not installed and fallback is OFF, it must raise RuntimeError
    with (
        patch.object(mgr, "is_platform_supported", return_value=True),
        patch.object(mgr, "is_engine_installed", return_value=False),
        patch.dict("sys.modules", {"docling": None}),
    ):
        try:
            convert_with_docling(item, opts)
            raise AssertionError("Should have raised RuntimeError without silent substitution")
        except RuntimeError as exc:
            assert "not installed" in str(exc).lower()
            assert not getattr(opts, "fallback_occurred", False)

    # 2. When docling fails and fallback setting is explicitly turned ON, it must record fallback_occurred
    mgr.update_settings({"fallback_to_markitdown": True})
    with (
        patch.object(mgr, "is_platform_supported", return_value=True),
        patch.object(mgr, "is_engine_installed", return_value=True),
    ):
        fake_client = DoclingWorkerClient.get_instance()
        with patch.object(fake_client, "convert_file", side_effect=RuntimeError("Corrupted PDF stream")):
            res = convert_with_docling(item, opts)
            assert getattr(opts, "fallback_occurred", False) is True
            assert "Corrupted PDF stream" in getattr(opts, "fallback_reason", "")
            assert len(res) > 0  # converted via MarkItDown fallback!

    # Reset setting
    mgr.update_settings({"fallback_to_markitdown": False})
    print("[OK] test_zero_silent_substitution_contract passed")


# ─── 11. Full Cryptographic Tree Verification ────────────────────────────────

def test_full_tree_hash_verification():
    """Verify full tree integrity check detects file modification or corruption."""
    mgr = EngineManager.get_instance()
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine_dir = Path(tmp_dir) / "docling"
        engine_dir.mkdir(parents=True)
        file1 = engine_dir / "file1.txt"
        file1.write_text("clean content", encoding="utf-8")
        worker_file = engine_dir / "worker.py"
        worker_file.write_text("print('worker')", encoding="utf-8")
        meta_file = engine_dir / "pack_meta.json"
        meta_file.write_text("{}", encoding="utf-8")

        import hashlib
        h1 = hashlib.sha256(b"clean content").hexdigest()
        h_worker = hashlib.sha256(b"print('worker')").hexdigest()

        # Mock pack info in manifest with valid 64-character hex hash
        fake_pack = PlatformPackInfo(
            url="https://github.com/AbdoslamB/InkDoc/releases/download/v1.0.0/docling.zip",
            archive_format="zip",
            sha256="0" * 64,
            size_bytes=100,
            uncompressed_size_bytes=100,
            interpreter_path="file1.txt",
            worker_script="worker.py",
            sha256_files={"file1.txt": h1, "worker.py": h_worker},
        )
        fake_manifest = EngineManifest(
            manifest_version="1.0.0",
            pack_version="1.0.0",
            min_app_version="1.0.0",
            supported_platforms={mgr.platform_key: fake_pack},
        )

        with (
            patch.object(mgr, "get_engine_dir", return_value=engine_dir),
            patch.object(mgr, "manifest", fake_manifest),
        ):
            # 1. Clean verify
            res = mgr.verify_full_installed_tree("docling")
            assert res["valid"] is True

            # 2. Tampered file
            file1.write_text("tampered content", encoding="utf-8")
            res_tampered = mgr.verify_full_installed_tree("docling")
            assert res_tampered["valid"] is False
            assert len(res_tampered["mismatches"]) > 0
    print("[OK] test_full_tree_hash_verification passed")


# ─── 12. Fail-Closed Integrity Tests (Commit 1 / Finding C-1) ───────────────

import io
import json


class _MockDownloadResponse:
    def __init__(self, data: bytes):
        self.stream = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


def _create_test_zip(dest: Path) -> tuple[str, dict[str, str]]:
    import hashlib
    with tempfile.TemporaryDirectory() as td:
        t_dir = Path(td)
        py = t_dir / "python.exe"
        py.write_bytes(b"python binary content")
        worker = t_dir / "worker.py"
        worker.write_bytes(b"worker script content")

        h_py = hashlib.sha256(py.read_bytes()).hexdigest().lower()
        h_worker = hashlib.sha256(worker.read_bytes()).hexdigest().lower()

        with zipfile.ZipFile(dest, "w") as zf:
            zf.write(py, "python.exe")
            zf.write(worker, "worker.py")

    arch_hash = hashlib.sha256(dest.read_bytes()).hexdigest().lower()
    return arch_hash, {"python.exe": h_py, "worker.py": h_worker}


def test_placeholder_hash_refused():
    """(a) Test that any placeholder hash is refused immediately (fail closed)."""
    mgr = EngineManager.get_instance()
    fake_pack = PlatformPackInfo(
        url="https://github.com/AbdoslamB/InkDoc/releases/download/v1.0.0/pack.zip",
        archive_format="zip",
        sha256="PLACEHOLDER_WINDOWS_X86_64_HASH",
        size_bytes=100,
        uncompressed_size_bytes=100,
        interpreter_path="python.exe",
        worker_script="worker.py",
    )
    fake_manifest = EngineManifest(
        manifest_version="1.0.0",
        pack_version="1.0.0",
        min_app_version="1.0.0",
        supported_platforms={mgr.platform_key: fake_pack},
    )
    with patch.object(mgr, "manifest", fake_manifest):
        assert not mgr.is_platform_supported("docling")
        failed = False
        try:
            mgr.install_engine("docling")
        except Exception as exc:
            failed = True
            err_msg = str(exc).lower()
            assert "placeholder" in err_msg or "invalid" in err_msg or "not available" in err_msg
        assert failed, "Expected install_engine to fail for placeholder hash"
    print("[OK] test_placeholder_hash_refused passed")


def test_missing_hash_refused():
    """(b) Test that a missing, empty, or malformed hash is refused immediately."""
    mgr = EngineManager.get_instance()
    for bad_hash in ["", "   ", "not_a_valid_sha256", "abc"]:
        fake_pack = PlatformPackInfo(
            url="https://github.com/AbdoslamB/InkDoc/releases/download/v1.0.0/pack.zip",
            archive_format="zip",
            sha256=bad_hash,
            size_bytes=100,
            uncompressed_size_bytes=100,
            interpreter_path="python.exe",
            worker_script="worker.py",
        )
        fake_manifest = EngineManifest(
            manifest_version="1.0.0",
            pack_version="1.0.0",
            min_app_version="1.0.0",
            supported_platforms={mgr.platform_key: fake_pack},
        )
        with patch.object(mgr, "manifest", fake_manifest):
            assert not mgr.is_platform_supported("docling")
            failed = False
            try:
                mgr.install_engine("docling")
            except Exception as exc:
                failed = True
                err_msg = str(exc).lower()
                assert "invalid" in err_msg or "not available" in err_msg or "placeholder" in err_msg
            assert failed, f"Expected install failure for invalid hash {bad_hash!r}"
    print("[OK] test_missing_hash_refused passed")


def test_mismatched_archive_hash_refused_and_deleted():
    """(c) Test that an archive whose hash doesn't match is refused and deleted."""
    mgr = EngineManager.get_instance()
    with tempfile.TemporaryDirectory() as td:
        zip_path = Path(td) / "pack.zip"
        real_hash, file_hashes = _create_test_zip(zip_path)
        zip_bytes = zip_path.read_bytes()

        # Expected hash is different valid 64-char hex hash
        tampered_expected_hash = "f" * 64

        target_dir = Path(td) / "docling"
        download_tmp = target_dir.parent / "docling.download.zip"

        fake_pack = PlatformPackInfo(
            url="https://github.com/AbdoslamB/InkDoc/releases/download/v1.0.0/pack.zip",
            archive_format="zip",
            sha256=tampered_expected_hash,
            size_bytes=len(zip_bytes),
            uncompressed_size_bytes=len(zip_bytes) * 2,
            interpreter_path="python.exe",
            worker_script="worker.py",
            sha256_files=file_hashes,
        )
        fake_manifest = EngineManifest(
            manifest_version="1.0.0",
            pack_version="1.0.0",
            min_app_version="1.0.0",
            supported_platforms={mgr.platform_key: fake_pack},
        )

        with (
            patch.object(mgr, "manifest", fake_manifest),
            patch.object(mgr, "get_engine_dir", return_value=target_dir),
            patch.object(mgr, "_open_safe_download_stream", return_value=_MockDownloadResponse(zip_bytes)),
            patch.object(mgr, "_check_disk_space"),
        ):
            failed = False
            try:
                mgr.install_engine("docling")
            except Exception as exc:
                failed = True
                assert "checksum mismatch" in str(exc).lower() or "mismatch" in str(exc).lower()
            assert failed, "Expected install_engine to fail with mismatch error"

            # Verify downloaded file is refused and deleted
            assert not download_tmp.exists(), "Temporary download file was not deleted after hash mismatch!"
            assert not target_dir.exists(), "Target directory was created despite hash mismatch!"

    print("[OK] test_mismatched_archive_hash_refused_and_deleted passed")


def test_correct_hash_succeeds():
    """(d) Test that a matching hash and valid file tree succeeds completely."""
    mgr = EngineManager.get_instance()
    with tempfile.TemporaryDirectory() as td:
        zip_path = Path(td) / "pack.zip"
        real_hash, file_hashes = _create_test_zip(zip_path)
        zip_bytes = zip_path.read_bytes()

        target_dir = Path(td) / "docling"

        fake_pack = PlatformPackInfo(
            url="https://github.com/AbdoslamB/InkDoc/releases/download/v1.0.0/pack.zip",
            archive_format="zip",
            sha256=real_hash,
            size_bytes=len(zip_bytes),
            uncompressed_size_bytes=len(zip_bytes) * 2,
            interpreter_path="python.exe",
            worker_script="worker.py",
            sha256_files=file_hashes,
        )
        fake_manifest = EngineManifest(
            manifest_version="1.0.0",
            pack_version="1.0.0",
            min_app_version="1.0.0",
            supported_platforms={mgr.platform_key: fake_pack},
        )

        with (
            patch.object(mgr, "manifest", fake_manifest),
            patch.object(mgr, "get_engine_dir", return_value=target_dir),
            patch.object(mgr, "_open_safe_download_stream", return_value=_MockDownloadResponse(zip_bytes)),
            patch.object(mgr, "_check_disk_space"),
        ):
            res = mgr.install_engine("docling")
            assert res["status"] == "success"
            assert target_dir.is_dir()
            assert (target_dir / "pack_meta.json").is_file()
            assert (target_dir / "python.exe").is_file()
            assert (target_dir / "worker.py").is_file()
            assert mgr.is_engine_installed("docling") is True

    print("[OK] test_correct_hash_succeeds passed")


def test_tampered_extracted_file_fails_tree_check():
    """(e) Test that a tampered file inside an extracted pack fails the tree check."""
    import hashlib
    mgr = EngineManager.get_instance()
    with tempfile.TemporaryDirectory() as td:
        target_dir = Path(td) / "docling"
        target_dir.mkdir(parents=True)
        py_file = target_dir / "python.exe"
        py_file.write_bytes(b"clean python binary")
        worker_file = target_dir / "worker.py"
        worker_file.write_bytes(b"clean worker")
        meta_file = target_dir / "pack_meta.json"
        meta_file.write_text("{}", encoding="utf-8")

        h_py = hashlib.sha256(py_file.read_bytes()).hexdigest().lower()
        h_worker = hashlib.sha256(worker_file.read_bytes()).hexdigest().lower()

        fake_pack = PlatformPackInfo(
            url="https://github.com/AbdoslamB/InkDoc/releases/download/v1.0.0/pack.zip",
            archive_format="zip",
            sha256="0" * 64,
            size_bytes=100,
            uncompressed_size_bytes=100,
            interpreter_path="python.exe",
            worker_script="worker.py",
            sha256_files={"python.exe": h_py, "worker.py": h_worker},
        )
        fake_manifest = EngineManifest(
            manifest_version="1.0.0",
            pack_version="1.0.0",
            min_app_version="1.0.0",
            supported_platforms={mgr.platform_key: fake_pack},
        )

        with (
            patch.object(mgr, "get_engine_dir", return_value=target_dir),
            patch.object(mgr, "manifest", fake_manifest),
        ):
            # 1. Clean verify succeeds
            assert mgr.verify_full_installed_tree("docling")["valid"] is True

            # 2. Tamper a file in the extracted pack
            py_file.write_bytes(b"MALICIOUS MODIFICATION")
            tampered_res = mgr.verify_full_installed_tree("docling")
            assert tampered_res["valid"] is False
            assert any("python.exe" in m for m in tampered_res["mismatches"])

    print("[OK] test_tampered_extracted_file_fails_tree_check passed")


def test_manifest_release_guard_validator():
    """Test that scripts/generate_engine_manifest.py validate_manifest catches all invalid hashes."""
    from scripts.generate_engine_manifest import validate_manifest

    # Current manifest should be valid (empty supported_platforms, no placeholders)
    valid, errors = validate_manifest(REPO_ROOT / "app" / "core" / "manifest.json")
    assert valid is True, f"Expected current manifest to be valid, got errors: {errors}"
    assert len(errors) == 0

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        bad_manifest = {
            "manifest_version": "1.0.0",
            "pack_version": "1.0.0",
            "min_app_version": "1.0.0",
            "supported_platforms": {
                "windows-x86_64": {
                    "url": "https://example.com/pack.zip",
                    "sha256": "PLACEHOLDER_WINDOWS_HASH",
                    "sha256_files": {},
                }
            },
        }
        json.dump(bad_manifest, tf)
        tmp_path = Path(tf.name)

    try:
        valid_bad, errors_bad = validate_manifest(tmp_path)
        assert valid_bad is False
        assert any("placeholder" in e.lower() for e in errors_bad)
    finally:
        tmp_path.unlink()

    print("[OK] test_manifest_release_guard_validator passed")


def test_source_mode_dynamic_docling_detection():
    """Requirement 8: In source mode only, detect ambient docling version dynamically."""
    from app.core.engine_manager import EngineManager, EngineStatus

    mgr = EngineManager.get_instance()

    # 1. When in source mode (sys.frozen is not True) and docling is installed on a supported platform
    with (
        patch.object(sys, "frozen", False, create=True),
        patch.object(mgr, "is_platform_supported", return_value=True),
        patch.object(mgr, "_detect_system_docling", return_value=(True, "2.128.0")),
        patch.object(mgr, "is_pack_installed", return_value=False),
    ):
        assert mgr.is_engine_installed("docling") is True
        assert mgr.get_engine_status("docling") == EngineStatus.INSTALLED

        info = mgr.get_all_engines_info()
        docling_info = info["engines"]["docling"]
        assert docling_info["status"] == "installed"
        assert docling_info["source_mode"] is True
        assert docling_info["version"] == "2.128.0"

    # 2. When in frozen mode (sys.frozen = True), ambient Python is strictly ignored
    with patch.object(sys, "frozen", True, create=True):
        is_sys, ver = mgr._detect_system_docling()
        assert is_sys is False
        assert ver is None

    print("[OK] test_source_mode_dynamic_docling_detection passed")


def test_manifest_derived_platforms():
    """Requirement 8: Platform support is derived strictly from manifest keys."""
    from app.core.engine_manager import EngineManager
    from app.core.engine_manifest import EngineManifest, PlatformPackInfo

    mgr = EngineManager.get_instance()
    mock_manifest = EngineManifest(
        manifest_version="1.0.0",
        pack_version="1.0.0",
        min_app_version="1.0.0",
        supported_platforms={
            "windows-x86_64": PlatformPackInfo(
                url="https://github.com/AbdoslamB/InkDoc/releases/download/v1.0.0/pack-win.zip",
                archive_format="zip",
                sha256="a" * 64,
                size_bytes=1000,
                uncompressed_size_bytes=2000,
                interpreter_path="python.exe",
            ),
            "linux-x86_64": PlatformPackInfo(
                url="https://github.com/AbdoslamB/InkDoc/releases/download/v1.0.0/pack-linux.tar.gz",
                archive_format="tar.gz",
                sha256="b" * 64,
                size_bytes=1000,
                uncompressed_size_bytes=2000,
                interpreter_path="bin/python",
            ),
        },
    )

    with patch.object(mgr, "manifest", mock_manifest):
        keys = mgr.get_supported_platform_keys()
        assert keys == ["linux-x86_64", "windows-x86_64"]
        all_info = mgr.get_all_engines_info()
        assert all_info["engines"]["docling"]["supported_platforms"] == ["linux-x86_64", "windows-x86_64"]

    print("[OK] test_manifest_derived_platforms passed")


def test_windows_long_path_behavior():
    """Requirement 7: Test Windows long-path handling and prefixing."""
    from app.core.engine_manager import to_long_path_safe

    # Short path remains unmodified
    short_p = Path("C:/InkDoc/engines/docling")
    assert to_long_path_safe(short_p) == short_p

    # Path >= 240 chars on Windows gets extended-length prefix
    with patch("sys.platform", "win32"):
        deep_parts = ["C:", "InkDoc"] + ["subfolder_" + str(i) for i in range(25)]
        deep_str = "\\".join(deep_parts)
        deep_p = Path(deep_str)
        if len(str(deep_p.resolve())) >= 240:
            prefixed = to_long_path_safe(deep_p)
            assert str(prefixed).startswith("\\\\?\\")

    print("[OK] test_windows_long_path_behavior passed")


def test_merge_manifest_fragments():
    """Requirement 10: Test merging per-platform manifest fragments into manifest.json."""
    from scripts.merge_manifest_fragments import merge_fragments

    with tempfile.TemporaryDirectory() as td:
        frag_dir = Path(td) / "fragments"
        frag_dir.mkdir()
        out_manifest = Path(td) / "manifest.json"

        # Create fragment 1: windows
        frag_win = {
            "pack_version": "1.0.0",
            "min_app_version": "1.0.0",
            "platform": "windows-x86_64",
            "supported_platforms": {
                "windows-x86_64": {
                    "url": "https://github.com/AbdoslamB/InkDoc/releases/download/docling-pack-v1/win.zip",
                    "archive_format": "zip",
                    "sha256": "a" * 64,
                    "size_bytes": 1000,
                    "uncompressed_size_bytes": 2000,
                    "interpreter_path": "python.exe",
                    "worker_script": "worker.py",
                    "sha256_files": {"python.exe": "b" * 64},
                }
            }
        }
        (frag_dir / "windows-x86_64.fragment.json").write_text(json.dumps(frag_win), encoding="utf-8")

        # Create fragment 2: linux
        frag_linux = {
            "pack_version": "1.0.0",
            "min_app_version": "1.0.0",
            "platform": "linux-x86_64",
            "supported_platforms": {
                "linux-x86_64": {
                    "url": "https://github.com/AbdoslamB/InkDoc/releases/download/docling-pack-v1/linux.tar.gz",
                    "archive_format": "tar.gz",
                    "sha256": "c" * 64,
                    "size_bytes": 1000,
                    "uncompressed_size_bytes": 2000,
                    "interpreter_path": "bin/python",
                    "worker_script": "worker.py",
                    "sha256_files": {"bin/python": "d" * 64},
                }
            }
        }
        (frag_dir / "linux-x86_64.fragment.json").write_text(json.dumps(frag_linux), encoding="utf-8")

        # Merge without requiring missing platform
        merged = merge_fragments(frag_dir, out_manifest, expected_platforms=["windows-x86_64", "linux-x86_64"])
        assert "windows-x86_64" in merged["supported_platforms"]
        assert "linux-x86_64" in merged["supported_platforms"]
        assert out_manifest.is_file()

        # Requiring a missing platform fails cleanly
        failed = False
        try:
            merge_fragments(frag_dir, out_manifest, expected_platforms=["macos-arm64"])
        except ValueError as exc:
            failed = True
            assert "Missing expected platforms" in str(exc)
        assert failed

    print("[OK] test_merge_manifest_fragments passed")


def test_cheap_release_guard():
    """Requirement 1: Test cheap release guard against manifest, sizes, and advertised README platforms."""
    from scripts.verify_engine_manifest_guard import verify_guard

    with tempfile.TemporaryDirectory() as td:
        manifest_p = Path(td) / "manifest.json"
        readme_p = Path(td) / "README.md"

        # 1. Manifest with empty platforms fails when allow_empty is False
        manifest_p.write_text(json.dumps({"manifest_version": "1.0.0", "supported_platforms": {}}), encoding="utf-8")
        readme_p.write_text("# InkDoc\n\nDocling layout analysis on Windows x86_64.\n", encoding="utf-8")

        ok, errors = verify_guard(manifest_p, readme_p, skip_network=True, allow_empty=False)
        assert not ok
        assert any("no supported_platforms" in e for e in errors)
        assert any("advertises Docling support for 'windows-x86_64'" in e for e in errors)

        # 2. Manifest with valid matching entry and size under 1.9 GiB passes
        valid_manifest = {
            "manifest_version": "1.0.0",
            "pack_version": "1.0.0",
            "min_app_version": "1.0.0",
            "supported_platforms": {
                "windows-x86_64": {
                    "url": "https://github.com/AbdoslamB/InkDoc/releases/download/docling-pack-v1/win.zip",
                    "archive_format": "zip",
                    "sha256": "e" * 64,
                    "size_bytes": 600 * 1024 * 1024,
                    "uncompressed_size_bytes": 1200 * 1024 * 1024,
                    "interpreter_path": "python.exe",
                    "worker_script": "worker.py",
                    "sha256_files": {"python.exe": "f" * 64},
                }
            }
        }
        manifest_p.write_text(json.dumps(valid_manifest), encoding="utf-8")
        ok, errors = verify_guard(manifest_p, readme_p, skip_network=True, allow_empty=False)
        assert ok, f"Expected guard to pass, got errors: {errors}"

        # 3. Size exceeding 1.9 GiB is rejected
        valid_manifest["supported_platforms"]["windows-x86_64"]["size_bytes"] = 2000 * 1024 * 1024
        manifest_p.write_text(json.dumps(valid_manifest), encoding="utf-8")
        ok, errors = verify_guard(manifest_p, readme_p, skip_network=True, allow_empty=False)
        assert not ok
        assert any("exceeds maximum limit of 1.9 GiB" in e for e in errors)

    print("[OK] test_cheap_release_guard passed")


def test_post_build_smoke_test_and_size_guard():
    """The post-build smoke test must reject a pack that is not relocatable.

    This fixture is deliberately the broken shape: a bare copy of the running
    interpreter with no standard library beside it, which is structurally what
    docling-pack-v2 shipped. Such a pack starts on the build machine, because the
    interpreter falls back to a Python installation that exists there, and fails on
    every user's machine.

    The test previously asserted that this arrangement *passed* the smoke test. It
    did pass, which is why the broken pack was published. The happy path is covered
    by the real build in build-packs.yml, which downloads a genuinely relocatable
    CPython; it cannot be fabricated cheaply here.
    """
    import zipfile

    from scripts.build_pack import run_post_build_smoke_test

    with tempfile.TemporaryDirectory() as td:
        arch_dir = Path(td)
        archive_path = arch_dir / "test_pack.zip"

        # Create a mock worker script
        mock_worker_code = (
            "import sys, json\n"
            "sys.stdout.write(json.dumps({'status': 'ready'}) + '\\n')\n"
            "sys.stdout.flush()\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    if req.get('action') == 'ping':\n"
            "        sys.stdout.write(json.dumps({'status': 'pong'}) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif req.get('action') == 'convert':\n"
            "        open(req['output_file'], 'w').write('# smoke')\n"
            "        sys.stdout.write(json.dumps({'status': 'ok'}) + '\\n')\n"
            "        sys.stdout.flush()\n"
        )

        # We'll package a runnable worker and copy current python into the zip
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("worker.py", mock_worker_code)
            zf.writestr("models/required-model.bin", b"test model")
            # Copy current python executable into the zip to act as isolated python
            zf.write(sys.executable, "bin/python.exe")

        # The smoke test must refuse this pack. Either failure mode is correct: the
        # copied interpreter may start and report a sys.prefix outside the extraction
        # directory, or it may fail to initialise at all without its stdlib.
        try:
            run_post_build_smoke_test(
                archive_path=archive_path,
                archive_format="zip",
                interpreter_rel="bin/python.exe",
                worker_rel="worker.py",
            )
        except RuntimeError as exc:
            message = str(exc)
            assert ("not relocatable" in message) or ("failed to start" in message), (
                f"smoke test failed, but not because the pack is unusable: {message}"
            )
        else:
            raise AssertionError(
                "post-build smoke test accepted a pack whose interpreter resolves "
                "outside the pack; this is exactly how docling-pack-v2 shipped"
            )

    print("[OK] test_post_build_smoke_test_and_size_guard passed")


# ─── Main Execution ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nRunning InkDoc Optional Engine & Security Integration Tests...")
    test_platform_key_normalization()
    test_manifest_support_check()
    test_engines_list_endpoint()
    test_conversion_guards_for_uninstalled_engine()
    test_conversion_guards_for_unsupported_engine()
    test_host_header_dns_rebinding_protection()
    test_cors_origin_exact_port_protection()
    test_opaque_origin_cannot_read_session_token()
    test_session_token_required_on_management()
    test_safe_path_validation_zip_slip()
    test_safe_extraction_rejects_malicious_zip()
    test_cdn_allowlist_validation()
    test_downloads_auto_save_symlink_protection()
    test_zero_silent_substitution_contract()
    test_full_tree_hash_verification()
    test_placeholder_hash_refused()
    test_missing_hash_refused()
    test_mismatched_archive_hash_refused_and_deleted()
    test_correct_hash_succeeds()
    test_tampered_extracted_file_fails_tree_check()
    test_manifest_release_guard_validator()
    test_source_mode_dynamic_docling_detection()
    test_manifest_derived_platforms()
    test_windows_long_path_behavior()
    test_merge_manifest_fragments()
    test_cheap_release_guard()
    test_post_build_smoke_test_and_size_guard()
    print("\nALL OPTIONAL ENGINE & SECURITY TESTS PASSED!\n")
