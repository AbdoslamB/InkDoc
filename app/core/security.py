"""Security validation utilities for InkDoc.

Provides SSRF protection, URL validation, and network safety boundaries.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse

# Cloud metadata IP addresses to explicitly block across AWS, Azure, GCP, DigitalOcean, etc.
BLOCKED_METADATA_IPS = {
    "169.254.169.254",  # AWS / Azure / GCP / OpenStack metadata
    "169.254.170.2",    # AWS ECS task metadata
    "100.100.100.200",  # Alibaba Cloud metadata
}


class SSRFValidationError(ValueError):
    """Raised when a URL fails SSRF safety validation."""
    pass


def validate_url_for_ssrf(url: str, allow_http: bool = True) -> str:
    """Validate and normalize a URL to prevent Server-Side Request Forgery (SSRF).

    Enforces:
    1. Scheme restriction (only https:// or optionally http://; blocks file:, data:, gopher:, etc.)
    2. Non-empty hostname check
    3. DNS resolution safety check (resolves host and rejects loopback, link-local,
       private RFC1918/RFC4193, broadcast, multicast, and cloud metadata addresses).

    Returns:
        The validated and normalized URL string.

    Raises:
        SSRFValidationError: If the URL fails any security check.
    """
    cleaned = url.strip()
    if not cleaned:
        raise SSRFValidationError("URL cannot be empty.")

    # Explicit rejection for dangerous non-HTTP schemes (even without '://')
    prohibited_prefixes = ("file:", "data:", "javascript:", "vbscript:", "about:", "blob:", "ftp:", "gopher:", "ldap:")
    cleaned_lower = cleaned.lower()
    for prefix in prohibited_prefixes:
        if cleaned_lower.startswith(prefix):
            raise SSRFValidationError(f"Prohibited scheme '{prefix}'. Only HTTP and HTTPS URLs are permitted.")

    # Auto-prepend https:// if user provided domain without scheme (e.g. 'example.com' or 'example.com/page')
    if "://" not in cleaned:
        if "." in cleaned and " " not in cleaned and not cleaned.startswith("/"):
            cleaned = f"https://{cleaned}"
        else:
            raise SSRFValidationError("Invalid URL format. Please provide a valid HTTP or HTTPS URL.")

    parsed = urllib.parse.urlsplit(cleaned)
    scheme = (parsed.scheme or "").lower()

    allowed_schemes = ("https", "http") if allow_http else ("https",)
    if scheme not in allowed_schemes:
        raise SSRFValidationError(
            f"Prohibited scheme '{scheme}:'. Only {', '.join(s.upper() for s in allowed_schemes)} URLs are permitted."
        )

    hostname = parsed.hostname
    if not hostname:
        raise SSRFValidationError("URL must include a valid hostname.")

    hostname_lower = hostname.lower().strip("[]")

    # Fast-path check for obvious localhost / metadata names
    if hostname_lower in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise SSRFValidationError(f"Requests to local host '{hostname}' are prohibited.")

    if hostname_lower in BLOCKED_METADATA_IPS:
        raise SSRFValidationError("Requests to cloud metadata endpoints are prohibited.")

    # Resolve hostname to IP addresses and check each resolved IP
    try:
        addr_info = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as err:
        raise SSRFValidationError(f"Failed to resolve host '{hostname}': {err}") from err

    if not addr_info:
        raise SSRFValidationError(f"No network addresses resolved for host '{hostname}'.")

    for _family, _, _, _, sockaddr in addr_info:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as err:
            raise SSRFValidationError(f"Invalid IP address resolved ('{ip_str}'): {err}") from err

        # Explicit cloud metadata check
        if str(ip) in BLOCKED_METADATA_IPS:
            raise SSRFValidationError("Requests to cloud metadata endpoints are prohibited.")

        # Check standard unsafe ranges
        if ip.is_loopback:
            raise SSRFValidationError(f"Host '{hostname}' resolves to prohibited loopback address ({ip}).")
        if ip.is_private:
            raise SSRFValidationError(f"Host '{hostname}' resolves to prohibited private network address ({ip}).")
        if ip.is_link_local:
            raise SSRFValidationError(f"Host '{hostname}' resolves to prohibited link-local address ({ip}).")
        if ip.is_reserved:
            raise SSRFValidationError(f"Host '{hostname}' resolves to prohibited reserved address ({ip}).")
        if ip.is_multicast:
            raise SSRFValidationError(f"Host '{hostname}' resolves to prohibited multicast address ({ip}).")
        if ip.is_unspecified:
            raise SSRFValidationError(f"Host '{hostname}' resolves to prohibited unspecified address ({ip}).")

    return cleaned
