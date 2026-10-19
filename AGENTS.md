# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this is

A unified desktop application and local web workbench around Microsoft's `markitdown` Python
package, with optional IBM `docling` layout analysis, broad-format `markit` conversion routes, and the
optional downloadable GLM-OCR model (run by llama.cpp's `llama-server`).
It is **not** a fork of markitdown — every actual file-to-Markdown conversion goes through
the real `markitdown` public API (`MarkItDown.convert_local()` / `convert_uri()`) or the engine adapters in `app/core/engines/`.
The one place InkDoc edits an engine's output is the **table stage** (`app/core/table_stage.py`, "Auto quality"
Balanced/Thorough, Auto only): a table the engine got wrong is replaced by the PDF's own, read from its ruling lines
and text layer, only when verified against that text layer. Every table InkDoc writes says where it came from, in an
HTML comment in the Markdown (`<!-- inkdoc:table page=N source=pdf-lines|pdf-words checked=text-layer[,glm-ocr] -->`)
and in the response's `tables` metadata. Explicit engines are only checked, never changed; at "Fast" (the default) nothing runs.

**Single Source of Truth UI**: Both the desktop executable (`main.py` / `inkdoc.exe`) and the local Web Bench (`http://localhost:13118/InkDoc`) share the **exact same UI** in `app/ui/` and the exact same conversion core in `app/core/`. The desktop app runs natively inside a lightweight Edge WebView2 window via `pywebview` without opening an external browser.

Core UX contract (do not break when changing things): **drop a file,
paste a URL, or click to browse → conversion starts immediately → with
Auto-save on, the resulting `.md` is saved to the save folder (`~/Downloads`
unless another folder is chosen in Settings) with no export step.** The
Auto-save switch is off on a fresh install (as since 1.0.0) and remembered
between launches (`auto_save` in `settings.json`); each request carries it as
`save_to_downloads`.

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
node tests/auto_engine_ui.test.js && node tests/preview_word_count.test.js && node tests/glm_ocr_ui.test.js && node tests/desktop_download_ui.test.js && node tests/save_folder_ui.test.js && node tests/preview_links_ui.test.js && node tests/theme_toggle_ui.test.js && node tests/settings_drawer_ui.test.js && node tests/layout_width_ui.test.js && node tests/text_size_ui.test.js && node tests/math_render_ui.test.js && node tests/preferences_ui.test.js && node tests/engine_visibility_ui.test.js && node tests/reset_defaults_ui.test.js && node tests/tables_ui.test.js && node tests/auto_steps_ui.test.js && node tests/layout_addon_ui.test.js && node tests/support_link_ui.test.js && node tests/release_notes_ui.test.js && node tests/large_file_ui.test.js

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

Any manual local testing that writes into the real `~/Downloads` or the chosen
save folder (e.g. via `auto_save_markdown()`) must clean up its own test files
afterward — nothing test-related should be left in the user's actual folders.

## Architecture

**Unified Domain-Driven Architecture (`app/`)**:

- `app/core/` — Core conversion logic & engine adapters:
  - `converter.py` — Maps options to real `MarkItDown(...)` constructor kwargs. Every MarkItDown conversion goes through `convert_local()`'s MarkItDown branch to `engines/markitdown_worker_client.convert_with_markitdown()`; `run_markitdown()` is the one function that calls `MarkItDown.convert_local()`, run inside the worker (or in-process only when the worker can't start). InkDoc's own process must never import `markitdown` (`tests/test_startup_imports.py`; `get_markitdown_version()` reads package metadata only). `auto_save_markdown()` / `get_downloads_dir()` / `unique_download_path()` implement collision-safe auto-save into a given folder (Downloads by default). The file is created with `O_EXCL`, so a file or link that appears at the chosen name is never written through; the next free name is used, and a failed write removes its own partial file.
  - `queue_model.py` — `EngineKind` enum (`markitdown`, `docling`, `markit`, `glm_ocr`, and `auto`, a routing mode that always resolves to one of the concrete engines), `QueueItem` dataclass.
  - `engines/` — Engine adapters (`docling_engine.py`, `markit_engine.py`).
  - `engines/markitdown_worker.py` / `engines/markitdown_worker_client.py` — The MarkItDown worker: the same executable started with `--markitdown-worker` (handled in `main.py` before the desktop app is imported; `main.py --markitdown-worker` from source). JSON lines over stdin/stdout, stdout moved to a dup'd fd before anything is imported so library output can't corrupt it (`claim_stdout`), stderr to DEVNULL. Results go through private files it creates exclusively, never the pipe; auto-save stays in the server. Besides `convert` it answers `tables` (the table finder's JSON, `find_tables_in_worker()`) and `layout` (the layout model's tables, `find_layout_in_worker()`), so pdfplumber and onnxruntime also stay out of InkDoc's own process. One worker, several conversions on threads; started on the first conversion or `POST /convert/warmup` (one shared start; callers wait on it), stopped after `IDLE_TIMEOUT_S` (5 min, `INKDOC_MARKITDOWN_IDLE_S`) with the idle clock stopped during work. Cancel kills it when it is the only conversion and waits for it to exit (so the document is released before the server deletes its temp copy; Windows refuses while it is open); otherwise the cancelled conversion finishes and is discarded. Results folder: `inkdoc-markitdown-*` in the temp dir, checked before each use (real dir, ours, 0700 on POSIX), also the worker's cwd, result files 0600 with O_EXCL; folders over a day old are removed. A crash fails that conversion (`MarkItDownWorkerCrashedError`) and the next one restarts it; a worker that can't start makes conversions run in-process (`INKDOC_MARKITDOWN_WORKER=0` forces that). Contained by a Windows Job Object (`engines/process_guard.py`), which also caps its memory at half of RAM, at least 2 GB (`conversion_memory_limit()`, `INKDOC_CONVERSION_MEMORY_LIMIT_MB`, `0` = off); a MemoryError, also one MarkItDown wraps, becomes `MarkItDownOutOfMemoryError` with a sentence for the user. On macOS and Linux the worker exits when its stdin closes or its parent pid changes. Not `PR_SET_PDEATHSIG`: Linux sends it when the *thread* that spawned the child exits, and the worker is spawned from server threads that come and go.
  - `pdf_probe.py` — One cheap pdfium pass over a PDF: per-page text layer, image coverage, and the scan-likely classifier that tells scanned pages from cover photos. Also counts vector paths per page from their bounds only (`rule_lines`, `vector_boxes` → `table_pages`), the table stage's filter. Imports `pypdfium2` lazily, never raises (returns `None`), serialises pdfium with a lock.
  - `pdf_pages.py` — `write_page_subset()`: a PDF of some pages (pypdfium2, pdfium lock held, both documents closed) for Auto's page split.
  - `thorough_passes.py` — The registry of "Auto quality" steps (pure, standard library only): one `PassInfo` per step in run order (`digital_layout`, `page_split`, `polish`, `table_check`, `rating_grids`, `borderless_tables`, `scanned_tables`, `figure_text`, `dropped_paragraphs`) with its lowest level, stage, the engines it needs, cost, finder detector and the table kinds it may write. `PassPlan(level, available, disabled)` says which steps are on and why the others are not (`ConversionOptions.passes_off`, from the `thorough_passes_off` setting: Settings → General → Auto quality → Steps; `borderless_tables` needs GLM-OCR, which confirms what it finds; `scanned_tables` needs GLM-OCR and the `layout_model` add-on); `PassLog` records what each did, reported as `tables.passes`. A new step: a `PassInfo` (with `modules`, the modules that do its work), its work in its own module, and a call where its stage runs. Steps are core code; see "Core or add-on" below.
  - `table_stage.py` — The table stage, run after a PDF conversion at the "Auto quality" levels Balanced and Thorough (`auto_quality` setting/request field; Fast, the default, skips it): pages come from the probe's `table_pages` plus the pages of the engine's own tables of 4+ columns (`wide_table_hints`); the finder runs in the MarkItDown worker; Auto repairs, explicit engines are only checked. Never raises; any failure leaves the Markdown unchanged.
  - `table_finder.py` — Runs in the MarkItDown worker (pdfplumber is loaded there; InkDoc's own process never imports it): ruled tables from pdfplumber's line strategy as InkDoc's grid, "verified" when every text-layer word in the table lands in a cell; shapes and images in cells named by fill colour (`[green shape]`, `[3 blue shapes]`); side-by-side tables grouped into bands with the text lines above/below as anchors; `split_merged_table()` splits an engine's borderless wide table at a gutter the PDF shows, keeping the engine's text; `detectors` (from the step plan; `deep` means all of them) add the rest: `segments` gives the page's positioned text (for figure_text), `rating_grids` runs `find_marker_grids()`, which reads rating grids drawn with dots and coloured markers/triangles (`source=pdf_shapes`; replaces any lines-"table" it overlaps; charts' gridlines with under 30% text cells are not tables), and `borderless_tables` runs `find_word_tables()`, which finds borderless tables from word positions (≥3 aligned columns, ≥3 rows; prose, dot-leader contents and two-column forms rejected), always unverified with `confirm=True`. Every table carries its `kind` (`lines`, `hint_split`, `rating_grids`, `borderless_tables`); `table_check.repair(kinds=...)` writes only the kinds whose step is on. Never raises.
  - Thorough's second reading: `table_stage.inspect_tables` → `confirm_tables` (GLM-OCR's lane; `glm_ocr_engine.read_table_region`, prompt `Table Recognition:` on just that area) → `finish_tables`. A word-position table is written only where the engine wrote no table (loose/picture/missing) and only when `grids_agree` (same shape, ≥90% of cells alike); it keeps the PDF's text, marked `checked=text-layer,glm-ocr`. Unconfirmed ones are a detail line, never a warning. At most 6 per file, within 5 minutes.
  - `table_check.py` — Pure: locates each band in the Markdown (a run of lines made only of its words, else between its anchors when its words are nowhere else), compares (same columns and body rows; each column under its own header words), classifies (merged, split, loose, missing, picture, shapes, mismatch) and repairs only verified bands whose located lines hold nothing but the band's own words, never cutting a Markdown table in two.
  - `markdown_tables.py` — Pure: finding pipe and HTML tables in Markdown, and InkDoc's rules for writing them (merged cells repeated, stacked headers flattened, full-width top rows as captions).
  - `scanned_tables.py` — Thorough, "Tables on scanned pages" (needs GLM-OCR and the `layout_model` add-on): on the pages GLM-OCR read that have no text layer, `find_scanned_tables` (the layout model, in MarkItDown's lane) → `read_scanned_tables` (GLM-OCR's `read_table_region` on each area, in GLM-OCR's lane; at most 12, within 5 minutes) → `write_scanned_tables` (pure). A reading takes the place of what GLM-OCR wrote for that table, a table or a paragraph of its words (GLM-OCR often writes a scanned table as plain lines), only when it is a full grid, at least 90% of its words are in that place and it uses no script the place doesn't (NFKC first, so µ, Ω and º are letters). In a paragraph only the lines made entirely of the readings' words are replaced, and only with no other line between them; a table GLM-OCR wrote is replaced only when the readings hold at least 90% of its words and differ from it. A place is never shared by readings of different pages; ties go to the earliest. Words rather than cells, because the structure is what may differ (a shifted stacked header comes back as "Revenue 2023"). Matched by words, since GLM-OCR's output has no page boundaries; headings, comments, code and paragraphs with dot leaders (contents lists) are never taken for a table, and nothing is added where GLM-OCR wrote other words. Marked `<!-- inkdoc:table page=N source=glm-ocr checked=layout,page-reading -->`, reported in `tables.repaired` with `issue: "scanned"`.
  - `engines/layout_model.py` — Runs the layout model (docling-layout-heron, ONNX): in the MarkItDown worker (`layout` action), so onnxruntime, numpy and Pillow never load in InkDoc's own process. Reads the model once, checks those bytes against the pinned digest and loads them (one session per process, at most 4 threads), renders pages at 150 DPI but never over 2000 px on the long side (a document chooses its page size), keeps `table` candidates scoring 0.3 or more, and `clean_up` keeps one box per table (a box holding two side-by-side candidates is dropped). Boxes come back in PDF points from the top-left, in reading order; rotated pages are skipped. It runs an add-on's model, as `glm_ocr_engine.py` does, so it is not a step module.
  - `addon_manager.py` / `addons.json` — Downloadable add-ons. `target` "docling_pack" installs into the Docling pack's models folder (the code/formula model, confirmed by the pack worker); "inkdoc" installs into InkDoc's own `addons/` folder beside `engines/` (the layout model). An "inkdoc" add-on may name a `pack_copy`, the same files inside the Docling pack pinned by SHA-256 (and size, so the status path spots another model without hashing): it is then usable without a download (`provided_by_pack`), and `model_location()` returns InkDoc's own install first, but only while the catalogue pins its files, else that copy. Status, install, verify and remove share one shape for both targets; verify fails when there is nothing to check. `validate_addon_entry` checks `target`, `model_dir` and `pack_copy` in every state, and a published "inkdoc" entry must pin the pack copy's files with the same digests. `scripts/build_layout_addon.py` builds the layout model's archive (reproducible) and catalogue entry from the pinned Hugging Face revision or a local copy, and refuses any model but the pinned one.
  - `figure_text.py` — Pure, Thorough: `recover_figure_text` adds the PDF's text inside each `<!-- image -->` area (located between the PDF lines the engine wrote before and after it; labelled bar charts as a Label/Value or Category/Value table); `recover_dropped_paragraphs` puts back runs of ≥3 missing lines (≥15 words; page headers/footers and the 5% margins excluded) after the block holding the line above them. Only adds lines, each after an `inkdoc:figure-text` / `inkdoc:dropped-text` comment.
  - `text_polish.py` — Pure, Thorough: `&amp;` → `&` (not `&amp;lt;`, not in code) and dashes/quotes an engine simplified, restored from the PDF's text layer only when the PDF has one spelling of the word (never a minus sign, never a bare `-`).
  - `web_shell.py` — Pure, standard library only: web pages that build their content with JavaScript. `open_url_source()` calls `inspect_page()` on every fetched HTML page (no type override): a page whose body shows any text is left alone (the scan stops at the first word); a shell is converted from its schema.org JSON-LD (`JobPosting`, `Article` family, `Product`, `Event`; the readable object with the most text, at least 200 characters) rebuilt as a small HTML page with every value escaped and no script, frame or form, with a note in `warnings` (`FetchedSource.notes`); a shell without, whose short `<noscript>` asks for JavaScript, raises `JavaScriptPageError` (`422` from `/convert/url`). Never inspected: YouTube, Wikipedia and Bing (MarkItDown reads their raw HTML), pages over 5 MB, pages without a `<body>` tag. Any other failure leaves the page as fetched.
  - `save_location.py` — The save folder. Stored as `save_dir` in `settings.json` (`""` = Downloads) and set only by the desktop folder dialog (`choose_save_folder` in `runner.py`); it is deliberately absent from `SettingsPayload`, so no HTTP request can set it. `save_markdown()` writes there and falls back to Downloads with a sentence saying why (missing folder: never re-created; folder or a parent replaced by a link or junction since it was chosen: not followed, detected because the stored path no longer resolves to itself; write error: `OSError`/`ValueError`); the choice is kept. A window that attached to an already-running InkDoc refuses to change it, since that process does the saving. `folder_state()` feeds `GET /settings` and the Download dialog's starting folder.
  - `quality_check.py` — Missing-text check: compares a PDF conversion's Markdown with the PDF's own text layer and builds the user-facing warning and suggestion. Read-only; never fails a conversion.
  - `auto_engine.py` — The Auto engine: pure routing (`decide`, `should_escalate`, `pick_result`, `glm_time_gate`, `AUTO_ROUTES`), OCR availability ("only what is on disk"; GLM-OCR first, then Docling), and the async `run_auto`, which takes each engine's lane *before* using a thread and may re-convert once with the OCR engine. GLM-OCR is used only for files estimated to finish within `GLM_OCR_TIME_LIMIT_S` (5 min); otherwise Docling. At Thorough, a mixed PDF is split by page (`plan_page_split`, `_convert_split`): Docling reads the text pages (as sub-PDFs), GLM-OCR only the pages needing OCR (`ConversionOptions.page_subset`, original page numbers kept), Docling's runs first, joined in page order; needs both engines, ≥2 text pages, ≤8 runs, and GLM-OCR's time limit on its own pages; a failed run falls back GLM-OCR → Docling → MarkItDown and is reported; `auto.segments` lists who read which pages. Also at Thorough, a digital PDF that would go to MarkItDown goes to Docling (`thorough_digital_route`; GLM-OCR if Docling is missing), in 12-page sub-PDF parts for long files (`docling_chunks`, Docling's worker has a 3-minute limit per conversion), and `_polish` (text_polish) runs on the result Auto keeps, after any re-conversion, unless MarkItDown or Markit made it. Then the table stage (Balanced/Thorough) runs in MarkItDown's lane; at Thorough the tables of the scanned pages GLM-OCR read are read again one by one (`_scanned_tables`; `_convert_split(read_by=...)` records which engine read each page of a split), and figure text and dropped paragraphs are added, before the single save. Every one of these steps is asked of a `thorough_passes.PassPlan` built once per conversion (level, the OCR engines installed, `passes_off`), and what it did goes into a `PassLog` (`tables.passes`).
  - `jobs.py` — In-memory job registry behind `/convert/progress/{job_id}` and `/convert/cancel/{job_id}` (phase labels and cooperative cancel; engine-neutral).
  - `glm_ocr_catalogue.py` / `glm_ocr_catalogue.json` — GLM-OCR pins: model files (Hugging Face) and llama.cpp runtime builds per platform, each with sha256 and ordered sources (upstream first, InkDoc mirror second). `validate_glm_catalogue` (UNRELEASED / PUBLISHED / DEFECTIVE) is the one validator used by the app, CI and the signer. A signed remote catalogue (same Ed25519 envelope as the update manifest, `verify_signed_envelope`) can raise the pins without an app release; rollback is blocked by `catalogue_version`. Regenerate the JSON with `scripts/build_glm_ocr_catalogue.py`, never by hand.
  - `glm_ocr_manager.py` — Download (per file: upstream, then mirror; separate partial file per source), selective runtime extraction (only `sha256_files`, symlinks written as regular files), self-test on the staged files, completion marker written LAST, partial updates, GPU (Vulkan) runtime, verify, remove (marker first). Status reads the marker only — never hashes, never touches the network.
  - `engines/glm_ocr_server.py` — `llama-server` lifecycle: exact command line (`--offline`, loopback, per-session `--api-key`), full runtime re-hash before every launch, Windows Job Object / Linux parent-death signal / PID-file reaping, health wait, idle shutdown, crash restart, GPU→CPU and Metal→`-ngl 0` fallbacks, memory coordination with the Docling worker, warm-timed self-test.
  - `engines/glm_ocr_engine.py` — GLM-OCR conversion: PDF pages (pdfium lock held per page) and images (EXIF transpose, transparency flattened, multi-frame TIFF), context budget, repetition guard, per-page progress and cancel through `options.job`.
- `app/server/` — Embedded REST API backend:
  - `server.py` — FastAPI REST API endpoints (`/convert/file`, `/convert/url`, `/convert/batch`, `/convert/progress/{job_id}`, `/convert/cancel/{job_id}`, `/health`, `/extensions`), dual-mounts `/static` and `/ui`. Lanes: Docling (1), GLM-OCR (1), default (2) and a probe lane (1) for pdfium work, all acquired in async code before `asyncio.to_thread`. `/engines/{name}/…` dispatches `glm_ocr` to `GlmOcrManager`; `/convert/estimate` gives GLM-OCR's page count and time estimate. Explicit engines convert → auto-save → check (missing text; tables too at Balanced/Thorough, report only, after the save so a late cancel can't undo it); Auto probes → converts → checks → (maybe re-converts) → table stage → auto-saves once. `auto_quality` comes from the request or the setting, the steps switched off from the `thorough_passes_off` setting (`_auto_quality_options`; names checked against the registry, `422` otherwise), and `GET /settings` adds the registry's step list (`thorough_passes`) for the page's switches. A site that refuses a URL fetch (401, 403, 429, or a bot check: `cf-mitigated: challenge`) raises `UrlBlockedError` (`url_fetcher.py`, a `UrlFetchError`), worded by `url_fetch_failure` with how to get the page through the user's own browser; still `502`. Never retry, disguise the request or solve the check: that would get around the site's access rules. Response metadata is built in one place, `_conversion_payload` (`tables`, and `X-Tables-Rebuilt`/`X-Tables-Warning` headers for text responses). `GET`/`POST /settings` and `POST /settings/reset` (the list of what is reset is `RESETTABLE_SETTINGS` in `engine_manager.py`; never the save folder, engine/add-on setup or `glm_ocr_catalogue_seen`). Saved interface preferences reach the page before first paint in the nonce'd script (`_initial_page_state`: `data-theme`, `data-hidden-engines`, `window.__INKDOC_PREFS__`), only as values checked by `sanitize_ui_preferences`. `POST /settings` and the reset write strictly (`update_settings(..., strict=True)`) and answer `503` if `settings.json` can't be written.
- `app/ui/` — **The single shared web & desktop UI**:
  - `index.html` — Application DOM structure, dropzone, engine pills, live preview pane, Settings drawer (General / Engines / Add-ons / Updates / About; it sits below the header so the title bar stays usable, and every engine prompt opens it on Engines).
  - `style.css` — Inkbench design system: obsidian dark & light themes, glowing engine selector pills, responsive layout.
  - `app.js` — Client-side logic: drag-and-drop, API conversion, live markdown preview (`marked.js`), auto-save toggle, engine switching. Interface preferences (theme, engine choice, hidden engines, auto-save, Auto's tip) live in `settings.json`, never in browser storage (the desktop window clears it at every launch): the `Preferences` block reads them from the page and saves changes in one grouped `POST /settings` after 250 ms, one at a time; `importLegacyPrefs` moves InkDoc 1.2's browser-storage values once. `preferredEngine` is the saved choice, `selectedEngine` the one in use (Auto while the choice is hidden or not usable; automatic changes go through `selectEngine` and are never saved). Hidden pills are hidden by CSS from `data-hidden-engines` on `<html>`, apart from `.is-hidden` (not offered here). Every preview parse goes through `markdownToHtml`: with `render_math` off, or no `$`, `\(` or `\[` in the document, it is the global `marked.parse`, unchanged; otherwise a separate `marked.Marked` instance with the math rules. `typesetMath` lazy-loads KaTeX and draws formulas in slices. The preview draws at most `PREVIEW_MAX_CHARS` (5 MB) of a result (`previewSlice`); a cut Raw view is read-only and Copy/Download take the whole result (`fullMarkdownFor`). Files of any size are accepted; over 100 MB the page only warns (`largeFileNotice`, the row's chip), and the server refuses an upload only when the disk can't hold it (over 100 MB: when less than 1 GB would stay free).
  - `vendor/` — Bundled third-party JS/CSS/fonts (marked, DOMPurify, highlight.js, KaTeX). Never fetched at runtime; every file pinned by SHA-256 in `vendor/VENDOR.json` (`tests/test_vendor_manifest.py`); never line-ending converted (`.gitattributes`).
  - **Add-ons** (Settings section): built-in add-ons are bundled code switched by a setting (math formulas → `render_math`). Downloadable add-ons may only ever be data (models, fonts, dictionaries) through the signed `AddonManager` catalogue, never JavaScript that runs in the page. Its downloadable add-on is the layout model (`layoutAddon*` ids, `/addons/layout_model/*`); the `addon*` ids are the Docling recognition model's card under Engines.
  - `logo.svg`, `favicon.svg` — Brand assets.
- `app/desktop/runner.py` — Desktop application runner:
  - Finds an available local loopback port (`13118` or ephemeral).
  - Starts FastAPI in a background daemon thread (`uvicorn.Server`).
  - Opens a native desktop window via `pywebview` (Microsoft Edge WebView2 on Windows) pointing to `http://127.0.0.1:<port>/InkDoc`.
  - Gives the page nine functions as `pywebview.api.*` through `window.expose(*_page_functions(...))`: title-bar controls, `open_external_link`, `save_markdown` (the Download button's Save dialog), and `choose_save_folder` / `reset_save_folder` (the only way the save folder is set). Never pass an object as `js_api`: pywebview looks up `js_api` attributes by any name the page sends, underscore-named ones included, while exposed functions are found by exact name only. `tests/test_desktop_save.py` checks the nine names match `app.js` and that nothing else is reachable. Methods that write files or settings also refuse any page not served by InkDoc itself.
  - In the desktop app, links inside converted documents and release notes never load in the window (`handleContentLinkClick` in `app.js`): web links open in the system browser, `#` links scroll, everything else is ignored.
  - Handles graceful shutdown when the window is closed.
**Threading and Execution Model**:
- Conversions never block the main loop. In desktop mode, FastAPI runs in a background daemon thread (`uvicorn.Server`) while `pywebview` runs on the main GUI thread.
- Processes: InkDoc's own (server + window), plus on demand the MarkItDown worker, the Docling worker and GLM-OCR's `llama-server`, each stopped when idle and contained so it can't outlive InkDoc. The window no longer prewarms markitdown at load; the UI sends `POST /convert/warmup` on signs of intent instead.
- In the browser/UI layer (`app/ui/app.js`), document conversions are dispatched via asynchronous `fetch()` requests to `/convert/file` or `/convert/url`. Each queue item updates reactively without freezing the user interface.
- Files are collision-safely auto-saved to the save folder (`~/Downloads` by default) by the backend (`save_location.save_markdown()`) without requiring any manual export step.

## Core or add-on

A feature becomes a download (an engine pack or add-on, installed from Settings) only if at least one of these is true:

- it needs model weights or a native runtime (an ML model, `llama-server`, a compiled engine);
- it adds more than 25 MB to the app or slows its startup;
- it needs separate builds per platform;
- its license differs from the core's: anything but a permissive license such as MIT, BSD or Apache-2.0 (for example GPL, LGPL, AGPL, MPL, or a model license that restricts use);
- it uses the network.

Everything else is core code. Docling and GLM-OCR are downloads; the steps of "Auto quality" are core. A step that needs a download stays in core, names the engine in `needs_all` / `needs_any` and is greyed out until it is installed, as "Tables without lines" is with GLM-OCR. The layout model is a download as well (model weights). The code that runs it, `engines/layout_model.py`, belongs to that add-on, as `glm_ocr_engine.py` belongs to GLM-OCR, so it is not a step module; the step that uses it, `scanned_tables.py`, is held to the rule below.

`tests/test_step_imports.py` holds every module a step names in `PassInfo.modules` to this rule. Such a module may import the standard library (but not its network modules, `ctypes` or `subprocess`), InkDoc's own code, and the PDF libraries InkDoc ships (pypdfium2, pdfplumber, pdfminer.six). Those libraries must be imported inside a function and be listed under a non-copyleft license in `LICENSES/python-packages-desktop.txt`. A heavy, unshipped or copyleft import fails CI. That test doesn't measure size or startup time; `tests/test_startup_imports.py` keeps markitdown and its libraries out of InkDoc's own process.

## Known limitations (security)

Accepted for now, each needing write access to the folder involved or a local process:
- The save folder is checked for links a moment before the file is created, and only the file name itself is protected from links at creation (`O_EXCL`); a parent folder swapped in between is still followed.
- The default Downloads folder is checked with `is_symlink()` only, which does not detect Windows junctions.
- After a failed write, the partial file is removed by name.
- `unique_download_path`'s containment check compares path prefixes without a separator (file names are sanitised, so it cannot be escaped today).
- In attach mode the window trusts any loopback server on the port whose `/health` looks like InkDoc's.

## CI/CD (all in `.github/workflows/`)

Interpreters: every bundle ships the Python that `setup-python` installs (`python-version` in
`release.yml` and `ci.yml`, now 3.14); the Docling pack has its own (`scripts/build_pack.py`).
Each autumn, move the bundles to the next minor version while the current one still gets
binary releases (a minor in security-only support stops at its last binary, as `"3.11"`
stopped at 3.11.9), after `scripts/runtime_bench.py` shows no regression. The 3.10 floor for
running from source is tested by `ci.yml`'s `source-floor` job only.

Linux release: built on ubuntu-24.04, so the AppImage needs glibc 2.39 (`MIN_GLIBC` in
`scripts/sign_manifest.py` names the same version; the updater refuses an update the system
can't start). The window is the user's own WebKit2GTK, so the bundle must carry none of GTK,
GLib, WebKit or the C++ runtime: a bundled copy is loaded first and the host's WebKit fails
against it (how 1.2.0's zip broke on every newer distribution). Hence PyGObject from pip
(compiled in CI, `gi.repository.*` excluded from PyInstaller), `scripts/linux_host_libs.py
prune`/`check` after the build (only `libgirepository-2.0` stays), `app/core/linux_host.py`
(GI_TYPELIB_PATH to the host's typelibs, and the host's LD_LIBRARY_PATH for xdg-open), and
`scripts/build_appimage.py` (pinned appimagetool and static runtime). `linux-compat` in
`release.yml` runs the AppImage's smoke test, window included, on ubuntu:24.04, debian:13
and fedora:44.

- `ci.yml` — on every push/PR to `main`: `ruff check .`, `python -m
  compileall`, the tests and the headless smoke test above, matrixed across
  windows-latest/macos-latest/ubuntu-24.04, plus the 3.10 `source-floor` job.
- `release.yml` — on any `vX.Y.Z` tag push: builds a PyInstaller
  `--onedir --windowed` bundle per OS (`--collect-all markitdown
  --collect-all magika`, needed because both do dynamic/plugin-style
  imports PyInstaller's static analysis can't see), packages it (Windows
  installer, onefile and zip; macOS zip; Linux AppImage), smoke-tests every
  artifact and attaches them to the draft GitHub Release.
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
