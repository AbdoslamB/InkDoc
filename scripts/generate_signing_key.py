#!/usr/bin/env python3
"""Generate an encrypted offline Ed25519 signing keypair for InkDoc releases.

Usage:
    python scripts/generate_signing_key.py [--out-key inkdoc_signing_key.pem]

Security rules:
- Private key is encrypted using PKCS#8 with AES-256 and a strong passphrase.
- Private key must NEVER be committed to Git or uploaded to GitHub.
- Public key hex is displayed so it can be embedded in app/core/update_verifier.py.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate encrypted Ed25519 keypair for InkDoc updates")
    parser.add_argument(
        "--out-key",
        type=Path,
        default=Path("inkdoc_signing_key.pem"),
        help="Path where the encrypted private key will be saved (default: inkdoc_signing_key.pem)",
    )
    args = parser.parse_args()

    out_path: Path = args.out_key
    if out_path.exists():
        print(f"[Error] Key file already exists at {out_path}. Refusing to overwrite.", file=sys.stderr)
        return 1

    print("=== InkDoc Offline Release Key Generator ===")
    print("This generates an Ed25519 signing keypair for release manifests.")
    print("Keep the private key strictly offline and password-protected.\n")

    p1 = getpass.getpass("Enter passphrase to encrypt private key: ")
    if len(p1) < 12:
        print("[Error] Passphrase must be at least 12 characters long for security.", file=sys.stderr)
        return 1

    p2 = getpass.getpass("Confirm passphrase: ")
    if p1 != p2:
        print("[Error] Passphrases do not match.", file=sys.stderr)
        return 1

    # 1. Generate Ed25519 keypair
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    # 2. Serialize encrypted private key
    encrypted_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(p1.encode("utf-8")),
    )

    out_path.write_bytes(encrypted_pem)
    # Set restrictive file permissions (read/write only by owner) on POSIX
    try:
        out_path.chmod(0o600)
    except OSError:
        pass

    # 3. Serialize raw public key
    pub_hex = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()

    print(f"\n[Success] Encrypted private key saved to: {out_path}")
    print(f"Public key (hex): {pub_hex}")
    print("\nTo configure this key in InkDoc, embed the hex string in app/core/update_verifier.py:")
    print(f'PRIMARY_PUBLIC_KEY_HEX = "{pub_hex}"\n')

    return 0


if __name__ == "__main__":
    sys.exit(main())
