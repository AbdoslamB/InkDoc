"""MarkItDown wrapper and background conversion workers.

Wires the GUI to the real markitdown 0.1.7 API as found in
markitdown/_markitdown.py, markitdown/_stream_info.py and
markitdown/converters/_doc_intel_converter.py / _cu_converter.py:

    MarkItDown(
        *,
        enable_builtins: bool | None = None,
        enable_plugins: bool | None = None,
        docintel_endpoint: str | None = ...,
        docintel_credential: ... | None = ...,
        docintel_file_types: list | None = ...,
        docintel_api_version: str | None = ...,
        cu_endpoint: str | None = ...,
        cu_credential: ... | None = ...,
        cu_analyzer_id: str | None = ...,
        cu_file_types: list | None = ...,
        requests_session: requests.Session | None = ...,
    )

    MarkItDown.convert(source, *, stream_info: StreamInfo | None = None, **kwargs)
    MarkItDown.convert_local(path, *, stream_info=None, file_extension=None, url=None, **kwargs)
    MarkItDown.convert_uri(uri, *, stream_info=None, file_extension=None, mock_url=None, **kwargs)

    StreamInfo(mimetype=None, extension=None, charset=None, filename=None,
               local_path=None, url=None)

`keep_data_uris` is a keyword accepted by convert()/convert_local() and is
forwarded down to the markdownify-based converters (see
markitdown/converters/_markdownify.py).
"""
from __future__ import annotations

import os
import re
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

# pydub (pulled in by markitdown audio converters) warns on import when
# ffmpeg is missing. Audio transcription is optional; surface real failures
# at conversion time instead of scaring every startup.
warnings.filterwarnings(
    "ignore",
    message=r"Couldn't find ffmpeg or avconv",
    category=RuntimeWarning,
    module=r"pydub\.utils",
)

# markitdown is imported lazily: importing it loads magika and onnxruntime,
# about 2s that would otherwise sit between launching the app and the window
# appearing. prewarm_markitdown() pays that cost in the background instead.
# tests/test_startup_imports.py keeps a module-level import from creeping back.
if TYPE_CHECKING:
    from markitdown import MarkItDown

from app.core.queue_model import EngineKind, QueueItem, SourceKind

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """Sanitize a URL/string into a safe base filename (no extension)."""
    name = _ILLEGAL_FILENAME_CHARS.sub("_", name).strip(" .")
    return name[:150] if name else "untitled"


def get_markitdown_version() -> str:
    """Best-effort markitdown package version for display (About dialog)."""
    try:
        return pkg_version("markitdown")
    except PackageNotFoundError:
        from markitdown import __about__
        return getattr(__about__, "__version__", "unknown")


def prewarm_markitdown() -> None:
    """Import markitdown ahead of the first conversion.

    Meant to run on a background thread once the UI is up. A conversion that
    arrives while this is still running just waits on Python's import lock.
    """
    try:
        import markitdown  # noqa: F401
    except Exception:
        # The real conversion will raise and report the problem properly.
        pass


def url_to_basename(url: str) -> str:
    """Derive a filesystem-safe basename from a URL (strip scheme/query)."""
    parsed = urlparse(url)
    candidate = (parsed.netloc + parsed.path).strip("/")
    candidate = candidate.replace("/", "_")
    if not candidate:
        candidate = parsed.netloc or "webpage"
    return sanitize_filename(candidate)


def default_basename(item: QueueItem) -> str:
    """The base filename (no extension) an item's markdown result should use.

    A file's display name wins over its source path: the server converts uploads
    from a temporary copy (tmpab12cd.pdf), and naming the result after that copy
    saved every upload to Downloads as tmpab12cd.md instead of report.md.
    """
    if item.kind == SourceKind.URL:
        return url_to_basename(item.source)
    # Normalise separators first: a client-supplied name may carry either kind,
    # and os.path.basename only splits on the host's own.
    name = (item.display_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not name.strip(" ."):
        name = os.path.basename(item.source)
    return os.path.splitext(name)[0]


def get_downloads_dir() -> Path:
    """Resolve the current user's Downloads folder.

    Uses Path.home() so this works for any account on Windows, macOS, or
    Linux, not just the machine this was developed on (never hardcode a
    specific user's path).
    """
    return Path.home() / "Downloads"


def unique_download_path(directory: Path, base_name: str) -> Path:
    """Return a non-colliding '<directory>/<base_name>.md' path without following symlinks.

    If that name already exists, numeric suffixes are appended --
    'name (1).md', 'name (2).md', etc. -- so an existing file is never
    silently overwritten.
    """
    if directory.is_symlink():
        raise ValueError("Security violation: Downloads directory cannot be a symbolic link.")

    directory.mkdir(parents=True, exist_ok=True)
    clean_base = sanitize_filename(base_name)
    candidate = directory / f"{clean_base}.md"
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = directory / f"{clean_base} ({counter}).md"
        counter += 1

    # Ensure path stays strictly inside directory
    if not str(candidate.resolve()).startswith(str(directory.resolve())):
        raise ValueError("Security violation: Path traversal detected in download path.")

    return candidate


def auto_save_markdown(item: QueueItem, markdown_text: str, downloads_dir: Path | None = None) -> Path:
    """Write `markdown_text` to the Downloads folder using a name derived from
    the item's source, avoiding collisions and symlinks. Returns the path written to."""
    directory = downloads_dir if downloads_dir is not None else get_downloads_dir()
    if directory.is_symlink():
        raise ValueError("Security violation: Downloads directory cannot be a symbolic link.")

    base_name = sanitize_filename(default_basename(item))
    path = unique_download_path(directory, base_name)
    path.write_text(markdown_text, encoding="utf-8")
    return path

# Extensions markitdown's built-in converters recognize (used to filter
# recursive folder scans). PlainTextConverter/HtmlConverter also catch many
# text/* and application/*+xml mimetypes generically, but for a folder scan
# we only want to pick up files we have reasonable confidence about.
SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".html", ".htm", ".xml", ".rss", ".atom",
    ".docx", ".xlsx", ".xls", ".pptx", ".pdf", ".csv", ".json", ".ipynb",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".mp3", ".wav", ".m4a", ".msg", ".zip", ".epub",
    ".yaml", ".yml",
}


def is_supported_file(path: str) -> bool:
    """True when `path` has an extension markitdown can reasonably convert."""
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS


def supported_name_filter(label: str) -> str:
    """Qt file-dialog filter that lists only compatible extensions."""
    patterns = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_EXTENSIONS))
    return f"{label} ({patterns})"


class UserFacingConversionError(RuntimeError):
    """A conversion failure whose message is written for the user and shown as is.

    The server normally rewrites exception text (to hide paths and stack-trace
    noise); an engine raises this when it has a precise, safe sentence to say,
    such as "This PDF is password-protected".
    """


@dataclass
class ConversionOptions:
    """Options mirrored from the Options panel, mapped to real MarkItDown kwargs."""

    enable_plugins: bool = False
    keep_data_uris: bool = False

    docintel_enabled: bool = False
    docintel_endpoint: str = ""

    cu_enabled: bool = False
    cu_endpoint: str = ""
    cu_analyzer_id: str = ""

    # Optional engine selection route (defaults to standard MarkItDown)
    engine: EngineKind = EngineKind.MARKITDOWN
    docling_ocr: bool = True
    docling_table_structure: bool = True
    docling_code_enrichment: bool = False
    docling_formula_enrichment: bool = False
    fallback_occurred: bool = False
    fallback_reason: str = ""

    # Whether an engine may substitute another one on its own when it fails.
    # Docling consults this together with the user's fallback_to_markitdown
    # setting. Auto turns it off because it runs its own fallback, in the right
    # lane (app/core/auto_engine.py).
    allow_engine_fallback: bool = True
    # Run the missing-text check after a PDF conversion (app/core/quality_check.py).
    quality_check: bool = True

    # Results written by whichever path ran, read by the server. Plain data only.
    engine_used: EngineKind | None = None   # always concrete, never AUTO
    auto_decision: Any = None               # auto_engine.AutoDecision when engine=auto
    quality_report: Any = None              # quality_check.QualityReport for PDFs
    # Notes about the result that are not failures (GLM-OCR: a page that looped
    # and was cut short, the GPU falling back to the CPU). Replaced, never
    # mutated in place, since dataclasses.replace shares the list.
    engine_warnings: list[str] = field(default_factory=list)

    # Progress/cancel handle (app/core/jobs.JobHandle) for engines that report
    # per page. Not a result: set by the server, read by the engine.
    job: Any = None

    def build_markitdown(self) -> MarkItDown:
        import requests
        from markitdown import MarkItDown
        from requests.adapters import HTTPAdapter

        class _TimeoutAdapter(HTTPAdapter):
            def send(self, request, **kwargs):
                if kwargs.get("timeout") is None:
                    kwargs["timeout"] = (5.0, 15.0)
                return super().send(request, **kwargs)

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/markdown, text/html;q=0.9, text/plain;q=0.8, */*;q=0.1",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        adapter = _TimeoutAdapter()
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        kwargs: dict = {
            "enable_plugins": self.enable_plugins,
            "requests_session": session,
        }
        if self.docintel_enabled and self.docintel_endpoint.strip():
            kwargs["docintel_endpoint"] = self.docintel_endpoint.strip()
        if self.cu_enabled and self.cu_endpoint.strip():
            kwargs["cu_endpoint"] = self.cu_endpoint.strip()
            if self.cu_analyzer_id.strip():
                kwargs["cu_analyzer_id"] = self.cu_analyzer_id.strip()
        return MarkItDown(**kwargs)


@dataclass
class FetchedSource:
    """A URL fetched to a local temporary file, plus what MarkItDown should know about it.

    Kept apart from the conversion so the temporary file can outlive it: the
    server fetches once, then routes, converts, saves and checks the same file,
    and only then deletes it (cleanup()).
    """

    path: Path
    final_url: str
    filename: str
    content_type: str
    stream_info_kwargs: dict[str, str]

    def cleanup(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass


def _override_stream_info(item: QueueItem) -> dict[str, str]:
    """StreamInfo hints from the item's explicit extension/mimetype/charset overrides."""
    kwargs: dict[str, str] = {}
    if item.extension_override.strip():
        ext = item.extension_override.strip()
        kwargs["extension"] = ext if ext.startswith(".") else f".{ext}"
    if item.mimetype_override.strip():
        kwargs["mimetype"] = item.mimetype_override.strip()
    if item.charset_override.strip():
        kwargs["charset"] = item.charset_override.strip()
    return kwargs


def open_url_source(item: QueueItem) -> FetchedSource:
    """Fetch a URL item to a temporary file through the SSRF-safe fetch layer.

    The caller owns the result and must call cleanup() (fetch_url_source does).
    Explicit overrides on the item win over what the server reported, exactly as
    when this lived inside convert_item.
    """
    import mimetypes

    from app.core.url_fetcher import fetch_url_safely

    fetched = fetch_url_safely(item.source, to_temp_file=True)
    if fetched.temp_file_path is None:  # pragma: no cover - to_temp_file=True always sets it
        raise RuntimeError("URL fetch did not produce a file.")

    hints = _override_stream_info(item)
    if "extension" not in hints:
        if Path(fetched.filename).suffix:
            hints["extension"] = Path(fetched.filename).suffix
        elif fetched.content_type:
            guessed_ext = mimetypes.guess_extension(fetched.content_type)
            if guessed_ext:
                hints["extension"] = guessed_ext
    if "mimetype" not in hints and fetched.content_type:
        hints["mimetype"] = fetched.content_type
    if "charset" not in hints and fetched.charset:
        hints["charset"] = fetched.charset

    return FetchedSource(
        path=fetched.temp_file_path,
        final_url=fetched.final_url,
        filename=fetched.filename,
        content_type=fetched.content_type or "",
        stream_info_kwargs=hints,
    )


@contextmanager
def fetch_url_source(item: QueueItem) -> Iterator[FetchedSource]:
    """Context-managed open_url_source(): the temporary file is deleted on exit."""
    source = open_url_source(item)
    try:
        yield source
    finally:
        source.cleanup()


def fetched_file_item(item: QueueItem, fetched: FetchedSource, engine: EngineKind) -> QueueItem:
    """The local-file QueueItem that converts `fetched` on behalf of URL item `item`."""
    return QueueItem(
        source=str(fetched.path),
        kind=SourceKind.FILE,
        display_name=item.display_name,
        engine=engine,
        extension_override=item.extension_override,
        mimetype_override=item.mimetype_override,
        charset_override=item.charset_override,
    )


def convert_local(
    file_item: QueueItem,
    options: ConversionOptions,
    *,
    fetched: FetchedSource | None = None,
) -> str:
    """Convert one local file with its concrete engine.

    `fetched` is set when the file is a downloaded URL: MarkItDown then receives
    the original URL and the server's content-type hints, which its YouTube,
    Wikipedia and HTML converters rely on.

    Records the engine that produced the result in options.engine_used. An
    engine that hands the work to another one (a Docling fallback, Markit's
    MarkItDown route) goes back through this function, which overwrites it.
    """
    engine = getattr(file_item, "engine", None) or options.engine
    if engine == EngineKind.AUTO:
        # Auto needs lanes and a probe; a direct caller (scripts, the AGENTS.md
        # example) gets the same routing run to completion on this thread.
        from app.core.auto_engine import run_auto_blocking

        return run_auto_blocking(file_item, options, fetched=fetched)

    options.engine_used = engine
    if engine == EngineKind.DOCLING:
        from app.core.engines.docling_engine import convert_with_docling
        return convert_with_docling(file_item, options)
    if engine == EngineKind.MARKIT:
        from app.core.engines.markit_engine import convert_with_markit
        return convert_with_markit(file_item, options)
    if engine == EngineKind.GLM_OCR:
        from app.core.engines.glm_ocr_engine import convert_with_glm_ocr
        return convert_with_glm_ocr(file_item, options)

    from markitdown import StreamInfo

    md = options.build_markitdown()
    stream_info_kwargs = dict(fetched.stream_info_kwargs) if fetched else _override_stream_info(file_item)

    convert_kwargs: dict[str, Any] = {}
    if options.keep_data_uris:
        convert_kwargs["keep_data_uris"] = True
    if fetched is not None:
        convert_kwargs["url"] = fetched.final_url

    stream_info = StreamInfo(**stream_info_kwargs) if stream_info_kwargs else None
    result = md.convert_local(file_item.source, stream_info=stream_info, **convert_kwargs)
    return result.text_content


def convert_item(
    item: QueueItem,
    options: ConversionOptions,
    *,
    fetched: FetchedSource | None = None,
) -> str:
    """Run an actual conversion for a single queue item.

    If the item source is a URL, it is safely fetched through the unified
    fetch layer with SSRF validation, IP pinning, timeouts, and size caps, and
    the temporary file is deleted afterwards. A caller that has already fetched
    it (the server, so it can check the file after converting) passes `fetched`
    and stays responsible for its cleanup.
    """
    engine = getattr(item, "engine", None) or options.engine
    if item.kind == SourceKind.URL:
        if fetched is not None:
            return convert_local(fetched_file_item(item, fetched, engine), options, fetched=fetched)
        with fetch_url_source(item) as source:
            return convert_local(fetched_file_item(item, source, engine), options, fetched=source)
    return convert_local(item, options)


def detect_type_label(source: str, kind: SourceKind) -> str:
    """Best-effort, cheap type label for the queue table (no conversion)."""
    if kind == SourceKind.URL:
        lowered = source.lower()
        if "youtube.com" in lowered or "youtu.be" in lowered:
            return "YouTube"
        return "type.webpage"
    ext = os.path.splitext(source)[1].lower()
    return ext.lstrip(".").upper() if ext else "type.file"


def iter_supported_files(folder: str):
    """Recursively yield file paths under `folder` matching SUPPORTED_EXTENSIONS."""
    for root, _dirs, files in os.walk(folder):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                yield os.path.join(root, name)


