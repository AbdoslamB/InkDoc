#!/usr/bin/env python3
"""Check a release signing key's passphrase, and which trusted key it is.

Signing happens once per release, at the end of a long process, and a wrong
passphrase there aborts the run. This answers the same question in a couple of
seconds and without touching the release:

    python scripts/check_signing_key.py                  # the primary key
    python scripts/check_signing_key.py --backup         # the backup key
    python scripts/check_signing_key.py --key-file PATH

It reports whether the passphrase unlocks the key, and whether the resulting
public key is one the application actually trusts -- because a key that unlocks
fine but is not embedded in update_verifier.py would produce a manifest every
client rejects.

No private key material is printed or written anywhere.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_KEY_DIR = Path.home() / ".inkdoc-keys"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key-file", type=Path, default=None)
    parser.add_argument("--backup", action="store_true",
                        help="Check inkdoc_backup_key.pem instead of the signing key")
    args = parser.parse_args()

    key_path = args.key_file or DEFAULT_KEY_DIR / (
        "inkdoc_backup_key.pem" if args.backup else "inkdoc_signing_key.pem"
    )
    if not key_path.is_file():
        print(f"[FAIL] No key at {key_path}")
        if DEFAULT_KEY_DIR.is_dir():
            found = sorted(p.name for p in DEFAULT_KEY_DIR.glob("*.pem"))
            print(f"       Keys present in {DEFAULT_KEY_DIR}: {', '.join(found) or 'none'}")
        return 1

    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        print("[FAIL] The 'cryptography' package is not installed in this interpreter.")
        return 1

    print(f"[*] Key: {key_path}")
    passphrase = getpass.getpass("Enter passphrase: ")

    try:
        key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=passphrase.encode("utf-8")
        )
    except Exception as exc:
        # cryptography says "Incorrect password" for a bad passphrase and
        # something else for a malformed file; both are worth distinguishing.
        print(f"[FAIL] Could not unlock: {exc}")
        print("       If this key has a different passphrase, try --backup, or")
        print("       point --key-file at the other key.")
        return 1

    print("[OK]   Passphrase is correct and the key unlocked.")

    try:
        public_hex = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()
    except Exception as exc:
        print(f"[FAIL] Unlocked, but this is not an Ed25519 key: {exc}")
        return 1

    try:
        from app.core.update_verifier import BACKUP_PUBLIC_KEY_HEX, PRIMARY_PUBLIC_KEY_HEX
    except Exception as exc:
        print(f"[!]    Could not read the embedded public keys to compare: {exc}")
        return 0

    if public_hex == PRIMARY_PUBLIC_KEY_HEX.lower():
        print("[OK]   This is the PRIMARY key the application trusts.")
    elif public_hex == BACKUP_PUBLIC_KEY_HEX.lower():
        print("[OK]   This is the BACKUP key the application trusts.")
    else:
        print("[FAIL] This key is NOT embedded in update_verifier.py.")
        print("       Signing with it would produce a manifest every client rejects.")
        print(f"       its public key: {public_hex}")
        print(f"       primary        : {PRIMARY_PUBLIC_KEY_HEX.lower()}")
        print(f"       backup         : {BACKUP_PUBLIC_KEY_HEX.lower()}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
