# Changelog

All notable changes to InkDoc are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Publishing an enrichment add-on release no longer marks it as the Latest
  release. The in-app updater reads `/releases/latest/download/`, so an add-on
  holding Latest would have cut off every installed copy's update check until the
  next app release.

## [1.0.0] - 2026-09-26

Initial release.

### Conversion
- Three conversion engines, switchable per item from the UI or per request from
  the API:
  - **MarkItDown** (Microsoft), bundled: Word, Excel, PowerPoint, PDF, HTML, CSV,
    JSON, XML, images and audio.
  - **Docling** (IBM Research), an optional in-app engine pack: layout analysis,
    OCR and TableFormer table reconstruction for complex and scanned PDFs.
  - **Markit**, bundled: EPUB, Jupyter notebooks, YAML and RSS/Atom feeds.
- Drop a file or folder, paste a URL, or browse: conversion starts immediately and
  the Markdown is saved to `~/Downloads` with collision-safe names. No export step.
- An engine is never substituted silently. Falling back from Docling to MarkItDown
  happens only when enabled in Settings, and is reported on the result.
- Optional Code & Formula Recognition add-on for Docling, installed from Settings,
  which transcribes code blocks and formulas from page images and labels each code
  block's language.

### Application
- One interface for the desktop app (Edge WebView2 on Windows, WebKit on macOS,
  WebKitGTK on Linux) and the local web workbench at
  `http://127.0.0.1:13118/InkDoc`.
- Live rendered and raw Markdown preview, with dark and light themes.
- Local REST API (`/convert/file`, `/convert/url`, `/convert/batch`, `/health`,
  `/extensions`) with OpenAPI documentation at `/docs`, and a headless server mode
  (`python main.py --headless`).
- Docling runs in an isolated worker process, from an engine pack checked against
  a pinned manifest of per-file SHA-256 hashes.

### Security
- In-app updates are verified against an Ed25519-signed manifest before anything is
  downloaded, and are refused for any version not newer than the one installed.
- URL conversion validates targets against SSRF, and the local server enforces
  Host and Origin checks and a per-session token on management endpoints.
- Release assets carry GitHub build provenance attestations and published SHA-256
  checksums.

### Distribution
- Windows installer (`inkdoc-setup.exe`), portable executable (`inkdoc.exe`) and
  portable ZIP; macOS (Apple Silicon) bundle; Linux (x86_64) bundle.

[Unreleased]: https://github.com/AbdoslamB/InkDoc/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/AbdoslamB/InkDoc/releases/tag/v1.0.0
