# Changelog

All notable changes to InkDoc are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.0] - 2026-10-14

### Changed
- **Settings** now opens as a wider panel on the right with five sections:
  **General**, **Engines**, **Add-ons**, **Updates** and **About**. "Check for updates
  daily" has moved to Updates, and About has a short description of InkDoc.
  The panel closes from the gear, the **X**, **Esc** or a click outside it.
  The title bar stays usable while it is open, and dragging a file onto the
  window closes it so the file lands in the drop zone. Prompts to install or
  fix an engine open it on Engines. While it is open, notifications appear
  beside it instead of over its buttons.
- In Settings → General, thin lines now separate the unrelated preferences
  (saving, the Docling fallback and the PDF check), so each description sits
  clearly with its own switch.
- Switching between the dark and light theme now reveals the new theme as
  a circle growing from the theme button, as on the website. Where the
  system's web engine doesn't support it, or "reduce motion" is on, the
  theme switches instantly as before.

### Security
- In the desktop app, links inside converted documents and release notes no
  longer open inside InkDoc's window: web links open in your browser, and
  other links are ignored.
- The page can now call only the nine desktop functions it uses; nothing else
  in the desktop app is reachable from page scripts.
- If the save folder's location can't be checked, files are saved to
  Downloads instead.
- Release notes are shown as plain text if the HTML sanitiser is unavailable.
- The preview's HTML sanitiser, DOMPurify, is updated from 3.1.6 to 3.4.16,
  picking up the upstream security fixes released since.

### Added
- **Save folder** setting (Settings → General): choose where auto-save
  writes, instead of always using Downloads. **Change…** opens your system's
  folder dialog, the choice is remembered, and the desktop **Download** button's
  Save dialog opens there too. If the folder is missing or can't be written to
  (for example an unplugged drive), files are saved to Downloads and InkDoc
  says so; the folder is used again once it is back. The folder can only be
  chosen in the desktop app: no web page or API request can change it. API
  responses report a fallback in the new `save_fallback` field
  ([#6](https://github.com/AbdoslamB/InkDoc/issues/6)).
- **Layout width** setting (Settings → General → Display): **Standard** (up to
  1,400 px, as before), **Wide** (up to 1,920 px) or **Full** (the whole
  window). On large screens, tables, code and images get the extra room while
  paragraphs keep a readable line length; windows narrower than 1,400 px look
  the same with any choice. It is remembered between launches of the desktop
  app and applied before the window first draws, so the layout never jumps.
  `GET`/`POST /settings` report and accept it as `layout_width`.
- **Preview text size** (Settings → General → Display): **A−** and **A+** make
  the converted document's text smaller or larger in seven steps (85% to 150%),
  in both the Rendered and Raw tabs, with a live sample right below the
  buttons. Headings, code and tables keep their proportions, and 100% is
  exactly the size used before. It is remembered between launches like the
  layout width; `GET`/`POST /settings` use `preview_text_size` (85, 90, 100,
  110, 120, 135 or 150).
- **Math formulas** in the preview, and a new **Settings → Add-ons** section
  where it can be switched off. LaTeX from Docling's formula recognition and
  GLM-OCR (`$…$`, `$$…$$`, `\(…\)`, `\[…\]`) is drawn as real formulas
  with KaTeX instead of showing as mangled text. KaTeX is bundled, works
  offline and is loaded only for documents that contain math, so other
  documents, startup and saved files are unchanged. Prices such as `$5` and
  code are left alone; if ordinary text with `$` signs is ever shown as math,
  turn **Math formulas** off. `GET`/`POST /settings` use `render_math`.

### Fixed
- A wide table in the preview now scrolls sideways on its own instead of
  moving the whole document, and large pictures shrink to fit the preview.
- Every switch in Settings can now be reached with Tab and flipped with
  Space, and keyboard focus in Settings is visible on macOS and Linux too.

## [1.1.1] - 2026-10-14

### Fixed
- The **Download** button in the desktop app now opens a Save dialog (starting
  in Downloads) and writes the `.md` file where you choose. It previously did
  nothing while showing "Downloaded", because the desktop window blocks
  in-page downloads. The message now only reports a save that actually
  happened. The browser version is unchanged
  ([#6](https://github.com/AbdoslamB/InkDoc/issues/6)).

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

[Unreleased]: https://github.com/AbdoslamB/InkDoc/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/AbdoslamB/InkDoc/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/AbdoslamB/InkDoc/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/AbdoslamB/InkDoc/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/AbdoslamB/InkDoc/releases/tag/v1.0.0
