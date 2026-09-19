"""Standalone Local REST API server for InkDoc Multi-Engine Workbench.

Provides high-speed HTTP endpoints for converting files, URLs, and batches to Markdown
without launching the desktop window.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Ensure repository root is on sys.path so app.core modules can be imported
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import APIRouter, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from app.core.converter import (
        SUPPORTED_EXTENSIONS,
        ConversionOptions,
        auto_save_markdown,
        convert_item,
        get_markitdown_version,
    )
    from app.core.engines.docling_engine import get_docling_version, is_docling_available
    from app.core.engines.markit_engine import get_markit_version, is_markit_available
    from app.core.queue_model import EngineKind, QueueItem, SourceKind
except ImportError as err:
    raise RuntimeError(
        f"Failed to import app.core modules from {REPO_ROOT}. "
        "Ensure requirements are installed."
    ) from err

app = FastAPI(
    title="InkDoc Local API",
    description=(
        "High-performance local REST API server for InkDoc multi-engine document-to-markdown workbench. "
        "Converts PDF, Office documents, images, audio, and web URLs directly to Markdown."
    ),
    version="1.0.0",
)

# Enable CORS for local web applications / frontend integrations
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_ui_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundle_dir = Path(sys._MEIPASS)
        for candidate in [bundle_dir / "app" / "ui", bundle_dir / "ui"]:
            if candidate.exists():
                return candidate
    # When running from source: app/server/../../app/ui -> app/ui
    candidate = Path(__file__).resolve().parent.parent / "ui"
    if candidate.exists():
        return candidate
    return Path(__file__).resolve().parent / "static"


UI_DIR = _resolve_ui_dir()
if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")
    app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")
    app.mount("/InkDoc/static", StaticFiles(directory=str(UI_DIR)), name="static_inkdoc")
    app.mount("/InkDoc/ui", StaticFiles(directory=str(UI_DIR)), name="ui_inkdoc")
    app.mount("/MarkItDown/static", StaticFiles(directory=str(UI_DIR)), name="static_markitdown")
    app.mount("/MarkItDown/ui", StaticFiles(directory=str(UI_DIR)), name="ui_markitdown")


api_router = APIRouter()


def resolve_engine(engine_name: str) -> EngineKind:
    norm = engine_name.lower().strip()
    if norm == "docling":
        return EngineKind.DOCLING
    if norm == "markit":
        return EngineKind.MARKIT
    return EngineKind.MARKITDOWN


class UrlConvertRequest(BaseModel):
    url: str = Field(..., description="Web page or YouTube URL to convert to Markdown")
    save_to_downloads: bool = Field(
        default=False,
        description="Whether to also save a copy of the markdown to the user's Downloads folder",
    )
    enable_plugins: bool = Field(
        default=False, description="Enable MarkItDown third-party plugins"
    )
    keep_data_uris: bool = Field(
        default=False, description="Keep data URIs in HTML conversions"
    )
    engine: str = Field(
        default="markitdown",
        description="Conversion engine route: 'markitdown' (default), 'docling', or 'markit'",
    )


def _serve_bench_html() -> HTMLResponse:
    index_file = UI_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(index_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="Web UI static files not found.")


@app.get("/InkDoc", tags=["Web UI"], response_class=HTMLResponse)
@app.get("/InkDoc/", tags=["Web UI"], response_class=HTMLResponse)
@app.get("/MarkItDown", tags=["Web UI"], response_class=HTMLResponse, include_in_schema=False)
@app.get("/MarkItDown/", tags=["Web UI"], response_class=HTMLResponse, include_in_schema=False)
def inkdoc_web_bench():
    """Main Web Bench UI view at /InkDoc."""
    return _serve_bench_html()


@app.get("/InkDoc/docs", include_in_schema=False)
@app.get("/MarkItDown/docs", include_in_schema=False)
def inkdoc_docs_redirect():
    """Redirect to Swagger UI."""
    return RedirectResponse(url="/docs")


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/InkDoc/favicon.ico", include_in_schema=False)
@app.get("/MarkItDown/favicon.ico", include_in_schema=False)
def favicon():
    """Serve product SVG favicon."""
    fav_path = UI_DIR / "favicon.svg"
    if fav_path.exists():
        return FileResponse(fav_path, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="Favicon not found.")


@app.get("/app", tags=["Web UI"], response_class=HTMLResponse)
def web_bench_view():
    """Direct URL for the MarkItDown Web Bench UI."""
    return _serve_bench_html()


@app.get("/", tags=["Info"])
def root(request: Request):
    """Server status or Web Bench UI if accessed from a web browser."""
    accept = request.headers.get("accept", "")
    # If request is coming from a web browser, serve the interactive Web Bench
    if "text/html" in accept:
        return _serve_bench_html()

    return {
        "app": "InkDoc Local API",
        "status": "running",
        "version": "1.0.0",
        "markitdown_version": get_markitdown_version(),
        "docling_available": is_docling_available(),
        "docling_version": get_docling_version(),
        "markit_available": is_markit_available(),
        "markit_version": get_markit_version(),
        "web_bench_url": "/InkDoc",
        "docs_url": "/docs",
        "supported_extensions_count": len(SUPPORTED_EXTENSIONS),
        "endpoints": {
            "web_ui": "GET /InkDoc",
            "convert_file": "POST /InkDoc/convert/file",
            "convert_url": "POST /InkDoc/convert/url",
            "convert_batch": "POST /InkDoc/convert/batch",
            "health": "GET /InkDoc/health",
            "extensions": "GET /InkDoc/extensions",
        },
    }


@api_router.get("/health", tags=["Info"])
def health_check():
    """Health check endpoint."""
    return {
        "status": "ok",
        "markitdown_version": get_markitdown_version(),
        "docling_available": is_docling_available(),
        "docling_version": get_docling_version(),
        "markit_available": is_markit_available(),
        "markit_version": get_markit_version(),
    }


@api_router.get("/extensions", tags=["Info"])
def supported_extensions():
    """List all file extensions recognized by MarkItDown."""
    return {
        "extensions": sorted(SUPPORTED_EXTENSIONS),
    }


@api_router.post("/convert/file", tags=["Conversion"])
async def convert_file(
    file: UploadFile = File(..., description="Document, image, or media file to convert"),
    save_to_downloads: bool = Query(
        False, description="Whether to also save the .md file to user's Downloads directory"
    ),
    enable_plugins: bool = Query(False, description="Enable MarkItDown plugins"),
    keep_data_uris: bool = Query(False, description="Keep data URIs in HTML conversions"),
    engine: str = Query(
        "markitdown", description="Conversion engine route: 'markitdown' (default), 'docling', or 'markit'"
    ),
    response_format: str = Query(
        "text",
        description="Output format: 'text' returns raw Markdown; 'json' returns JSON metadata and content",
    ),
):
    """Convert an uploaded file (PDF, DOCX, XLSX, PPTX, Images, Audio, HTML, etc.) to Markdown."""
    filename = file.filename or "uploaded_file"
    ext = Path(filename).suffix
    engine_kind = resolve_engine(engine)

    # Save to a temporary file for MarkItDown to read
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    saved_path_str: str | None = None
    try:
        item = QueueItem(
            source=tmp_path,
            kind=SourceKind.FILE,
            display_name=filename,
            engine=engine_kind,
        )
        options = ConversionOptions(
            enable_plugins=enable_plugins,
            keep_data_uris=keep_data_uris,
            engine=engine_kind,
        )
        markdown_text = convert_item(item, options)

        if save_to_downloads:
            saved_path = auto_save_markdown(item, markdown_text)
            saved_path_str = str(saved_path)

        if response_format.lower() == "json":
            return JSONResponse(
                {
                    "success": True,
                    "filename": filename,
                    "engine": engine_kind.value,
                    "markdown": markdown_text,
                    "saved_to_downloads": saved_path_str,
                }
            )
        return PlainTextResponse(markdown_text, media_type="text/markdown; charset=utf-8")

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Conversion failed: {exc}",
        ) from exc
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@api_router.post("/convert/url", tags=["Conversion"])
def convert_url(
    payload: UrlConvertRequest,
    response_format: str = Query(
        "text",
        description="Output format: 'text' returns raw Markdown; 'json' returns JSON metadata and content",
    ),
):
    """Convert a web page or YouTube URL to Markdown."""
    url = payload.url.strip()
    if not url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="URL cannot be empty."
        )
    if not url.startswith(("http://", "https://", "file:", "data:")):
        if "." in url and " " not in url:
            url = f"https://{url}"
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid URL format. Please provide a valid HTTP or HTTPS URL.",
            )

    engine_kind = resolve_engine(payload.engine)
    saved_path_str: str | None = None
    try:
        item = QueueItem(
            source=url,
            kind=SourceKind.URL,
            display_name=url,
            engine=engine_kind,
        )
        options = ConversionOptions(
            enable_plugins=payload.enable_plugins,
            keep_data_uris=payload.keep_data_uris,
            engine=engine_kind,
        )
        markdown_text = convert_item(item, options)

        if payload.save_to_downloads:
            saved_path = auto_save_markdown(item, markdown_text)
            saved_path_str = str(saved_path)

        if response_format.lower() == "json":
            return JSONResponse(
                {
                    "success": True,
                    "url": url,
                    "engine": engine_kind.value,
                    "markdown": markdown_text,
                    "saved_to_downloads": saved_path_str,
                }
            )
        return PlainTextResponse(markdown_text, media_type="text/markdown; charset=utf-8")

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"URL conversion failed: {exc}",
        ) from exc


@api_router.post("/convert/batch", tags=["Conversion"])
async def convert_batch(
    files: list[UploadFile] = File(..., description="Multiple files to convert in a single request"),
    save_to_downloads: bool = Query(
        False, description="Whether to also save the .md files to user's Downloads directory"
    ),
    enable_plugins: bool = Query(False, description="Enable MarkItDown plugins"),
    engine: str = Query(
        "markitdown", description="Conversion engine route: 'markitdown' (default), 'docling', or 'markit'"
    ),
):
    """Batch convert multiple files to Markdown. Returns a JSON map of results."""
    results = {}
    engine_kind = resolve_engine(engine)
    options = ConversionOptions(enable_plugins=enable_plugins, engine=engine_kind)

    for file in files:
        filename = file.filename or "file"
        ext = Path(filename).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        try:
            item = QueueItem(
                source=tmp_path,
                kind=SourceKind.FILE,
                display_name=filename,
                engine=engine_kind,
            )
            markdown_text = convert_item(item, options)
            saved_str: str | None = None
            if save_to_downloads:
                saved_str = str(auto_save_markdown(item, markdown_text))

            results[filename] = {
                "success": True,
                "engine": engine_kind.value,
                "markdown": markdown_text,
                "saved_to_downloads": saved_str,
            }
        except Exception as exc:
            results[filename] = {
                "success": False,
                "error": str(exc),
            }
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    return JSONResponse({"total": len(files), "results": results})


# Mount API routes at root, /InkDoc, and /MarkItDown (alias)
app.include_router(api_router)
app.include_router(api_router, prefix="/InkDoc")
app.include_router(api_router, prefix="/MarkItDown")


if __name__ == "__main__":
    import uvicorn

    DEFAULT_PORT = 13118
    print(f"Starting InkDoc Local API server at http://localhost:{DEFAULT_PORT}/InkDoc ...")
    uvicorn.run("app.server.server:app", host="0.0.0.0", port=DEFAULT_PORT, reload=True, app_dir=str(REPO_ROOT))
