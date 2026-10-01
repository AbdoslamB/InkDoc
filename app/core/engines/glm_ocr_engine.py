"""GLM-OCR conversion engine: PDFs and images to Markdown, one page at a time.

Called from converter.convert_local like Docling and Markit. Each page is
rendered (PDF via pypdfium2, images via Pillow), scaled to the render size for
the active acceleration, sent to the local llama-server as a PNG data URI with
the prompt "Text Recognition:", and the per-page Markdown is joined with a
blank line. GLM-OCR emits LaTeX for math and HTML/Markdown tables; both are
kept as they are (the preview sanitises HTML with DOMPurify).

Memory stays flat: one page image exists at a time. pdfium is not thread-safe,
so the shared pdfium lock (pdf_probe.pdfium_lock) is held per page render,
never across the whole job.

Progress and cancel go through the job handle on ConversionOptions: the page
counter drives "Page 7 / 30 · ~8 min left" in the queue row, and a cancel
stops before the next page (409 conversion_cancelled, nothing saved).
"""
from __future__ import annotations

import io
import logging
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.converter import UserFacingConversionError
from app.core.jobs import NULL_JOB

if TYPE_CHECKING:
    from app.core.converter import ConversionOptions
    from app.core.queue_model import QueueItem

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp")
PROMPT_TEXT = "Text Recognition:"

# Context budget (7.1): image tokens + prompt + max_tokens must fit --ctx-size.
PROMPT_OVERHEAD_TOKENS = 64
MIN_OUTPUT_TOKENS = 1024
MAX_OUTPUT_TOKENS = 4096
# GLM-OCR's vision encoder: 14 px patches merged 2x2, so one token per 28x28 px.
PIXELS_PER_TOKEN_SIDE = 28
RERENDER_FACTOR = 0.75
MAX_RERENDERS = 3
MAX_UPSCALE = 2.0

# Repetition guard (7.1 step 5).
REPEAT_PENALTY_RETRY = 1.1
REPEAT_MIN_RUN = 6            # the same line this many times in a row is a loop
REPEAT_PERIOD_MAX = 80        # characters in a repeating unit
REPEAT_PERIOD_MIN_COPIES = 5


class GlmOcrUnsupportedFormat(UserFacingConversionError):
    """The input is neither a PDF nor an image GLM-OCR can read.

    The message starts "Unsupported format" so the UI files the item under
    Unsupported, not Failed.
    """


def _user_error(message: str) -> Exception:
    return UserFacingConversionError(message)


# ─── Pure helpers (tested directly) ──────────────────────────────────────────
def estimate_image_tokens(width: int, height: int) -> int:
    return math.ceil(width / PIXELS_PER_TOKEN_SIDE) * math.ceil(height / PIXELS_PER_TOKEN_SIDE) + 2


def output_budget(ctx_size: int, image_tokens: int) -> int | None:
    """max_tokens for a page, clamped to [1024, 4096]; None when the page cannot fit."""
    room = ctx_size - image_tokens - PROMPT_OVERHEAD_TOKENS
    if room < MIN_OUTPUT_TOKENS:
        return None
    return min(MAX_OUTPUT_TOKENS, room)


def scaled_size(width: int, height: int, long_side: int) -> tuple[int, int]:
    """Scale so the long side is `long_side`, never upscaling more than 2x."""
    longest = max(width, height, 1)
    factor = long_side / longest
    factor = min(factor, MAX_UPSCALE)
    return max(1, round(width * factor)), max(1, round(height * factor))


def find_repetition_start(text: str) -> int | None:
    """Index where a trailing loop starts, or None when the text does not end in one.

    Two shapes: the same non-empty line many times in a row, and a short unit of
    characters repeated back to back (the model stuck on "| | | |" or a word).
    """
    lines = text.rstrip().split("\n")
    if len(lines) >= REPEAT_MIN_RUN:
        last = lines[-1].strip()
        if last:
            run = 0
            for line in reversed(lines):
                if line.strip() == last:
                    run += 1
                else:
                    break
            if run >= REPEAT_MIN_RUN:
                keep = len(lines) - run + 1           # keep one copy of the line
                return len("\n".join(lines[:keep]))
    stripped = text.rstrip()
    for period in range(1, REPEAT_PERIOD_MAX + 1):
        span = period * REPEAT_PERIOD_MIN_COPIES
        if len(stripped) < span:
            break
        unit = stripped[-period:]
        if not unit.strip():
            continue
        if stripped[-span:] == unit * REPEAT_PERIOD_MIN_COPIES:
            start = len(stripped) - span
            while start - period >= 0 and stripped[start - period:start] == unit:
                start -= period
            return start + period                     # keep one copy of the unit
    return None


def clean_page_output(text: str) -> str:
    """Strip a Markdown code fence the model sometimes wraps its whole answer in."""
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1 and t.endswith("```"):
            t = t[first_nl + 1:-3].strip()
    return t


# ─── Page sources ────────────────────────────────────────────────────────────
def _flatten_rgb(img: Any) -> Any:
    """RGB with transparency composited on white (a transparent PNG would read as black)."""
    from PIL import Image

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


class _ImagePages:
    """Frames of an image file. Multi-frame TIFFs are pages; other formats use frame 0."""

    def __init__(self, path: str) -> None:
        from PIL import Image

        try:
            self._img = Image.open(path)
            self._img.load()
        except Exception as exc:
            raise _user_error(f"This image could not be opened: {exc}") from exc
        fmt = (self._img.format or "").upper()
        self.count = int(getattr(self._img, "n_frames", 1) or 1) if fmt == "TIFF" else 1

    def render(self, index: int, long_side: int) -> Any:
        from PIL import Image, ImageOps

        if self.count > 1:
            self._img.seek(index)
        frame = self._img.copy()
        # Phone photos keep their rotation in EXIF; without this they reach the
        # model sideways.
        frame = ImageOps.exif_transpose(frame) or frame
        frame = _flatten_rgb(frame)
        size = scaled_size(frame.width, frame.height, long_side)
        if size != frame.size:
            frame = frame.resize(size, Image.LANCZOS)
        return frame

    def close(self) -> None:
        try:
            self._img.close()
        except Exception:
            pass


class _PdfPages:
    """Pages of a PDF, rendered on demand with the shared pdfium lock held per page."""

    def __init__(self, path: str) -> None:
        import pypdfium2 as pdfium

        from app.core.pdf_probe import pdfium_lock

        self._lock = pdfium_lock()
        try:
            with self._lock:
                self._doc = pdfium.PdfDocument(path)
                self.count = len(self._doc)
        except Exception as exc:
            text = str(exc).lower()
            if "password" in text:
                raise _user_error(
                    "This PDF is password-protected. Remove the password and try again."
                ) from exc
            raise _user_error(f"This PDF could not be opened: {exc}") from exc

    def render(self, index: int, long_side: int) -> Any:
        with self._lock:
            page = self._doc[index]
            try:
                width, height = page.get_size()
                # Points to pixels; pdfium applies the page's own /Rotate.
                scale = long_side / max(width, height, 1.0)
                bitmap = page.render(scale=scale)
                try:
                    return bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()

    def close(self) -> None:
        with self._lock:
            try:
                self._doc.close()
            except Exception:
                pass


def open_pages(path: str) -> _PdfPages | _ImagePages:
    from app.core.pdf_probe import is_pdf

    if is_pdf(path):
        return _PdfPages(path)
    if Path(path).suffix.lower() in IMAGE_EXTENSIONS:
        return _ImagePages(path)
    # Uploads keep their extension, but sniff anyway in case it is missing.
    try:
        from PIL import Image

        with Image.open(path) as probe:
            probe.verify()
        return _ImagePages(path)
    except Exception:
        pass
    raise GlmOcrUnsupportedFormat(
        "Unsupported format: GLM-OCR reads PDFs and images. Use MarkItDown for this file."
    )


def count_pages(path: str) -> int | None:
    """Pages GLM-OCR would read in `path` (PDF pages or TIFF frames). None if unknown."""
    try:
        pages = open_pages(path)
    except Exception:
        return None
    try:
        return pages.count
    finally:
        pages.close()


def _png_bytes(img: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


# ─── Conversion ──────────────────────────────────────────────────────────────
def convert_with_glm_ocr(item: QueueItem, options: ConversionOptions) -> str:
    from app.core.engines.glm_ocr_server import (
        DEFAULT_CTX_SIZE,
        GlmOcrServer,
        GlmOcrServerError,
        image_to_data_uri,
    )
    from app.core.glm_ocr_manager import GlmOcrManager

    mgr = GlmOcrManager.get_instance()
    if not mgr.is_installed():
        raise _user_error("GLM-OCR is not installed. Download it in Settings before use.")
    if not mgr.is_usable():
        raise _user_error(
            "GLM-OCR is installed but did not pass its self-test. Open Settings and run the self-test again."
        )

    job = getattr(options, "job", None) or NULL_JOB
    server = GlmOcrServer.get_instance()
    pages = open_pages(item.source)
    total = pages.count
    long_side = mgr.render_long_side()
    ctx_size = DEFAULT_CTX_SIZE
    token_factor = 1.0          # actual / estimated image tokens, learned from page 1
    results: list[str] = []
    warnings: list[str] = []
    page_seconds: list[float] = []

    try:
        if total <= 0:
            return ""
        cold = not server.is_running()
        if cold:
            job.set_phase("starting", "Starting GLM-OCR…", engine="glm_ocr")
        job.set_pages(0, total)
        for index in range(total):
            job.raise_if_cancelled()
            job.set_phase(
                "converting",
                f"Reading page {index + 1} of {total} with GLM-OCR…" if total > 1 else "Reading with GLM-OCR…",
                engine="glm_ocr",
            )
            started = time.time()
            text, token_factor = _read_page(
                server, pages, index, total, long_side, ctx_size, token_factor, warnings, image_to_data_uri,
            )
            if not (cold and index == 0):     # the first page after a cold start also loads the model
                page_seconds.append(time.time() - started)
            results.append(text)
            job.set_pages(index + 1, total)
    except GlmOcrServerError as exc:
        # Its messages are written for the user ("did not become ready within 90
        # seconds", "needs the Visual C++ Redistributable").
        raise UserFacingConversionError(str(exc)) from exc
    finally:
        pages.close()
        warnings.extend(server.take_notices())
        if warnings:
            existing = list(getattr(options, "engine_warnings", []) or [])
            options.engine_warnings = existing + warnings

    if page_seconds:
        mgr.record_measurement(sum(page_seconds) / len(page_seconds), len(page_seconds), accel=server.accel)
    return "\n\n".join(r for r in results if r) + ("\n" if results else "")


def _read_page(
    server: Any,
    pages: _PdfPages | _ImagePages,
    index: int,
    total: int,
    long_side: int,
    ctx_size: int,
    token_factor: float,
    warnings: list[str],
    to_uri: Any,
) -> tuple[str, float]:
    """OCR one page. Returns (markdown, updated actual/estimated image-token factor)."""
    label = f"Page {index + 1}" if total > 1 else "The image"
    side = long_side
    for _attempt in range(MAX_RERENDERS + 1):
        img = pages.render(index, side)
        est = int(estimate_image_tokens(img.width, img.height) * token_factor)
        budget = output_budget(ctx_size, est)
        if budget is not None:
            break
        side = int(side * RERENDER_FACTOR)
    else:
        raise _user_error(f"{label} is too large for GLM-OCR to read.")

    uri = to_uri(_png_bytes(img))
    del img
    result = server.ocr(uri, PROMPT_TEXT, budget)
    if result.prompt_tokens > 0 and est > 0:
        # llama.cpp may resize images itself; learn the real token count from page 1.
        token_factor = max(0.2, min(3.0, result.prompt_tokens / max(1.0, est / token_factor)))

    text = result.text
    looped = result.finish_reason == "length" or find_repetition_start(text) is not None
    if looped:
        logger.info("GLM-OCR %s hit its limit or repeated itself; retrying with a repeat penalty.", label.lower())
        retry = server.ocr(uri, PROMPT_TEXT, budget, repeat_penalty=REPEAT_PENALTY_RETRY)
        text = retry.text
        cut = find_repetition_start(text)
        if cut is not None or retry.finish_reason == "length":
            if cut is not None:
                text = text[:cut]
            warnings.append(
                f"{label}: the model started repeating itself, so the text after that point was dropped."
            )
    return clean_page_output(text), token_factor


def estimate_for_file(path: str) -> dict[str, Any]:
    """Pages and estimated seconds for converting `path` with GLM-OCR now."""
    from app.core.glm_ocr_manager import LONG_JOB_SECONDS, GlmOcrManager

    mgr = GlmOcrManager.get_instance()
    pages = count_pages(path)
    seconds = mgr.estimate_seconds(pages or 1) if pages is not None else None
    return {
        "pages": pages,
        "seconds_est": round(seconds, 1) if seconds is not None else None,
        "seconds_per_page": round(mgr.seconds_per_page(), 1),
        "accel": mgr.active_accel(),
        "long_job_seconds": LONG_JOB_SECONDS,
        "long_job": bool(seconds is not None and seconds > LONG_JOB_SECONDS),
    }


__all__ = [
    "GlmOcrUnsupportedFormat",
    "IMAGE_EXTENSIONS",
    "clean_page_output",
    "convert_with_glm_ocr",
    "count_pages",
    "estimate_for_file",
    "estimate_image_tokens",
    "find_repetition_start",
    "open_pages",
    "output_budget",
    "scaled_size",
]

