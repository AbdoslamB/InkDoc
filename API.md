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
  - `file`: The binary document file to convert.
* **Query Parameters**:
  - `engine` *(string, optional, default: `'markitdown'`)*: Conversion engine choice:
    - `'markitdown'`: Microsoft MarkItDown engine (Office, PDF, audio, images).
    - `'docling'`: IBM Docling AI layout & table extraction engine.
    - `'markit'`: Broad-format specialist (EPUB, YAML, Jupyter Notebooks).
    - `'glm_ocr'` (also `'glm-ocr'`): the downloadable [GLM-OCR](#glm-ocr) model, for PDFs and images only. `409 engine_not_installed` until it is downloaded in Settings.
    - `'auto'`: InkDoc picks the engine per file (see [Auto engine](#auto-engine)) and reports its choice in `auto`.
  - `save_to_downloads` *(bool, optional, default: `false`)*: Automatically save `.md` output directly to `~/Downloads` with collision-safe naming. The file is named after the uploaded file (`report.pdf` → `report.md`).
  - `response_format` *(string, optional, `'text'` | `'json'`, default: `'text'`)*.
  - `enable_plugins` *(bool, optional, default: `false`)*: Enable MarkItDown plugins.
  - `keep_data_uris` *(bool, optional, default: `false`)*: Retain base64 data URIs for images.
  - `check_quality` *(bool, optional)*: Run the [missing-text check](#missing-text-check) on PDFs for this request. Omit it to follow the `quality_check_enabled` setting (on by default); pass `false` to skip it when converting thousands of files.
  - `job_id` *(string, optional)*: A client-generated id (8–100 characters of letters, digits, `-`, `_`, `.` or `:`) for [progress and cancel](#get-convertprogressjob_id--post-convertcanceljob_id).

**cURL Example (Raw Markdown Output)**:
```bash
curl -F "file=@annual_report.pdf" "http://localhost:13118/convert/file?engine=docling"
```

**cURL Example (Auto-save to Downloads & JSON Response)**:
```bash
curl -F "file=@dataset.xlsx" "http://localhost:13118/convert/file?response_format=json&save_to_downloads=true"
```

**JSON response** (`response_format=json`). `auto` is `null` unless `engine=auto`; `quality` is `null` for anything that is not a PDF, or when the check is off:
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
                   "to": "docling", "kept": "docling", "error": null}
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
  "warnings": [],                         // notes that are not failures (GLM-OCR: a page cut short, GPU fallback)
  "markdown": "…",
  "saved_to_downloads": "C:\\Users\\you\\Downloads\\report.md"
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
    "job_id": null
  }
  ```
  `engine`, `check_quality` and `job_id` work as for `/convert/file`. The response has the same `auto` and `quality` fields: a URL that serves a PDF (detected by content, not name) is routed and checked like an uploaded one.

**cURL Example**:
```bash
curl -X POST "http://localhost:13118/convert/url?response_format=json" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://en.wikipedia.org/wiki/Markdown", "save_to_downloads": true}'
```

---

### `POST /convert/batch`
Convert multiple files in a single HTTP request.

* **Multipart Form Fields**:
  - `files`: Multiple uploaded files.
* **Query Parameters**:
  - `engine` *(string, optional, default: `'markitdown'`)*: including `'auto'`.
  - `save_to_downloads` *(bool, optional, default: `false`)*
  - `check_quality` *(bool, optional)*
  - `job_id` *(string, optional)*: each file reports progress under `<job_id>-<index>` (0-based).

Each entry in `results` carries the same `engine_*`, `fallback*`, `auto` and `quality` fields as a single conversion.

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

`POST /convert/cancel/{job_id}` stops the job before its next phase. Nothing is saved, and the conversion request itself ends with `409 {"detail": "conversion_cancelled"}`. A cancel that arrives before its request is remembered and applied when the request does.

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
  "markitdown_version": "0.1.7",
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
      "version": "0.1.7",
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
    "glm_ocr_download_source": "auto"
  }
  ```
  `quality_check_enabled` (default `true`) turns the missing-text check on or off for requests that do not pass `check_quality`. `glm_ocr_download_source` is `auto` (official source first, InkDoc mirror as fallback) or `mirror_only`.

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
- `X-Engine-Warnings`: Notes that are not failures (`warnings` in JSON), joined with ` | `.

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

