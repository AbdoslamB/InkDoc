# InkDoc Local REST API Reference

A high-throughput local REST API server powered by **FastAPI** and **Uvicorn**, providing programmatic access to InkDoc's 3-engine conversion pipeline without launching the desktop window.

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
  - `save_to_downloads` *(bool, optional, default: `false`)*: Automatically save `.md` output directly to `~/Downloads` with collision-safe naming.
  - `response_format` *(string, optional, `'text'` | `'json'`, default: `'text'`)*.
  - `enable_plugins` *(bool, optional, default: `false`)*: Enable MarkItDown plugins.
  - `keep_data_uris` *(bool, optional, default: `false`)*: Retain base64 data URIs for images.

**cURL Example (Raw Markdown Output)**:
```bash
curl -F "file=@annual_report.pdf" "http://localhost:13118/convert/file?engine=docling"
```

**cURL Example (Auto-save to Downloads & JSON Response)**:
```bash
curl -F "file=@dataset.xlsx" "http://localhost:13118/convert/file?response_format=json&save_to_downloads=true"
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
    "keep_data_uris": false
  }
  ```

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
  - `engine` *(string, optional, default: `'markitdown'`)*
  - `save_to_downloads` *(bool, optional, default: `false`)*

**cURL Example**:
```bash
curl -F "files=@document.docx" -F "files=@sheet.xlsx" \
  "http://localhost:13118/convert/batch?save_to_downloads=true"
```

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
  "markit_version": "0.6.1"
}
```

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
    "fallback_to_markitdown": true
  }
  ```

---

### Engine Pack Lifecycle Management

All mutating management endpoints require the `X-InkDoc-Token: <session_token>` header (injected by the desktop / web workbench interface; query string tokens are rejected).

- `POST /engines/{engine_id}/install`: Starts streaming download and safe installation of the engine pack. *(Note: Disabled until official production release hashes are published)*
- `GET /engines/{engine_id}/install/progress`: Polls real-time download and extraction percentage.
- `POST /engines/{engine_id}/install/cancel`: Cancels an active download and cleans up staging files.
- `POST /engines/{engine_id}/verify`: Performs full cryptographic SHA-256 tree verification against the bundled manifest.
- `POST /engines/{engine_id}/remove`: Uninstalls the engine pack and cleans up disk footprint.

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
| Requested engine not yet installed | `409 Conflict` | `{"detail": "Engine 'docling' is not installed (...)", "code": "engine_not_installed"}` |
| Conversion requested while update is applying | `409 Conflict` | `{"detail": "Update in progress: InkDoc is applying an update and restarting. New conversions are temporarily rejected."}` |
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

