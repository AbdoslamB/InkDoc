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

InkDoc binaries embed two trusted Ed25519 public keys as `PRIMARY_PUBLIC_KEY_HEX` and
`BACKUP_PUBLIC_KEY_HEX` in [`app/core/update_verifier.py`](../app/core/update_verifier.py).

**That file is the only source of truth.** Do not copy the hex values into this runbook
or anywhere else — a stale duplicate here previously disagreed with the code, which is
exactly the kind of drift that makes it impossible to tell which key a shipped binary
actually trusts. To read the values a given release trusts, check out its tag and look
at that file.

A signed manifest carries a **single** signature, and a build accepts it if it verifies
against *either* embedded key. So a manifest is only usable by a build whose embedded
keys include the key that signed it. Changing both embedded keys therefore cuts off
every already-released build from in-app updates — plan rotation accordingly (see
Key Rotation below).

### One-Time Keypair Generation

To generate a new offline keypair:

```bash
python scripts/generate_signing_key.py
```

The default output path is `~/.inkdoc-keys/inkdoc_signing_key.pem`; override it with
`--out-key <path>`. The script refuses to write anywhere inside this repository.

When prompted, choose a strong passphrase (minimum 12 characters, longer is better).
The script will:
1. Generate an Ed25519 private key.
2. Encrypt it with PKCS#8 using `BestAvailableEncryption` (AES-256).
3. Create the containing directory `0700` and save the private key `0600`.
4. Print the corresponding raw 32-byte public key hex to stdout.

The public key hex is **printed, not saved to a file**. Copy it from the terminal into
`PRIMARY_PUBLIC_KEY_HEX` in `app/core/update_verifier.py` before building the release
that should trust it.

### Safe Key Storage

- Store `inkdoc_signing_key.pem` on an encrypted, air-gapped USB drive or secure hardware token.
- Keep the passphrase in a password manager separate from the storage drive.
- **NEVER** commit private key PEM files to Git. `.gitignore` blocks `*.pem`, `*.key`,
  `*.p8` and `*.pfx` as a second line of defence, but do not rely on it.
- **NEVER** store private keys in GitHub Actions secrets or environment variables.
- If a private key is lost, it cannot be recovered and no future manifest can be signed
  for builds that trust only that key. Verify you can decrypt the key **before** tagging
  a release that depends on it.

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

1. Confirm GitHub build provenance for every asset. `sign_manifest.py` does **not** do
   this for you, so run it yourself first:
   ```bash
   gh release download vX.Y.Z --dir /tmp/inkdoc-vXYZ --repo AbdoslamB/InkDoc
   for f in /tmp/inkdoc-vXYZ/*; do gh attestation verify "$f" --repo AbdoslamB/InkDoc; done
   ```
2. Run the local manifest generation and signing script:
   ```bash
   python scripts/sign_manifest.py --tag vX.Y.Z
   ```
   Add `--key-file <path>` if the key is not at the default `~/.inkdoc-keys/inkdoc_signing_key.pem`.
   Use `--dry-run --out-manifest <path>` first to inspect the envelope without uploading.
3. The script will interactively:
   - Fetch the draft release metadata and download all release assets into a temporary directory.
   - Verify all downloaded binaries against `SHA256SUMS-*.txt`.
   - Prompt for the private key passphrase.
   - Construct the release envelope containing download URLs, expected file sizes, and SHA-256 digests.
   - Cryptographically sign the base64 payload bytes using the Ed25519 key.
   - Upload the signed `inkdoc-update-manifest.json` to the draft release on GitHub.

### Step 4: Publish the Release

1. Navigate to the GitHub Releases page: `https://github.com/AbdoslamB/InkDoc/releases`.
2. Open the draft release for `vX.Y.Z`.
3. Confirm that `inkdoc-update-manifest.json` is present among the release assets.
4. Ensure **Set as the latest release** is checked. The in-app updater fetches
   `/releases/latest/download/inkdoc-update-manifest.json`, which only resolves for the
   release GitHub marks as Latest. If this is left unchecked, every install gets an
   HTTP 404 on "Check for updates".
5. Click **Publish release**.

The release is now live. Existing InkDoc desktop installations will discover the update
upon their next check — provided their embedded public keys include the key that signed
this manifest.

---

## 4. Honest Assessment of Key Compromise & Rotation

### What Key Compromise Means

If the Ed25519 private key is compromised:
- An attacker can forge valid signatures on arbitrary release manifests.
- However, the attacker **cannot** cause arbitrary remote code execution freely because InkDoc clients enforce multiple layers of defense:
  1. **Strict CDN Allowlist:** Asset URLs must resolve to `github.com`, `objects.githubusercontent.com`, or `release-assets.githubusercontent.com`, checked on every redirect hop. Downloads from third-party servers are rejected.
  2. **Repo URL Path Restriction:** URLs must sit strictly under `/AbdoslamB/InkDoc/releases/download/v{version}/`.
  3. **Semver Anti-Downgrade:** The client will refuse any manifest claiming a version less than or equal to the currently installed version.

  Together these mean a stolen key alone is not enough: the attacker must also get the
  malicious binary served from a GitHub-hosted release asset path, or break TLS to
  `github.com`.

  **Not a client-side defence:** GitHub build provenance attestations are *not* checked
  by the app. Nothing in `app/core/update_manager.py` or `update_verifier.py` verifies
  them. Attestations are a release-process control that the maintainer runs manually
  (Step 3 above) — do not count them as a runtime protection.

### Key Rotation & Revocation Procedure

If the primary private key is suspected or confirmed compromised:

1. **Switch to Backup Signing Key:**
   InkDoc clients accept a manifest signed by *either* embedded key (see
   `get_official_public_keys()` in `app/core/update_verifier.py`), so a manifest signed
   with the backup private key is accepted by all already-installed clients without
   requiring an emergency update first. This only works if the backup private key is
   held and its passphrase is known — verify that periodically.
2. **Issue an Emergency Release:**
   - Sign `inkdoc-update-manifest.json` for release `vX.Y.(Z+1)` using the backup private key.
   - In `app/core/update_verifier.py`, promote the backup key's hex to
     `PRIMARY_PUBLIC_KEY_HEX` and set `BACKUP_PUBLIC_KEY_HEX` to a freshly generated key,
     so the compromised value is no longer trusted by new builds.
   - Commit and publish the update.
   - Keep signing with the promoted key until the compromised build is drained; only
     then start signing with the new backup key, or older installs will stop updating.
3. **Revoke the Compromised Key:**
   Publish a security advisory on GitHub informing users to update immediately.
   Already-installed builds keep trusting the compromised key until they update — there
   is no revocation channel, which is why step 2 must ship promptly.

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
