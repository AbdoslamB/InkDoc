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
from collections.abc import AsyncIterator
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

# Concurrency caps on active conversions, one lane per engine class.
#
# Docling converts in a single worker process, one document at a time
# (DoclingWorkerClient holds its mutex for the whole job), so a second Docling
# request can only wait. When all engines shared one cap, that waiting request
# still held a slot, and a quick .docx could queue behind two long PDF jobs.
# Docling now waits in a lane of its own sized to what the worker can run.
#
# MarkItDown and markit share the other lane (default 2, configurable via
# INKDOC_MAX_CONCURRENT_CONVERSIONS).
MAX_CONCURRENT_CONVERSIONS: int = int(os.environ.get("INKDOC_MAX_CONCURRENT_CONVERSIONS", "2"))
MAX_CONCURRENT_DOCLING_CONVERSIONS = 1
# One llama-server reads one page at a time, so GLM-OCR gets its own lane of 1:
# a 30-page scan never holds a slot a quick MarkItDown job needs.
MAX_CONCURRENT_GLM_OCR_CONVERSIONS = 1
_conversion_semaphores: dict[str, asyncio.Semaphore] = {}
_semaphore_lock = threading.Lock()


def _lane_semaphore(lane: str, size: int) -> asyncio.Semaphore:
    sem = _conversion_semaphores.get(lane)
    if sem is None:
        with _semaphore_lock:
            sem = _conversion_semaphores.get(lane)
            if sem is None:
                sem = _conversion_semaphores[lane] = asyncio.Semaphore(size)
    return sem


def get_conversion_semaphore(engine: EngineKind) -> asyncio.Semaphore:
    """Return the asyncio.Semaphore capping concurrent conversions for `engine`'s lane."""
    if engine == EngineKind.DOCLING:
        return _lane_semaphore("docling", MAX_CONCURRENT_DOCLING_CONVERSIONS)
    if engine == EngineKind.GLM_OCR:
        return _lane_semaphore("glm_ocr", MAX_CONCURRENT_GLM_OCR_CONVERSIONS)
    return _lane_semaphore("default", MAX_CONCURRENT_CONVERSIONS)


def get_probe_semaphore() -> asyncio.Semaphore:
    """The lane for pdfium work (PDF probe and missing-text check), one at a time.

    pdfium is not thread-safe, so app/core/pdf_probe.py also holds a thread lock.
    Taking this semaphore in async code first means a probe waiting its turn
    holds no thread: fifty PDFs dropped at once queue here, not in the default
    executor, where they would starve every other conversion of threads.
    """
    return _lane_semaphore("probe", 1)


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
    fetched: Any = None,
    job: Any = None,
) -> tuple[str, str | None]:
    """Execute document conversion in worker thread and optionally auto-save.

    `fetched` is a URL already downloaded by the caller (it keeps the file for the
    missing-text check). A job cancelled while the conversion ran stops here,
    before anything is written to Downloads.
    """
    with track_active_conversion():
        if fetched is not None:
            markdown_text = convert_item(item, options, fetched=fetched)
        else:
            markdown_text = convert_item(item, options)
    if job is not None:
        job.raise_if_cancelled()
    saved_path_str: str | None = None
    if save_to_downloads:
        saved_path = auto_save_markdown(item, markdown_text)
        saved_path_str = str(saved_path)
    return markdown_text, saved_path_str


def _header_value(text: str) -> str:
    """A header-safe rendering: one line, at most 200 chars, latin-1 encodable."""
    single = re.sub(r"[\r\n]+", " ", text)[:200]
    return single.encode("latin-1", "replace").decode("latin-1")


def _quality_check_enabled(override: bool | None) -> bool:
    """The per-request override, else the user's setting (default on)."""
    if override is not None:
        return bool(override)
    try:
        return bool(EngineManager.get_instance().get_settings().get("quality_check_enabled", True))
    except Exception as exc:
        logger.warning("Could not read the missing-text check setting: %s", exc)
        return True


def _start_job(job_id: str | None) -> Any:
    """A progress/cancel handle for `job_id`, or a no-op one when none was sent."""
    from app.core.jobs import JOBS, NULL_JOB, is_valid_job_id

    if not job_id:
        return NULL_JOB
    if not is_valid_job_id(job_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid job_id: use 8-100 letters, digits, '-', '_', '.' or ':'.",
        )
    return JOBS.start(job_id)


def _assess_in_thread(source_path: str, markdown: str, engine_used: EngineKind) -> Any:
    """Missing-text check for an explicit engine. Never raises; None for non-PDFs."""
    try:
        from app.core.auto_engine import ocr_install_target, ocr_status
        from app.core.pdf_probe import is_pdf
        from app.core.quality_check import assess

        if not is_pdf(source_path):
            return None
        ocr_engine, installable = ocr_status()
        target = ocr_install_target() if installable else None
        return assess(
            source_path, markdown, engine_used, ocr_engine=ocr_engine, ocr_installable=installable,
            ocr_install_engine=target or "docling",
        )
    except Exception as exc:
        logger.warning("Missing-text check failed: %s", exc)
        return None


async def _run_quality_check(source_path: str, markdown: str, options: ConversionOptions, job: Any) -> None:
    """Check an explicit-engine result after it was converted and saved.

    Runs outside the engine lane and inside the probe lane, so it neither holds
    a conversion slot nor a thread while it waits. Only adds information to the
    response; the saved file is never touched.
    """
    if not options.quality_check:
        return
    engine_used = options.engine_used or (
        EngineKind.MARKITDOWN if options.fallback_occurred else options.engine
    )
    if job is not None:
        job.set_phase("checking", "Checking…")
    async with get_probe_semaphore():
        options.quality_report = await asyncio.to_thread(
            _assess_in_thread, source_path, markdown, engine_used
        )


async def _convert_explicit(
    item: QueueItem,
    options: ConversionOptions,
    save_to_downloads: bool,
    *,
    source_path: str,
    fetched: Any = None,
    job: Any,
) -> tuple[str, str | None]:
    """Explicit engine: convert -> auto-save -> check. The file reaches Downloads first."""
    engine = options.engine
    label = _ENGINE_LABELS.get(engine, engine.value)
    options.job = job
    job.set_phase("queued", f"Waiting for {label}…", engine=engine.value)
    async with get_conversion_semaphore(engine):
        job.raise_if_cancelled()
        job.set_phase("converting", f"Converting with {label}…", engine=engine.value)
        markdown_text, saved_path_str = await asyncio.to_thread(
            _perform_conversion, item, options, save_to_downloads, fetched, job
        )
    await _run_quality_check(source_path, markdown_text, options, job)
    return markdown_text, saved_path_str


async def _convert_auto(
    item: QueueItem,
    options: ConversionOptions,
    save_to_downloads: bool,
    *,
    fetched: Any = None,
    job: Any,
) -> tuple[str, str | None]:
    """Auto: probe -> convert -> check -> (maybe re-convert) -> auto-save once.

    The whole job counts as one active conversion, including the gaps between
    its steps, so an update can never be applied in the middle of it.
    """
    from app.core.auto_engine import run_auto

    options.job = job
    with track_active_conversion():
        markdown_text = await run_auto(
            item,
            options,
            lane=get_conversion_semaphore,
            probe_lane=get_probe_semaphore(),
            fetched=fetched,
            job=job,
        )
        job.raise_if_cancelled()
        saved_path_str: str | None = None
        if save_to_downloads:
            job.set_phase("saving", "Saving…")
            saved_path = await asyncio.to_thread(auto_save_markdown, item, markdown_text)
            saved_path_str = str(saved_path)
    return markdown_text, saved_path_str


_ENGINE_LABELS = {
    EngineKind.MARKITDOWN: "MarkItDown",
    EngineKind.DOCLING: "Docling",
    EngineKind.MARKIT: "Markit",
    EngineKind.GLM_OCR: "GLM-OCR",
    EngineKind.AUTO: "Auto",
}


def _conversion_payload(options: ConversionOptions, engine_kind: EngineKind) -> dict[str, Any]:
    """Engine, fallback, Auto and quality metadata shared by every conversion response."""
    fallback_occurred = bool(getattr(options, "fallback_occurred", False))
    fallback_reason = getattr(options, "fallback_reason", "") or None
    engine_used = getattr(options, "engine_used", None) or (
        EngineKind.MARKITDOWN if fallback_occurred else engine_kind
    )
    if engine_used == EngineKind.AUTO:  # defensive: Auto always reports a concrete engine
        engine_used = EngineKind.MARKITDOWN
    decision = getattr(options, "auto_decision", None)
    report = getattr(options, "quality_report", None)
    return {
        "engine_requested": engine_kind.value,
        "engine_used": engine_used.value,
        "fallback": fallback_occurred,
        "fallback_reason": fallback_reason,
        "auto": decision.to_dict(engine_used) if engine_kind == EngineKind.AUTO and decision else None,
        "quality": report.to_dict() if report is not None else None,
        # Notes that are not failures, e.g. a GLM-OCR page cut short because the
        # model repeated itself, or GPU acceleration falling back to the CPU.
        "warnings": list(getattr(options, "engine_warnings", None) or []),
    }


def _markdown_response(markdown_text: str, payload: dict[str, Any]) -> PlainTextResponse:
    """The text response format: raw Markdown, metadata in X- headers."""
    resp = PlainTextResponse(markdown_text, media_type="text/markdown; charset=utf-8")
    resp.headers["X-Engine-Requested"] = payload["engine_requested"]
    resp.headers["X-Engine-Used"] = payload["engine_used"]
    resp.headers["X-Fallback-Occurred"] = str(payload["fallback"]).lower()
    if payload["fallback_reason"]:
        resp.headers["X-Fallback-Reason"] = _header_value(payload["fallback_reason"])
    if payload["auto"]:
        resp.headers["X-Auto-Reason"] = _header_value(payload["auto"]["reason"])
    if payload.get("warnings"):
        resp.headers["X-Engine-Warnings"] = _header_value(" | ".join(payload["warnings"]))
    quality = payload["quality"]
    if quality:
        resp.headers["X-Quality-Warning"] = str(bool(quality.get("warning"))).lower()
        if quality.get("missing_pct") is not None:
            resp.headers["X-Quality-Missing-Pct"] = str(quality["missing_pct"])
    return resp


def _cancelled_exception() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail="conversion_cancelled")


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

# Result of the startup reconciliation, served with GET /settings so the UI can
# tell the user a toggle was turned off for them and why. Module level rather
# than app.state because get_settings is a plain function, and this is read-only
# after startup.
_ADDON_RECONCILIATION: dict[str, Any] = {"cleared": [], "reason": ""}


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Reconcile settings against installed add-ons once, before serving.

    Startup is the only place this can happen exactly once per run with the
    filesystem in its final state. Doing it per request would repeat a disk check
    on a hot path; doing it per conversion is what the removed
    _effective_enrichment did, and that hid the inconsistency rather than
    resolving it.
    """
    global _ADDON_RECONCILIATION
    try:
        from app.core.engine_manager import reconcile_addon_gated_settings

        _ADDON_RECONCILIATION = reconcile_addon_gated_settings()
    except Exception as exc:
        # A failed reconciliation must never stop the server starting: every
        # gated setting is still enforced at the point of use by the worker.
        logger.warning("Add-on settings reconciliation failed: %s", exc)
    yield
    _shutdown_engine_processes()


def _shutdown_engine_processes() -> None:
    """Stop llama-server and the Docling worker when the server stops (window
    closed, Ctrl+C in --headless). Only instances that exist are touched."""
    try:
        from app.core.engines.glm_ocr_server import GlmOcrServer

        inst = GlmOcrServer.peek_instance()
        if inst is not None:
            inst.shutdown()
    except Exception as exc:
        logger.debug("GLM-OCR server shutdown: %s", exc)
    try:
        from app.core.engines.docling_worker_client import DoclingWorkerClient

        client = DoclingWorkerClient._instance
        if client is not None:
            client.shutdown()
    except Exception as exc:
        logger.debug("Docling worker shutdown: %s", exc)


app = FastAPI(
    lifespan=_lifespan,
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


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def is_trusted_origin(origin: str) -> bool:
    """Return True only for a loopback origin on this server's exact port.

    Every legitimate caller is same-origin: the pywebview desktop window and any
    browser tab both load the UI from http://127.0.0.1:<SERVER_PORT>, so they
    present exactly that origin on state-changing requests.

    Opaque origins ("null", "file://") are deliberately NOT trusted. A sandboxed
    iframe on any website carries `Origin: null`, so trusting it would let an
    arbitrary page read GET /InkDoc cross-origin and lift SESSION_TOKEN straight
    out of the injected <script>, which in turn unlocks every management endpoint
    (engine install/remove, settings, update apply). Custom schemes such as
    pywebview:// and vscode-webview:// are likewise untrusted: no supported
    configuration produces them, and each would be a bypass if some embedder did.
    """
    if not origin:
        return False
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    if (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS:
        return False
    try:
        return parsed.port == SERVER_PORT
    except ValueError:
        # Malformed port in the Origin header.
        return False


@app.middleware("http")
async def security_and_cors_middleware(request: Request, call_next: Any) -> Response:
    """Enforce DNS rebinding protection (Host header), Origin verification, and dynamic CORS."""
    # 1. Host header validation against DNS rebinding (Security Requirement #11)
    host_header = request.headers.get("host", "").lower()
    host_name = host_header.split(":")[0] if host_header else ""
    allowed_hostnames = {"127.0.0.1", "localhost", "testserver", "testclient"}
    if host_name and host_name not in allowed_hostnames:
        return PlainTextResponse("Forbidden: Invalid Host header (DNS rebinding protection)", status_code=403)

    # A single predicate drives rejection, preflight and response headers alike, so
    # the three cannot drift apart and leave a read path open that the write path blocks.
    origin_header = request.headers.get("origin", "")
    origin_trusted = is_trusted_origin(origin_header)

    # 2. Origin validation on state-changing requests (Security Requirement #11).
    # A request with no Origin header at all is still permitted: native API clients
    # (curl, examples/client_example.py) send none, and browsers always attach one
    # to cross-origin state-changing requests, so this is not a browser-reachable gap.
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and origin_header and not origin_trusted:
        logger.warning(
            "Rejected %s %s from untrusted Origin %r (expected loopback on port %d)",
            request.method,
            request.url.path,
            origin_header,
            SERVER_PORT,
        )
        return PlainTextResponse("Forbidden: Cross-origin request not allowed", status_code=403)

    # 3. Dynamic CORS preflight (OPTIONS)
    if request.method == "OPTIONS" and origin_trusted:
        resp = PlainTextResponse("OK", status_code=200)
        resp.headers["Access-Control-Allow-Origin"] = origin_header
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = request.headers.get("Access-Control-Request-Headers", "*")
        resp.headers["Vary"] = "Origin"
        return resp

    response: Response = await call_next(request)

    # 4. Attach CORS response headers for trusted origins only
    if origin_trusted:
        response.headers["Access-Control-Allow-Origin"] = origin_header
        response.headers["Access-Control-Allow-Credentials"] = "true"
        # Responses vary by Origin; without this an intermediary could serve a
        # cached allow-listed response to a request from a different origin.
        response.headers["Vary"] = "Origin"

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


def _docling_enrichment_options(engine_kind: EngineKind) -> dict[str, bool]:
    """Read the user's Docling enrichment preferences.

    Only consulted for the Docling route and for Auto, which may run Docling:
    the flags mean nothing to the other engines, and reading settings takes a
    lock and may touch disk, which is not worth paying on every MarkItDown
    conversion. A failure to read settings yields the defaults rather than
    failing the conversion. Auto drops the flags itself, and says so, when the
    recognition model is not installed (auto_engine._apply_enrichment_policy).
    """
    if engine_kind not in (EngineKind.DOCLING, EngineKind.AUTO):
        return {}
    try:
        settings = EngineManager.get_instance().get_settings()
    except Exception as exc:
        # Falling back to the defaults silently disables the toggles, which is
        # the safe direction but invisible, so leave a trail.
        logger.warning("Could not read Docling enrichment settings: %s", exc)
        return {}
    return {
        "docling_code_enrichment": bool(settings.get("docling_code_enrichment", False)),
        "docling_formula_enrichment": bool(settings.get("docling_formula_enrichment", False)),
    }


def resolve_engine(engine_name: str) -> EngineKind:
    norm = engine_name.lower().strip()
    if norm == "docling":
        return EngineKind.DOCLING
    if norm in ("glm_ocr", "glm-ocr", "glmocr"):
        return EngineKind.GLM_OCR
    if norm == "markit":
        return EngineKind.MARKIT
    if norm == "auto":
        return EngineKind.AUTO
    return EngineKind.MARKITDOWN


ENGINE_PARAM_DESCRIPTION = (
    "Conversion engine route: 'markitdown' (default), 'docling', 'markit', 'glm_ocr' "
    "(downloadable OCR model for scans, photos, math and tables; PDFs and images only), "
    "or 'auto' (InkDoc picks the engine per file and reports its choice in `auto`)"
)
CHECK_QUALITY_DESCRIPTION = (
    "Run the missing-text check on PDFs (overrides the user's setting for this request). "
    "Omit to use the setting."
)
JOB_ID_DESCRIPTION = (
    "Optional client-generated id (8-100 chars of letters, digits, - _ . :) for "
    "GET /convert/progress/{job_id} and POST /convert/cancel/{job_id}"
)


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
        description=ENGINE_PARAM_DESCRIPTION,
    )
    check_quality: bool | None = Field(default=None, description=CHECK_QUALITY_DESCRIPTION)
    job_id: str | None = Field(default=None, description=JOB_ID_DESCRIPTION)


class SettingsPayload(BaseModel):
    fallback_to_markitdown: bool | None = Field(
        default=None,
        description="Whether to fall back to MarkItDown if Docling conversion fails",
    )
    check_for_updates_daily: bool | None = Field(
        default=None,
        description="Opt-in to automatic daily update checks on startup (desktop only)",
    )
    docling_code_enrichment: bool | None = Field(
        default=None,
        description=(
            "Docling: transcribe code blocks with the code/formula model and label "
            "their language. Requires the Docling enrichment model."
        ),
    )
    docling_formula_enrichment: bool | None = Field(
        default=None,
        description=(
            "Docling: recognise mathematical formulas and convert them to LaTeX. "
            "Shares one model with code enrichment."
        ),
    )
    quality_check_enabled: bool | None = Field(
        default=None,
        description=(
            "Compare each PDF conversion with the PDF's embedded text and warn when part "
            "of it is missing. Read-only; never changes the saved file."
        ),
    )
    glm_ocr_download_source: str | None = Field(
        default=None,
        description=(
            "Where GLM-OCR downloads come from: 'auto' (official source first, InkDoc mirror "
            "as fallback) or 'mirror_only' (for networks that block Hugging Face)."
        ),
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

    from app.core.converter import UserFacingConversionError

    if isinstance(exc, UserFacingConversionError):
        # Written for the user by the engine; only paths are redacted.
        clean = re.sub(r"[A-Za-z]:\\[^\s:;,]+", "[path]", exc_str)
        clean = re.sub(r"/(?:home|Users|usr|etc|var|tmp)/[^\s:;,]+", "[path]", clean)
        return re.sub(r"\s+", " ", clean).strip()[:400] or f"Conversion failed for {context_name}."

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
        # The custom title bar must be decided before first paint, otherwise the
        # window controls flash in after a round trip. Only the frameless Windows
        # desktop window has them: a browser tab and the native-framed macOS and
        # Linux windows keep their own chrome.
        custom_titlebar = (
            os.environ.get("INKDOC_DESKTOP_RUNNER") == "1" and sys.platform == "win32"
        )
        # The class goes on <html> here rather than waiting for app.js to put it
        # on <body>: app.js does not run until the document has been parsed, and
        # the header would render at the wrong width until then.
        titlebar_class = (
            "document.documentElement.classList.add('has-custom-titlebar');"
            if custom_titlebar
            else ""
        )
        token_injection = (
            f'<script id="inkdoc-session-token" nonce="{nonce}">'
            f'window.__INKDOC_SESSION_TOKEN__ = "{SESSION_TOKEN}";'
            f'window.__INKDOC_CUSTOM_TITLEBAR__ = {"true" if custom_titlebar else "false"};'
            f'{titlebar_class}'
            f'</script>'
        )
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
        **_glm_ocr_health(),
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
        **_glm_ocr_health(),
    }


def _glm_ocr_health() -> dict[str, Any]:
    """glm_ocr_available is true only when installed and self-tested (marker read only)."""
    try:
        from app.core.glm_ocr_manager import GlmOcrManager

        mgr = GlmOcrManager.get_instance()
        marker = mgr.read_marker()
        version = (marker.get("model") or {}).get("version") if marker else None
        return {"glm_ocr_available": mgr.is_usable(), "glm_ocr_version": version or "Not installed"}
    except Exception:
        return {"glm_ocr_available": False, "glm_ocr_version": "Not installed"}


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


def _is_glm(engine_name: str) -> bool:
    return engine_name.lower().strip().replace("-", "_") in ("glm_ocr", "glmocr")


def _glm_manager() -> Any:
    from app.core.glm_ocr_manager import GlmOcrManager

    return GlmOcrManager.get_instance()


class GlmOcrInstallRequest(BaseModel):
    gpu: bool = Field(default=False, description="Also download the Vulkan GPU runtime (Windows/Linux)")
    variant: str | None = Field(default=None, description="Model precision: 'q8' (default) or 'f16'")


class GlmOcrGpuRequest(BaseModel):
    enabled: bool = Field(..., description="Turn GPU acceleration on or off")


def _run_glm_background(fn: Any, label: str) -> None:
    """Run a GLM-OCR operation on its own thread; its progress is polled separately."""

    def _run() -> None:
        try:
            fn()
        except Exception as exc:
            # Already recorded on the progress object the UI polls.
            logger.error("GLM-OCR %s failed: %s", label, exc)

    threading.Thread(target=_run, daemon=True, name=f"GlmOcr-{label}").start()


@api_router.get("/engines/glm_ocr/status", tags=["Engines"])
def glm_ocr_status(check_remote: bool = Query(False, description="Also check the signed online catalogue (once per session)")):
    """GLM-OCR card state. Reads the completion marker only; never hashes model files."""
    return _glm_manager().get_status(check_remote=check_remote)


@api_router.post("/engines/glm_ocr/gpu", tags=["Engines"])
def glm_ocr_gpu(payload: GlmOcrGpuRequest, request: Request):
    """Switch GPU acceleration. Downloads and self-tests the Vulkan runtime on first enable."""
    require_session_token(request)
    from app.core.glm_ocr_manager import GlmOcrInstallError

    mgr = _glm_manager()
    try:
        result = mgr.set_gpu(payload.enabled)
    except GlmOcrInstallError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if result.get("needs_download"):
        if mgr.is_busy():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A GLM-OCR download is already running.")
        _run_glm_background(mgr.install_gpu, "gpu")
        return {"status": "started", "engine": "glm_ocr", "operation": "gpu"}
    return {"status": "ok", "engine": "glm_ocr", **result}


@api_router.post("/engines/glm_ocr/selftest", tags=["Engines"])
def glm_ocr_selftest(request: Request):
    """Run the GLM-OCR self-test again in the background. Requires session token."""
    require_session_token(request)
    mgr = _glm_manager()
    if not mgr.is_installed():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="GLM-OCR is not installed.")
    if mgr.is_busy():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A GLM-OCR operation is already running.")
    _run_glm_background(mgr.rerun_selftest, "selftest")
    return {"status": "started", "engine": "glm_ocr", "operation": "selftest"}


@api_router.post("/engines/glm_ocr/update", tags=["Engines"])
def glm_ocr_update(request: Request):
    """Download only the changed parts of a newer pinned GLM-OCR, self-test, switch. Never automatic."""
    require_session_token(request)
    mgr = _glm_manager()
    st = mgr.get_status(check_remote=True)
    if not st.get("update_available"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="GLM-OCR is up to date.")
    if mgr.is_busy():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A GLM-OCR operation is already running.")
    _run_glm_background(lambda: mgr.install(operation="update"), "update")
    return {"status": "started", "engine": "glm_ocr", "operation": "update"}


@api_router.get("/engines/{engine_name}/progress", tags=["Engines"])
def engine_progress(engine_name: str):
    """Query download / installation progress for an engine."""
    if _is_glm(engine_name):
        return _glm_manager().get_progress()
    return EngineManager.get_instance().get_progress(engine_name)


@api_router.post("/engines/{engine_name}/install", tags=["Engines"])
def install_engine(
    engine_name: str,
    background_tasks: BackgroundTasks,
    request: Request,
    payload: GlmOcrInstallRequest | None = None,
):
    """Download and install an optional engine pack. Requires session token.

    GLM-OCR takes an optional body {"gpu": bool, "variant": "q8"|"f16"}.
    """
    require_session_token(request)
    if _is_glm(engine_name):
        mgr = _glm_manager()
        st = mgr.get_status()
        if st["status"] == "unsupported":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=st["reason"])
        if st["status"] in ("unreleased", "defective"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=st["reason"])
        if mgr.is_busy():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A GLM-OCR download is already running.")
        body = payload or GlmOcrInstallRequest()
        if body.variant is not None and body.variant not in {v["id"] for v in st["variants"]}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown precision '{body.variant}'.")
        _run_glm_background(lambda: mgr.install(gpu=body.gpu, variant=body.variant), "install")
        return {"status": "started", "engine": "glm_ocr", "message": "Download started in background."}
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
    if _is_glm(engine_name):
        cancelled = _glm_manager().cancel()
    else:
        cancelled = EngineManager.get_instance().cancel_install(engine_name)
    return {"status": "cancelled" if cancelled else "not_running", "engine": engine_name}


@api_router.post("/engines/{engine_name}/remove", tags=["Engines"])
def remove_engine(engine_name: str, request: Request):
    """Remove an installed engine and all cached models. Requires session token."""
    require_session_token(request)
    try:
        if _is_glm(engine_name):
            return _glm_manager().remove()
        EngineManager.get_instance().remove_engine(engine_name)
        return {"status": "removed", "engine": engine_name}
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@api_router.post("/engines/{engine_name}/verify", tags=["Engines"])
def verify_engine(engine_name: str, request: Request):
    """Verify cryptographic file integrity of an installed engine tree. Requires session token."""
    require_session_token(request)
    if _is_glm(engine_name):
        return _glm_manager().verify()
    return EngineManager.get_instance().verify_full_installed_tree(engine_name)


# Settings that cannot be turned on unless the recognition-model add-on is
# usable. Docling loads the CodeFormula weights from the pack's pinned
# artifacts_path at pipeline construction and raises when they are absent, so
# persisting either flag without the model would break every Docling conversion.
ENRICHMENT_SETTING_KEYS = ("docling_code_enrichment", "docling_formula_enrichment")
ENRICHMENT_ADDON_NAME = "code_enrichment"


@api_router.get("/addons", tags=["Addons"])
def list_addons():
    """Status of every optional add-on, for the settings UI."""
    from app.core.addon_manager import AddonManager

    mgr = AddonManager.get_instance()
    return {"addons": [mgr.get_status(name) for name in mgr.list_addon_names()]}


@api_router.get("/addons/{addon_name}", tags=["Addons"])
def get_addon_status(addon_name: str):
    """Status of one add-on, including install progress."""
    from app.core.addon_manager import AddonManager

    return AddonManager.get_instance().get_status(addon_name)


@api_router.post("/addons/{addon_name}/install", tags=["Addons"])
def install_addon(addon_name: str, background_tasks: BackgroundTasks, request: Request):
    """Download and install an optional add-on. Requires session token."""
    require_session_token(request)
    from app.core.addon_manager import AddonManager

    mgr = AddonManager.get_instance()
    status_info = mgr.get_status(addon_name)
    if not status_info.get("installable"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=status_info.get("reason") or f"Add-on '{addon_name}' cannot be installed.",
        )

    def _run() -> None:
        try:
            mgr.install(addon_name)
        except Exception as exc:
            # Already recorded on the progress object the UI polls; logged so a
            # background failure is not invisible in the server log either.
            logger.error("Add-on '%s' installation failed: %s", addon_name, exc)

    background_tasks.add_task(_run)
    return {"status": "started", "addon": addon_name}


@api_router.post("/addons/{addon_name}/cancel", tags=["Addons"])
def cancel_addon_install(addon_name: str, request: Request):
    """Cancel an in-flight add-on download. Requires session token."""
    require_session_token(request)
    from app.core.addon_manager import AddonManager

    cancelled = AddonManager.get_instance().cancel(addon_name)
    return {"status": "cancelled" if cancelled else "not_running", "addon": addon_name}


@api_router.post("/addons/{addon_name}/remove", tags=["Addons"])
def remove_addon(addon_name: str, request: Request):
    """Remove an installed add-on. Requires session token."""
    require_session_token(request)
    from app.core.addon_manager import AddonManager

    try:
        result = AddonManager.get_instance().remove(addon_name)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # The weights are gone, so any flag that depended on them has to go off too.
    # Leaving them set would persist a state the update endpoint below refuses to
    # accept, and every later Docling conversion would be refused by the worker.
    if addon_name == ENRICHMENT_ADDON_NAME:
        EngineManager.get_instance().update_settings(
            {key: False for key in ENRICHMENT_SETTING_KEYS}
        )

    return result


@api_router.post("/addons/{addon_name}/verify", tags=["Addons"])
def verify_addon(addon_name: str, request: Request):
    """Re-hash an installed add-on against the catalogue. Requires session token."""
    require_session_token(request)
    from app.core.addon_manager import AddonManager

    return AddonManager.get_instance().verify(addon_name)


@api_router.get("/settings", tags=["Settings"])
def get_settings():
    """Retrieve current user settings, plus anything startup had to correct."""
    settings = dict(EngineManager.get_instance().get_settings())
    settings["addon_reconciliation"] = _ADDON_RECONCILIATION
    return settings


@api_router.post("/settings", tags=["Settings"])
def update_settings(payload: SettingsPayload, request: Request):
    """Update user settings (e.g. fallback toggle, daily update check). Requires session token."""
    require_session_token(request)
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "glm_ocr_download_source" in updates:
        from app.core.glm_ocr_manager import DOWNLOAD_SOURCES

        if updates["glm_ocr_download_source"] not in DOWNLOAD_SOURCES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"glm_ocr_download_source must be one of {list(DOWNLOAD_SOURCES)}.",
            )

    if any(updates.get(key) for key in ENRICHMENT_SETTING_KEYS):
        from app.core.addon_manager import AddonManager

        status_info = AddonManager.get_instance().get_status(ENRICHMENT_ADDON_NAME)
        if not status_info.get("usable"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    status_info.get("reason")
                    or "The code and formula recognition model is not installed."
                ),
            )

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
    elif engine_kind == EngineKind.GLM_OCR:
        # No silent substitution: an uninstalled GLM-OCR is refused, exactly like Docling.
        mgr = _glm_manager()
        st = mgr.get_status()
        if st["status"] in ("unsupported", "unreleased", "defective") and not st["installed"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"engine_unsupported: {st['reason'] or 'GLM-OCR is not available on this platform.'}",
            )
        if not st["installed"] or st["status"] == "installing":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="engine_not_installed: GLM-OCR is not downloaded. Download it in Settings.",
            )
        if not st["usable"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="engine_not_ready: GLM-OCR did not pass its self-test. Open Settings and run it again.",
            )


@api_router.post("/convert/file", tags=["Conversion"])
async def convert_file(
    file: UploadFile = File(..., description="Document, image, or media file to convert"),
    save_to_downloads: bool = Query(
        False, description="Whether to also save the .md file to user's Downloads directory"
    ),
    enable_plugins: bool = Query(False, description="Enable MarkItDown plugins"),
    keep_data_uris: bool = Query(False, description="Keep data URIs in HTML conversions"),
    engine: str = Query("markitdown", description=ENGINE_PARAM_DESCRIPTION),
    response_format: str = Query(
        "text",
        description="Output format: 'text' returns raw Markdown; 'json' returns JSON metadata and content",
    ),
    check_quality: bool | None = Query(None, description=CHECK_QUALITY_DESCRIPTION),
    job_id: str | None = Query(None, description=JOB_ID_DESCRIPTION),
):
    """Convert an uploaded file (PDF, DOCX, XLSX, PPTX, Images, Audio, HTML, etc.) to Markdown."""
    _guard_update_not_applying()
    filename = file.filename or "uploaded_file"
    ext = Path(filename).suffix
    engine_kind = resolve_engine(engine)

    _check_plugin_permission(enable_plugins)
    _guard_engine_availability(engine_kind)
    job = _start_job(job_id)

    tmp_path = await _save_upload_to_temp(file, ext)
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
            quality_check=_quality_check_enabled(check_quality),
            **_docling_enrichment_options(engine_kind),
        )
        if engine_kind == EngineKind.AUTO:
            markdown_text, saved_path_str = await _convert_auto(
                item, options, save_to_downloads, job=job
            )
        else:
            markdown_text, saved_path_str = await _convert_explicit(
                item, options, save_to_downloads, source_path=tmp_path, job=job
            )
        job.finish("done")

        payload = _conversion_payload(options, engine_kind)
        if response_format.lower() == "json":
            return JSONResponse(
                {
                    "success": True,
                    "filename": filename,
                    **payload,
                    "markdown": markdown_text,
                    "saved_to_downloads": saved_path_str,
                }
            )
        return _markdown_response(markdown_text, payload)

    except HTTPException:
        job.finish("error")
        raise
    except Exception as exc:
        from app.core.jobs import ConversionCancelledError

        if isinstance(exc, ConversionCancelledError):
            job.finish("cancelled")
            raise _cancelled_exception() from exc
        job.finish("error")
        logger.exception("File conversion failed for '%s': %s", filename, exc)
        if ext.lower() in {".wav", ".mp3", ".m4a", ".mp4"} and "AudioConverter" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Audio transcription unavailable: no speech was recognized or "
                    "the optional speech-recognition dependency is unavailable."
                ),
            ) from exc
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
    job = _start_job(payload.job_id)

    fetched: Any = None
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
            quality_check=_quality_check_enabled(payload.check_quality),
            **_docling_enrichment_options(engine_kind),
        )
        # Fetched first, in a thread but outside any lane: the fetch has its own
        # timeouts and size cap, and the temporary file has to outlive the
        # conversion so a downloaded PDF can be routed and checked.
        from app.core.converter import open_url_source

        job.set_phase("fetching", "Downloading…")
        fetched = await asyncio.to_thread(open_url_source, item)
        job.raise_if_cancelled()

        if engine_kind == EngineKind.AUTO:
            markdown_text, saved_path_str = await _convert_auto(
                item, options, payload.save_to_downloads, fetched=fetched, job=job
            )
        else:
            markdown_text, saved_path_str = await _convert_explicit(
                item,
                options,
                payload.save_to_downloads,
                source_path=str(fetched.path),
                fetched=fetched,
                job=job,
            )
        job.finish("done")

        result = _conversion_payload(options, engine_kind)
        if response_format.lower() == "json":
            return JSONResponse(
                {
                    "success": True,
                    "url": url,
                    **result,
                    "markdown": markdown_text,
                    "saved_to_downloads": saved_path_str,
                }
            )
        return _markdown_response(markdown_text, result)

    except HTTPException:
        job.finish("error")
        raise
    except SSRFValidationError as exc:
        job.finish("error")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid or prohibited URL: {exc}",
        ) from exc
    except UrlFetchTimeoutError as exc:
        job.finish("error")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"URL fetch timed out: {exc}",
        ) from exc
    except UrlFetchSizeExceededError as exc:
        job.finish("error")
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"URL content too large: {exc}",
        ) from exc
    except UrlFetchError as exc:
        job.finish("error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"URL fetch failed: {exc}",
        ) from exc
    except Exception as exc:
        from app.core.jobs import ConversionCancelledError

        if isinstance(exc, ConversionCancelledError):
            job.finish("cancelled")
            raise _cancelled_exception() from exc
        job.finish("error")
        logger.exception("URL conversion failed for '%s': %s", url, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=sanitize_conversion_error(exc, context_name=url),
        ) from exc
    finally:
        if fetched is not None:
            await asyncio.to_thread(fetched.cleanup)


@api_router.post("/convert/batch", tags=["Conversion"])
async def convert_batch(
    files: list[UploadFile] = File(..., description="Multiple files to convert in a single request"),
    save_to_downloads: bool = Query(
        False, description="Whether to also save the .md files to user's Downloads directory"
    ),
    enable_plugins: bool = Query(False, description="Enable MarkItDown plugins"),
    engine: str = Query("markitdown", description=ENGINE_PARAM_DESCRIPTION),
    check_quality: bool | None = Query(None, description=CHECK_QUALITY_DESCRIPTION),
    job_id: str | None = Query(
        None,
        description=JOB_ID_DESCRIPTION + "; each file reports under '<job_id>-<index>' (0-based)",
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
    if job_id:
        _start_job(f"{job_id}-0")  # validates the id up front (400 before any work)

    results = {}
    base_options = ConversionOptions(
        enable_plugins=enable_plugins,
        engine=engine_kind,
        quality_check=_quality_check_enabled(check_quality),
        **_docling_enrichment_options(engine_kind),
    )

    for index, file in enumerate(files):
        filename = file.filename or "file"
        ext = Path(filename).suffix
        job = _start_job(f"{job_id}-{index}") if job_id else _start_job(None)
        tmp_path = None
        try:
            tmp_path = await _save_upload_to_temp(file, ext)
        except HTTPException as he:
            job.finish("error")
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
            if engine_kind == EngineKind.AUTO:
                markdown_text, saved_str = await _convert_auto(
                    item, item_options, save_to_downloads, job=job
                )
            else:
                markdown_text, saved_str = await _convert_explicit(
                    item, item_options, save_to_downloads, source_path=tmp_path, job=job
                )
            job.finish("done")

            results[filename] = {
                "success": True,
                **_conversion_payload(item_options, engine_kind),
                "markdown": markdown_text,
                "saved_to_downloads": saved_str,
            }

        except Exception as exc:
            from app.core.jobs import ConversionCancelledError

            if isinstance(exc, ConversionCancelledError):
                job.finish("cancelled")
                results[filename] = {"success": False, "error": "conversion_cancelled"}
                continue
            job.finish("error")
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


@api_router.post("/convert/estimate", tags=["Conversion"])
async def convert_estimate(
    file: UploadFile = File(..., description="PDF or image to estimate"),
    engine: str = Query("glm_ocr", description="Only 'glm_ocr' is estimated"),
):
    """Pages and estimated GLM-OCR time for a file on this machine. Converts nothing.

    The UI asks before a GLM-OCR job longer than `long_job_seconds`.
    """
    if resolve_engine(engine) != EngineKind.GLM_OCR:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only engine=glm_ocr is estimated.")
    filename = file.filename or "file"
    tmp_path = await _save_upload_to_temp(file, Path(filename).suffix)
    try:
        from app.core.engines.glm_ocr_engine import estimate_for_file

        return {"filename": filename, **await asyncio.to_thread(estimate_for_file, tmp_path)}
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@api_router.get("/convert/progress/{job_id}", tags=["Conversion"])
def conversion_progress(job_id: str):
    """Current phase of a conversion started with `job_id` (kept for one hour).

    No session token: it only describes the caller's own job, and the id is a
    random value the caller chose.
    """
    from app.core.jobs import JOBS

    state = JOBS.get(job_id)
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job_id.")
    return state


@api_router.post("/convert/cancel/{job_id}", tags=["Conversion"])
def cancel_conversion(job_id: str):
    """Ask a running conversion to stop before its next phase. Nothing is saved.

    The conversion's own request then ends with 409 `conversion_cancelled`. A
    cancel that arrives before its request is remembered and applied when it does.
    """
    from app.core.jobs import JOBS, is_valid_job_id

    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id.")
    running = JOBS.cancel(job_id)
    return {"status": "cancelling" if running else "not_running", "job_id": job_id}


# Mount API routes at root, /InkDoc, and /MarkItDown (alias)
app.include_router(api_router)
app.include_router(api_router, prefix="/InkDoc")
app.include_router(api_router, prefix="/MarkItDown")


if __name__ == "__main__":
    import uvicorn

    DEFAULT_PORT = 13118
    print(f"Starting InkDoc Local API server at http://127.0.0.1:{DEFAULT_PORT}/InkDoc ...")
    uvicorn.run("app.server.server:app", host="127.0.0.1", port=DEFAULT_PORT, reload=False, app_dir=str(REPO_ROOT))
