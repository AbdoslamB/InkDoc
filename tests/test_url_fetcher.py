"""Tests for unified secure URL fetcher: SSRF validation, IP pinning, timeouts, size caps, and redirects."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import urllib3
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.security import SSRFValidationError  # noqa: E402
from app.core.url_fetcher import (  # noqa: E402
    UrlFetchError,
    UrlFetchSizeExceededError,
    UrlFetchTimeoutError,
    fetch_url_safely,
)
from app.server.server import app  # noqa: E402

client = TestClient(app)


def test_redirect_to_127_0_0_1():
    """Verify that a redirect pointing to 127.0.0.1 is caught and rejected."""
    mock_resp = MagicMock()
    mock_resp.status = 302
    mock_resp.headers = {"Location": "http://127.0.0.1:8000/secret"}

    with (
        patch("socket.getaddrinfo") as mock_dns,
        patch.object(urllib3.HTTPConnectionPool, "request", return_value=mock_resp),
    ):
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        try:
            fetch_url_safely("http://example.com/redirect-to-localhost")
            raise AssertionError("Should have raised SSRFValidationError")
        except SSRFValidationError as exc:
            assert "prohibited" in str(exc).lower() or "loopback" in str(exc).lower()

    # Also test via API endpoint
    with patch(
        "app.core.url_fetcher.fetch_url_safely",
        side_effect=SSRFValidationError("Host '127.0.0.1' resolves to prohibited loopback address."),
    ):
        resp = client.post("/convert/url", json={"url": "http://example.com/redirect-to-localhost"})
        assert resp.status_code == 400
        assert "prohibited" in resp.text.lower()
    print("[OK] test_redirect_to_127_0_0_1 passed")


def test_redirect_to_169_254_169_254():
    """Verify that a redirect pointing to 169.254.169.254 is caught and rejected."""
    mock_resp = MagicMock()
    mock_resp.status = 302
    mock_resp.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

    with (
        patch("socket.getaddrinfo") as mock_dns,
        patch.object(urllib3.HTTPConnectionPool, "request", return_value=mock_resp),
    ):
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        try:
            fetch_url_safely("http://example.com/redirect-to-metadata")
            raise AssertionError("Should have raised SSRFValidationError")
        except SSRFValidationError as exc:
            assert (
                "metadata" in str(exc).lower()
                or "prohibited" in str(exc).lower()
                or "link-local" in str(exc).lower()
            )

    # Also test via API endpoint
    with patch(
        "app.core.url_fetcher.fetch_url_safely",
        side_effect=SSRFValidationError("Requests to cloud metadata endpoints are prohibited."),
    ):
        resp = client.post("/convert/url", json={"url": "http://example.com/redirect-to-metadata"})
        assert resp.status_code == 400
        assert "metadata" in resp.text.lower() or "prohibited" in resp.text.lower()
    print("[OK] test_redirect_to_169_254_169_254 passed")


def test_slow_server_timeout():
    """Verify that connect or read timeout raises UrlFetchTimeoutError and HTTP 504."""
    with (
        patch("socket.getaddrinfo") as mock_dns,
        patch.object(
            urllib3.HTTPConnectionPool,
            "request",
            side_effect=urllib3.exceptions.ReadTimeoutError(
                MagicMock(), "http://example.com", "Read timed out"
            ),
        ),
    ):
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        try:
            fetch_url_safely("http://example.com/slow")
            raise AssertionError("Should have raised UrlFetchTimeoutError")
        except UrlFetchTimeoutError as exc:
            assert "timed out" in str(exc).lower()

    # Via API endpoint
    with patch(
        "app.core.url_fetcher.fetch_url_safely",
        side_effect=UrlFetchTimeoutError("Read timed out"),
    ):
        resp = client.post("/convert/url", json={"url": "http://example.com/slow"})
        assert resp.status_code == 504
        assert "timed out" in resp.text.lower()
    print("[OK] test_slow_server_timeout passed")


def test_oversized_body():
    """Verify that Content-Length or streaming over max_size_bytes raises UrlFetchSizeExceededError and HTTP 413."""
    # Test 1: Content-Length header exceeding cap
    mock_resp_cl = MagicMock()
    mock_resp_cl.status = 200
    mock_resp_cl.headers = {"Content-Length": "100000000", "Content-Type": "text/html"}

    with (
        patch("socket.getaddrinfo") as mock_dns,
        patch.object(urllib3.HTTPConnectionPool, "request", return_value=mock_resp_cl),
    ):
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        try:
            fetch_url_safely("http://example.com/huge", max_size_bytes=1000)
            raise AssertionError("Should have raised UrlFetchSizeExceededError")
        except UrlFetchSizeExceededError as exc:
            assert "exceeds maximum limit" in str(exc).lower()

    # Test 2: Streaming chunk exceeding cap
    mock_resp_stream = MagicMock()
    mock_resp_stream.status = 200
    mock_resp_stream.headers = {"Content-Type": "text/html"}
    mock_resp_stream.stream.return_value = [b"a" * 500, b"b" * 600]

    with (
        patch("socket.getaddrinfo") as mock_dns,
        patch.object(urllib3.HTTPConnectionPool, "request", return_value=mock_resp_stream),
    ):
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        try:
            fetch_url_safely("http://example.com/huge-stream", max_size_bytes=1000)
            raise AssertionError("Should have raised UrlFetchSizeExceededError")
        except UrlFetchSizeExceededError as exc:
            assert "exceeded maximum limit" in str(exc).lower()

    # Via API endpoint
    with patch(
        "app.core.url_fetcher.fetch_url_safely",
        side_effect=UrlFetchSizeExceededError("Content too large"),
    ):
        resp = client.post("/convert/url", json={"url": "http://example.com/huge"})
        assert resp.status_code == 413
        assert "too large" in resp.text.lower()
    print("[OK] test_oversized_body passed")


def test_ip_pinning():
    """Verify that HTTPConnectionPool is created with host set to pinned_ip and Host header preserved."""
    created_pools = []
    original_pool_init = urllib3.HTTPConnectionPool.__init__

    def mock_pool_init(self, *args, **kwargs):
        created_pools.append((self, args, kwargs))
        original_pool_init(self, *args, **kwargs)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.stream.return_value = [b"# Hello"]

    with (
        patch("socket.getaddrinfo") as mock_dns,
        patch.object(urllib3.HTTPConnectionPool, "__init__", mock_pool_init),
        patch.object(urllib3.HTTPConnectionPool, "request", return_value=mock_resp),
    ):
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        result = fetch_url_safely("http://example.com/index.html")
        assert result.content_type == "text/html"
        assert len(created_pools) >= 1
        _pool_self, _pool_args, pool_kwargs = created_pools[0]
        assert pool_kwargs.get("host") == "93.184.216.34", (
            f"Expected pinned_ip 93.184.216.34, got {pool_kwargs.get('host')}"
        )
        assert pool_kwargs.get("port") == 80
    print("[OK] test_ip_pinning passed")


def test_hostname_resolves_to_private_ip():
    """Verify that a hostname resolving to RFC1918 private IP is caught and rejected."""
    with patch("socket.getaddrinfo") as mock_dns:
        mock_dns.return_value = [(2, 1, 6, "", ("10.0.0.1", 80))]
        try:
            fetch_url_safely("http://internal-portal.corp/secret")
            raise AssertionError("Should have raised SSRFValidationError")
        except SSRFValidationError as exc:
            assert "private" in str(exc).lower() or "prohibited" in str(exc).lower()

    # Via API endpoint
    with patch(
        "app.core.url_fetcher.fetch_url_safely",
        side_effect=SSRFValidationError("Host 'internal-portal.corp' resolves to prohibited private network address (10.0.0.1)."),
    ):
        resp = client.post("/convert/url", json={"url": "http://internal-portal.corp/secret"})
        assert resp.status_code == 400
        assert "prohibited" in resp.text.lower() or "private" in resp.text.lower()
    print("[OK] test_hostname_resolves_to_private_ip passed")


def test_too_many_redirects():
    """Verify that redirect chains exceeding hop limit raise SSRFValidationError and HTTP 400."""
    mock_resp = MagicMock()
    mock_resp.status = 302
    mock_resp.headers = {"Location": "http://example.com/loop"}

    with (
        patch("socket.getaddrinfo") as mock_dns,
        patch.object(urllib3.HTTPConnectionPool, "request", return_value=mock_resp),
    ):
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        try:
            fetch_url_safely("http://example.com/loop", max_redirects=3)
            raise AssertionError("Should have raised SSRFValidationError")
        except SSRFValidationError as exc:
            assert "too many redirects" in str(exc).lower()

    # Via API endpoint
    with patch(
        "app.core.url_fetcher.fetch_url_safely",
        side_effect=SSRFValidationError("Too many redirects: exceeded maximum hop limit of 5."),
    ):
        resp = client.post("/convert/url", json={"url": "http://example.com/loop"})
        assert resp.status_code == 400
        assert "too many redirects" in resp.text.lower()
    print("[OK] test_too_many_redirects passed")


def test_filename_and_extension_hints_preserved():
    """Verify Content-Disposition filename and inferred extension are captured."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.headers = {
        "Content-Type": "text/markdown; charset=utf-8",
        "Content-Disposition": 'attachment; filename="annual_report_2026.md"',
    }
    mock_resp.stream.return_value = [b"# Annual Report 2026\n\nAll metrics green."]

    with (
        patch("socket.getaddrinfo") as mock_dns,
        patch.object(urllib3.HTTPConnectionPool, "request", return_value=mock_resp),
    ):
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        result = fetch_url_safely("http://example.com/download?id=99", to_temp_file=True)
        assert result.filename == "annual_report_2026.md"
        assert result.content_type == "text/markdown"
        assert result.charset == "utf-8"
        assert result.temp_file_path is not None
        assert result.temp_file_path.suffix == ".md"
        assert result.temp_file_path.read_text(encoding="utf-8") == "# Annual Report 2026\n\nAll metrics green."
        result.temp_file_path.unlink()
    print("[OK] test_filename_and_extension_hints_preserved passed")


def test_https_local_server_with_test_ca_cert_verification():
    """Prove with a local HTTPS test server and a test CA that certificate verification
    against the hostname still works with the pinned IP (SNI + hostname cert check)."""
    import datetime
    import http.server
    import ssl
    import tempfile
    import threading

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID


    # 1. Generate Test CA
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "InkDoc Test CA")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    # 2. Generate Server Certificate for test.inkdoc.local
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test.inkdoc.local")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("test.inkdoc.local")]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )



    with tempfile.TemporaryDirectory() as td:
        ca_path = Path(td) / "ca.crt"
        server_crt_path = Path(td) / "server.crt"
        server_key_path = Path(td) / "server.key"

        ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        server_crt_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
        server_key_path.write_bytes(
            server_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )

        class HTTPSHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Verified HTTPS response via pinned IP!")

            def log_message(self, *args):
                pass

        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(certfile=str(server_crt_path), keyfile=str(server_key_path))

        server = http.server.HTTPServer(("127.0.0.1", 0), HTTPSHandler)
        server.socket = ssl_ctx.wrap_socket(server.socket, server_side=True)
        port = server.server_address[1]

        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        try:
            # Case 1: Pinned IP to 127.0.0.1 with hostname test.inkdoc.local and trusted CA -> SUCCEEDS!
            with patch("socket.getaddrinfo") as mock_dns:
                mock_dns.return_value = [(2, 1, 6, "", ("127.0.0.1", port))]
                res = fetch_url_safely(
                    f"https://test.inkdoc.local:{port}/hello",
                    ca_certs=str(ca_path),
                    allow_local=True,
                    to_temp_file=False,
                )
                assert res.content_bytes == b"Verified HTTPS response via pinned IP!"
                assert res.content_type == "text/plain"

            # Case 2: Hostname mismatch (mismatch.inkdoc.local) against same server -> MUST FAIL verification!
            with patch("socket.getaddrinfo") as mock_dns:
                mock_dns.return_value = [(2, 1, 6, "", ("127.0.0.1", port))]
                try:
                    fetch_url_safely(
                        f"https://mismatch.inkdoc.local:{port}/hello",
                        ca_certs=str(ca_path),
                        allow_local=True,
                        to_temp_file=False,
                    )
                    raise AssertionError("Should have failed TLS certificate verification due to hostname mismatch")
                except UrlFetchError as exc:
                    assert "certificate" in str(exc).lower() or "failed to connect" in str(exc).lower() or "ssl" in str(exc).lower()

            # Case 3: Untrusted CA (system certs only) -> MUST FAIL verification!
            with patch("socket.getaddrinfo") as mock_dns:
                mock_dns.return_value = [(2, 1, 6, "", ("127.0.0.1", port))]
                try:
                    fetch_url_safely(
                        f"https://test.inkdoc.local:{port}/hello",
                        ca_certs=None,
                        allow_local=True,
                        to_temp_file=False,
                    )
                    raise AssertionError("Should have failed TLS certificate verification due to untrusted CA")
                except UrlFetchError as exc:
                    assert "certificate" in str(exc).lower() or "failed to connect" in str(exc).lower() or "ssl" in str(exc).lower()
        finally:
            server.shutdown()

    print("[OK] test_https_local_server_with_test_ca_cert_verification passed")


# ─── Sites that refuse the request (401, 403, 429, bot checks) ───────────────

def _http_error(status: int, headers: dict | None = None):
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    return resp


def _fetch_with(resp):
    """fetch_url_safely against a mocked reply; (the error raised, how many requests were made)."""
    with (
        patch("socket.getaddrinfo") as mock_dns,
        patch.object(urllib3.HTTPSConnectionPool, "request", return_value=resp) as request,
    ):
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        try:
            fetch_url_safely("https://jobs.example.com/job/123")
        except UrlFetchError as exc:
            return exc, request.call_count
    raise AssertionError("an HTTP error was not raised")


def test_refusals_are_told_apart_from_other_http_errors():
    from app.core.url_fetcher import UrlBlockedError

    cases = [
        (403, {"Server": "cloudflare", "Cf-Mitigated": "challenge"}, True),
        (503, {"cf-mitigated": "challenge"}, True),   # a bot check, whatever the status
        (403, {}, False),
        (401, {}, False),
        (429, {"Retry-After": "120"}, False),
    ]
    for status, headers, challenge in cases:
        exc, requests = _fetch_with(_http_error(status, headers))
        assert isinstance(exc, UrlBlockedError), (status, headers)
        assert isinstance(exc, UrlFetchError)          # every existing handler still catches it
        assert exc.status == status and exc.challenge is challenge
        # The same text as before, for anyone who reads str(exc).
        assert str(exc) == f"HTTP error {status} fetching 'https://jobs.example.com/job/123'."
        assert requests == 1, "a refused request was retried"

    for status in (400, 404, 410, 500, 502):
        exc, requests = _fetch_with(_http_error(status))
        assert type(exc) is UrlFetchError, status
        assert str(exc) == f"HTTP error {status} fetching 'https://jobs.example.com/job/123'."
        assert requests == 1
    print("[OK] test_refusals_are_told_apart_from_other_http_errors passed")


def test_a_bot_check_from_a_real_http_server():
    """urllib3's own header object (any case), through a real local server; one request only."""
    import http.server
    import threading

    from app.core.url_fetcher import UrlBlockedError

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.headers.get("User-Agent"))
            body = b"<title>Just a moment...</title>"
            self.send_response(403)
            self.send_header("CF-MITIGATED", "challenge")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        try:
            fetch_url_safely(f"http://127.0.0.1:{server.server_port}/job", allow_local=True)
            raise AssertionError("a bot check was not reported")
        except UrlBlockedError as exc:
            assert exc.challenge is True and exc.status == 403
    finally:
        server.shutdown()
        server.server_close()
    assert len(hits) == 1, "the request was retried"
    assert "InkDoc" in hits[0], "the request was disguised"
    print("[OK] test_a_bot_check_from_a_real_http_server passed")


def test_url_route_says_why_a_site_refused_and_what_to_do():
    import re

    from app.core.jobs import JOBS
    from app.core.url_fetcher import UrlBlockedError
    from app.server.server import SAVE_PAGE_IN_BROWSER

    url = "https://www.example-jobs.com/job/123"
    cases = [
        (UrlBlockedError(403, url, challenge=True), "This site blocks automated downloads", True),
        (UrlBlockedError(403, url), "The site refused access to this page (HTTP 403)", True),
        (UrlBlockedError(401, url), "This page needs you to sign in (HTTP 401)", True),
        (UrlBlockedError(429, url), "The site is limiting requests (HTTP 429)", False),
        (UrlFetchError(f"HTTP error 404 fetching '{url}'."), f"HTTP error 404 fetching '{url}'.", False),
    ]
    # The page shows any error containing these words as "this engine does not support the
    # format" (classifyError in app.js); a refusal must read as an ordinary failure.
    app_js = (REPO_ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")
    body = app_js[app_js.index("function classifyError(message)"):]
    body = body[:body.index("return \"failed\"")]
    unsupported_words = re.findall(r'msg\.includes\("([^"]+)"\)', body)
    assert unsupported_words, "classifyError's words were not found"

    for i, (error, expected, browser_hint) in enumerate(cases):
        job_id = f"blocked-url-{i:04d}"
        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
            patch("app.core.url_fetcher.fetch_url_safely", side_effect=error),
        ):
            resp = client.post("/convert/url?response_format=json", json={"url": url, "job_id": job_id})
        assert resp.status_code == 502, resp.text          # as before
        detail = resp.json()["detail"]
        assert detail.startswith("URL fetch failed: ")      # as before
        assert expected in detail, detail
        assert (SAVE_PAGE_IN_BROWSER in detail) is browser_hint
        assert not any(w in detail.lower() for w in unsupported_words), detail
        assert JOBS.get(job_id)["status"] == "error"
    print("[OK] test_url_route_says_why_a_site_refused_and_what_to_do passed")


if __name__ == "__main__":
    test_redirect_to_127_0_0_1()
    test_redirect_to_169_254_169_254()
    test_hostname_resolves_to_private_ip()
    test_slow_server_timeout()
    test_oversized_body()
    test_too_many_redirects()
    test_ip_pinning()
    test_filename_and_extension_hints_preserved()
    test_https_local_server_with_test_ca_cert_verification()
    test_refusals_are_told_apart_from_other_http_errors()
    test_a_bot_check_from_a_real_http_server()
    test_url_route_says_why_a_site_refused_and_what_to_do()
    print("\nALL URL FETCHER TESTS PASSED!")

