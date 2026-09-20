"""Unit and cryptographic integrity tests for InkDoc update envelope verifier."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

# Add project root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cryptography.hazmat.primitives.asymmetric import ed25519

from app.core.update_verifier import (
    DowngradeError,
    UpdateVerificationError,
    is_version_newer,
    parse_semver,
    verify_envelope_bytes,
)


def create_test_manifest_envelope(
    version: str = "1.1.0",
    key: ed25519.Ed25519PrivateKey | None = None,
    tamper_payload: bool = False,
    tamper_sig: bool = False,
    url_override: str | None = None,
) -> tuple[bytes, ed25519.Ed25519PublicKey]:
    signer = key or ed25519.Ed25519PrivateKey.generate()
    public_key = signer.public_key()

    target_url = (
        url_override
        if url_override is not None
        else f"https://github.com/AbdoslamB/InkDoc/releases/download/v{version}/inkdoc-setup.exe"
    )

    manifest = {
        "manifest_version": "1.0.0",
        "version": version,
        "min_supported_version": "1.0.0",
        "issued_at": "2026-09-20T12:00:00Z",
        "release_url": f"https://github.com/AbdoslamB/InkDoc/releases/tag/v{version}",
        "release_notes": "Test release notes",
        "assets": {
            "windows-x86_64-installer": {
                "filename": "inkdoc-setup.exe",
                "url": target_url,
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "size_bytes": 1024,
                "install_method": "inno_silent",
            }
        },
    }

    manifest_bytes = json.dumps(manifest).encode("utf-8")
    payload_str = base64.b64encode(manifest_bytes).decode("ascii")

    sig_bytes = signer.sign(payload_str.encode("utf-8"))
    sig_str = base64.b64encode(sig_bytes).decode("ascii")

    if tamper_payload:
        # Alter one character in the payload
        payload_str = payload_str[:-2] + ("AA" if payload_str[-2:] != "AA" else "BB")

    if tamper_sig:
        # Alter signature
        sig_str = sig_str[:-2] + ("AA" if sig_str[-2:] != "AA" else "BB")

    envelope = {
        "envelope_version": "1.0.0",
        "payload": payload_str,
        "signature": sig_str,
    }

    return json.dumps(envelope).encode("utf-8"), public_key


def test_semver_parsing_and_comparison():
    assert parse_semver("1.0.0") == (1, 0, 0, "")
    assert parse_semver("v2.3.4") == (2, 3, 4, "")
    assert parse_semver("1.2.3-rc1") == (1, 2, 3, "rc1")

    # Strict upgrade tests
    assert is_version_newer("1.1.0", "1.0.0") is True
    assert is_version_newer("1.0.1", "1.0.0") is True
    assert is_version_newer("2.0.0", "1.9.9") is True
    assert is_version_newer("1.1.0", "1.1.0-rc1") is True  # final newer than prerelease

    # Downgrade / equal rejection
    assert is_version_newer("1.0.0", "1.0.0") is False
    assert is_version_newer("0.9.9", "1.0.0") is False
    assert is_version_newer("1.1.0-rc1", "1.1.0") is False
    print("[OK] test_semver_parsing_and_comparison passed")


def test_valid_envelope_signature_verification():
    raw_env, pubkey = create_test_manifest_envelope(version="1.1.0")
    manifest = verify_envelope_bytes(
        raw_env,
        installed_version="1.0.0",
        trusted_public_keys=[pubkey],
    )
    assert manifest["version"] == "1.1.0"
    assert "windows-x86_64-installer" in manifest["assets"]
    print("[OK] test_valid_envelope_signature_verification passed")


def test_tampered_payload_rejected():
    raw_env, pubkey = create_test_manifest_envelope(version="1.1.0", tamper_payload=True)
    try:
        verify_envelope_bytes(raw_env, installed_version="1.0.0", trusted_public_keys=[pubkey])
        raise AssertionError("Should have rejected tampered payload")
    except UpdateVerificationError as exc:
        assert "signature verification failed" in str(exc).lower()
    print("[OK] test_tampered_payload_rejected passed")


def test_tampered_signature_rejected():
    raw_env, pubkey = create_test_manifest_envelope(version="1.1.0", tamper_sig=True)
    try:
        verify_envelope_bytes(raw_env, installed_version="1.0.0", trusted_public_keys=[pubkey])
        raise AssertionError("Should have rejected tampered signature")
    except UpdateVerificationError as exc:
        assert "signature verification failed" in str(exc).lower() or "malformed" in str(exc).lower()
    print("[OK] test_tampered_signature_rejected passed")


def test_wrong_key_rejected():
    raw_env, _ = create_test_manifest_envelope(version="1.1.0")
    wrong_key = ed25519.Ed25519PrivateKey.generate().public_key()
    try:
        verify_envelope_bytes(raw_env, installed_version="1.0.0", trusted_public_keys=[wrong_key])
        raise AssertionError("Should have rejected unknown public key")
    except UpdateVerificationError as exc:
        assert "signature verification failed" in str(exc).lower()
    print("[OK] test_wrong_key_rejected passed")


def test_downgrade_rejected():
    raw_env, pubkey = create_test_manifest_envelope(version="1.0.0")
    try:
        verify_envelope_bytes(raw_env, installed_version="1.0.0", trusted_public_keys=[pubkey])
        raise AssertionError("Should have rejected same version update")
    except DowngradeError as exc:
        assert "refusing update" in str(exc).lower()
    print("[OK] test_downgrade_rejected passed")


def test_disallowed_repo_url_rejected():
    raw_env, pubkey = create_test_manifest_envelope(
        version="1.1.0",
        url_override="https://github.com/Attacker/MaliciousRepo/releases/download/v1.1.0/inkdoc-setup.exe",
    )
    try:
        verify_envelope_bytes(raw_env, installed_version="1.0.0", trusted_public_keys=[pubkey])
        raise AssertionError("Should have rejected non-official repo download URL")
    except UpdateVerificationError as exc:
        assert "official repository" in str(exc).lower()
    print("[OK] test_disallowed_repo_url_rejected passed")


def test_missing_cryptography_lazy_failure():
    """Verify that if cryptography is missing, server still imports and verifier fails with clear error."""
    import subprocess

    code = (
        "import sys\n"
        f"sys.path.insert(0, {repr(str(REPO_ROOT))})\n"
        "sys.modules['cryptography'] = None\n"
        "sys.modules['cryptography.hazmat'] = None\n"
        "sys.modules['cryptography.hazmat.primitives'] = None\n"
        "sys.modules['cryptography.hazmat.primitives.asymmetric'] = None\n"
        "sys.modules['cryptography.exceptions'] = None\n"
        "from app.server.server import app\n"
        "from app.core.update_verifier import verify_envelope_bytes, UpdateVerificationError\n"
        "try:\n"
        "    verify_envelope_bytes(b'{}')\n"
        "    sys.exit(1)\n"
        "except UpdateVerificationError as e:\n"
        "    assert 'cryptography' in str(e).lower()\n"
        "    print('VERIFIER_HANDLED_CLEANLY')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Subprocess failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    assert "VERIFIER_HANDLED_CLEANLY" in result.stdout
    print("[OK] test_missing_cryptography_lazy_failure passed")


if __name__ == "__main__":
    print("Running Update Verifier Unit Tests...")
    test_semver_parsing_and_comparison()
    test_valid_envelope_signature_verification()
    test_tampered_payload_rejected()
    test_tampered_signature_rejected()
    test_wrong_key_rejected()
    test_downgrade_rejected()
    test_disallowed_repo_url_rejected()
    test_missing_cryptography_lazy_failure()
    print("\nALL UPDATE VERIFIER TESTS PASSED!")
