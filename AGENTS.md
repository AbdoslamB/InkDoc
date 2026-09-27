# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this is

A unified desktop application and local web workbench around Microsoft's `markitdown` Python
package, with optional IBM `docling` layout analysis and broad-format `markit` conversion routes.
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

# Integration tests
python tests/test_server.py

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
  - `queue_model.py` — `EngineKind` enum (`markitdown`, `docling`, `markit`), `QueueItem` dataclass.
  - `engines/` — Engine adapters (`docling_engine.py`, `markit_engine.py`).
- `app/server/` — Embedded REST API backend:
  - `server.py` — FastAPI REST API endpoints (`/convert/file`, `/convert/url`, `/convert/batch`, `/health`, `/extensions`), dual-mounts `/static` and `/ui`.
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
