# Changelog

All notable changes to InkDoc are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
