# InkDoc Local REST API Reference

A high-throughput local REST API server powered by **FastAPI** and **Uvicorn**, providing programmatic access to InkDoc's 3-engine conversion pipeline without launching the desktop window.

---

## 1. Quick Start

### Starting the Server

```bash
# Recommended: start headless mode using the unified entry point
python main.py --headless --port 13118

# Or launch directly with Uvicorn
python -m uvicorn app.server.server:app --host 0.0.0.0 --port 13118
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

### `GET /extensions`
Returns an array of all file extensions supported for conversion.

```bash
curl http://localhost:13118/extensions
```

---

## 3. Python Client Example

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
