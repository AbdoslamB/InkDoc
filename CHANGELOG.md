# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.0] - 2026-09-18

### Added
- **Project Rebranded to InkDoc**: Unified brand identity representing the multi-engine desktop application, local Web Workbench, and REST API.
- **Three-Engine Conversion Pipeline**:
  - `⚡ MarkItDown`: Microsoft's engine for Office docs, PDFs, HTML, CSV, and audio.
  - `🧠 IBM Docling`: Deep layout segmentation and TableFormer table structure recovery.
  - `🌐 Markit`: Broad-format specialist with built-in pure Python extractors for EPUB, YAML, and Jupyter Notebooks (`.ipynb`).
- **Unified Web & Desktop Architecture**: Edge WebView2 (Windows), WebKit (macOS), and WebKitGTK (Linux) native desktop shell sharing 100% of UI code with the local browser Web Bench (`http://localhost:13118/InkDoc`).
- **Embedded Local REST API**: High-throughput FastAPI server with headless mode (`python main.py --headless`), interactive Swagger docs, and endpoints for file, URL, and batch conversions.
- **Inkbench Obsidian Design System**: Modern dark and light glassmorphism interface, glowing engine selector pills, and live Markdown preview powered by `marked.js`.
- **Domain-Driven Codebase Structure**: Clean separation into `app/core/`, `app/server/`, `app/desktop/`, `app/ui/`, `tests/`, and `examples/`.
- **Automated Integration Test Suite**: Complete test coverage via `tests/test_server.py`.

### Removed
- Legacy PySide6/Qt desktop interface and Qt stylesheets.
- Redundant standalone `api/` directory (now unified inside `app/server/`).
- Outdated Qt translation module and QThreadPool worker infrastructure.

## [1.3.0] - 2026-08-13

### Added
- Release workflow: Windows builds now also produce a standalone
  single-file `markitdown-desktop.exe`, attached to the GitHub Release
  alongside the existing zipped binaries — no extraction needed to run it.

## [1.2.0] - 2026-07-24

### Added
- CI workflow: lint (ruff) + syntax check + headless smoke test on every
  push/PR, run on Windows, macOS, and Linux.
- Dependabot: weekly dependency update PRs for pip and GitHub Actions.
- Release workflow: pushing a `vX.Y.Z` tag now builds a standalone binary
  for Windows, macOS, and Linux with PyInstaller and attaches each as a
  release asset automatically — no Python installation required to run
  the app anymore.

## [1.1.0] - 2026-07-24

### Added
- Landing page (GitHub Pages) with feature overview, screenshot, supported
  formats, and per-OS install instructions.
- "Star on GitHub" button with a live star count on the landing page.
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), and
  GitHub issue/pull request templates.
- `SECURITY.md` with a private vulnerability-reporting process.
- Release, platform, and license badges in the README, plus a security
  callout and a Contributing section linking to the new files.
- `CHANGELOG.md` following Keep a Changelog.
- Branch protection on `main`: external contributors must go through a
  reviewed pull request.

## [1.0.0] - 2026-07-24

### Added
- Drag-and-drop, click-to-browse, and paste-a-URL (webpage or YouTube)
  input — conversion starts automatically, no separate "Convert" step.
- Auto-save of converted `.md` files straight to the Downloads folder,
  with collision-safe numeric-suffix naming.
- Rendered and raw Markdown preview tabs, with optional "save a copy
  elsewhere" / "export all" actions.
- Advanced settings dialog: plugins, `keep_data_uris`, Azure Document
  Intelligence, Azure Content Understanding, and per-item format hints.
- Light/dark theme support following the system, with a hover-animated
  drop zone.
- English/Spanish (neutral) UI, switchable live from the menu bar.
- Cross-platform support: Windows (`run.bat`), macOS/Linux (`run.sh`);
  "open containing folder" dispatches to `explorer`/`open -R`/`xdg-open`
  per OS.

[Unreleased]: https://github.com/AbdoslamB/inkdoc/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/AbdoslamB/inkdoc/compare/v1.3.0...v2.0.0
[1.3.0]: https://github.com/AbdoslamB/inkdoc/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/AbdoslamB/inkdoc/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/AbdoslamB/inkdoc/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/AbdoslamB/inkdoc/releases/tag/v1.0.0
