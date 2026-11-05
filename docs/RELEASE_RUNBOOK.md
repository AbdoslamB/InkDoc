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
   This section becomes the release notes: `release.yml` puts it on the release page
   and `sign_manifest.py` signs it into the update manifest. Settings → Updates shows
   five entries, the first ones of each heading in turn, so put each heading's most
   important entries first.
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
- Compiles binaries for Windows (`inkdoc-setup.exe`, `inkdoc.exe`, `inkdoc-windows.zip`), macOS (`inkdoc-macos.dmg`, from 1.3.0; earlier releases shipped `inkdoc-macos.zip`), and Linux (`inkdoc-linux-x86_64.AppImage`, from 1.3.0; earlier releases shipped `inkdoc-linux.zip`).
- Runs the Linux AppImage's smoke test on Ubuntu 24.04, Debian 13 and Fedora 44 (`linux-compat`); the `publish-release` job (its asset check, and publishing for pre-releases) runs only when all three pass.
- Injects the build version dynamically into `app/_version.py`.
- Generates SHA-256 checksums (`SHA256SUMS-*.txt`).
- Generates GitHub build provenance attestations (`actions/attest-build-provenance`).
- Creates a **DRAFT** GitHub release containing all compiled binaries and checksum files.

### Step 3: Sign the Manifest and Publish the Release

Once the GitHub Actions workflow finishes, and the release's draft holds every asset:

0. Open `https://abdoslamb.github.io/InkDoc/support/` and check that it lands on
   Buy Me a Coffee. The app's support links open that page (from 1.3.0), and
   GitHub Pages serves it from `main`, so `docs/support/index.html` must be on
   `main` before anyone can download the release.
1. Sign and publish with one command, from the repo root:
   ```bash
   python scripts/sign_manifest.py --tag vX.Y.Z --publish
   ```
   Add `--key-file <path>` if the key is not at the default `~/.inkdoc-keys/inkdoc_signing_key.pem`.
   Use `--dry-run --out-manifest <path>` first to inspect the envelope without uploading
   (it can't be combined with `--publish`). Any Python with `cryptography` works, an
   embeddable one included.
2. The script, in this order, stopping at the first thing that fails:
   - Fetches the draft's metadata (it refuses a release that is already published) and
     downloads every asset into a temporary directory.
   - Verifies them against `SHA256SUMS-*.txt`, and checks each one's build provenance
     (`gh attestation verify`).
   - Prompts for the private key passphrase, builds the envelope (download URLs, sizes,
     SHA-256 digests, the CHANGELOG's notes) and signs its base64 payload with Ed25519.
   - Checks the envelope as the app will: signed by a key the app embeds, for this
     version, with an asset for every platform.
   - Uploads `inkdoc-update-manifest.json` to the draft and reads it back.
   - Publishes the draft as **Latest**. The updater fetches
     `/releases/latest/download/inkdoc-update-manifest.json`, which only resolves for the
     release GitHub marks as Latest.
   - Waits (up to 5 minutes) until that address serves the exact manifest it signed.
3. When it says `[Success] Installed copies now find X.Y.Z`, the release is live. The
   **Release check** workflow runs on its own when the release is published and checks the
   same address and every file the manifest lists; if it fails, GitHub emails you. Run it
   yourself at any time: `python scripts/check_release.py --tag vX.Y.Z`.

Without `--publish`, the script only signs and uploads: verify provenance yourself first
(`gh attestation verify <file> --repo AbdoslamB/InkDoc` for every asset), then publish the
draft with **Set as the latest release** checked, never before the manifest is on it. A
Latest release without its manifest is what 1.3.0 shipped as: every installed copy said
"No signed update information has been published" until it was uploaded.

Existing InkDoc desktop installations discover the update upon their next check, provided
their embedded public keys include the key that signed this manifest.

### Step 4: After Publishing

1. **Only now** push a commit that renames a download in `README.md` or `docs/index.html`
   (as 1.3.0's switches from `inkdoc-linux.zip` to `inkdoc-linux-x86_64.AppImage` and from
   `inkdoc-macos.zip` to `inkdoc-macos.dmg` do). Their
   links go through `/releases/latest/download/`, and the landing page pins them to the
   newest stable tag, so a new file name 404s until the release that has it is Latest.

### GLM-OCR catalogue

GLM-OCR's model and runtime are not app assets. The app downloads them from their
official source (Hugging Face, llama.cpp's GitHub releases) and falls back to a
byte-identical **mirror** on InkDoc's own releases (`glm-ocr-vN`). The pins live in
`app/core/glm_ocr_catalogue.json`, generated by `scripts/build_glm_ocr_catalogue.py`.

**Before the first app release that ships GLM-OCR (milestone 1, `glm-ocr-v1`):**

1. Run the **GLM-OCR Track** workflow manually with **Mirror the CURRENT bundled
   pins** checked. It tests the pins on Windows, macOS and Linux, then creates the
   *draft* release `glm-ocr-v1` (never Latest) carrying the exact upstream files,
   the license files and provenance attestations.
2. Verify provenance like any release asset
   (`gh attestation verify <file> --repo AbdoslamB/InkDoc`), then publish the draft
   **without** "Set as the latest release". Only app releases may be Latest: the
   updater resolves `/releases/latest/download/`.
3. Nothing needs signing for milestone 1: the bundled catalogue is covered by the
   signed app release itself.

Until `glm-ocr-v1` is published, installs still work from upstream; only the
fallback would fail (the error names both sources).

**Newer tested pins without an app release (milestone 2):**

1. Opt in once: set the repository variable `GLM_OCR_TRACKER_ENABLED` to `true`.
   The workflow then runs weekly. It does nothing when upstream is unchanged, and on
   a change it tests, drafts `glm-ocr-vN`, opens a PR for the bundled catalogue,
   and opens an issue "GLM-OCR update ready to sign: glm-ocr-vN" with the report.
2. Sign offline (the key never touches CI):
   ```bash
   python scripts/sign_glm_ocr_catalogue.py --tag glm-ocr-vN            # check, then
   python scripts/sign_glm_ocr_catalogue.py --tag glm-ocr-vN --publish  # publish
   ```
   The script validates the payload, re-downloads every file from **both** upstream
   and the mirror and checks every hash, signs with the release key, verifies the
   envelope against the keys embedded in the app, and with `--publish` publishes the
   draft and replaces `inkdoc-glm-ocr-catalogue.json` on the never-Latest
   `glm-ocr-catalogue` release.
3. Installed apps see "Update available" in the GLM-OCR card the next time it is
   opened. They accept a catalogue only with a valid signature and a
   `catalogue_version` higher than both the bundled one and any they have accepted
   before, so an old catalogue can never be replayed.

If the tracker fails, it records why in the issue and drafts nothing; users keep the
last good pins.

### Docling engine pack

The Docling pack is not an app asset either. It is built by `build-packs.yml` on its own
release (`docling-pack-vN`, never Latest; published assets are never replaced, so a
rebuild always takes the next N). Each app release names one pack: `app/core/manifest.json`
is that release's merged `manifest.json`, copied in unchanged. Users who have an earlier
pack keep it, checked against its launch hashes in `engine_manifest.EARLIER_PACKS`,
until they click **Update Docling** in Settings → Engines.

**A new pack, before the app release that ships it** (1.3.0 ships `docling-pack-v2`):

1. Set the versions in `scripts/docling-constraints.txt` and push the commit. CI's
   `test_the_shipped_pack_matches_the_pin` fails from here until step 4, by design: the
   pin no longer matches the pack in `manifest.json`.
2. Build and publish the pack:
   ```bash
   git tag docling-pack-vN && git push origin docling-pack-vN
   ```
   The workflow builds on Windows, Linux and macOS, smoke-tests each pack (offline PDF
   conversion), merges the three fragments, re-verifies the packs as `EngineManager`
   installs them, publishes them with `manifest.json` (not Latest), attests provenance,
   then re-downloads the public assets and checks their sizes and SHA-256s.
3. Download that release's `manifest.json` and check it:
   ```bash
   gh release download docling-pack-vN --pattern manifest.json --dir /tmp/pack-vN --repo AbdoslamB/InkDoc
   gh attestation verify /tmp/pack-vN/manifest.json --repo AbdoslamB/InkDoc
   ```
4. Before replacing the file, add the **outgoing** pack to `EARLIER_PACKS` in
   `app/core/engine_manifest.py`: for each platform, its `interpreter_path`, `worker_script` and
   their two hashes from the current `app/core/manifest.json`. Without this, everyone with
   the outgoing pack gets "Launch integrity check failed" after updating InkDoc.
   Then copy `/tmp/pack-vN/manifest.json` over `app/core/manifest.json`, unchanged.
5. Bring the pack's license list up to date, then read the license of every package it
   reports as new (PyPI metadata and the files in its dist-info) and write its row:
   ```bash
   python scripts/pack_license_list.py --write
   python scripts/pack_license_list.py            # must end with [OK]
   ```
   Update the package count and release tag in `NOTICE` and `THIRD_PARTY_NOTICES.md`.
6. Check the result:
   ```bash
   python scripts/verify_engine_manifest_guard.py       # hashes, sizes, asset presence
   python -m pytest tests/ -q                           # the pin test passes again
   ```
7. The code & formula add-on: if the new Docling still looks for the model under the same
   folder (`CodeFormulaModel._model_repo_folder`, `docling-project--CodeFormulaV2` in
   Docling 2.130 and 2.140), the published add-on stays valid and needs no rebuild.
   Installed copies are moved into the new pack and re-checked by its worker during the
   update. Otherwise, publish `docling-addon-vN+1` with `build-addon.yml` after step 4.
8. On a Windows PC with the outgoing pack installed, run the new build from source and
   update Docling from Settings → Engines. Check that a scanned PDF converts with OCR,
   and that the code & formula add-on is still usable afterwards.

Then release the app as usual (Step 1 onwards).

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
