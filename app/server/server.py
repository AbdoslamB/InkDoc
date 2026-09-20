"""Standalone Local REST API server for InkDoc Multi-Engine Workbench.

Provides high-speed HTTP endpoints for converting files, URLs, and batches to Markdown
without launching the desktop window. Implements strict Host/Origin validation, dynamic CORS,
and per-session token authorization on management endpoints.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("inkdoc.server")

# Ensure repository root is on sys.path so app.core modules can be imported
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import (  # noqa: E402
    APIRouter,
    BackgroundTasks,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import (  # noqa: E402
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app._version import get_version

try:
    from app.core.converter import (
        SUPPORTED_EXTENSIONS,
        ConversionOptions,
        auto_save_markdown,
        convert_item,
        get_markitdown_version,
    )
    from app.core.download_utils import SecurityError
    from app.core.engine_manager import EngineManager, EngineStatus
    from app.core.engines.docling_engine import get_docling_version, is_docling_available
    from app.core.engines.markit_engine import get_markit_version, is_markit_available
    from app.core.queue_model import EngineKind, QueueItem, SourceKind
    from app.core.security import SSRFValidationError, validate_url_for_ssrf
    from app.core.url_fetcher import (
        UrlFetchError,
        UrlFetchSizeExceededError,
        UrlFetchTimeoutError,
    )
except ImportError as err:
    raise RuntimeError(
        f"Failed to import app.core modules from {REPO_ROOT}. "
        "Ensure requirements are installed."
    ) from err

# Per-session random token for engine management and settings (Security Requirement #12)
SESSION_TOKEN: str = os.environ.get("INKDOC_SESSION_TOKEN") or secrets.token_urlsafe(32)
SERVER_PORT: int = int(os.environ.get("INKDOC_SERVER_PORT", "13118"))

# Global conversion activity counter to guard against update apply during active conversions
_active_conversions = 0
_active_conversions_lock = threading.Lock()

# Concurrency cap on active conversions (default 2, configurable via INKDOC_MAX_CONCURRENT_CONVERSIONS)
MAX_CONCURRENT_CONVERSIONS: int = int(os.environ.get("INKDOC_MAX_CONCURRENT_CONVERSIONS", "2"))
_conversion_semaphore: asyncio.Semaphore | None = None
_semaphore_lock = threading.Lock()


def get_conversion_semaphore() -> asyncio.Semaphore:
    """Return singleton asyncio.Semaphore capping concurrent conversions."""
    global _conversion_semaphore
    if _conversion_semaphore is None:
        with _semaphore_lock:
            if _conversion_semaphore is None:
                _conversion_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS)
    return _conversion_semaphore


@contextlib.contextmanager
def track_active_conversion():
    """Context manager tracking active document conversions (thread-safe)."""
    global _active_conversions
    with _active_conversions_lock:
        _active_conversions += 1
    try:
        yield
    finally:
        with _active_conversions_lock:
            _active_conversions = max(0, _active_conversions - 1)


def is_conversion_active() -> bool:
    """Return True if any document conversion is currently processing."""
    with _active_conversions_lock:
        return _active_conversions > 0


def get_active_conversions_count() -> int:
    """Return count of currently processing conversions."""
    with _active_conversions_lock:
        return _active_conversions


_update_applying_lock = threading.Lock()
_update_applying: bool = False


def is_update_applying() -> bool:
    """Return True if an update is actively being applied and restarting."""
    with _update_applying_lock:
        if _update_applying:
            return True
    if _update_manager is not None:
        try:
            from app.core.update_manager import UpdateState

            if _update_manager.get_status().get("state") == UpdateState.APPLYING.value:
                return True
        except Exception:
            pass
    return False


def set_update_applying(value: bool = True) -> None:
    """Flag that an update is currently being applied, rejecting new conversions."""
    global _update_applying
    with _update_applying_lock:
        _update_applying = value


def _guard_update_not_applying() -> None:
    """Refuse conversion requests if an update is actively being applied."""
    if is_update_applying():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Update in progress: InkDoc is applying an update and restarting. New conversions are temporarily rejected.",
        )


def _perform_conversion(
    item: QueueItem,
    options: ConversionOptions,
    save_to_downloads: bool = False,
) -> tuple[str, str | None]:
    """Execute document conversion in worker thread and optionally auto-save."""
    with track_active_conversion():
        markdown_text = convert_item(item, options)
    saved_path_str: str | None = None
    if save_to_downloads:
        saved_path = auto_save_markdown(item, markdown_text)
        saved_path_str = str(saved_path)
    return markdown_text, saved_path_str


_update_manager: Any | None = None


def get_update_manager() -> Any:
    """Return or initialize the singleton UpdateManager instance."""
    global _update_manager
    if _update_manager is None:
        from app.core.update_manager import UpdateManager

        is_runner = os.environ.get("INKDOC_DESKTOP_RUNNER", "").strip() in ("1", "true")
        _update_manager = UpdateManager(is_desktop_runner=is_runner)
        _update_manager.set_conversion_active_checker(is_conversion_active)
    return _update_manager


def set_server_port(port: int) -> None:
    """Update active server port for dynamic CORS and Host validation."""
    global SERVER_PORT
    SERVER_PORT = port


def verify_session_token(token: str | None) -> bool:
    """Constant-time token validation against SESSION_TOKEN."""
    if not token:
        return False
    return hmac.compare_digest(token, SESSION_TOKEN)


def require_session_token(request: Request) -> None:
    """Dependency requiring valid X-InkDoc-Token header on management endpoints."""
    token = request.headers.get("X-InkDoc-Token")
    if not verify_session_token(token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Invalid or missing session token (X-InkDoc-Token header required).",
        )


# Disable interactive API docs in packaged production desktop builds unless explicitly requested
_is_frozen = getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
_docs_enabled = (not _is_frozen) or os.environ.get("INKDOC_ENABLE_DOCS", "").lower() in ("1", "true")

app = FastAPI(
    title="InkDoc Local API",
    description=(
        "High-performance local REST API server for InkDoc multi-engine document-to-markdown workbench. "
        "Converts PDF, Office documents, images, audio, and web URLs directly to Markdown."
    ),
    version=get_version(),
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)


@app.middleware("http")
async def security_and_cors_middleware(request: Request, call_next: Any) -> Response:
    """Enforce DNS rebinding protection (Host header), Origin verification, and dynamic CORS."""
    # 1. Host header validation against DNS rebinding (Security Requirement #11)
    host_header = request.headers.get("host", "").lower()
    host_name = host_header.split(":")[0] if host_header else ""
    allowed_hostnames = {"127.0.0.1", "localhost", "testserver", "testclient"}
    if host_name and host_name not in allowed_hostnames:
        return PlainTextResponse("Forbidden: Invalid Host header (DNS rebinding protection)", status_code=403)

    # 2. Origin validation on state-changing requests (Security Requirement #11)
    origin_header = request.headers.get("origin", "")
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and origin_header:
        parsed_orig = urlparse(origin_header)
        orig_host = parsed_orig.hostname or ""
        orig_port = parsed_orig.port

        is_webview_origin = (
            origin_header in ("null", "file://")
            or origin_header.startswith("pywebview://")
            or origin_header.startswith("vscode-webview://")
        )
        is_valid_loopback = (
            orig_host in ("127.0.0.1", "localhost")
            and (orig_port == SERVER_PORT or orig_port is None or orig_host == "testserver")
        )

        if not is_webview_origin and not is_valid_loopback and orig_host != "testserver":
            return PlainTextResponse("Forbidden: Untrusted Origin", status_code=403)

    # 3. Dynamic CORS preflight (OPTIONS)
    if request.method == "OPTIONS" and origin_header:
        parsed_orig = urlparse(origin_header)
        orig_host = parsed_orig.hostname or ""
        orig_port = parsed_orig.port
        is_allowed = (
            origin_header in ("null", "file://")
            or origin_header.startswith("pywebview://")
            or orig_host == "testserver"
            or (orig_host in ("127.0.0.1", "localhost") and (orig_port == SERVER_PORT or orig_port is None))
        )
        if is_allowed:
            resp = PlainTextResponse("OK", status_code=200)
            resp.headers["Access-Control-Allow-Origin"] = origin_header
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = request.headers.get("Access-Control-Request-Headers", "*")
            return resp

    response: Response = await call_next(request)

    # 4. Attach CORS response headers for allowed origins
    if origin_header:
        parsed_orig = urlparse(origin_header)
        orig_host = parsed_orig.hostname or ""
        orig_port = parsed_orig.port
        is_allowed = (
            origin_header in ("null", "file://")
            or origin_header.startswith("pywebview://")
            or orig_host == "testserver"
            or (orig_host in ("127.0.0.1", "localhost") and (orig_port == SERVER_PORT or orig_port is None))
        )
        if is_allowed:
            response.headers["Access-Control-Allow-Origin"] = origin_header
            response.headers["Access-Control-Allow-Credentials"] = "true"

    # 5. Security hardening headers (Requirement M)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")

    return response


def _resolve_ui_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundle_dir = Path(sys._MEIPASS)
        for candidate in [bundle_dir / "app" / "ui", bundle_dir / "ui"]:
            if candidate.exists():
                return candidate
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


class SettingsPayload(BaseModel):
    fallback_to_markitdown: bool | None = Field(
        default=None,
        description="Whether to fall back to MarkItDown if Docling conversion fails",
    )
    check_for_updates_daily: bool | None = Field(
        default=None,
        description="Opt-in to automatic daily update checks on startup (desktop only)",
    )


# Resource and DoS protection limits
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB per file limit
MAX_BATCH_FILES = 50                     # Maximum files per batch request

# MarkItDown plugin permissions (disabled by default to prevent arbitrary Python code execution)
ALLOW_PLUGINS = os.environ.get("INKDOC_ALLOW_PLUGINS", "").lower() in ("1", "true")


def _check_plugin_permission(enable_plugins: bool) -> None:
    """Validate whether MarkItDown third-party plugin execution is permitted."""
    if enable_plugins and not ALLOW_PLUGINS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Third-party MarkItDown plugins are disabled for security. "
                "Set environment variable INKDOC_ALLOW_PLUGINS=1 to enable."
            ),
        )


def sanitize_conversion_error(exc: Exception, context_name: str = "document") -> str:
    """Convert an internal conversion exception into a safe, informative user message."""
    exc_str = str(exc)
    exc_lower = exc_str.lower()

    if "ssrf" in exc_lower or "private" in exc_lower or "prohibited" in exc_lower:
        return "Access to local or private network resources is prohibited."

    if "unsupported" in exc_lower or "not supported" in exc_lower:
        return f"Unsupported format for '{context_name}' by this engine."

    if "not installed" in exc_lower:
        return exc_str

    # Redact sensitive absolute file paths
    clean_msg = re.sub(r"[A-Za-z]:\\[^\s:;,]+", "[path]", exc_str)
    clean_msg = re.sub(r"/(?:home|Users|usr|etc|var|tmp)/[^\s:;,]+", "[path]", clean_msg)
    clean_msg = re.sub(r"\s+", " ", clean_msg).strip()

    if len(clean_msg) > 160 or "\n" in exc_str or not clean_msg:
        ext = Path(context_name).suffix.lower()
        if ext in (".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt") and "docling" in exc_lower:
            return f"Docling was unable to parse this Office document ({context_name}). Try switching to the MarkItDown engine."
        return f"Conversion failed for {context_name}. Please check file validity or try another engine."

    return f"Conversion failed: {clean_msg}"


async def _save_upload_to_temp(file: UploadFile, ext: str) -> str:
    """Stream an uploaded file to a temporary file while strictly validating size limits."""
    # Sanitize extension to prevent extension-spoofing or traversal
    safe_ext = re.sub(r"[^a-zA-Z0-9_\.]", "", ext)[:10] if ext else ""

    with tempfile.NamedTemporaryFile(delete=False, suffix=safe_ext) as tmp:
        tmp_path = tmp.name
        bytes_written = 0
        chunk_size = 64 * 1024  # 64 KB chunks

        try:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_FILE_SIZE_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File exceeds maximum allowed size of {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB.",
                    )
                tmp.write(chunk)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
    return tmp_path


def _serve_bench_html() -> HTMLResponse:
    index_file = UI_DIR / "index.html"
    if index_file.exists():
        html_content = index_file.read_text(encoding="utf-8")
        # Inject per-session random token into <head> with cryptographic CSP nonce (Requirement M)
        nonce = secrets.token_hex(16)
        token_injection = f'<script id="inkdoc-session-token" nonce="{nonce}">window.__INKDOC_SESSION_TOKEN__ = "{SESSION_TOKEN}";</script>'
        if "<head>" in html_content:
            html_content = html_content.replace("<head>", f"<head>\n  {token_injection}", 1)
        elif "</head>" in html_content:
            html_content = html_content.replace("</head>", f"  {token_injection}\n</head>", 1)

        csp_policy = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob: https:; "
            "connect-src 'self' http://127.0.0.1:* http://localhost:* ws://127.0.0.1:* ws://localhost:*; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self';"
        )
        response = HTMLResponse(html_content)
        response.headers["Content-Security-Policy"] = csp_policy
        return response
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
    if "text/html" in accept:
        return _serve_bench_html()

    return {
        "app": "InkDoc Local API",
        "status": "running",
        "version": get_version(),
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
            "engines": "GET /InkDoc/engines",
            "settings": "GET /InkDoc/settings",
            "health": "GET /InkDoc/health",
            "extensions": "GET /InkDoc/extensions",
        },
    }


@api_router.get("/health", tags=["Info"])
def health_check():
    """Health check endpoint. docling_available is true only when installed."""
    return {
        "status": "ok",
        "version": get_version(),
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


# ─── Engine Management Endpoints ─────────────────────────────────────────────

@api_router.get("/engines", tags=["Engines"])
def list_engines():
    """List all engines with status (installed | installable | unsupported), version, and size."""
    return EngineManager.get_instance().get_all_engines_info()


@api_router.get("/engines/{engine_name}/progress", tags=["Engines"])
def engine_progress(engine_name: str):
    """Query download / installation progress for an engine."""
    return EngineManager.get_instance().get_progress(engine_name)


@api_router.post("/engines/{engine_name}/install", tags=["Engines"])
def install_engine(engine_name: str, background_tasks: BackgroundTasks, request: Request):
    """Download and install an optional engine pack. Requires session token."""
    require_session_token(request)
    mgr = EngineManager.get_instance()
    if not mgr.is_platform_supported(engine_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Engine '{engine_name}' is not available for this build ({mgr.platform_key}).",
        )

    # Launch installation in background thread
    background_tasks.add_task(mgr.install_engine, engine_name)
    return {"status": "started", "engine": engine_name, "message": "Installation started in background."}


@api_router.post("/engines/{engine_name}/cancel", tags=["Engines"])
def cancel_install(engine_name: str, request: Request):
    """Cancel an active download or installation. Requires session token."""
    require_session_token(request)
    cancelled = EngineManager.get_instance().cancel_install(engine_name)
    return {"status": "cancelled" if cancelled else "not_running", "engine": engine_name}


@api_router.post("/engines/{engine_name}/remove", tags=["Engines"])
def remove_engine(engine_name: str, request: Request):
    """Remove an installed engine and all cached models. Requires session token."""
    require_session_token(request)
    try:
        EngineManager.get_instance().remove_engine(engine_name)
        return {"status": "removed", "engine": engine_name}
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@api_router.post("/engines/{engine_name}/verify", tags=["Engines"])
def verify_engine(engine_name: str, request: Request):
    """Verify cryptographic file integrity of an installed engine tree. Requires session token."""
    require_session_token(request)
    return EngineManager.get_instance().verify_full_installed_tree(engine_name)


@api_router.get("/settings", tags=["Settings"])
def get_settings():
    """Retrieve current user settings."""
    return EngineManager.get_instance().get_settings()


@api_router.post("/settings", tags=["Settings"])
def update_settings(payload: SettingsPayload, request: Request):
    """Update user settings (e.g. fallback toggle, daily update check). Requires session token."""
    require_session_token(request)
    updates = {k: v for k, v in payload.dict().items() if v is not None}
    return EngineManager.get_instance().update_settings(updates)


# ─── Update Management Endpoints ──────────────────────────────────────────────

@api_router.get("/update/status", tags=["Update"])
def get_update_status():
    """Query current update status, latest release info, and download progress."""
    return get_update_manager().get_status()


class UpdateCheckRequest(BaseModel):
    force: bool = Field(default=True, description="Force check bypassing frequency cap")


@api_router.post("/update/check", tags=["Update"])
def check_update(request: Request, payload: UpdateCheckRequest | None = None):
    """Check for latest release on GitHub, verify Ed25519 envelope signature. Requires session token."""
    require_session_token(request)
    force = payload.force if payload else True
    return get_update_manager().check_for_updates(force=force)


@api_router.post("/update/download", tags=["Update"])
def download_update(request: Request):
    """Start streaming download of verified release asset in background. Requires session token."""
    require_session_token(request)
    try:
        return get_update_manager().download_update()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@api_router.post("/update/cancel", tags=["Update"])
def cancel_update(request: Request):
    """Cancel an active update download. Requires session token."""
    require_session_token(request)
    return get_update_manager().cancel_download()


@api_router.post("/update/reveal", tags=["Update"])
def reveal_update(request: Request):
    """Open file manager showing the downloaded release asset. Requires session token."""
    require_session_token(request)
    success = get_update_manager().reveal_downloaded_file()
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Downloaded update file not found to reveal.",
        )
    return {"status": "revealed"}


@api_router.post("/update/apply", tags=["Update"])
def apply_update(request: Request):
    """Apply verified staged update and restart into new version. Requires session token.

    Refused if running in source mode, browser tab, or headless mode (Requirement D).
    Refused if active document conversions are running.
    """
    require_session_token(request)
    mgr = get_update_manager()

    try:
        from app.desktop.runner import close_desktop_window

        set_update_applying(True)
        return mgr.apply_update(shutdown_callback=close_desktop_window)
    except PermissionError as exc:
        set_update_applying(False)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RuntimeError as exc:
        set_update_applying(False)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SecurityError as exc:
        set_update_applying(False)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except (ValueError, FileNotFoundError) as exc:
        set_update_applying(False)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        set_update_applying(False)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


# ─── Conversion Endpoints ───────────────────────────────────────────────────

def _guard_engine_availability(engine_kind: EngineKind) -> None:
    """Refuse uninstalled or unsupported engine requests according to contract."""
    if engine_kind == EngineKind.DOCLING:
        mgr = EngineManager.get_instance()
        docling_status = mgr.get_engine_status("docling")
        if docling_status == EngineStatus.UNSUPPORTED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="engine_unsupported: IBM Docling is not supported on this platform architecture.",
            )
        if docling_status == EngineStatus.INSTALLABLE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="engine_not_installed: IBM Docling is not installed. Please install it in Settings.",
            )


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
    _guard_update_not_applying()
    filename = file.filename or "uploaded_file"
    ext = Path(filename).suffix
    engine_kind = resolve_engine(engine)

    _check_plugin_permission(enable_plugins)
    _guard_engine_availability(engine_kind)

    tmp_path = await _save_upload_to_temp(file, ext)
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
        async with get_conversion_semaphore():
            markdown_text, saved_path_str = await asyncio.to_thread(
                _perform_conversion, item, options, save_to_downloads
            )

        fallback_occurred = getattr(options, "fallback_occurred", False)
        fallback_reason = getattr(options, "fallback_reason", "") or None
        engine_used = "markitdown" if fallback_occurred else engine_kind.value

        if response_format.lower() == "json":
            return JSONResponse(
                {
                    "success": True,
                    "filename": filename,
                    "engine_requested": engine_kind.value,
                    "engine_used": engine_used,
                    "fallback": fallback_occurred,
                    "fallback_reason": fallback_reason,
                    "markdown": markdown_text,
                    "saved_to_downloads": saved_path_str,
                }
            )

        resp = PlainTextResponse(markdown_text, media_type="text/markdown; charset=utf-8")
        resp.headers["X-Engine-Requested"] = engine_kind.value
        resp.headers["X-Engine-Used"] = engine_used
        resp.headers["X-Fallback-Occurred"] = str(fallback_occurred).lower()
        if fallback_reason:
            resp.headers["X-Fallback-Reason"] = re.sub(r"[\r\n]+", " ", fallback_reason)[:200]
        return resp

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("File conversion failed for '%s': %s", filename, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=sanitize_conversion_error(exc, context_name=filename),
        ) from exc
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@api_router.post("/convert/url", tags=["Conversion"])
async def convert_url(
    payload: UrlConvertRequest,
    response_format: str = Query(
        "text",
        description="Output format: 'text' returns raw Markdown; 'json' returns JSON metadata and content",
    ),
):
    """Convert a web page or YouTube URL to Markdown."""
    _guard_update_not_applying()
    url = payload.url.strip()
    if not url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="URL cannot be empty."
        )
    try:
        url = validate_url_for_ssrf(url)
    except SSRFValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid or prohibited URL: {exc}",
        ) from exc

    engine_kind = resolve_engine(payload.engine)
    _check_plugin_permission(payload.enable_plugins)
    _guard_engine_availability(engine_kind)

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
        async with get_conversion_semaphore():
            markdown_text, saved_path_str = await asyncio.to_thread(
                _perform_conversion, item, options, payload.save_to_downloads
            )

        fallback_occurred = getattr(options, "fallback_occurred", False)
        fallback_reason = getattr(options, "fallback_reason", "") or None
        engine_used = "markitdown" if fallback_occurred else engine_kind.value

        if response_format.lower() == "json":
            return JSONResponse(
                {
                    "success": True,
                    "url": url,
                    "engine_requested": engine_kind.value,
                    "engine_used": engine_used,
                    "fallback": fallback_occurred,
                    "fallback_reason": fallback_reason,
                    "markdown": markdown_text,
                    "saved_to_downloads": saved_path_str,
                }
            )

        resp = PlainTextResponse(markdown_text, media_type="text/markdown; charset=utf-8")
        resp.headers["X-Engine-Requested"] = engine_kind.value
        resp.headers["X-Engine-Used"] = engine_used
        resp.headers["X-Fallback-Occurred"] = str(fallback_occurred).lower()
        if fallback_reason:
            resp.headers["X-Fallback-Reason"] = re.sub(r"[\r\n]+", " ", fallback_reason)[:200]
        return resp

    except HTTPException:
        raise
    except SSRFValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid or prohibited URL: {exc}",
        ) from exc
    except UrlFetchTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"URL fetch timed out: {exc}",
        ) from exc
    except UrlFetchSizeExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"URL content too large: {exc}",
        ) from exc
    except UrlFetchError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"URL fetch failed: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception("URL conversion failed for '%s': %s", url, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=sanitize_conversion_error(exc, context_name=url),
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
    _guard_update_not_applying()
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Batch size exceeds maximum limit of {MAX_BATCH_FILES} files per request.",
        )

    engine_kind = resolve_engine(engine)
    _check_plugin_permission(enable_plugins)
    _guard_engine_availability(engine_kind)

    results = {}
    base_options = ConversionOptions(enable_plugins=enable_plugins, engine=engine_kind)

    for file in files:
        filename = file.filename or "file"
        ext = Path(filename).suffix
        tmp_path = None
        try:
            tmp_path = await _save_upload_to_temp(file, ext)
        except HTTPException as he:
            results[filename] = {
                "success": False,
                "error": he.detail,
            }
            continue

        try:
            item = QueueItem(
                source=tmp_path,
                kind=SourceKind.FILE,
                display_name=filename,
                engine=engine_kind,
            )
            item_options = dataclasses.replace(base_options)
            async with get_conversion_semaphore():
                markdown_text, saved_str = await asyncio.to_thread(
                    _perform_conversion, item, item_options, save_to_downloads
                )

            fallback_occurred = getattr(item_options, "fallback_occurred", False)
            fallback_reason = getattr(item_options, "fallback_reason", "") or None
            engine_used = "markitdown" if fallback_occurred else engine_kind.value

            results[filename] = {
                "success": True,
                "engine_requested": engine_kind.value,
                "engine_used": engine_used,
                "fallback": fallback_occurred,
                "fallback_reason": fallback_reason,
                "markdown": markdown_text,
                "saved_to_downloads": saved_str,
            }

        except Exception as exc:
            logger.exception("Batch conversion failed for '%s': %s", filename, exc)
            results[filename] = {
                "success": False,
                "error": sanitize_conversion_error(exc, context_name=filename),
            }
        finally:
            if tmp_path and os.path.exists(tmp_path):
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
    print(f"Starting InkDoc Local API server at http://127.0.0.1:{DEFAULT_PORT}/InkDoc ...")
    uvicorn.run("app.server.server:app", host="127.0.0.1", port=DEFAULT_PORT, reload=False, app_dir=str(REPO_ROOT))
