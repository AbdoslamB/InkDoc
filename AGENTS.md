# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this is

A unified desktop application and local web workbench around Microsoft's `markitdown` Python
package, with optional IBM `docling` layout analysis, broad-format `markit` conversion routes, and the
optional downloadable GLM-OCR model (run by llama.cpp's `llama-server`).
It is **not** a fork of markitdown and contains no conversion logic of its own — every actual file-to-Markdown conversion goes through
the real `markitdown` public API (`MarkItDown.convert_local()` / `convert_uri()`) or the engine adapters in `app/core/engines/`.

**Single Source of Truth UI**: Both the desktop executable (`main.py` / `inkdoc.exe`) and the local Web Bench (`http://localhost:13118/InkDoc`) share the **exact same UI** in `app/ui/` and the exact same conversion core in `app/core/`. The desktop app runs natively inside a lightweight Edge WebView2 window via `pywebview` without opening an external browser.

Core UX contract (do not break when changing things): **drop a file,
paste a URL, or click to browse → conversion starts immediately → the
resulting `.md` is auto-saved to `~/Downloads` with no export step.**

## Commands

Run from the repo root, using whatever interpreter has `requirements.txt`
installed:

```bash
python -m pip install -r requirements.txt   # install deps
python main.py                              # run the desktop application
python main.py --headless                   # run API server only without window
./run.sh                                     # macOS/Linux launcher
run.bat                                      # Windows launcher
```

Verification is done by:

```bash
# Syntax check sources
python -m compileall -q main.py app tests examples

# Headless smoke test — verifies CLI and local health endpoint (see .github/workflows/ci.yml)
python main.py --headless --port 13199 &
python -c "import urllib.request, time; time.sleep(2); res = urllib.request.urlopen('http://127.0.0.1:13199/health'); assert res.getcode() == 200; print('OK')"

# Test suite (CI runs exactly this) and the UI unit tests (Node, no npm)
python -m pytest tests/ -q
node tests/auto_engine_ui.test.js && node tests/preview_word_count.test.js && node tests/glm_ocr_ui.test.js

# Lint (also run in CI)
pip install ruff
ruff check .
ruff check . --fix
```

When testing real end-to-end conversion behavior, `app/core/converter.py`'s
`convert_item()` can be called directly against a scratch file without spinning up the GUI:

```python
from app.core.converter import convert_item, ConversionOptions
from app.core.queue_model import QueueItem, SourceKind
item = QueueItem(source="/path/to/file.txt", kind=SourceKind.FILE, display_name="file.txt")
print(convert_item(item, ConversionOptions()))
```

Any manual local testing that writes into the real `~/Downloads` (e.g. via
`auto_save_markdown()`) must clean up its own test files afterward —
nothing test-related should be left in the user's actual Downloads folder.

## Architecture

**Unified Domain-Driven Architecture (`app/`)**:

- `app/core/` — Core conversion logic & engine adapters:
  - `converter.py` — Maps options to real `MarkItDown(...)` constructor kwargs. Dispatches `convert_local()` or `convert_uri()`. `auto_save_markdown()` / `get_downloads_dir()` / `unique_download_path()` implement collision-safe Downloads auto-save.
  - `queue_model.py` — `EngineKind` enum (`markitdown`, `docling`, `markit`, `glm_ocr`, and `auto`, a routing mode that always resolves to one of the concrete engines), `QueueItem` dataclass.
  - `engines/` — Engine adapters (`docling_engine.py`, `markit_engine.py`).
  - `pdf_probe.py` — One cheap pdfium pass over a PDF: per-page text layer, image coverage, and the scan-likely classifier that tells scanned pages from cover photos. Imports `pypdfium2` lazily, never raises (returns `None`), serialises pdfium with a lock.
  - `quality_check.py` — Missing-text check: compares a PDF conversion's Markdown with the PDF's own text layer and builds the user-facing warning and suggestion. Read-only; never fails a conversion.
  - `auto_engine.py` — The Auto engine: pure routing (`decide`, `should_escalate`, `pick_result`, `glm_time_gate`, `AUTO_ROUTES`), OCR availability ("only what is on disk"; GLM-OCR first, then Docling), and the async `run_auto`, which takes each engine's lane *before* using a thread and may re-convert once with the OCR engine. GLM-OCR is used only for files estimated to finish within `GLM_OCR_TIME_LIMIT_S` (5 min); otherwise Docling.
  - `jobs.py` — In-memory job registry behind `/convert/progress/{job_id}` and `/convert/cancel/{job_id}` (phase labels and cooperative cancel; engine-neutral).
  - `glm_ocr_catalogue.py` / `glm_ocr_catalogue.json` — GLM-OCR pins: model files (Hugging Face) and llama.cpp runtime builds per platform, each with sha256 and ordered sources (upstream first, InkDoc mirror second). `validate_glm_catalogue` (UNRELEASED / PUBLISHED / DEFECTIVE) is the one validator used by the app, CI and the signer. A signed remote catalogue (same Ed25519 envelope as the update manifest, `verify_signed_envelope`) can raise the pins without an app release; rollback is blocked by `catalogue_version`. Regenerate the JSON with `scripts/build_glm_ocr_catalogue.py`, never by hand.
  - `glm_ocr_manager.py` — Download (per file: upstream, then mirror; separate partial file per source), selective runtime extraction (only `sha256_files`, symlinks written as regular files), self-test on the staged files, completion marker written LAST, partial updates, GPU (Vulkan) runtime, verify, remove (marker first). Status reads the marker only — never hashes, never touches the network.
  - `engines/glm_ocr_server.py` — `llama-server` lifecycle: exact command line (`--offline`, loopback, per-session `--api-key`), full runtime re-hash before every launch, Windows Job Object / Linux parent-death signal / PID-file reaping, health wait, idle shutdown, crash restart, GPU→CPU and Metal→`-ngl 0` fallbacks, memory coordination with the Docling worker, warm-timed self-test.
  - `engines/glm_ocr_engine.py` — GLM-OCR conversion: PDF pages (pdfium lock held per page) and images (EXIF transpose, transparency flattened, multi-frame TIFF), context budget, repetition guard, per-page progress and cancel through `options.job`.
- `app/server/` — Embedded REST API backend:
  - `server.py` — FastAPI REST API endpoints (`/convert/file`, `/convert/url`, `/convert/batch`, `/convert/progress/{job_id}`, `/convert/cancel/{job_id}`, `/health`, `/extensions`), dual-mounts `/static` and `/ui`. Lanes: Docling (1), GLM-OCR (1), default (2) and a probe lane (1) for pdfium work, all acquired in async code before `asyncio.to_thread`. `/engines/{name}/…` dispatches `glm_ocr` to `GlmOcrManager`; `/convert/estimate` gives GLM-OCR's page count and time estimate. Explicit engines convert → auto-save → check; Auto probes → converts → checks → (maybe re-converts) → auto-saves once. Response metadata is built in one place, `_conversion_payload`.
- `app/ui/` — **The single shared web & desktop UI**:
  - `index.html` — Application DOM structure, dropzone, engine pills, live preview pane, settings popover.
  - `style.css` — Inkbench design system: obsidian dark & light themes, glowing engine selector pills, responsive layout.
  - `app.js` — Client-side logic: drag-and-drop, API conversion, live markdown preview (`marked.js`), auto-save toggle, engine switching.
  - `logo.svg`, `favicon.svg` — Brand assets.
- `app/desktop/runner.py` — Desktop application runner:
  - Finds an available local loopback port (`13118` or ephemeral).
  - Starts FastAPI in a background daemon thread (`uvicorn.Server`).
  - Opens a native desktop window via `pywebview` (Microsoft Edge WebView2 on Windows) pointing to `http://127.0.0.1:<port>/InkDoc`.
  - Handles graceful shutdown when the window is closed.
**Threading and Execution Model**:
- Conversions never block the main loop. In desktop mode, FastAPI runs in a background daemon thread (`uvicorn.Server`) while `pywebview` runs on the main GUI thread.
- In the browser/UI layer (`app/ui/app.js`), document conversions are dispatched via asynchronous `fetch()` requests to `/convert/file` or `/convert/url`. Each queue item updates reactively without freezing the user interface.
- Files are collision-safely auto-saved to `~/Downloads` by the backend (`auto_save_markdown()`) without requiring any manual export step.

## CI/CD (all in `.github/workflows/`)

- `ci.yml` — on every push/PR to `main`: `ruff check .`, `python -m
  compileall`, and the headless smoke test above, matrixed across
  windows-latest/macos-latest/ubuntu-latest.
- `release.yml` — on any `vX.Y.Z` tag push: builds a PyInstaller
  `--onedir --windowed` bundle per OS (`--collect-all markitdown
  --collect-all magika`, needed because both do dynamic/plugin-style
  imports PyInstaller's static analysis can't see), zips it, and attaches
  it to the GitHub Release via `softprops/action-gh-release`.
- `dependabot.yml` — weekly PRs for `pip` and `github-actions` deps.

Branch protection is enabled on `main` (required PR review) but
`enforce_admins` is `false` — the repo owner can still push directly;
external contributors cannot.

Version bumps follow semver in `CHANGELOG.md` (Keep a Changelog format):
move `[Unreleased]` entries into a new `[X.Y.Z] - date` section, update
the comparison links at the bottom of the file, then `git tag vX.Y.Z &&
git push origin vX.Y.Z` to trigger `release.yml`.

## Landing page (`docs/`)

`docs/index.html` is a single self-contained file (inline CSS/JS, no
build step, no external script/font dependencies) served by GitHub Pages
from `/docs` on `main`. It mirrors — but is not generated from — the
README; update both when a user-facing feature changes. Respects
`prefers-color-scheme` plus a `data-theme` override, and fetches a live
GitHub star count client-side (`fetch` against the public GitHub REST
API, no auth, degrades silently if it fails/rate-limits).
