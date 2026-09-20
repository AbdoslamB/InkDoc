"""Secure single-layer URL fetcher for document conversion.

Enforces:
1. Strict connect (default 5.0s) and read (default 15.0s) timeouts.
2. Streaming total-size cap to prevent memory exhaustion / OOM.
3. Manual redirect following with a strict hop limit (default 5).
4. SSRF validation on EVERY hop against loopback, private RFC1918, link-local,
   reserved, multicast, and cloud metadata IP ranges.
5. IP pinning: connects directly to the validated IP address to eliminate DNS rebinding / TOCTOU.
"""
from __future__ import annotations

import logging
import mimetypes
import re
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import urllib3

from app.core.security import SSRFValidationError, validate_and_pin_url_for_ssrf

logger = logging.getLogger("inkdoc.url_fetcher")

DEFAULT_CONNECT_TIMEOUT: float = 5.0
DEFAULT_READ_TIMEOUT: float = 15.0
DEFAULT_MAX_URL_SIZE_BYTES: int = 50 * 1024 * 1024  # 50 MB
DEFAULT_MAX_REDIRECT_HOPS: int = 5


class UrlFetchError(Exception):
    """Base exception for URL fetch failures."""


class UrlFetchTimeoutError(UrlFetchError):
    """Raised when connecting or reading from remote server times out."""


class UrlFetchSizeExceededError(UrlFetchError):
    """Raised when remote response size exceeds permitted maximum."""


@dataclass
class FetchedUrlResult:
    """Represents a safely fetched remote document."""

    final_url: str
    temp_file_path: Path | None
    content_bytes: bytes | None
    content_type: str
    charset: str | None
    filename: str


def fetch_url_safely(
    url: str,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    max_size_bytes: int = DEFAULT_MAX_URL_SIZE_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECT_HOPS,
    to_temp_file: bool = True,
    ca_certs: str | None = None,
    ssl_context: Any | None = None,
    allow_local: bool = False,
) -> FetchedUrlResult:
    """Safely fetch a remote URL enforcing SSRF validation, IP pinning, timeouts, and size caps.

    Parameters:
        url: Target HTTP or HTTPS URL.
        connect_timeout: Maximum seconds to establish TCP/TLS connection.
        read_timeout: Maximum seconds waiting for server response bytes.
        max_size_bytes: Maximum total bytes permitted for the response body.
        max_redirects: Maximum number of HTTP redirects allowed.
        to_temp_file: Whether to stream body to a temporary file on disk.
        ca_certs: Optional path to custom CA certificate bundle for TLS verification.
        ssl_context: Optional custom SSL context.
        allow_local: Whether to allow local network connections (testing only).

    Returns:
        FetchedUrlResult containing destination path, metadata, and detected content type.

    Raises:
        SSRFValidationError: If any hop attempts to reach an unsafe/private/metadata IP.
        UrlFetchTimeoutError: If connect or read operations exceed timeout limits.
        UrlFetchSizeExceededError: If content length or streamed bytes exceed max_size_bytes.
        UrlFetchError: For other HTTP or network-level connection failures.
    """
    current_url = url
    hop_count = 0

    while True:
        # 1. SSRF validation & IP pinning on EVERY hop
        norm_url, scheme, hostname, port, pinned_ip = validate_and_pin_url_for_ssrf(
            current_url, allow_local=allow_local
        )

        timeout = urllib3.Timeout(connect=connect_timeout, read=read_timeout)
        headers = {
            "Host": hostname,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) InkDoc/1.0",
            "Accept": "text/markdown, text/html, application/pdf, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        parsed = urllib.parse.urlsplit(norm_url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        # 2. Connection pool bound directly to the validated pinned IP
        if scheme == "https":
            pool_kwargs: dict[str, Any] = {
                "host": pinned_ip,
                "port": port,
                "server_hostname": hostname,
                "timeout": timeout,
                "retries": False,
            }
            if ca_certs:
                pool_kwargs["ca_certs"] = ca_certs
            if ssl_context:
                pool_kwargs["ssl_context"] = ssl_context
            pool = urllib3.HTTPSConnectionPool(**pool_kwargs)
        else:
            pool = urllib3.HTTPConnectionPool(
                host=pinned_ip,
                port=port,
                timeout=timeout,
                retries=False,
            )


        try:
            resp = pool.request(
                "GET",
                path,
                headers=headers,
                assert_same_host=False,
                redirect=False,
                preload_content=False,
            )
        except (
            urllib3.exceptions.ConnectTimeoutError,
            urllib3.exceptions.ReadTimeoutError,
            urllib3.exceptions.TimeoutError,
            TimeoutError,
        ) as exc:
            pool.close()
            raise UrlFetchTimeoutError(
                f"Connection or read timed out fetching '{norm_url}': {exc}"
            ) from exc
        except Exception as exc:
            pool.close()
            raise UrlFetchError(f"Failed to connect to host '{hostname}': {exc}") from exc

        # 3. Handle redirects manually with hop limit
        if resp.status in (301, 302, 303, 307, 308):
            resp.release_conn()
            pool.close()

            location = resp.headers.get("Location") or resp.headers.get("location")
            if not location:
                raise UrlFetchError(
                    f"Server returned redirect status {resp.status} with no Location header."
                )

            hop_count += 1
            if hop_count > max_redirects:
                raise SSRFValidationError(
                    f"Too many redirects: exceeded maximum hop limit of {max_redirects}."
                )

            current_url = urllib.parse.urljoin(norm_url, location)
            logger.info("Redirect hop %d -> %s", hop_count, current_url)
            continue

        # 4. Check for HTTP errors
        if resp.status >= 400:
            resp.release_conn()
            pool.close()
            raise UrlFetchError(f"HTTP error {resp.status} fetching '{norm_url}'.")

        # 5. Fast-path check for Content-Length
        content_length_str = resp.headers.get("Content-Length") or resp.headers.get("content-length")
        if content_length_str:
            try:
                cl = int(content_length_str)
                if cl > max_size_bytes:
                    resp.release_conn()
                    pool.close()
                    raise UrlFetchSizeExceededError(
                        f"Remote content size ({cl} bytes) exceeds maximum limit of {max_size_bytes} bytes."
                    )
            except ValueError:
                pass

        # 6. Extract media type, charset, and Content-Disposition filename
        content_type_header = resp.headers.get("Content-Type", "")
        content_type = content_type_header.split(";")[0].strip() or "application/octet-stream"
        charset: str | None = None
        if "charset=" in content_type_header.lower():
            for part in content_type_header.split(";"):
                if "charset=" in part.lower():
                    charset = part.split("=")[-1].strip().strip("\"'")

        cd_header = resp.headers.get("Content-Disposition") or resp.headers.get("content-disposition")
        cd_filename: str | None = None
        if cd_header:
            match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^";\r\n]+)["\']?', cd_header)
            if match:
                cd_filename = urllib.parse.unquote(match.group(1).strip())

        filename = cd_filename or Path(parsed.path).name or "webpage"

        # Determine extension from filename or guess from content-type
        ext = Path(filename).suffix
        if not ext and content_type:
            ext = mimetypes.guess_extension(content_type) or ""

        # 7. Stream content with strict total size cap
        total_downloaded = 0
        chunks: list[bytes] = []
        tmp_file = None
        tmp_path: Path | None = None

        if to_temp_file:
            tmp_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
                prefix="inkdoc_fetch_",
                suffix=ext if ext else ".tmp",
                delete=False,
            )
            tmp_path = Path(tmp_file.name)

        try:
            for chunk in resp.stream(65536):
                total_downloaded += len(chunk)
                if total_downloaded > max_size_bytes:
                    raise UrlFetchSizeExceededError(
                        f"Remote content exceeded maximum limit of {max_size_bytes} bytes."
                    )
                if tmp_file:
                    tmp_file.write(chunk)
                else:
                    chunks.append(chunk)
        except (
            urllib3.exceptions.ReadTimeoutError,
            TimeoutError,
        ) as exc:
            raise UrlFetchTimeoutError(
                f"Read timed out during streaming download of '{norm_url}': {exc}"
            ) from exc
        finally:
            resp.release_conn()
            pool.close()
            if tmp_file:
                tmp_file.flush()
                tmp_file.close()

        content_bytes = b"".join(chunks) if not to_temp_file else None

        return FetchedUrlResult(
            final_url=norm_url,
            temp_file_path=tmp_path,
            content_bytes=content_bytes,
            content_type=content_type,
            charset=charset,
            filename=filename,
        )

