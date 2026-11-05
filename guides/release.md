# CI, releases, updates and the website

How InkDoc is checked, built, released and updated, and the website on GitHub Pages. The files
this guide covers are listed in AGENTS.md's routing table.

## Rules

- Every list of release asset names must agree (`tests/test_release_assets.py`).
- The Linux bundle carries none of GTK, GLib, WebKit or the C++ runtime.
- Releases other than the app's own (add-ons, engine packs) never become "Latest": the updater
  reads `/releases/latest/download/`.
- Updates are verified before they are parsed: Ed25519 over the exact bytes received, with no
  downgrades and only URLs under the repository's own release path.
- Release dependencies are pinned exactly, so a release ships what was tested.
- `docs/support/index.html` is never removed or renamed, and is live on `main` before a release
  that links to it is published.
- An app release is published only after its signed manifest is on it
  (`sign_manifest.py --publish`).

## How it works

### Interpreters

Every bundle ships the Python that `setup-python` installs (`python-version` in `release.yml`
and `ci.yml`, now 3.14); the Docling pack has its own (`scripts/build_pack.py`). Each autumn,
move the bundles to the next minor version while the current one still gets binary releases
(a minor in security-only support stops at its last binary, as `"3.11"` stopped at 3.11.9),
after `scripts/runtime_bench.py` shows no regression. The 3.10 floor for running from source
is tested by `ci.yml`'s `source-floor` job only.

### Performance

`scripts/perf_baseline.py check` (CI, every push) measures InkDoc's own process at startup,
the server's and the MarkItDown worker's start, and a generated 100-page PDF through
MarkItDown and the table finder (time and peak memory), and fails when memory grows by more
than a quarter or a time by more than three quarters plus a second. Each runner has its
entry in `scripts/perf_baseline.json`; a runner without one is only measured and the log
prints the entry to add. Copy entries from CI's log, never edit the numbers; update one
when a cost is meant (a new feature, a new Python), in the same change. A developer's own
baseline stays in `scratch/`.

`scripts/optimize_audit.py` (development only, suggestions only) looks for savings with
evidence: newer packages and advisories (`--trial` installs an upgrade in a throwaway
environment and compares the baseline's numbers and output digests), a profile of one
conversion, the largest parts of a release `.zip`, and Python's and the pinned engines'
newest versions. A candidate keeps every feature only if the full test suite passes and
the output digests stay the same. Audit a release file, not a local build: a local build
packs whatever the development environment has installed.

### Linux release

Built on ubuntu-24.04, so the AppImage needs glibc 2.39 (`MIN_GLIBC` in
`scripts/sign_manifest.py` names the same version; the updater refuses an update the system
can't start). The window is the user's own WebKit2GTK, so the bundle must carry none of GTK,
GLib, WebKit or the C++ runtime: a bundled copy is loaded first and the host's WebKit fails
against it (how 1.2.0's zip broke on every newer distribution). Hence:

- PyGObject from pip (compiled in CI, `gi.repository.*` excluded from PyInstaller);
- `scripts/linux_host_libs.py` `prune`/`check` after the build (only `libgirepository-2.0`
  stays);
- `app/core/linux_host.py` (GI_TYPELIB_PATH to the host's typelibs, and the host's
  LD_LIBRARY_PATH for xdg-open; see [desktop.md](desktop.md));
- `scripts/build_appimage.py` (pinned appimagetool and static runtime).

`linux-compat` in `release.yml` runs the AppImage's smoke test, window included, on
ubuntu:24.04, debian:13 and fedora:44.

### Workflows (all in `.github/workflows/`)

- `ci.yml`: on every push/PR to `main`: `ruff check .`, `python -m compileall`, the tests,
  the performance baseline and the headless smoke test, matrixed across
  windows-latest/macos-latest/ubuntu-24.04, plus the 3.10 `source-floor` job.
- `release.yml`: on any `vX.Y.Z` tag push: builds a PyInstaller `--onedir --windowed` bundle
  per OS (`--collect-all markitdown --collect-all magika`, needed because both do
  dynamic/plugin-style imports PyInstaller's static analysis can't see), packages it (Windows
  installer, onefile and zip; macOS disk image, `scripts/build_dmg.py`; Linux AppImage),
  smoke-tests every artifact (the disk image mounted read-only and its app copied out first)
  and attaches them to the draft GitHub Release. Every list of asset names (the publish job's,
  the size caps, `sign_manifest.py`'s `TARGET_ASSETS`) must agree:
  `tests/test_release_assets.py`.
- `release-check.yml`: whenever a release is published: `scripts/check_release.py` checks that
  the updater's address serves a manifest for the tag that passes the app's own check, and that
  every file it lists answers at its size. Tags other than vX.Y.Z are skipped.
- `build-packs.yml`: the Docling engine pack, on a `docling-pack-vN` tag (see
  [engines.md](engines.md)).
- `build-addon.yml`: the Docling code/formula recognition add-on, on a `docling-addon-vN` tag.
- `glm-ocr-track.yml`: keeps GLM-OCR on the newest *tested* upstream pins without an app
  release: it detects new upstream files, smoke-tests them on all three systems, mirrors them
  and opens a PR for the bundled catalogue. Nothing in CI signs; the maintainer signs offline
  (`scripts/sign_glm_ocr_catalogue.py`, `docs/RELEASE_RUNBOOK.md`).
- `dependabot.yml` (in `.github/`): weekly PRs for `pip` and `github-actions` deps.

Branch protection is enabled on `main` (required PR review) but `enforce_admins` is `false`:
the repo owner can still push directly; external contributors cannot.

### Versions and the changelog

`app/_version.py` is the one place the version is defined, for the desktop runner, the server
and the updater (`scripts/write_version.py` sets it). Version bumps follow semver in
`CHANGELOG.md` (Keep a Changelog format): move `[Unreleased]` entries into a new
`[X.Y.Z] - date` section, update the comparison links at the bottom of the file, then
`git tag vX.Y.Z && git push origin vX.Y.Z` to trigger `release.yml`. The maintainer's release
checklist is `docs/RELEASE_RUNBOOK.md`.

### Updates

- `update_verifier.py` verifies the signed update manifest: Ed25519 over the exact base64 bytes
  received, before parsing; a version strictly newer than the installed one; download URLs only
  under the repository's own release path; a primary and a backup public key.
- `update_manager.py` checks for updates (on demand, and optionally daily), picks the asset for
  this platform, downloads it with resume, a disk-space check and SHA-256 verification, and
  applies it (on Windows, the installer runs silently).
- `scripts/sign_manifest.py` signs the manifest for a release and checks it as the app will
  (embedded keys, this version, every platform) before uploading. With `--publish` it also
  checks build provenance first, and after the upload reads the manifest back, publishes the
  draft as Latest and waits until the updater's address serves it: a release is never Latest
  without its manifest. It starts under any interpreter (it puts the repo root on `sys.path`
  itself; `python -I` in the tests).
- An update check that fails sets `offer_download` in the status, and Settings → Updates then
  links to the website's download section (`WEBSITE_DOWNLOAD_URL` in `app.js`, a fixed address
  like `SUPPORT_URL`). A certificate error is worded by `update_manager.connection_error`.

### The website (`docs/`)

`docs/index.html` is a single self-contained file (inline CSS/JS, no build step, no external
script/font dependencies) served by GitHub Pages from `/docs` on `main`. It mirrors (but is not
generated from) the README; update both when a user-facing feature changes. Respects
`prefers-color-scheme` plus a `data-theme` override, and fetches a live GitHub star count
client-side (`fetch` against the public GitHub REST API, no auth, degrades silently if it
fails/rate-limits).

`docs/support/index.html` is the app's support address
(`https://abdoslamb.github.io/InkDoc/support/`, `SUPPORT_URL` in `app.js`): it forwards at once
to the support platform (Buy Me a Coffee). Released apps link only there, so changing platform
means editing that page, never the app. Never remove or rename it, and it must be live on
`main` before a release that links to it is published. The landing page links the platform
directly.

GitHub Pages publishes every file under `docs/`, Markdown included. Developer documentation
belongs in [guides/](README.md), not there.

## Tests

- `tests/test_release_assets.py`, `tests/test_sign_manifest_contract.py`,
  `tests/test_packaging_parity.py`, `tests/test_check_venv_imports.py`,
  `tests/test_write_version.py`, `tests/test_changelog_notes.py`, `tests/test_perf_baseline.py`,
  `tests/test_optimize_audit.py`.
- Linux: `tests/test_build_appimage.py`, `tests/test_linux_host_libs.py`. macOS:
  `tests/test_build_dmg.py`.
- Updates: `tests/test_update_verifier.py`, `tests/test_update_manager.py`,
  `tests/test_update_api.py`, `tests/test_update_e2e.py`, `tests/test_release_publish.py`,
  `tests/test_tls_trust.py`, `tests/update_error_download_ui.test.js`.
- Website: `tests/docs_release_selector.test.js`, `tests/support_link_ui.test.js`.

## Lessons

### A bundled copy of a host library breaks the host's WebKit
- **When:** you add a dependency to the Linux build, or the window fails on a distribution
  other than the build machine's.
- **Lesson:** 1.2.0's Linux zip carried part of Ubuntu 22.04's GTK stack but took WebKit2GTK
  from the host, so its window failed on Ubuntu 24.04, Debian 13 and Fedora 44. Anything in
  the host WebKit2GTK's dependency closure must come from the host;
  `scripts/linux_host_libs.py check` enforces it after the build. Test new Linux builds on all
  three distributions (`linux-compat`).
- **Evidence:** a86bf0e.

### Pin release dependencies exactly
- **When:** you add or raise a dependency in `requirements.txt`.
- **Lesson:** the release build installs from `requirements.txt` with no lock, so a `>=` floor
  ships whichever version is newest on the day of the tag, untested. Pin exactly and let
  Dependabot propose newer versions.
- **Evidence:** 4921465 (MarkItDown pinned to 0.1.8).

### Releases other than the app's own must not take "Latest"
- **When:** you add a workflow step that publishes a GitHub release (an add-on, a pack).
- **Lesson:** the updater reads `/releases/latest/download/`. An add-on release published
  without `make_latest: false` took Latest from the app, which would have cut off every
  client's update check.
- **Evidence:** a7e8178; `test_non_app_releases_never_become_latest` in
  `tests/test_build_addon.py`.

### Check what the release commit stages
- **When:** you make a release or other large commit.
- **Lesson:** two files created by mistyped shell redirects (named after commit-message
  fragments) were swept into the v1.2.0 release commit by `git add -A`, and shipped. Read
  `git status` and stage by path.
- **Evidence:** 1e187ca.

### A pywebview self-test must let its call finish, and must end itself
- **When:** you change `--selftest-bridge`, the artifact smoke test, or anything that closes
  a pywebview window from a page call.
- **Lesson:** pywebview sends a call's result to the page only after the call returns, so a
  window closed inside the call loses the result: close it a moment later. pywebview also runs
  each page call on a thread that is not a daemon, and one still waiting on a closed window
  (macOS) keeps the process alive: the self-test ends with `os._exit` and has its own
  watchdog. The smoke script starts the self-test in its own process group, so killing the
  self-test's process tree never kills the script.
- **Evidence:** 971e419.

### Test HTTPS from the packaged app, on every system it ships to
- **When:** you add code that opens an HTTPS connection, change the bundled Python or its
  build, or add a platform.
- **Lesson:** the packaged app's OpenSSL looks for certificates where the build machine kept
  them. 1.3.0's macOS app found none on a Mac without python.org's Python, and its AppImage
  none on Fedora, Arch or openSUSE, so every update check, download and URL conversion failed
  there while CI stayed green: nothing in the smoke test opened an HTTPS connection.
  `--selftest-https` now does, for every artifact and in the Fedora container. Source runs
  can't show this; only the frozen app can.
- **Evidence:** `tests/test_tls_trust.py`; the released v1.3.0 AppImage's update check in a
  fedora:44 container ("unable to get local issuer certificate") versus debian:13 (reaches
  GitHub).

### Publish an app release only after its manifest is up
- **When:** you publish a release, or change how releases are signed or published.
- **Lesson:** the updater reads `/releases/latest/download/inkdoc-update-manifest.json`, so a
  release published as Latest before its manifest is uploaded tells every installed copy "No
  signed update information has been published". v1.3.0 went out that way, because the signer
  failed to start under an embeddable Python and publishing didn't depend on it.
  `sign_manifest.py --publish` publishes only after reading the manifest back, and
  `release-check.yml` fails on every release installed copies can't use.
- **Evidence:** `tests/test_release_publish.py`; v1.3.0 published 2026-11-01T14:26Z with no
  manifest asset (404 at the latest URL until it was uploaded by hand).
