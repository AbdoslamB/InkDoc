# Changelog

All notable changes to InkDoc are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Code & Formula Enrichment could never be enabled.** Both toggles were
  hardcoded `disabled` in the markup and no client code ever cleared the
  attribute or called `/addons`, so a complete add-on backend shipped with no way
  to install anything. Settings now carries an add-on card that renders every
  state `AddonManager` reports and enables the toggles only when it reports
  `usable`.
- **The add-on artifact had no publication path.** `build_addon.py` printed a
  catalogue entry containing the literal URL placeholder
  `<publish the archive and put its URL here>` for someone to paste into
  `addons.json` by hand. It now derives the asset URL from the release tag, the
  same way `build_pack.py` does, and `build-addon.yml` publishes, attests and
  re-verifies the asset before `release.py` commits the generated catalogue back.
- **`get_status()` and `install()` disagreed on what "installable" meant.** Status
  asked `bool(url) and bool(sha256)`, which the placeholder satisfied, so the UI
  would have offered an Install button that always failed mid-download. Both now
  use one `validate_addon_entry` predicate, which also classifies the legitimate
  pre-release state instead of conflating it with a broken entry.
- Enrichment toggle change handlers were registered inside `loadSettings()`,
  which runs on every settings-popover open, so listeners accumulated one per
  open. Registered once at startup instead.
- An enrichment toggle can no longer be left checked while disabled, in either
  order the settings popover's two concurrent refreshes resolve.

### Added
- `scripts/verify_addon_catalogue.py`, a fail-closed catalogue guard in CI
  alongside the existing engine-manifest guard. An unreleased add-on passes; a
  placeholder, half-filled or malformed entry does not.
- A `capabilities` worker RPC reporting whether Docling can load the recognition
  model, reading the model directory name off Docling's own class rather than
  trusting a name in the catalogue. Installs are confirmed through it, and a pack
  carrying a different Docling version marks the add-on as needing reinstall
  instead of reporting it ready.
- `tests/enrichment_addon_ui.test.js`, covering the toggle-enable invariant
  across every add-on state and both popover refresh orders, and
  `tests/test_addon_catalogue.py`, which installs over real HTTP — the download,
  extraction and per-file hashing had only ever run with `stream_download`
  stubbed out.
- An `addon` phase in `release.py`, conditional like `pack`, which refuses to
  publish an add-on that requires a pack newer than the one the app ships.

### Changed
- The add-on requires Docling pack **6.0.0**. `worker.py` is hashed into the pack
  manifest and the launcher rejects a modified copy, so an older pack cannot be
  given the capability RPC or the enrichment precheck.
- Requesting enrichment without the model now fails with an actionable error
  instead of silently converting without it. The error is exempt from
  `fallback_to_markitdown`, which exists for documents Docling cannot parse, not
  for a configuration inconsistency the user needs to see and fix.
- Add-on-gated settings are reconciled against what is installed once at startup,
  so a stale enabled flag is corrected where it is stored rather than worked
  around on every conversion. `GET /settings` reports what was corrected and why.

### Security
- `POST /settings` refuses to enable either enrichment flag while the recognition
  model is not usable, returning HTTP 409 with the reason, and removing the
  add-on clears both flags. Previously the only thing preventing a persisted flag
  with no model behind it was a disabled attribute in the markup.

## [1.0.1] - 2026-09-20

### Fixed & Hardened
- **IBM Docling Engine Support**:
  - Isolated out-of-process worker RPC architecture (`worker.py`, `docling_worker_client.py`) preventing process pollution.
  - Automatic dynamic detection in developer/source mode (`Active (Source)`) without requiring binary packs.
  - Manifest-derived platform support in Settings UI and README, removing all hardcoded OS strings.
  - Safe Tar extraction with permission preservation (`0o755` for exec) on Unix and Zip on Windows.
  - Windows long-path safety with `\\?\` prefix support (paths >= 240 chars) and shortened `%SYSTEMDRIVE%` root fallback.
  - CPU-only PyTorch wheels (`--extra-index-url https://download.pytorch.org/whl/cpu`) keeping pack archives strictly under 1.9 GiB.
  - Post-build smoke testing: clean directory extraction, Ping RPC, and offline PDF conversion verification.
- **Downloader Security**:
  - `If-Range` header caching and stale `.part` eviction on remote modification.
  - Per-hop redirect re-validation against CDN allowlist across retries.
  - Fail-fast on 4xx errors with HTTP 429 `Retry-After` backoff.
  - Pre-hash file size verification before SHA-256 calculation.
  - Combined disk headroom verification (`download + 1.5 * uncompressed + 250MB`).
  - Automatic stale `.part` file cleanup (> 24 hours).
- **UI & Layout Polish**:
  - Zero-scroll application shell (`100dvh` bound, `overflow: hidden`) preventing native desktop window-level scrolling.
  - Collapsible dropzone with 350ms auto-collapse on generated results and manual expansion latching.
  - Top-right collapse toggle hidden until results section is generated down below.
  - Window-level drag/drop safety preventing unintended browser navigation on accidental drops.

## [1.0.0] - 2026-09-18

### Initial Release of InkDoc

- **Unified Multi-Engine Document-to-Markdown Pipeline**:
  - `⚡ MarkItDown`: Microsoft's engine for Office docs (DOCX, XLSX, PPTX), PDFs, HTML, CSV, and audio speech-to-text.
  - `🧠 IBM Docling`: Deep OCR layout analysis and TableFormer table structure recovery.
  - `🌐 Markit`: Broad-format specialist with built-in pure Python extractors for EPUB, YAML, and Jupyter Notebooks (`.ipynb`).
- **Unified Web & Desktop UI Architecture**:
  - Edge WebView2 (Windows), WebKit (macOS), and WebKitGTK (Linux) native desktop shell sharing 100% of UI code with the local browser Web Workbench (`http://localhost:13118/InkDoc`).
  - Seamless zero-friction conversion: drop files, paste URLs, or click to browse with instant conversion.
  - Collision-safe automatic saving to `~/Downloads` without manual export required.
- **Embedded Local REST API**:
  - High-throughput FastAPI backend with headless mode support (`python main.py --headless`).
  - Interactive OpenAPI Swagger documentation at `/docs`.
  - Comprehensive endpoints: `/convert/file`, `/convert/url`, `/convert/batch`, `/health`, and `/extensions`.
- **Inkbench Obsidian Design System**:
  - Modern dark and light glassmorphism themes with glowing engine selector pills.
  - Live rendered and raw Markdown preview pane powered by `marked.js`.
- **Cross-Platform Installers & Binaries**:
  - Windows Inno Setup installer (`inkdoc-setup.exe`), portable single-file executable (`inkdoc.exe`), macOS bundle, and Linux bundle.
- **Automated Integration Test Suite**:
  - Complete backend verification via `tests/test_server.py`.

[Unreleased]: https://github.com/AbdoslamB/inkdoc/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/AbdoslamB/inkdoc/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/AbdoslamB/inkdoc/releases/tag/v1.0.0
