# InkDoc Local REST API Reference

A high-throughput local REST API server powered by **FastAPI** and **Uvicorn**, providing programmatic access to InkDoc's 3-engine conversion pipeline (plus Auto routing) without launching the desktop window.

---

## 1. Quick Start

### Starting the Server

```bash
# Recommended: start headless mode using the unified entry point
python main.py --headless --port 13118

# Or launch directly with Uvicorn on local loopback
python -m uvicorn app.server.server:app --host 127.0.0.1 --port 13118

> **Security Note**: By default, InkDoc binds to `127.0.0.1` (loopback). Do not bind to `0.0.0.0` unless deployed inside an isolated private container or behind an authenticated reverse proxy, as binding to all interfaces exposes the API to your local network.
```

### Interactive Documentation & Web Workbench
Once the server is running:
* **Interactive Swagger UI**: [http://localhost:13118/docs](http://localhost:13118/docs)
* **ReDoc Specification**: [http://localhost:13118/redoc](http://localhost:13118/redoc)
* **Web Workbench UI**: [http://localhost:13118/InkDoc](http://localhost:13118/InkDoc) (also aliased at `/MarkItDown`)

---

## 2. API Endpoints

### `POST /convert/file`
Convert an uploaded document to Markdown.

* **Multipart Form Fields**:
  - `file`: The binary document file to convert. Any size is accepted (the UI only warns that files over 100 MB can take longer). The upload is refused with `507` only when the temp disk can't hold its copy, or, for a file over 100 MB, when copying it would leave less than 1 GB free. On Windows a MarkItDown conversion may use at most half of the computer's memory (at least 2 GB; `INKDOC_CONVERSION_MEMORY_LIMIT_MB` overrides it, `0` turns it off); past that it fails with a message saying so.
* **Query Parameters**:
  - `engine` *(string, optional, default: `'markitdown'`)*: Conversion engine choice:
    - `'markitdown'`: Microsoft MarkItDown engine (Office, PDF, audio, images).
    - `'docling'`: IBM Docling AI layout & table extraction engine.
    - `'markit'`: Broad-format specialist (EPUB, YAML, Jupyter Notebooks).
    - `'glm_ocr'` (also `'glm-ocr'`): the downloadable [GLM-OCR](#glm-ocr) model, for PDFs and images only. `409 engine_not_installed` until it is downloaded in Settings.
    - `'auto'`: InkDoc picks the engine per file (see [Auto engine](#auto-engine)) and reports its choice in `auto`.
  - `save_to_downloads` *(bool, optional, default: `false`)*: Automatically save `.md` output to the save folder with collision-safe naming. The save folder is `~/Downloads` unless another folder was chosen in the desktop app's Settings; a request cannot choose it. If the chosen folder is missing or can't be written to, the file is saved to `~/Downloads` and `save_fallback` says why. The file is named after the uploaded file (`report.pdf` → `report.md`).
  - `response_format` *(string, optional, `'text'` | `'json'`, default: `'text'`)*.
  - `enable_plugins` *(bool, optional, default: `false`)*: Enable MarkItDown plugins.
  - `keep_data_uris` *(bool, optional, default: `false`)*: Retain base64 data URIs for images.
  - `check_quality` *(bool, optional)*: Run the [missing-text check](#missing-text-check) on PDFs for this request. Omit it to follow the `quality_check_enabled` setting (on by default); pass `false` to skip it when converting thousands of files.
  - `auto_quality` *(string, optional, `'fast'` | `'balanced'` | `'thorough'`)*: The [table check and page split](#table-check) for this request. Omit it to follow the `auto_quality` setting (`fast` by default: nothing changes). Any other value is refused with `422`.
  - `job_id` *(string, optional)*: A client-generated id (8–100 characters of letters, digits, `-`, `_`, `.` or `:`) for [progress and cancel](#get-convertprogressjob_id--post-convertcanceljob_id).

**cURL Example (Raw Markdown Output)**:
```bash
curl -F "file=@annual_report.pdf" "http://localhost:13118/convert/file?engine=docling"
```

**cURL Example (Auto-save to Downloads & JSON Response)**:
```bash
curl -F "file=@dataset.xlsx" "http://localhost:13118/convert/file?response_format=json&save_to_downloads=true"
```

**JSON response** (`response_format=json`). `auto` is `null` unless `engine=auto`; `quality` is `null` for anything that is not a PDF, or when the check is off; `tables` is `null` unless the [table check](#table-check) ran (`auto_quality` Balanced or Thorough, PDFs only):
```jsonc
{
  "success": true,
  "filename": "report.pdf",
  "engine_requested": "auto",
  "engine_used": "docling",              // always a concrete engine
  "fallback": false,
  "fallback_reason": null,
  "auto": {
    "chosen": "docling",                 // engine whose output was kept
    "routed_to": "markitdown",           // engine Auto tried first
    "reason": "Digital PDF with a full text layer",
    "hint": "",                          // e.g. "Docling would read the 5 scanned pages. Install it in Settings."
    "escalated": true,
    "escalation": {"from": "markitdown", "from_missing_pct": 18, "from_scan_pages": 0,
                   "to": "docling", "kept": "docling", "error": null},
    "segments": null                     // Thorough page split: [{"engine": "docling", "pages": "1-12"}, …]
  },
  "tables": {
    "checked": true, "skipped_reason": "", "pages_checked": [3], "tables_found": 2,
    "repaired": [{"page": 3, "tables": 2, "issue": "merged", "source": "pdf_lines"}],
    "issues": [],                        // tables found wrong and left as they were: {page, tables, issue, message, why}
    "warning": false, "message": "",
    "details": ["Compared 2 tables with the PDF on page 3.",
                "Page 3: rebuilt 2 tables from the PDF's ruling lines (merged); checked against the PDF's text."],
    "figures_filled": 0, "paragraphs_restored": 0, "characters_restored": 0,  // Thorough
    "passes": [{"name": "table_check", "ran": true, "changed": 2, "skipped": ""}]  // engine=auto: each step of the level
  },
  "quality": {
    "checked": true, "coverage": 0.97, "missing_pct": 3,
    "low_pages": [], "scan_pages": [], "image_pages": [1],
    "garbled": false, "page_count": 12, "pages_checked": 11,
    "warning": false, "message": "", "suggestion": null, "skipped_reason": "",
    "details": ["97% of the text layer found in the output across 11 of 12 pages.",
                "Page 1 is a full-page image; any text inside it wasn't extracted."],
    "engine": "docling", "output_tokens": 5120
  },
  "warnings": [],                         // notes that are not failures (GLM-OCR: a page cut short, GPU fallback, save_fallback)
  "markdown": "…",
  "saved_to_downloads": "C:\\Users\\you\\Downloads\\report.md",  // where the file was saved (any save folder), or null
  "save_fallback": null                   // e.g. "Saved to Downloads because the save folder D:\\Notes was not found."
}
```

---

### `POST /convert/url`
Convert a web page or YouTube video to Markdown.

* **Headers**: `Content-Type: application/json`
* **Query Parameters**:
  - `engine` *(string, optional, default: `'markitdown'`)*
  - `response_format` *(string, optional, `'text'` | `'json'`, default: `'text'`)*
* **JSON Request Body**:
  ```json
  {
    "url": "https://en.wikipedia.org/wiki/Markdown",
    "save_to_downloads": true,
    "enable_plugins": false,
    "keep_data_uris": false,
    "engine": "markitdown",
    "check_quality": null,
    "auto_quality": null,
    "job_id": null
  }
  ```
  `engine`, `check_quality`, `auto_quality` and `job_id` work as for `/convert/file`. The response has the same `auto`, `quality` and `tables` fields: a URL that serves a PDF (detected by content, not name) is routed and checked like an uploaded one.

  InkDoc runs none of a page's JavaScript. A page whose body shows no text without it (an app that builds the page in the browser) is converted from the copy it carries for search engines, its schema.org JSON-LD `JobPosting`, `Article`, `Product` or `Event`, and `warnings` says so. Without one, and when the page's `<noscript>` asks for JavaScript, the request is refused with `422` and a sentence saying what to do (print the page to PDF and convert that). Every other page is converted exactly as fetched; YouTube, Wikipedia and Bing pages are never inspected.

**cURL Example**:
```bash
curl -X POST "http://localhost:13118/convert/url?response_format=json" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://en.wikipedia.org/wiki/Markdown", "save_to_downloads": true}'
```

A page that can't be fetched is refused with `502` and a `detail` starting `URL fetch failed:`. When the site itself refuses the request (`401`, `403`, `429`, or a bot check such as Cloudflare's, which answers with `cf-mitigated: challenge`), the detail says so and, except for `429`, how to convert the page anyway: open it in a browser, print it to PDF or save it, and convert that file. InkDoc makes one request, as itself, and never retries or disguises it; the refusal page is not read.

---

### `POST /convert/batch`
Convert multiple files in a single HTTP request.

* **Multipart Form Fields**:
  - `files`: Multiple uploaded files.
* **Query Parameters**:
  - `engine` *(string, optional, default: `'markitdown'`)*: including `'auto'`.
  - `save_to_downloads` *(bool, optional, default: `false`)*
  - `check_quality` *(bool, optional)*
  - `auto_quality` *(string, optional)*
  - `job_id` *(string, optional)*: each file reports progress under `<job_id>-<index>` (0-based).

Each entry in `results` carries the same `engine_*`, `fallback*`, `auto`, `quality`, `tables` and `save_fallback` fields as a single conversion.

**cURL Example**:
```bash
curl -F "files=@document.docx" -F "files=@sheet.xlsx" \
  "http://localhost:13118/convert/batch?save_to_downloads=true"
```

---

### `GET /convert/progress/{job_id}` & `POST /convert/cancel/{job_id}`
Live status and cancellation for a conversion started with a `job_id`. No session token: they only act on the caller's own job, whose id the caller chose.

```bash
curl http://localhost:13118/convert/progress/my-job-0001
```
```json
{"job_id": "my-job-0001", "status": "running", "phase": "retrying",
 "message": "Retrying with Docling…", "engine": "docling",
 "seconds_elapsed": 41.2, "cancel_requested": false}
```
`status` is `running`, `done`, `error` or `cancelled`. Phases are `queued`, `fetching`, `routing`, `starting`, `converting`, `checking`, `retrying` and `saving`. Engines that know their page count (GLM-OCR) also report `page` (pages finished), `pages` and, after the first page, `seconds_remaining_est`; GLM-OCR checks for cancellation between pages. Entries are kept in memory for one hour; an unknown id returns `404`.

`POST /convert/cancel/{job_id}` stops the job before its next phase. Nothing is saved, and the conversion request itself ends with `409 {"detail": "conversion_cancelled"}`. A cancel that arrives before its request is remembered and applied when the request does. A MarkItDown conversion that is the only one running stops at once: its worker process is ended (and has exited, releasing the document, before the request answers). With other MarkItDown conversions running, it finishes in the worker and its result is discarded, as before.

### `POST /convert/warmup`
Start the MarkItDown worker ahead of a conversion. Requires `X-InkDoc-Token`. Returns at once with `{"status": "ready" | "starting" | "unavailable"}`; calling it again is harmless.

MarkItDown runs in a separate worker process that InkDoc starts on demand and stops after 5 minutes without work, so InkDoc's own process never holds MarkItDown's ~95 MB of libraries. The first MarkItDown conversion after a pause waits for that start (1–3 s). The UI calls this endpoint when a conversion looks imminent (a file dragged over the window, the file picker opened, the URL box focused); API clients can call it before a batch. `unavailable` means the worker could not be started, and conversions then run in InkDoc's own process, with the same results. Conversion requests and responses are unchanged either way.

---

### <a id="auto-engine"></a>Auto engine (`engine=auto`)
Auto picks a concrete engine for each file and verifies the result. It never downloads or installs a model: it only uses engines that are on disk right now.

| Input | Engine |
| :--- | :--- |
| PDF with scanned pages (scanner/fax codecs, bilevel or gray full-page images, runs of image pages, scanner software, or mostly image pages) | The best installed OCR engine (GLM-OCR, then Docling); otherwise MarkItDown with an install `hint` |
| PDF with 2+ full-page images making up 20%+ of its pages | The best installed OCR engine |
| Digital PDF, including one with a cover photo | MarkItDown |
| Images (`.png .jpg .jpeg .bmp .tif .tiff .webp`) | The best installed OCR engine, otherwise MarkItDown |
| GIF | GLM-OCR when installed (Docling does not read GIF), otherwise MarkItDown |
| `.yaml .yml .xml` | Markit |
| `.epub .ipynb .rss .atom`, Office, HTML, CSV, JSON, text, audio, ZIP | MarkItDown |
| URL | PDF rules for a PDF, otherwise MarkItDown |

**GLM-OCR time limit.** GLM-OCR is chosen only for a file estimated to finish within 5 minutes on this computer (pages × measured seconds per page, plus a cold start). A longer file goes to Docling instead, and `auto.reason` says so (*"GLM-OCR would take about 25 min on this computer, so Docling was used"*); without Docling it goes to MarkItDown with a `hint` explaining how to run GLM-OCR explicitly. Explicit `engine=glm_ocr` has no limit.

When MarkItDown's result fails the missing-text check and an OCR engine is installed, Auto converts again with it (Docling, or GLM-OCR within its time limit) and keeps the better result (reported in `auto.escalation`); only one file is saved. If Docling fails, Auto falls back to MarkItDown whatever the `fallback_to_markitdown` setting says, and reports it in `fallback`. If the code/formula model is not installed, Auto runs Docling without enrichment and says so in `auto.reason`. Explicit engines never re-run by themselves.

**Page split (`auto_quality=thorough`).** A PDF with both text pages and pages that need OCR is read page by page: Docling for the text pages, GLM-OCR only for the others, joined in page order. GLM-OCR's time limit then counts its pages only. It needs both engines installed, leaves at least two pages to Docling and uses at most eight runs of consecutive pages; otherwise the file is converted whole as above. `auto.segments` lists who read which pages, and `engine_used` is the engine that read the most. A run whose engine fails is read by the next one (GLM-OCR → Docling → MarkItDown), reported in `fallback` and `warnings`.

### <a id="table-check"></a>Table check (`auto_quality`)
`auto_quality` is `fast` (the default: conversions are exactly as before), `balanced` or `thorough`. At Balanced and Thorough, after a PDF conversion InkDoc reads the PDF's own tables (from their ruling lines and the text layer, in the MarkItDown worker) on the pages that draw lines or boxes, plus the pages of any table of four or more columns in the result, and compares them with the Markdown:

- **`engine=auto`**: a table the engine got wrong is replaced by the PDF's own. This covers side-by-side tables merged into one, a table shown as loose lines or as a picture placeholder, a dropped table, shapes in cells left out, and rows or columns that don't match. A table is replaced only when every word of the PDF's text layer inside it lands in one of its cells, and only the lines holding that table's own words are replaced. Each table InkDoc writes is preceded by `<!-- inkdoc:table page=N source=pdf-lines|pdf-words checked=text-layer -->`. A table that can't be verified or located is left as it was and reported in `tables.issues`.
- **Explicit engines**: the tables are only checked. `tables.warning` and `tables.message` say what looks wrong; the Markdown is exactly the engine's.

InkDoc writes tables in plain Markdown. A merged cell's text is repeated in each row and column it covers, and stacked header rows are flattened ("Revenue 2023"). A full-width row at the top becomes a caption line. Shapes and images in a cell are named by what is drawn: `[green shape]`, `[3 blue shapes]`, `[image]`. Borderless tables are split only when an engine merged them and the PDF shows a gutter between their columns. The engine's own cell text is kept. Pages turned by `/Rotate` and scanned pages (no text layer) are not table-checked.

**Thorough, digital PDFs.** A digital PDF that Auto would give to MarkItDown goes to Docling instead (or to GLM-OCR when Docling isn't installed), in parts of 12 pages; `auto.reason` says so. Afterwards, from the PDF's own text:

- Docling's `&amp;`, hyphens and straight quotes are put back to the PDF's `&`, dashes and curly quotes. A word changes only when the PDF spells it one way only.
- The text inside each figure Docling marked `<!-- image -->` is added after the placeholder, as a table for a labelled bar chart: `<!-- inkdoc:figure-text page=N source=pdf-text -->`.
- Rating grids drawn with markers become a table (label, rating, scale such as `○ ○ ○ ● ○ (green)`, `◀`/`▶` for change arrows); `source=pdf-shapes`.
- Whole paragraphs the engine dropped (three or more lines) are put back after the block above them: `<!-- inkdoc:dropped-text page=N source=pdf-text -->`.

`tables.figures_filled`, `tables.paragraphs_restored` and `tables.characters_restored` count these.

**Thorough**, with GLM-OCR installed, also looks for tables without lines on every text page, from the positions of the PDF's words (`source=pdf-words`). Such a table is written only when both of these hold. First, the engine wrote no table there (loose lines, a picture, nothing). Second, GLM-OCR's reading of the same area (`Table Recognition:`) has the same rows and columns, with at least 90% of the cells alike. The written table keeps the PDF's own text and is marked `checked=text-layer,glm-ocr`. At most six tables per file are read a second time, within GLM-OCR's 5-minute limit. Without agreement the table is left as it is, with a line in `tables.details` and no warning.

**Tables on scanned pages (Thorough, with GLM-OCR and the [layout model add-on](#addons)).** On the pages GLM-OCR read that have no text layer, the layout model finds each table and GLM-OCR reads that area on its own (`Table Recognition:`; at most 12 per file, within 5 minutes). The reading takes the place of what GLM-OCR wrote for that table only when it is a full grid, at least 90% of its words are in that place, and it uses no script that place doesn't. In a paragraph of plain lines, only the lines made entirely of the reading's words are replaced, and only when no other line sits between them, so a note or a source line next to the table stays. A table GLM-OCR wrote is replaced only when the readings hold at least 90% of its words (the rest being the same cells read slightly differently) and differ from it. Two tables side by side that GLM-OCR wrote as one become two. Each is marked `<!-- inkdoc:table page=N source=glm-ocr checked=layout,page-reading -->` and listed in `tables.repaired` with `"issue": "scanned", "source": "glm_ocr"`. Headings, comments, code and contents lists (dot leaders) are never taken for a table, nothing is added where GLM-OCR wrote other words, and a reading that is left out gets a line in `tables.details`.

**What each step did (`tables.passes`, `engine=auto`).** One entry per step of the chosen level, in the order they run: `{"name", "ran", "changed", "skipped"}`. `changed` counts what the step changed (pages given to GLM-OCR, characters put back, tables written, figures filled, paragraphs put back); `skipped` says why a step did not run, such as `"needs Docling and GLM-OCR"` or `"switched off"` (the `thorough_passes_off` setting). Balanced has one step, `table_check`. Thorough adds `digital_layout`, `page_split`, `polish`, `rating_grids`, `borderless_tables`, `scanned_tables`, `figure_text` and `dropped_paragraphs`. The list is empty for explicit engines; their report-only table check follows `table_check`, so with that step switched off they return `tables: null`. With Auto, a table whose step is switched off is neither changed nor reported. When every step that uses the table finder is off, `tables.checked` is `false` with `skipped_reason` `"switched_off"`.

### <a id="missing-text-check"></a>Missing-text check
After a PDF conversion, InkDoc compares the Markdown with the PDF's own embedded text layer. It skips pages it cannot trust (image-only pages, OCR'd scans, CJK-heavy pages) and running headers/footers. `quality.warning` is `true` when about 10% or more of the document's text, or 25% of one page, is missing, when scanned pages were left unread by MarkItDown/Markit, or when the output is full of `(cid:N)` font junk. `quality.suggestion` names the engine to try (`docling`, `glm_ocr`, `markitdown`) or the engine to install (`install_docling`, `install_glm_ocr`). A GLM-OCR result with missing text suggests MarkItDown (a vision model can skip a table; the PDF's own text layer is what MarkItDown reads). The check is read-only: it never changes or delays the saved file (explicit engines save first, then check).

---

### `GET /health`
Returns system status, active version, and engine availability.

```bash
curl http://localhost:13118/health
```

**Sample Response**:
```json
{
  "status": "ok",
  "version": "1.0.0",
  "markitdown_version": "0.1.8",
  "docling_available": true,
  "docling_version": "2.128.0",
  "markit_available": true,
  "markit_version": "0.6.1",
  "glm_ocr_available": false,
  "glm_ocr_version": "Not installed"
}
```
`glm_ocr_available` is `true` only when GLM-OCR is downloaded and passed its self-test.

---

### `GET /engines`
Returns status, availability, version, download size, and metadata for all engines.

```bash
curl http://localhost:13118/engines
```

**Sample Response**:
```json
{
  "platform": "windows-x86_64",
  "engines": {
    "markitdown": {
      "name": "MarkItDown",
      "available": true,
      "installed": true,
      "version": "0.1.8",
      "optional": false
    },
    "docling": {
      "name": "Docling",
      "available": true,
      "installed": true,
      "status": "installed",
      "installable": true,
      "supported": true,
      "version": "2.128.0",
      "optional": true,
      "download_size_bytes": 1420000000,
      "disk_size_bytes": 2850000000
    },
    "markit": {
      "name": "Markit",
      "available": true,
      "installed": true,
      "version": "0.6.1",
      "optional": false
    },
    "glm_ocr": {
      "name": "GLM-OCR",
      "status": "installed",            // installable | installing | installed | unsupported | unreleased | defective
      "installed": true,
      "usable": true,                   // installed and passed its self-test
      "version": {"model": "2026.03.10-65a42de1", "runtime": "b11307"},
      "download_size_bytes": 1454102427,
      "accel_in_use": "cpu",            // cpu | vulkan | metal
      "seconds_per_page": 38.6,         // measured on this computer
      "long_job_seconds": 300,
      "gpu": {"kind": "vulkan", "available": true, "installed": false, "selected": false, "reason": ""},
      "update_available": false,
      "progress": {"status": "idle"}
    }
  },
  "settings": {
    "fallback_to_markitdown": false
  }
}
```

---

### `GET /settings` & `POST /settings`
Query or update application engine preferences.

* **Authentication**: Requires header `X-InkDoc-Token: <session_token>` on `POST`.
* **Request Body** (`POST`):
  ```json
  {
    "fallback_to_markitdown": true,
    "quality_check_enabled": true,
    "auto_quality": "fast",
    "thorough_passes_off": [],
    "glm_ocr_download_source": "auto",
    "layout_width": "standard",
    "preview_text_size": 100,
    "render_math": true,
    "theme": "dark",
    "preferred_engine": "auto",
    "hidden_engines": [],
    "auto_save": false,
    "auto_hint_hidden": false
  }
  ```
  `quality_check_enabled` (default `true`) turns the missing-text check on or off for requests that do not pass `check_quality`. `auto_quality` (default `fast`) is the [table check and page split](#table-check) level for requests that do not pass `auto_quality`: `fast`, `balanced` or `thorough`; anything else is refused with `422`. `thorough_passes_off` (default `[]`) lists the [steps](#table-check) of Balanced and Thorough not to run, for every request (there is no per-request override); a name that is not a step, or a list longer than the number of steps, is refused with `422`, and the list is stored once per step, in run order. `GET` also returns `thorough_passes`, every step with its `name`, `title`, `description`, lowest `level`, the engines it needs (`needs_all`, or one of `needs_any`) and `cost`; it is read-only. `glm_ocr_download_source` is `auto` (official source first, InkDoc mirror as fallback) or `mirror_only`. `layout_width` (default `standard`) is how much of a wide window the UI uses: `standard` (up to 1400px), `wide` (up to 1920px) or `full`; any other value is refused with `422`. The UI applies it from the next page load. `preview_text_size` (default `100`) is the document preview's text size in percent: `85`, `90`, `100`, `110`, `120`, `135` or `150`; anything else is refused with `422`. `render_math` (default `true`) draws LaTeX math in the UI preview with the bundled KaTeX; it never changes conversion output, saved files or API responses.

  Interface preferences, applied by the UI before first paint: `theme` (`dark`, the default, or `light`); `preferred_engine` (`auto`, the default, `markitdown`, `docling`, `markit` or `glm_ocr`), the engine selected on the main screen; `hidden_engines`, the engines whose button is hidden on the main screen (any of `markitdown`, `docling`, `markit`, `glm_ocr`; never `auto`; stored once each, in a fixed order); `auto_save` (default `false`), the Auto-save switch; `auto_hint_hidden` (default `false`), "Don't show again" on Auto's install tip. Any other value is refused with `422`. None of them changes what the conversion endpoints do: Auto and explicit `engine` values ignore `hidden_engines`, and only a request's own `save_to_downloads` saves a file. `legacy_prefs_imported` is set by the UI once it has moved these out of the browser storage InkDoc 1.2 used.

  If `settings.json` can't be written (after a short retry, for example while an antivirus scan holds the file), `POST /settings` answers `503` with a message saying the change will be lost when InkDoc restarts; it still applies until then. A successful `POST /settings` returns the stored settings, without the `save_folder`, `addon_reconciliation` and `thorough_passes` fields that `GET` adds.

* **Save folder** (`GET` only): the response includes `save_folder`, for example
  `{"path": "C:\\Users\\you\\Downloads", "chosen": "D:\\Notes", "custom": true, "problem": "This folder was not found. Files are saved to Downloads until it is back."}`.
  `path` is where auto-save writes now; `chosen` is the folder picked in Settings (`""` for Downloads). The save folder can only be changed in the desktop app's folder dialog: `POST /settings` ignores `save_dir`, and no other request can set it.

### `POST /settings/reset`
Restore the default preferences. Requires `X-InkDoc-Token`. Resets `theme`, `preferred_engine`, `hidden_engines`, `auto_save`, `auto_hint_hidden`, `fallback_to_markitdown`, `quality_check_enabled`, `auto_quality`, `thorough_passes_off`, `layout_width`, `preview_text_size`, `render_math` and `check_for_updates_daily` in one write, and returns the same body as `GET /settings`. Installed engines and add-ons and their settings (Docling enrichment, GLM-OCR GPU and download source), the save folder and all files are kept. `503` as above if the file can't be written.

---

### Engine Pack Lifecycle Management

All mutating management endpoints require the `X-InkDoc-Token: <session_token>` header (injected by the desktop / web workbench interface; query string tokens are rejected).

- `POST /engines/{engine_id}/install`: Starts streaming download and safe installation of the engine pack. *(Note: Disabled until official production release hashes are published)*
- `GET /engines/{engine_id}/install/progress`: Polls real-time download and extraction percentage.
- `POST /engines/{engine_id}/install/cancel`: Cancels an active download and cleans up staging files.
- `POST /engines/{engine_id}/verify`: Performs full cryptographic SHA-256 tree verification against the bundled manifest.
- `POST /engines/{engine_id}/remove`: Uninstalls the engine pack and cleans up disk footprint.

### <a id="glm-ocr"></a>GLM-OCR (`engine_id` = `glm_ocr`)

GLM-OCR is downloaded, never bundled, and never downloads anything on its own: every download starts from a user's click (the Settings card first shows a warning with the size and the slower processing time, and asks for confirmation). Each file comes from its official source first (Hugging Face for the model, llama.cpp's GitHub releases for the runtime) and from InkDoc's GitHub mirror if that fails, and is accepted only with the SHA-256 pinned in InkDoc.

- `GET /engines/glm_ocr/status?check_remote=false`: Card state (the same object as in `/engines`). Reads the completion marker only. `check_remote=true` also checks the signed online catalogue once per session.
- `POST /engines/glm_ocr/install` *(token)*: Body `{"gpu": false, "variant": "q8"}` (`variant`: `q8` default, `f16` high precision +0.8 GB). Runs in the background; poll progress.
- `GET /engines/glm_ocr/progress`: `{"status", "percent", "bytes_downloaded", "total_bytes", "source": "upstream"|"mirror", "source_label": "Hugging Face"|"llama.cpp releases"|"InkDoc mirror", "fallback_reason", "message", "error_message", "can_try_mirror", "operation"}`.
- `POST /engines/glm_ocr/cancel` *(token)*: Cancels a download at any point; nothing is left installed.
- `POST /engines/glm_ocr/gpu` *(token)*: Body `{"enabled": true}`. On first enable (Windows/Linux) downloads the Vulkan runtime and self-tests it (`{"status": "started"}`); a GPU that fails or is slower than the CPU stays installed but unselected, with the reason in `gpu.reason`. macOS always uses Metal, falling back to the CPU by itself.
- `POST /engines/glm_ocr/selftest` *(token)*: Runs the self-test again.
- `POST /engines/glm_ocr/update` *(token)*: When `update_available`, downloads only the changed parts, self-tests them, then switches; the old version keeps working until then. Never automatic.
- `POST /engines/glm_ocr/verify` *(token)*: Re-hashes every installed file (the only operation that hashes the 1.4 GB model).
- `POST /engines/glm_ocr/remove` *(token)*: Deletes the completion marker first, then the files.

**`POST /convert/estimate?engine=glm_ocr`** (multipart `file`): pages and estimated time on this computer, converting nothing. `{"pages": 30, "seconds_est": 1150.0, "seconds_per_page": 38.0, "accel": "cpu", "long_job_seconds": 300, "long_job": true}`. The workbench asks before a job where `long_job` is true.

### <a id="addons"></a>Add-ons (`/addons`)

Optional models, downloaded only when the user installs them. Each has a `target`: `docling_pack` installs into the Docling engine pack (the code and formula recognition model, `code_enrichment`), `inkdoc` into InkDoc's own add-on folder beside the engines folder (the layout model for tables on scanned pages, `layout_model`). The Docling engine carries the same layout model; while it is installed, `layout_model` is usable with nothing to download.

- `GET /addons`: `{"addons": [status, …]}`.
- `GET /addons/{name}`: one add-on's status. `installed`, `usable`, `installable`, `available` (published), `catalogue_state` (`published`, `unreleased` or `defective`), `reason` (why it can't be used or installed), `size_bytes`, `license`, `target`, `provided_by_pack` (the Docling engine provides it), `progress` (`{"status", "percent", "bytes_downloaded", "total_bytes", "error_message"}`), and for the Docling pack's add-ons `pack_installed`, `pack_version`, `min_pack_version`, `pack_drift` and `worker_verified`.
- `POST /addons/{name}/install` *(token)*: starts the download in the background; poll the status. `400` with the reason when `installable` is false.
- `POST /addons/{name}/cancel` *(token)*: cancels a download.
- `POST /addons/{name}/verify` *(token)*: re-hashes the files against the catalogue: `{"valid": true, "path": …}` or `{"valid": false, "reason": …}`. For `layout_model` provided by the Docling engine, that copy is checked.
- `POST /addons/{name}/remove` *(token)*: deletes the completion record first, then the files.

---

### Update Management Endpoints

Endpoints for checking, downloading, and applying cryptographically signed application updates. Mutating endpoints require `X-InkDoc-Token: <session_token>` header.

- `GET /update/status`: Returns current update state, active version, latest available version, and platform key.
- `POST /update/check`: Checks GitHub Releases for a signed manifest (`manifest.json`) and verifies Ed25519 signature before parsing.
- `POST /update/download`: Streams verified update asset to `~/Downloads` with Range-resume and hash verification.
- `POST /update/cancel`: Cancels in-progress update download and deletes temporary download files.
- `POST /update/apply`: Applies verified installer (Windows installer only; refused in portable, source, or headless mode, or while conversions are active).
- `POST /update/reveal`: Opens system file manager highlighting the downloaded update asset (Windows portable, macOS, Linux).

---

## 3. Error Handling & Engine Routing Contracts

| Scenario | HTTP Status | Detail / Reason |
| :--- | :--- | :--- |
| Requested engine unsupported on this OS/arch | `400 Bad Request` | `{"detail": "Engine 'docling' is not supported on this platform (...)", "code": "engine_unsupported"}` |
| Requested engine not yet installed | `409 Conflict` | `{"detail": "engine_not_installed: ..."}` (Docling or GLM-OCR) |
| GLM-OCR installed but its self-test did not pass | `409 Conflict` | `{"detail": "engine_not_ready: GLM-OCR did not pass its self-test. ..."}` |
| GLM-OCR given a file that is neither a PDF nor an image | `500` | `{"detail": "Unsupported format: GLM-OCR reads PDFs and images. Use MarkItDown for this file."}` |
| Conversion requested while update is applying | `409 Conflict` | `{"detail": "Update in progress: InkDoc is applying an update and restarting. New conversions are temporarily rejected."}` |
| Conversion cancelled through `POST /convert/cancel/{job_id}` | `409 Conflict` | `{"detail": "conversion_cancelled"}` (nothing was saved) |
| Malformed `job_id` | `400 Bad Request` | `{"detail": "Invalid job_id: ..."}` |
| `engine=auto` with Docling not installed | `200 OK` | Never refused: Auto converts with what is installed |
| Update apply requested while conversions are active | `409 Conflict` | `{"detail": "Cannot apply update while document conversions are in progress. Please wait."}` |
| Update apply requested outside packaged desktop runner | `403 Forbidden` | `{"detail": "Applying updates in-app is only permitted in the packaged desktop application."}` |
| Management request without valid session token header | `403 Forbidden` | `{"detail": "Forbidden: Invalid or missing session token (X-InkDoc-Token header required)."}` |
| Non-loopback Host header (DNS rebinding attempt) | `403 Forbidden` | Plaintext: `"Forbidden: Invalid Host header (DNS rebinding protection)"` |
| Unauthorized Cross-Origin Request | `403 Forbidden` | Plaintext: `"Forbidden: Cross-origin request not allowed"` |

### Response Headers
Conversion responses include engine diagnostic headers:
- `X-Engine-Requested`: The engine specified in the request (e.g., `docling`).
- `X-Engine-Used`: The engine that executed the conversion (e.g., `docling` or `markitdown`).
- `X-Fallback-Occurred`: `true` if and only if explicit fallback was triggered; omitted otherwise.
- `X-Fallback-Reason`: Explanatory message when fallback occurred.
- `X-Auto-Reason`: Why Auto chose its engine (`engine=auto` only).
- `X-Quality-Warning`: `true` or `false` when a PDF was checked for missing text.
- `X-Quality-Missing-Pct`: Estimated share of the PDF's text missing from the output, when measured.
- `X-Tables-Rebuilt`: How many tables InkDoc rebuilt, when the [table check](#table-check) ran: from the PDF's own text, or on scanned pages from GLM-OCR's second reading (`tables.repaired` says which).
- `X-Tables-Warning`: `true` when a table was found wrong and left as it was (always reported for explicit engines).
- `X-Engine-Warnings`: Notes that are not failures (`warnings` in JSON), joined with ` | `.
- `X-Save-Fallback`: Present when auto-save could not use the chosen save folder and saved to `~/Downloads` instead (`save_fallback` in JSON). Like the other text headers it is limited to 200 characters and non-Latin-1 characters become `?`; use `response_format=json` for the exact text.

Reasons are reduced to one line of at most 200 characters.

---

## 4. Python Client Example

A complete client script is available at [`examples/client_example.py`](examples/client_example.py):

```python
import requests

SERVER_URL = "http://localhost:13118"

# 1. Convert local file
with open("sample.pdf", "rb") as f:
    response = requests.post(
        f"{SERVER_URL}/convert/file",
        files={"file": f},
        params={"engine": "markitdown", "save_to_downloads": True}
    )
    print("Converted Markdown:\n", response.text)

# 2. Convert URL
res = requests.post(
    f"{SERVER_URL}/convert/url",
    json={"url": "https://en.wikipedia.org/wiki/Markdown", "save_to_downloads": False}
)
print("URL Markdown:\n", res.text)
```

