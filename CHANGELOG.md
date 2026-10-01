# Changelog

All notable changes to InkDoc are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-10-01

### Added
- **GLM-OCR engine**, an optional download (~1.45 GB) from Settings → Processing
  Engines: the zai-org GLM-OCR model (MIT), run locally by llama.cpp's
  `llama-server` (MIT), for dense scans, phone photos of documents, math and
  complex tables. Clicking **Download** first shows a warning with the download
  size and the slower processing time, and nothing is downloaded until it is
  confirmed. Files come from the official source first (Hugging Face, llama.cpp's
  GitHub releases) and from InkDoc's own GitHub mirror if that fails for any
  reason, including a wrong hash; every file is checked against SHA-256 hashes
  pinned in InkDoc, and the runtime is re-hashed before every launch. The install
  only completes once the model reads a test image on the computer, which also
  measures its speed there. Optional GPU acceleration (Vulkan on Windows/Linux for
  NVIDIA, AMD and Intel graphics, Metal on Apple Silicon) is kept only when it
  beats the CPU. Documents never leave the computer: the server is loopback-only
  with a per-session key and has downloads disabled. Also: a precision choice
  (Q8, or F16 for +0.8 GB), Verify, Remove, Self-test again, "InkDoc mirror only"
  for networks that block Hugging Face, and "Update available" when newer tested
  pins exist (never automatic). API: `engine=glm_ocr`, `/engines/glm_ocr/…`,
  `POST /convert/estimate`, `glm_ocr_available` in `/health`, and a `warnings`
  field (`X-Engine-Warnings`).
- **Per-page progress for GLM-OCR**: the queue shows *"Page 7 / 30 · ~8 min left"*,
  Cancel stops before the next page and saves nothing, and InkDoc asks before a
  GLM-OCR job longer than 5 minutes (once for a whole folder drop). A speed hint
  under the pills shows the measured seconds per page.
- **Auto uses GLM-OCR** for scans and images when it is installed and the file
  should finish within 5 minutes on this computer; otherwise it uses Docling, and
  says why. GIF images, which Docling cannot read, go to GLM-OCR when installed.
- A newer *signed* GLM-OCR catalogue published on GitHub can bring newer tested
  model and runtime pins without an app release (Ed25519, same keys as app
  updates, rollback blocked), checked only when the GLM-OCR card is opened.
- The Windows uninstaller asks whether to also remove downloaded engines and
  models (Docling, GLM-OCR), showing their size, and keeps them by default.
- **Auto engine**, a fourth pill and the default for new users (a saved engine
  choice is kept; the API default stays `markitdown`). Auto picks the engine per
  file with a quick check of the PDF's text layer: MarkItDown for digital
  documents, Docling for scanned pages and images when it is installed, and Markit
  for YAML and XML. If MarkItDown's result is missing text and Docling is
  installed, Auto converts again with Docling and keeps the better result. Every
  choice is reported in the queue, the preview and the API (`engine=auto`, `auto`
  field, `X-Auto-Reason`). Auto only uses engines already on disk: it never
  downloads a model, and suggests installing Docling instead.
- **Missing-text check** for PDFs, with every engine and for URLs that serve a PDF.
  The result is compared with the PDF's own text, and a warning names the likely
  cause (scanned pages, unreadable font encoding, a dropped table) with a
  one-click "Re-convert with …" fix and a Details list. An empty scanned PDF now
  says it is scanned instead of a bare "Empty". The check never changes or delays
  the saved file, can be turned off in Settings ("Check PDFs for missing text"),
  and is available to API clients as the `quality` field and the `check_quality`
  option.
- Live progress and Cancel for conversions: the queue shows each phase
  ("Checking…", "Retrying with Docling…"), and Cancel stops before the next phase
  without saving anything. API: optional `job_id`,
  `GET /convert/progress/{job_id}` and `POST /convert/cancel/{job_id}`.
- A summary toast when a multi-file drop finishes, with the number of warnings
  and failures.
- `scripts/calibrate_quality_check.py` measures the check and Auto's routes on a
  local document corpus.

### Changed
- The engine pills form a proper radio group: arrow keys move the selection, and
  screen readers announce the checked engine. Warnings are announced politely and
  carry text, not just an icon or colour.
- Re-converting one item with another engine (Retry, Switch Engine, or a warning's
  fix) no longer changes the engine selected for new files.
- Re-converting a URL with Switch Engine now reports the engine used, fallbacks
  and warnings, like any other conversion.
- `engine_used` (and `X-Engine-Used`) now names the engine that actually produced
  the result. A file the Markit route hands to MarkItDown (PDF and Office files on
  Windows) is reported as `markitdown`, where 1.0.0 reported `markit`.
- `/convert/url` downloads the page before taking a conversion slot, so a slow
  download no longer holds up other conversions.
- `pypdfium2` is listed in `requirements.txt` (it was already installed and
  bundled through MarkItDown's PDF support). So is `Pillow`, which GLM-OCR uses
  directly (also already bundled).
- Missing-text warnings name whichever OCR engine is installed or installable, so
  a GLM-OCR result with missing text suggests MarkItDown rather than itself.
- The engine processes (Docling worker, GLM-OCR server) are stopped when the window
  closes or `--headless` is stopped.

### Fixed
- Files converted through the app or `/convert/file` were saved to Downloads under
  the server's temporary name (`tmpab12cd.md`) instead of their own (`report.md`).
- A URL download that failed part way (size cap, timeout) left its temporary file
  behind.
- Every HTTP 409 was reported as "Docling is not installed", including "update in
  progress".
- Clicking the Docling pill before Docling is installed opened Settings and closed
  it again straight away.
- An earlier toast's timer could hide a newer toast early.
- Uninstalling on Windows left the downloaded Docling pack (~1 GB) behind with no
  way to remove it short of deleting the folder by hand.
- The release workflow finds the Inno Setup compiler wherever version 6 or 7 is
  installed, and CI now compiles `installer.iss` on every run.
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

[Unreleased]: https://github.com/AbdoslamB/InkDoc/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/AbdoslamB/InkDoc/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/AbdoslamB/InkDoc/releases/tag/v1.0.0
