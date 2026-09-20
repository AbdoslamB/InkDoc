# InkDoc Release & Key Management Runbook

This document details the exact operational procedures for generating signing keys, executing release builds, locally signing release manifests, publishing updates, and managing cryptographic keys.

---

## 1. Security Architecture & Threat Model

InkDoc's update system is designed with a **zero-trust release pipeline**:

1. **Air-Gapped Offline Signing (Option B):**
   The Ed25519 private signing key **never touches GitHub or CI runners**. It is held exclusively offline by the maintainer in an encrypted PKCS#8 container protected by a high-entropy passphrase.
2. **Draft-Only CI Releases:**
   GitHub Actions workflows are strictly constrained to building artifacts and uploading them to a **DRAFT** release. CI has zero publishing capability and no access to signing credentials.
3. **Envelope Integrity:**
   The release manifest (`manifest.json`) is delivered in a cryptographic envelope (`payload` + `signature`). The desktop client verifies the Ed25519 signature over the exact, unparsed base64 payload bytes before parsing.
4. **Defense in Depth on Clients:**
   Even if GitHub infrastructure or release assets were compromised:
   - Assets are checked against official CDN allowlists (`github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com`).
   - Every redirect hop is checked to prevent open-redirect hijacking.
   - Asset URLs must strictly reside under `https://github.com/AbdoslamB/InkDoc/releases/download/v{version}/`.
   - Manifest versions must be strictly greater than the currently installed version (anti-downgrade protection).
   - In packaged (`sys.frozen`) builds, all test overrides for URLs and keys are forcibly disabled.

---

## 2. Cryptographic Key Management

### Embedded Public Keys

InkDoc binaries embed two trusted Ed25519 public keys in `app/core/update_verifier.py`:
- **Primary Public Key:** `3cb74bdeeb53556d49603f90919aa7aeaa36d93380e2d31ce43bf7e23737b600`
- **Backup Public Key:** `060b4bc6e8b417eef987bbdf7675662f3f7215904ea8d6fb24aa66b5e13586bf`

### One-Time Keypair Generation

To generate a new offline keypair:

```bash
python scripts/generate_signing_key.py --output-dir ~/.inkdoc-keys
```

When prompted, choose a strong passphrase (minimum 16 characters). The script will:
1. Generate an Ed25519 private key.
2. Encrypt it using PKCS#8 format (Scrypt KDF + AES-256-CBC).
3. Save the private key to `ed25519_private_key.pem` with restricted file permissions (`0600`).
4. Save the corresponding raw 32-byte public key hex to `ed25519_public_key.hex`.

### Safe Key Storage

- Store `ed25519_private_key.pem` on an encrypted, air-gapped USB drive or secure hardware token.
- Keep the passphrase in a password manager separate from the storage drive.
- **NEVER** commit private key PEM files to Git.
- **NEVER** store private keys in GitHub Actions secrets or environment variables.

---

## 3. Step-by-Step Release Workflow

### Step 1: Prepare the Release

1. Update `CHANGELOG.md`: move items from `[Unreleased]` into a new section `[X.Y.Z] - YYYY-MM-DD`.
2. Commit and push the changelog:
   ```bash
   git add CHANGELOG.md
   git commit -m "chore: prepare release vX.Y.Z"
   git push origin main
   ```

### Step 2: Tag and Trigger CI

Create an annotated tag and push it to GitHub:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

GitHub Actions will automatically run `.github/workflows/release.yml`, which:
- Compiles binaries for Windows (`inkdoc-setup.exe`, `inkdoc.exe`, `inkdoc-windows.zip`), macOS (`inkdoc-macos.zip`), and Linux (`inkdoc-linux.zip`).
- Injects the build version dynamically into `app/_version.py`.
- Generates SHA-256 checksums (`SHA256SUMS-*.txt`).
- Generates GitHub build provenance attestations (`actions/attest-build-provenance`).
- Creates a **DRAFT** GitHub release containing all compiled binaries and checksum files.

### Step 3: Locally Verify and Sign the Release Manifest

Once the GitHub Actions workflow finishes:

1. Run the local manifest generation and signing script:
   ```bash
   python scripts/sign_manifest.py --tag vX.Y.Z --key-file ~/.inkdoc-keys/ed25519_private_key.pem
   ```
2. The script will interactively:
   - Fetch the draft release metadata and download all release assets into a temporary directory.
   - Verify all downloaded binaries against `SHA256SUMS-*.txt`.
   - Run `gh attestation verify` against each asset to confirm GitHub Actions build provenance.
   - Prompt for the private key passphrase.
   - Construct the release envelope containing download URLs, expected file sizes, and SHA-256 digests.
   - Cryptographically sign the raw JSON envelope bytes using the Ed25519 key.
   - Upload the signed `manifest.json` directly to the draft release on GitHub.

### Step 4: Publish the Release

1. Navigate to the GitHub Releases page: `https://github.com/AbdoslamB/InkDoc/releases`.
2. Open the draft release for `vX.Y.Z`.
3. Confirm that `manifest.json` is present among the release assets.
4. Click **Publish release**.

The release is now live. Existing InkDoc desktop installations will discover the update upon their next check.

---

## 4. Honest Assessment of Key Compromise & Rotation

### What Key Compromise Means

If the Ed25519 private key is compromised:
- An attacker can forge valid signatures on arbitrary release manifests.
- However, the attacker **cannot** cause arbitrary remote code execution freely because InkDoc clients enforce multiple layers of defense:
  1. **Strict CDN Allowlist:** Manifest URLs must point to `github.com`, `objects.githubusercontent.com`, or `release-assets.githubusercontent.com`. Downloads from third-party servers are rejected.
  2. **Repo URL Path Restriction:** URLs must sit strictly under `https://github.com/AbdoslamB/InkDoc/releases/download/v{version}/`.
  3. **Build Provenance:** GitHub Actions attestations can only be minted by the official GitHub workflow runner.
  4. **Semver Anti-Downgrade:** The client will refuse any manifest claiming a version less than or equal to the currently installed version.

### Key Rotation & Revocation Procedure

If the primary private key is suspected or confirmed compromised:

1. **Switch to Backup Signing Key:**
   InkDoc clients already include a secondary **Backup Public Key** (`060b4bc6...`). Signs made with the backup key are accepted by all installed clients without requiring an emergency update first.
2. **Issue an Emergency Release:**
   - Sign `manifest.json` for release `vX.Y.(Z+1)` using the backup private key.
   - In `app/core/update_verifier.py`, remove the compromised primary key from `TRUSTED_PUBLIC_KEYS`.
   - Generate a new backup keypair using `scripts/generate_signing_key.py`, promote the current backup key to primary, and add the new key as backup.
   - Commit and publish the update.
3. **Revoke the Compromised Key:**
   Publish a security advisory on GitHub informing users to update immediately.

---

## 5. Rollback Procedures

### Windows Installer (`inkdoc-setup.exe`)

The Windows Inno Setup installer executes an in-place upgrade over the existing installation directory:
- **No Automatic Rollback:** The application has no automatic self-rollback mechanism. If an update introduces an unexpected regression or fails on a user's machine, the installer does not automatically revert to the previous executable.
- **Manual Rollback:**
  1. Download the installer or portable `.zip` of the desired previous version from `https://github.com/AbdoslamB/InkDoc/releases`.
  2. Run the installer or extract the portable build. The installer safely overwrites existing program files while preserving user documents and settings.

### Portable Executables, macOS, and Linux

Portable builds and macOS/Linux distributions operate via **verified download only**:
- The new release is downloaded directly to `~/Downloads/`.
- The user's existing application bundle remains completely untouched until the user manually replaces it.
- Rollback is trivial: simply continue running the previous binary or download an older release from GitHub Releases.
