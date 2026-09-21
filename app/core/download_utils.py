"""Shared network download and safe archive extraction utilities for InkDoc.

Enforces strict security controls:
- HTTPS only with an explicit CDN allowlist
- Custom redirect handler validating every single redirect hop
- HTTP Range-based download resume
- Pre-download disk space verification
- Maximum download size cap
- Safe archive extraction (path traversal, zip/tar bombs, symlink blocking, mode masking)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import IO, Any

# Strictly allowlisted download CDN hosts (Requirement C)
ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})

# Windows reserved device names that cannot be created as files
_WINDOWS_RESERVED_NAMES = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$",
    re.IGNORECASE,
)

# Safe extraction limits
DEFAULT_MAX_FILE_COUNT = 50000
DEFAULT_MAX_UNCOMPRESSED_BYTES = 3500 * 1024 * 1024  # 3.5 GB
DEFAULT_MAX_COMPRESSION_RATIO = 15.0


class SecurityError(Exception):
    """Raised when a cryptographic or network security rule is violated."""


class DownloadError(Exception):
    """Raised when an HTTP transfer or staging operation fails."""


def validate_download_url(url: str, allowed_hosts: frozenset[str] = ALLOWED_DOWNLOAD_HOSTS) -> None:
    """Validate that URL uses HTTPS and resolves to an allowlisted CDN host."""
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()

    if scheme != "https":
        # Allow http only for localhost/127.0.0.1 in testing when explicitly included in allowed_hosts
        if scheme == "http" and host in ("127.0.0.1", "localhost") and host in allowed_hosts:
            pass
        else:
            raise SecurityError(f"Insecure protocol '{parsed.scheme}'. Only HTTPS downloads are permitted.")

    if host not in allowed_hosts:
        raise SecurityError(
            f"Host '{host}' is not in the trusted CDN allowlist: {sorted(allowed_hosts)}"
        )


class StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Custom redirect handler validating EVERY hop against the strict allowlist."""

    def __init__(self, allowed_hosts: frozenset[str] = ALLOWED_DOWNLOAD_HOSTS) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes] | None,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        validate_download_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_disk_space(
    target_dir: Path,
    required_bytes: int,
    multiplier: float = 1.0,
    uncompressed_bytes: int = 0,
    safety_margin_bytes: int = 200 * 1024 * 1024,
) -> None:
    """Verify combined disk space: pending download bytes + uncompressed extraction workspace."""
    check_path = target_dir if target_dir.is_dir() else target_dir.parent
    check_path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(check_path)

    if uncompressed_bytes > 0:
        extraction_workspace = int(uncompressed_bytes * 1.5)
        needed = int(required_bytes * multiplier) + extraction_workspace + safety_margin_bytes
    else:
        effective_multiplier = multiplier if multiplier > 1.0 else 2.5
        needed = int(required_bytes * effective_multiplier)

    needed = max(needed, safety_margin_bytes)
    if usage.free < needed:
        free_mb = usage.free // (1024 * 1024)
        needed_mb = needed // (1024 * 1024)
        raise DownloadError(
            f"Insufficient disk space in {check_path}. Free: {free_mb} MB, Required: {needed_mb} MB."
        )


def is_safe_path(base_dir: Path, target_path: Path) -> bool:
    """Verify that target_path resolves strictly inside base_dir."""
    try:
        base_resolved = base_dir.resolve()
        target_resolved = target_path.resolve()
        return target_resolved.is_relative_to(base_resolved)
    except Exception:
        return False


def stream_download(
    url: str,
    destination_path: Path,
    expected_size: int | None = None,
    uncompressed_size: int = 0,
    max_size_cap: int = 2000 * 1024 * 1024,  # 2 GB cap
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    user_agent: str = "InkDoc-Downloader",
    support_resume: bool = True,
    allowed_hosts: frozenset[str] = ALLOWED_DOWNLOAD_HOSTS,
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    stale_part_age_seconds: float = 86400.0,
) -> str:
    """Stream download a file with range-resume, If-Range, retry policies, and integrity checks.

    Returns the computed SHA-256 hex digest of the completed file.
    """
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination_path.with_suffix(destination_path.suffix + ".part")
    meta_path = destination_path.with_suffix(destination_path.suffix + ".part.meta")

    # 1. Stale .part cleanup, strictly limited to this download's own partial file.
    #
    # There was previously a second pass that globbed the destination directory for
    # "*.part" and deleted every match older than stale_part_age_seconds. For an
    # update with install_method "download_reveal" the destination directory is the
    # user's Downloads folder, and ".part" is the extension Firefox gives its own
    # in-progress downloads -- so InkDoc silently destroyed unrelated user data.
    #
    # The sweep also bought nothing. Every caller writes to a fixed filename
    # (inkdoc-setup.exe, inkdoc-windows.zip, docling.download.tar.gz), so an
    # abandoned partial is always reclaimed by the single-file check below the next
    # time that same asset is fetched. Never delete a file this function did not create.
    if part_path.is_file():
        try:
            age = time.time() - part_path.stat().st_mtime
            if age > stale_part_age_seconds:
                part_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
        except OSError:
            pass

    existing_bytes = 0
    cached_etag: str | None = None
    cached_last_modified: str | None = None

    if support_resume and part_path.is_file():
        existing_bytes = part_path.stat().st_size
        if meta_path.is_file():
            try:
                meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
                cached_etag = meta_data.get("etag")
                cached_last_modified = meta_data.get("last_modified")
            except Exception:
                pass

    # 2. Combined disk space check
    if expected_size is not None:
        check_disk_space(
            destination_path.parent,
            required_bytes=max(0, expected_size - existing_bytes),
            uncompressed_bytes=uncompressed_size,
        )

    # 3. Retry loop with strict redirect re-validation, 4xx non-retry, and If-Range
    resp = None
    for attempt in range(max_retries + 1):
        if cancel_event and cancel_event.is_set():
            raise DownloadError("Download cancelled by user.")

        # Re-validate URL on every attempt/retry
        validate_download_url(url, allowed_hosts)

        req_headers = {
            "User-Agent": user_agent,
            "Accept": "application/octet-stream, application/zip, application/gzip, */*",
        }
        if existing_bytes > 0:
            req_headers["Range"] = f"bytes={existing_bytes}-"
            if cached_etag:
                req_headers["If-Range"] = cached_etag
            elif cached_last_modified:
                req_headers["If-Range"] = cached_last_modified

        req = urllib.request.Request(url, headers=req_headers)
        opener = urllib.request.build_opener(StrictRedirectHandler(allowed_hosts))

        try:
            resp = opener.open(req, timeout=30.0)
        except urllib.error.HTTPError as err:
            # HTTP 429 Too Many Requests: inspect Retry-After
            if err.code == 429:
                if attempt >= max_retries:
                    raise DownloadError(f"HTTP 429 Too Many Requests: retry limit ({max_retries}) exceeded.") from err
                retry_after_str = err.headers.get("Retry-After")
                wait_secs = backoff_factor * (2 ** attempt)
                if retry_after_str:
                    if retry_after_str.strip().isdigit():
                        wait_secs = float(retry_after_str.strip())
                    else:
                        try:
                            dt = parsedate_to_datetime(retry_after_str)
                            wait_secs = max(0.5, dt.timestamp() - time.time())
                        except Exception:
                            pass
                wait_secs = min(max(wait_secs, 0.5), 60.0)
                time.sleep(wait_secs)
                continue

            # HTTP 416 Range Not Satisfiable: file changed or range invalid, restart from 0
            if err.code == 416 and existing_bytes > 0:
                existing_bytes = 0
                part_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                cached_etag = None
                cached_last_modified = None
                continue

            # 5xx Server Errors: retry with exponential backoff
            if 500 <= err.code < 600:
                if attempt >= max_retries:
                    raise DownloadError(f"HTTP error {err.code} after {max_retries} retries.") from err
                time.sleep(backoff_factor * (2 ** attempt))
                continue

            # 4xx Client Errors: fail immediately, do NOT retry
            if 400 <= err.code < 500:
                raise DownloadError(f"HTTP {err.code} {err.reason} downloading '{url}'. Client errors are not retried.") from err

            raise DownloadError(f"HTTP error {err.code} downloading '{url}'.") from err

        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as net_err:
            if attempt >= max_retries:
                raise DownloadError(f"Network error after {max_retries} retries: {net_err}") from net_err
            time.sleep(backoff_factor * (2 ** attempt))
            continue

        # Check whether server accepted partial content
        is_partial = (getattr(resp, "status", None) == 206) or (getattr(resp, "code", None) == 206)
        if not is_partial and existing_bytes > 0:
            existing_bytes = 0
            part_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)

        resp_etag = resp.headers.get("ETag")
        resp_last_mod = resp.headers.get("Last-Modified")
        if resp_etag or resp_last_mod:
            try:
                meta_path.write_text(
                    json.dumps({"etag": resp_etag, "last_modified": resp_last_mod}),
                    encoding="utf-8",
                )
            except OSError:
                pass

        total_bytes = 0
        content_length = resp.headers.get("Content-Length")
        if content_length:
            total_bytes = int(content_length) + (existing_bytes if is_partial else 0)
        elif expected_size:
            total_bytes = expected_size

        mode = "ab" if (is_partial and existing_bytes > 0) else "wb"
        downloaded = existing_bytes

        chunk_size = 65536
        stream_interrupted = False
        try:
            with open(part_path, mode) as out_f:
                while True:
                    if cancel_event and cancel_event.is_set():
                        raise DownloadError("Download cancelled by user.")

                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break

                    out_f.write(chunk)
                    downloaded += len(chunk)

                    if downloaded > max_size_cap:
                        part_path.unlink(missing_ok=True)
                        meta_path.unlink(missing_ok=True)
                        raise SecurityError(
                            f"Download exceeded maximum size cap of {max_size_cap} bytes."
                        )

                    if progress_callback:
                        progress_callback(downloaded, total_bytes or downloaded)
        except (TimeoutError, ConnectionError, OSError) as read_err:
            if cancel_event and cancel_event.is_set():
                raise DownloadError("Download cancelled by user.") from read_err
            if attempt >= max_retries:
                raise DownloadError(f"Stream interrupted after {max_retries} retries: {read_err}") from read_err
            stream_interrupted = True
            if part_path.is_file():
                existing_bytes = part_path.stat().st_size
            time.sleep(backoff_factor * (2 ** attempt))
            continue

        if not stream_interrupted:
            break

    # 4. Size check BEFORE hash computation (Fail fast)
    if not part_path.is_file():
        raise DownloadError("Download failed: incomplete or missing payload.")

    actual_size = part_path.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        part_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise SecurityError(
            f"Integrity check failed: downloaded size mismatch (expected {expected_size} bytes, got {actual_size} bytes)."
        )

    # 5. Checksum verification
    hasher = hashlib.sha256()
    with open(part_path, "rb") as pf:
        while chunk := pf.read(65536):
            hasher.update(chunk)

    # 6. Atomic move to destination
    destination_path.unlink(missing_ok=True)
    part_path.replace(destination_path)
    meta_path.unlink(missing_ok=True)

    return hasher.hexdigest().lower()


def extract_archive_safely(
    archive_path: Path,
    staging_dir: Path,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_file_count: int = DEFAULT_MAX_FILE_COUNT,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
) -> None:
    """Safely extract a .zip, .tar.gz, or .tgz archive with strict security checks."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    archive_str = str(archive_path).lower()

    if archive_str.endswith(".zip"):
        _extract_zip(
            archive_path,
            staging_dir,
            max_uncompressed_bytes,
            max_file_count,
            max_compression_ratio,
        )
    elif archive_str.endswith((".tar.gz", ".tgz")):
        _extract_tar(
            archive_path,
            staging_dir,
            max_uncompressed_bytes,
            max_file_count,
            max_compression_ratio,
        )
    else:
        raise SecurityError(f"Unsupported archive format for extraction: '{archive_path.name}'")


def _extract_zip(
    archive_path: Path,
    staging_dir: Path,
    max_uncompressed_bytes: int,
    max_file_count: int,
    max_compression_ratio: float,
) -> None:
    total_uncompressed = 0
    file_count = 0
    archive_size = max(archive_path.stat().st_size, 1)

    with zipfile.ZipFile(archive_path, "r") as zf:
        infolist = zf.infolist()
        if len(infolist) > max_file_count:
            raise SecurityError(f"Archive exceeds maximum file count limit ({max_file_count}).")

        for member in infolist:
            norm_name = member.filename.replace("\\", "/")
            p = Path(norm_name)

            if p.is_absolute() or ".." in p.parts or p.drive:
                raise SecurityError(f"Illegal path traversal in archive: '{member.filename}'")

            for part in p.parts:
                if _WINDOWS_RESERVED_NAMES.match(part) or ":" in part:
                    raise SecurityError(f"Illegal Windows reserved filename in archive: '{member.filename}'")

            # Check mode from external_attr (upper 16 bits on POSIX)
            raw_mode = member.external_attr >> 16
            if stat.S_ISLNK(raw_mode) or stat.S_ISCHR(raw_mode) or stat.S_ISBLK(raw_mode) or stat.S_ISFIFO(raw_mode):
                raise SecurityError(f"Archive contains symlink or special file: '{member.filename}'")

            total_uncompressed += member.file_size
            file_count += 1

            if total_uncompressed > max_uncompressed_bytes:
                raise SecurityError(f"Archive uncompressed size exceeds limit ({max_uncompressed_bytes} bytes).")

            if (total_uncompressed / archive_size) > max_compression_ratio and total_uncompressed > 50 * 1024 * 1024:
                raise SecurityError("Archive compression ratio exceeds safe limit (possible decompression bomb).")

            dest = staging_dir / p
            if not is_safe_path(staging_dir, dest):
                raise SecurityError(f"Zip slip escape detected for: '{member.filename}'")

            if member.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                # Apply safe POSIX mode mask: strip setuid (0o4000) and setgid (0o2000)
                if os.name == "posix":
                    is_exec = bool(raw_mode & 0o111)
                    safe_mask = 0o755 if is_exec else 0o644
                    try:
                        dest.chmod(safe_mask)
                    except OSError:
                        pass


def _extract_tar(
    archive_path: Path,
    staging_dir: Path,
    max_uncompressed_bytes: int,
    max_file_count: int,
    max_compression_ratio: float,
) -> None:
    total_uncompressed = 0
    file_count = 0
    archive_size = max(archive_path.stat().st_size, 1)

    with tarfile.open(archive_path, "r:*") as tf:
        for member in tf:
            norm_name = member.name.replace("\\", "/")
            p = Path(norm_name)

            if p.is_absolute() or ".." in p.parts or p.drive:
                raise SecurityError(f"Illegal path traversal in tar: '{member.name}'")

            for part in p.parts:
                if _WINDOWS_RESERVED_NAMES.match(part) or ":" in part:
                    raise SecurityError(f"Illegal reserved filename in tar: '{member.name}'")

            if member.islnk() or member.issym() or member.ischr() or member.isblk() or member.isfifo():
                raise SecurityError(f"Tar contains symlink, hardlink or special file: '{member.name}'")

            total_uncompressed += member.size
            file_count += 1

            if file_count > max_file_count:
                raise SecurityError(f"Tar exceeds maximum file count limit ({max_file_count}).")

            if total_uncompressed > max_uncompressed_bytes:
                raise SecurityError(f"Tar uncompressed size exceeds limit ({max_uncompressed_bytes} bytes).")

            if (total_uncompressed / archive_size) > max_compression_ratio and total_uncompressed > 50 * 1024 * 1024:
                raise SecurityError("Tar compression ratio exceeds safe limit (possible decompression bomb).")

            dest = staging_dir / p
            if not is_safe_path(staging_dir, dest):
                raise SecurityError(f"Tar escape detected for: '{member.name}'")

            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                if os.name == "posix":
                    is_exec = bool(member.mode & 0o111)
                    safe_mask = 0o755 if is_exec else 0o644
                    try:
                        dest.chmod(safe_mask)
                    except OSError:
                        pass
