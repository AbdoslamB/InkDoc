"""Cryptographic verification and envelope authentication for InkDoc updates.

Enforces:
- Ed25519 signature verification over exact received base64 payload bytes BEFORE parsing
- Rejection of downgrade or replayed versions (version must be strictly > installed)
- Validation of download URLs under official repository release path
- Support for primary and backup public keys
"""
from __future__ import annotations

import base64
import json
import re
import sys
from typing import Any
from urllib.parse import urlparse


def _get_crypto_primitives() -> tuple[Any, Any]:
    """Lazily import cryptography primitives.

    Raises UpdateVerificationError if cryptography is not installed.
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric import ed25519

        return InvalidSignature, ed25519
    except ImportError as exc:
        raise UpdateVerificationError(
            "Update verification is unavailable: 'cryptography' is not installed. "
            "Please install cryptography>=42.0.0."
        ) from exc


# Embedded official Ed25519 public keys (Requirement 1 & 3).
#
# A manifest is accepted if its single signature verifies against EITHER key, so a
# build can only be updated by a manifest signed with one of the keys listed here.
# Replacing both values therefore cuts every previously released build off from
# in-app updates — see docs/RELEASE_RUNBOOK.md before changing them.
#
# Rotated 2026-09-20: the previous values were superseded because no corresponding
# private key was held, which made it impossible to sign any release manifest and
# left builds trusting keys of unverified provenance. Both keys below were generated
# offline with scripts/generate_signing_key.py and their private halves are held
# encrypted off-machine.
PRIMARY_PUBLIC_KEY_HEX = "89bb0f8cd25c437a9c4a255f4de87f1e5434db8af45ad68c5ac235605a3bf2e8"
BACKUP_PUBLIC_KEY_HEX = "dbf402f479a61644ca107597d7964800290f7512684a7e980a941f0f3f66c480"

# Official GitHub Releases URL prefix (case-insensitive comparison)
OFFICIAL_REPO_PATH_PREFIX = "/abdoslamb/inkdoc/releases/download/"


class UpdateVerificationError(Exception):
    """Raised when an update envelope fails signature, schema, or integrity validation."""


class DowngradeError(UpdateVerificationError):
    """Raised when the remote version is older than or equal to the installed version."""


def parse_semver(v: str) -> tuple[int, int, int, str]:
    """Parse semver string into (major, minor, patch, prerelease)."""
    cleaned = v.strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+](.+))?$", cleaned)
    if not m:
        # Fallback for dev tags like 0.0.0+dev
        parts = re.split(r"[-+.]", cleaned)
        nums = []
        for p in parts:
            if p.isdigit():
                nums.append(int(p))
            else:
                break
        while len(nums) < 3:
            nums.append(0)
        return (nums[0], nums[1], nums[2], cleaned)

    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    prerelease = m.group(4) or ""
    return (major, minor, patch, prerelease)


def is_version_newer(remote_ver: str, installed_ver: str) -> bool:
    """Return True if remote_ver is strictly newer than installed_ver, False otherwise.

    Rejects downgrades and identical versions.
    """
    rem_maj, rem_min, rem_pat, rem_pre = parse_semver(remote_ver)
    inst_maj, inst_min, inst_pat, inst_pre = parse_semver(installed_ver)

    if (rem_maj, rem_min, rem_pat) > (inst_maj, inst_min, inst_pat):
        return True
    if (rem_maj, rem_min, rem_pat) < (inst_maj, inst_min, inst_pat):
        return False

    # Same major.minor.patch: a final release is newer than a prerelease
    if inst_pre and not rem_pre:
        return True
    if not inst_pre and rem_pre:
        return False
    if rem_pre and inst_pre:
        return rem_pre > inst_pre

    return False


def get_official_public_keys() -> list[Any]:
    """Return the embedded primary and backup Ed25519 public keys."""
    _, ed25519_mod = _get_crypto_primitives()
    keys: list[Any] = []
    for h in (PRIMARY_PUBLIC_KEY_HEX, BACKUP_PUBLIC_KEY_HEX):
        try:
            raw = bytes.fromhex(h)
            keys.append(ed25519_mod.Ed25519PublicKey.from_public_bytes(raw))
        except Exception:
            pass
    return keys


def verify_envelope_bytes(
    envelope_raw: bytes,
    installed_version: str | None = None,
    trusted_public_keys: list[Any] | None = None,
) -> dict[str, Any]:
    """Verify single-file envelope bytes and return parsed manifest payload.

    Security guarantees:
    1. Signature is verified over the exact received base64 payload bytes BEFORE decoding or parsing.
    2. In frozen releases, only official embedded public keys are accepted (Requirement H).
    3. Rejects any version <= installed_version.
    4. Validates asset URLs sit under the official repository release path (Requirement C).
    """
    invalid_sig_cls, _ = _get_crypto_primitives()

    try:
        envelope = json.loads(envelope_raw.decode("utf-8"))
    except Exception as err:
        raise UpdateVerificationError("Invalid JSON envelope format.") from err

    if not isinstance(envelope, dict):
        raise UpdateVerificationError("Update envelope must be a JSON object.")

    payload_b64 = envelope.get("payload")
    sig_b64 = envelope.get("signature")

    if not isinstance(payload_b64, str) or not isinstance(sig_b64, str):
        raise UpdateVerificationError("Envelope missing required 'payload' or 'signature' strings.")

    try:
        sig_bytes = base64.b64decode(sig_b64)
    except Exception as err:
        raise UpdateVerificationError("Malformed base64 signature in envelope.") from err

    # Exact bytes to verify: the raw UTF-8 bytes of the payload string
    payload_exact_bytes = payload_b64.encode("utf-8")

    # In frozen builds, override any passed keys to strictly use embedded keys (Requirement H)
    is_frozen = getattr(sys, "frozen", False)
    if is_frozen or trusted_public_keys is None:
        keys_to_try = get_official_public_keys()
    else:
        keys_to_try = trusted_public_keys

    valid_signature = False
    for pk in keys_to_try:
        try:
            pk.verify(sig_bytes, payload_exact_bytes)
            valid_signature = True
            break
        except invalid_sig_cls:
            continue
        except Exception:
            continue

    if not valid_signature:
        raise UpdateVerificationError(
            "Cryptographic signature verification failed. The update manifest is invalid or untrusted."
        )

    # Decode and parse payload ONLY after signature verification succeeds
    try:
        manifest_json_bytes = base64.b64decode(payload_b64)
        manifest = json.loads(manifest_json_bytes.decode("utf-8"))
    except Exception as err:
        raise UpdateVerificationError("Failed to decode or parse manifest payload.") from err

    if not isinstance(manifest, dict):
        raise UpdateVerificationError("Manifest payload must be a JSON object.")

    # Validate version & anti-downgrade
    remote_version = manifest.get("version")
    if not remote_version or not isinstance(remote_version, str):
        raise UpdateVerificationError("Manifest missing required 'version' string.")

    if installed_version and not is_version_newer(remote_version, installed_version):
        raise DowngradeError(
            f"Refusing update: remote version '{remote_version}' is not newer than installed version '{installed_version}'."
        )

    # Validate issued_at timestamp
    if not manifest.get("issued_at"):
        raise UpdateVerificationError("Manifest missing 'issued_at' timestamp.")

    # Validate assets dictionary
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        raise UpdateVerificationError("Manifest missing 'assets' object.")

    expected_release_prefix = f"{OFFICIAL_REPO_PATH_PREFIX}v{remote_version}/".lower()

    for asset_key, asset_info in assets.items():
        if not isinstance(asset_info, dict):
            raise UpdateVerificationError(f"Asset '{asset_key}' metadata must be an object.")

        url = asset_info.get("url")
        sha256 = asset_info.get("sha256")
        size = asset_info.get("size_bytes")

        if not url or not isinstance(url, str):
            raise UpdateVerificationError(f"Asset '{asset_key}' missing valid URL.")
        if not sha256 or len(sha256) != 64:
            raise UpdateVerificationError(f"Asset '{asset_key}' has invalid SHA-256 hash.")
        if not isinstance(size, int) or size <= 0:
            raise UpdateVerificationError(f"Asset '{asset_key}' has invalid size_bytes.")

        # Validate URL sits under official repo path (Requirement C)
        parsed = urlparse(url)
        path_lower = parsed.path.lower()
        if not path_lower.startswith(expected_release_prefix):
            raise UpdateVerificationError(
                f"Asset URL path '{parsed.path}' does not sit under official repository prefix '{expected_release_prefix}'."
            )

    return manifest
