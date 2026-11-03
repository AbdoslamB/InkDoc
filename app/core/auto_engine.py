"""Auto engine: pick the engine per file, then verify the result.

"Auto" is a routing mode, not a converter. For each file it:

1. probes PDFs cheaply (app/core/pdf_probe.py) and picks a concrete engine:
   MarkItDown for digital documents, the best installed OCR engine for scans
   and images, Markit for the few formats MarkItDown has no converter for;
2. converts with that engine, in that engine's lane;
3. runs the missing-text check on PDFs (app/core/quality_check.py);
4. if the check says an OCR engine would do better and one is installed,
   converts again with it and keeps the better of the two results;
5. at the Balanced and Thorough "Auto quality" levels, checks the PDF's tables
   against the PDF itself and replaces the ones the engine got wrong
   (app/core/table_stage.py).

Auto is the only mode that ever re-runs a conversion by itself, and every
choice it makes is reported (engine_used, auto.reason, auto.escalation).

Decisions are pure functions (decide, should_escalate, pick_result) so they are
tested without servers, threads or files. Only run_auto touches lanes and
threads, and it acquires a lane in async code *before* handing work to a
thread, the same way the server's endpoints do: a job waiting for the Docling
lane never occupies a thread, so a batch of scans cannot starve MarkItDown.

Model availability: Auto only uses what is on disk. It never installs,
downloads or waits for a model; the check runs on every conversion, so
installing or removing Docling takes effect on the next file.

GLM-OCR comes first among the OCR engines when it is installed, but only for a
file it is estimated to finish within GLM_OCR_TIME_LIMIT_S (the same limit at
which the UI asks before an explicit GLM-OCR job). Above it, Auto routes the
file to Docling instead, and says so; choosing the GLM-OCR pill explicitly
still runs it.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.engines.layout_model import ADDON as LAYOUT_MODEL
from app.core.jobs import NULL_JOB, ConversionCancelledError, JobHandle
from app.core.pdf_probe import PdfProbe, is_pdf, probe_pdf
from app.core.quality_check import (
    SKIP_UNREADABLE,
    QualityReport,
    assess,
    format_pages,
)
from app.core.queue_model import EngineKind, QueueItem
from app.core.thorough_passes import LEVEL_THOROUGH, PASSES, PassLog, PassPlan, level_rank

if TYPE_CHECKING:
    from app.core.converter import ConversionOptions, FetchedSource

logger = logging.getLogger(__name__)

# ─── Routing table ───────────────────────────────────────────────────────────
ROUTE_MARKITDOWN = "markitdown"
ROUTE_MARKIT = "markit"
ROUTE_OCR = "ocr"

# Extensions Docling's image pipeline accepts. GIF is not among them, so a GIF
# stays with MarkItDown (EXIF only) rather than failing in Docling.
OCR_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp")
# What each OCR engine reads. GLM-OCR takes GIF too (Pillow decodes it).
ENGINE_IMAGE_EXTENSIONS: dict[EngineKind, tuple[str, ...]] = {
    EngineKind.DOCLING: OCR_IMAGE_EXTENSIONS,
    EngineKind.GLM_OCR: (*OCR_IMAGE_EXTENSIONS, ".gif"),
}

# One dict so tuning a route is a one-line change. Anything not listed (Office,
# HTML, CSV, JSON, text, MSG, ZIP, audio, GIF) goes to MarkItDown. EPUB, IPYNB
# and RSS/Atom are provisional: MarkItDown has dedicated converters for them,
# and the format bake-off in scripts/calibrate_quality_check.py may move one.
AUTO_ROUTES: dict[str, str] = {
    ".epub": ROUTE_MARKITDOWN,
    ".ipynb": ROUTE_MARKITDOWN,
    ".rss": ROUTE_MARKITDOWN,
    ".atom": ROUTE_MARKITDOWN,
    # MarkItDown has no YAML converter (application/yaml is not text/*), and
    # treats XML as plain text; Markit renders both as readable Markdown.
    ".yaml": ROUTE_MARKIT,
    ".yml": ROUTE_MARKIT,
    ".xml": ROUTE_MARKIT,
    **dict.fromkeys(OCR_IMAGE_EXTENSIONS, ROUTE_OCR),
}

FORMAT_LABELS = {
    ".epub": "EPUB",
    ".ipynb": "Jupyter notebook",
    ".rss": "RSS feed",
    ".atom": "Atom feed",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".xml": "XML",
}

# A PDF with at least this share of plain full-page images (no scan evidence)
# goes to OCR anyway: that many image pages probably carry text. It also needs
# at least ROUTE_IMAGE_PAGE_MIN of them, so a lone cover photo never qualifies,
# however short the document (one cover in a four-page brochure is 25%).
ROUTE_IMAGE_PAGE_FRACTION = 0.20
ROUTE_IMAGE_PAGE_MIN = 2
# On escalation, keep the OCR result unless its coverage is lower than the first
# result's by more than this. On a near tie the OCR engine's structure wins.
KEEP_OCR_MARGIN = 0.02

ENGINE_LABELS = {
    EngineKind.MARKITDOWN: "MarkItDown",
    EngineKind.DOCLING: "Docling",
    EngineKind.MARKIT: "Markit",
    EngineKind.GLM_OCR: "GLM-OCR",
}

# The OCR engines Auto can route to, best first.
OCR_ENGINE_ORDER: tuple[EngineKind, ...] = (EngineKind.GLM_OCR, EngineKind.DOCLING)

# Auto uses GLM-OCR for a file only when it should finish within this many
# seconds; otherwise the file goes to Docling. Same value as the UI's long-job
# confirmation (glm_ocr_manager.LONG_JOB_SECONDS).
GLM_OCR_TIME_LIMIT_S = 300.0


def engine_label(engine: EngineKind) -> str:
    return ENGINE_LABELS.get(engine, engine.value)


@dataclass
class AutoDecision:
    engine: EngineKind                # concrete engine chosen
    reason: str                       # "Digital PDF with a full text layer", "5 of 8 pages are scanned images"…
    hint: str = ""                    # "Docling would read the 5 scanned pages. Install it in Settings."
    probe: PdfProbe | None = None
    escalated: bool = False
    escalation: dict[str, Any] | None = None
    # {"from": "markitdown", "from_missing_pct": 18, "to": "docling",
    #  "kept": "docling" | "markitdown", "error": str | None}
    # A page split (Auto quality: Thorough): [{"engine": "docling", "pages": "1-12"}, ...]
    # in page order; `engine` is then the one that read the most pages.
    segments: list[dict[str, str]] | None = None

    def to_dict(self, engine_used: EngineKind | None = None) -> dict[str, Any]:
        return {
            "chosen": (engine_used or self.engine).value,
            "routed_to": self.engine.value,
            "reason": self.reason,
            "hint": self.hint,
            "escalated": self.escalated,
            "escalation": self.escalation,
            "segments": self.segments,
        }


# ─── Page split (Auto quality: Thorough) ─────────────────────────────────────
# A mixed PDF (text pages and scanned ones) is read page by page: Docling for
# the pages with a text layer, GLM-OCR only for the pages that need OCR, then
# joined in page order. Only with both engines usable, only when it leaves at
# least SPLIT_MIN_TEXT_PAGES to Docling, and only in at most SPLIT_MAX_RUNS
# runs of consecutive pages (each run is one conversion).
SPLIT_MIN_TEXT_PAGES = 2
SPLIT_MAX_RUNS = 8


@dataclass
class PageSegment:
    engine: EngineKind
    pages: list[int]                  # 1-based, consecutive


def plan_page_split(
    probe: PdfProbe | None, ocr_engines: list[EngineKind], *, reread_ocr_layer: bool = False,
) -> list[PageSegment] | None:
    """The page runs and the engine for each, or None to convert the file whole. Pure.

    Pages needing OCR are the scanned ones, plus the full-page images when
    there are enough of them that decide() would send the file to OCR, plus
    (`reread_ocr_layer`, the ocr_layer_pages step) scans that carry an
    earlier OCR's text: that text is someone else's reading, GLM-OCR reads
    the image itself.
    """
    if probe is None or probe.truncated or not probe.page_count or len(probe.pages) != probe.page_count:
        return None
    if EngineKind.GLM_OCR not in ocr_engines or EngineKind.DOCLING not in ocr_engines:
        return None
    ocr = set(probe.scan_pages)
    if reread_ocr_layer:
        ocr |= set(ocr_layer_pages(probe))
    images = probe.image_pages
    if len(images) >= ROUTE_IMAGE_PAGE_MIN and len(images) / len(probe.pages) >= ROUTE_IMAGE_PAGE_FRACTION:
        ocr |= set(images)
    if not ocr or probe.page_count - len(ocr) < SPLIT_MIN_TEXT_PAGES:
        return None
    segments: list[PageSegment] = []
    for page in range(1, probe.page_count + 1):
        engine = EngineKind.GLM_OCR if page in ocr else EngineKind.DOCLING
        if segments and segments[-1].engine == engine:
            segments[-1].pages.append(page)
        else:
            segments.append(PageSegment(engine, [page]))
    return segments if len(segments) <= SPLIT_MAX_RUNS else None


# ─── Digital PDFs at Thorough ────────────────────────────────────────────────
# MarkItDown reads a PDF's text layer line by line across the whole page, so
# two columns, side-by-side tables and sidebars come out interleaved. At
# Thorough a digital PDF goes to a layout-aware engine instead: Docling (it
# keeps the PDF's own text and rebuilds tables), or GLM-OCR when Docling is
# not installed. Long PDFs go to Docling in parts of THOROUGH_DOCLING_PAGES.
THOROUGH_DOCLING_PAGES = 12


def thorough_digital_route(decision: AutoDecision, docling_ok: bool, glm_ok: bool) -> None:
    """At Thorough, move a digital PDF off MarkItDown onto a layout-aware engine. Pure."""
    if decision.engine != EngineKind.MARKITDOWN or "Digital PDF" not in decision.reason:
        return
    if docling_ok:
        decision.engine = EngineKind.DOCLING
        decision.reason += " (Thorough: Docling follows the page layout and keeps the PDF's own text)"
    elif glm_ok:
        decision.engine = EngineKind.GLM_OCR
        decision.reason += " (Thorough: GLM-OCR follows the page layout; Docling isn't installed)"


def ocr_layer_pages(probe: PdfProbe | None) -> list[int]:
    """1-based pages that are scans with an earlier OCR's text on them."""
    return [p.index + 1 for p in probe.pages if p.ocr_layer] if probe is not None else []


# A PDF this much made of OCR'd scans goes to GLM-OCR whole (the ocr_layer_pages step).
OCR_LAYER_ROUTE_SHARE = 0.5


def thorough_ocr_layer_route(decision: AutoDecision, probe: PdfProbe | None, glm_ok: bool) -> int:
    """At Thorough, send a PDF of OCR'd scans to GLM-OCR rather than copy their old OCR. Pure.

    Returns the number of such pages GLM-OCR will read (0 when the route stands).
    """
    pages = ocr_layer_pages(probe)
    if not glm_ok or not pages or probe is None or decision.engine == EngineKind.GLM_OCR:
        return 0
    if len(pages) < OCR_LAYER_ROUTE_SHARE * max(1, len(probe.pages)):
        return 0
    decision.engine = EngineKind.GLM_OCR
    decision.reason = (
        _pages_phrase(len(pages), probe.page_count or len(probe.pages), "is a scan", "are scans")
        + " with an earlier OCR layer (Thorough: GLM-OCR reads the images again)"
    )
    return len(pages)


def docling_chunks(probe: PdfProbe | None) -> list[PageSegment] | None:
    """Docling runs of at most THOROUGH_DOCLING_PAGES pages, or None for a short PDF."""
    if probe is None or probe.page_count <= THOROUGH_DOCLING_PAGES:
        return None
    pages = list(range(1, probe.page_count + 1))
    return [
        PageSegment(EngineKind.DOCLING, pages[i:i + THOROUGH_DOCLING_PAGES])
        for i in range(0, len(pages), THOROUGH_DOCLING_PAGES)
    ]


def _polish(markdown: str, probe: PdfProbe, used: EngineKind, options: ConversionOptions) -> str:
    """Thorough: put back the PDF's '&', dashes and quotes an engine simplified (app/core/text_polish.py)."""
    if used in (EngineKind.MARKITDOWN, EngineKind.MARKIT):
        return markdown  # they copy the text layer as it is
    from app.core.text_polish import (
        restore_typography,
        unescape_ampersands,
        unescape_angle_brackets,
    )

    try:
        markdown, amps = unescape_ampersands(markdown)
        markdown, angles = unescape_angle_brackets(markdown)
        amps += angles
        markdown, words = restore_typography(markdown, [p.text for p in probe.pages])
    except Exception as exc:  # a nicety: never a reason to fail
        logger.warning("Thorough polish skipped: %s", exc)
        return markdown
    options.polish_counts = (amps, words)
    return markdown


def split_decision(segments: list[PageSegment], probe: PdfProbe) -> AutoDecision:
    """The decision Auto reports for a page split."""
    from app.core.pdf_pages import page_runs

    counts = {e: sum(len(s.pages) for s in segments if s.engine == e) for e in (EngineKind.DOCLING, EngineKind.GLM_OCR)}
    primary = max(counts, key=lambda e: counts[e])
    n_text, n_ocr = counts[EngineKind.DOCLING], counts[EngineKind.GLM_OCR]
    scans = set(probe.scan_pages) | set(ocr_layer_pages(probe))
    kind = "scanned" if all(p in scans for s in segments if s.engine == EngineKind.GLM_OCR for p in s.pages) else "image"
    reason = (
        f"Mixed PDF: {n_text} text page{'s' if n_text != 1 else ''} read with Docling, "
        f"{n_ocr} {kind} page{'s' if n_ocr != 1 else ''} with GLM-OCR"
    )
    return AutoDecision(
        primary, reason, probe=probe,
        segments=[{"engine": s.engine.value, "pages": page_runs(s.pages)} for s in segments],
    )


# ─── Availability (disk only, never cached) ──────────────────────────────────
def _dir_has_files(path: Path) -> bool:
    try:
        for _root, _dirs, files in os.walk(path):
            if files:
                return True
    except OSError:
        return False
    return False


def _source_mode_models_present() -> bool:
    """Whether a pip-installed Docling (no pack) already has its models locally.

    Source mode is development-only. Docling then resolves models through the
    Hugging Face cache, so "present" means the layout model is in that cache,
    or DOCLING_ARTIFACTS_PATH points at a populated directory. Unlike the pack
    worker, an in-process Docling cannot be forced offline after
    huggingface_hub has been imported (it reads HF_HUB_OFFLINE once, at
    import), so this check is what keeps Auto from triggering a download there.
    """
    artifacts = os.environ.get("DOCLING_ARTIFACTS_PATH", "").strip()
    if artifacts and _dir_has_files(Path(artifacts)):
        return True
    cache = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if cache:
        hub = Path(cache)
    else:
        home = os.environ.get("HF_HOME")
        hub = Path(home) / "hub" if home else Path.home() / ".cache" / "huggingface" / "hub"
    try:
        names = [entry.name.lower() for entry in os.scandir(hub) if entry.is_dir()]
    except OSError:
        return False
    return any(
        name.startswith("models--") and ("docling-layout" in name or "docling-models" in name)
        for name in names
    )


def docling_usable() -> bool:
    """Docling is installed, its models are on disk, and no install/removal is running.

    Stricter than "installed": EngineManager.is_pack_installed only checks the
    interpreter and worker script, so a pack whose models/ folder is empty
    would be reported usable and then fail every conversion. Never raises.
    """
    try:
        from app.core.engine_manager import EngineManager, EngineStatus

        mgr = EngineManager.get_instance()
        if mgr.is_install_in_progress("docling"):
            return False
        if mgr.is_pack_installed("docling"):
            return _dir_has_files(Path(mgr.get_engine_dir("docling")) / "models")
        if mgr.get_engine_status("docling") != EngineStatus.INSTALLED:
            return False
        return _source_mode_models_present()
    except Exception as exc:
        logger.debug("Docling availability check failed: %s", exc)
        return False


def docling_installable() -> bool:
    """Docling is not usable but could be installed from Settings on this platform."""
    try:
        from app.core.engine_manager import EngineManager, EngineStatus

        return EngineManager.get_instance().get_engine_status("docling") == EngineStatus.INSTALLABLE
    except Exception:
        return False


def glm_ocr_usable() -> bool:
    """GLM-OCR is installed and passed its CPU self-test. Marker read only; never raises."""
    try:
        from app.core.glm_ocr_manager import glm_ocr_usable as _usable

        return _usable()
    except Exception:
        return False


def layout_model_location() -> tuple[Path, dict[str, str]] | None:
    """The layout model add-on's folder and pinned digests: InkDoc's install, or the
    copy in the Docling pack. On-disk state only; never raises."""
    try:
        from app.core.addon_manager import AddonManager

        return AddonManager.get_instance().model_location(LAYOUT_MODEL)
    except Exception:
        return None


def layout_model_usable() -> bool:
    return layout_model_location() is not None


def glm_ocr_installable() -> bool:
    try:
        from app.core.glm_ocr_manager import glm_ocr_installable as _installable

        return _installable()
    except Exception:
        return False


def ocr_engines_usable() -> list[EngineKind]:
    """OCR engines usable right now, best first: [GLM_OCR, DOCLING], a subset, or []."""
    checks: dict[EngineKind, Callable[[], bool]] = {
        EngineKind.GLM_OCR: glm_ocr_usable,
        EngineKind.DOCLING: docling_usable,
    }
    return [engine for engine in OCR_ENGINE_ORDER if checks.get(engine, lambda: False)()]


def ocr_install_target() -> str | None:
    """The OCR engine an "Install ..." hint should name: Docling first, else GLM-OCR."""
    if docling_installable():
        return EngineKind.DOCLING.value
    if glm_ocr_installable():
        return EngineKind.GLM_OCR.value
    return None


def ocr_installable() -> bool:
    """Some OCR engine could be installed from Settings (drives "Install ..." hints)."""
    return ocr_install_target() is not None


def glm_time_gate(
    ocr_engines: list[EngineKind],
    estimate_s: float | None,
    limit_s: float = GLM_OCR_TIME_LIMIT_S,
) -> tuple[list[EngineKind], str]:
    """Drop GLM-OCR for a file estimated to take longer than `limit_s`. Pure.

    Returns the engines to route with and a note explaining any change ("" when
    nothing changed). With GLM-OCR dropped, the next OCR engine (Docling) takes
    the file; if there is none, the note says how to run GLM-OCR anyway.
    """
    if EngineKind.GLM_OCR not in ocr_engines or estimate_s is None or estimate_s <= limit_s:
        return list(ocr_engines), ""
    remaining = [e for e in ocr_engines if e != EngineKind.GLM_OCR]
    took = format_duration(estimate_s)
    if remaining:
        return remaining, f"GLM-OCR would take about {took} on this computer, so {engine_label(remaining[0])} was used"
    return remaining, (
        f"GLM-OCR would take about {took} on this computer. Choose GLM-OCR to run it anyway, "
        "or install Docling for faster OCR"
    )


def format_duration(seconds: float) -> str:
    """'45 s', '6 min', '1 h 10 min'."""
    s = max(0, int(round(seconds)))
    if s < 90:
        return f"{s} s"
    minutes = round(s / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min" if minutes % 60 else f"{minutes // 60} h"


def glm_estimate_for_pages(pages: int) -> float | None:
    """GLM-OCR's estimated time for `pages` pages on this machine, or None if unknown."""
    try:
        from app.core.glm_ocr_manager import GlmOcrManager

        return GlmOcrManager.get_instance().estimate_seconds(max(1, pages))
    except Exception as exc:
        logger.debug("GLM-OCR estimate unavailable: %s", exc)
        return None


def glm_estimate_seconds(path: str, probe: PdfProbe | None, pdf: bool) -> float | None:
    """GLM-OCR's estimated time for this file on this machine, or None if unknown."""
    try:
        from app.core.glm_ocr_manager import GlmOcrManager

        if pdf:
            if probe is None:
                return None
            pages = probe.page_count or len(probe.pages)
        else:
            from app.core.engines.glm_ocr_engine import count_pages

            pages = count_pages(path) or 1
        return GlmOcrManager.get_instance().estimate_seconds(pages)
    except Exception as exc:
        logger.debug("GLM-OCR estimate unavailable: %s", exc)
        return None


def ocr_status() -> tuple[EngineKind | None, bool]:
    """(best usable OCR engine or None, whether one is installable) for the quality check."""
    engines = ocr_engines_usable()
    if engines:
        return engines[0], False
    return None, ocr_installable()


# ─── Pure decisions ──────────────────────────────────────────────────────────
_NOT_PROBED: Any = object()


def _pages_phrase(n: int, total: int, one: str, many: str) -> str:
    """'1 of 8 pages is a scanned image', '5 of 8 pages are scanned images'."""
    return f"{n} of {total} page{'s' if total != 1 else ''} {one if n == 1 else many}"


def decide(
    path: str,
    *,
    ocr_engines: list[EngineKind],
    ocr_installable: bool = False,
    probe: PdfProbe | None = _NOT_PROBED,
    pdf: bool | None = None,
    from_url: bool = False,
    install_name: str = "Docling",
) -> AutoDecision:
    """Choose the concrete engine for `path`.

    Pure apart from the probe: pass `probe` (None meaning "could not be read")
    and `pdf` to keep it free of I/O, as run_auto and the tests do.
    """
    ocr = ocr_engines[0] if ocr_engines else None
    ocr_name = engine_label(ocr) if ocr else install_name
    if pdf is None:
        pdf = is_pdf(path)
    prefix = "URL: " if from_url else ""

    if pdf:
        if probe is _NOT_PROBED:
            probe = probe_pdf(path)
        if probe is None:
            # MarkItDown then raises its own, sanitised error for an encrypted or
            # corrupt file, rather than Auto inventing one.
            return AutoDecision(EngineKind.MARKITDOWN, f"{prefix}PDF structure unreadable; trying MarkItDown")
        total = probe.page_count or len(probe.pages)
        scans = probe.scan_pages
        images = probe.image_pages
        if scans:
            reason = _pages_phrase(len(scans), total, "is a scanned image", "are scanned images")
            if ocr:
                return AutoDecision(ocr, f"{prefix}{reason}", probe=probe)
            hint = (
                f"{install_name} would read the {len(scans)} scanned page{'s' if len(scans) != 1 else ''}. "
                "Install it in Settings."
                if ocr_installable
                else ""
            )
            return AutoDecision(EngineKind.MARKITDOWN, f"{prefix}{reason}; no OCR engine installed", hint, probe)
        probed = len(probe.pages) or 1
        if len(images) >= ROUTE_IMAGE_PAGE_MIN and len(images) / probed >= ROUTE_IMAGE_PAGE_FRACTION:
            reason = _pages_phrase(len(images), total, "is a full-page image", "are full-page images")
            if ocr:
                return AutoDecision(ocr, f"{prefix}{reason}", probe=probe)
            hint = (
                f"{ocr_name} could read text inside the image pages. Install it in Settings."
                if ocr_installable
                else ""
            )
            return AutoDecision(EngineKind.MARKITDOWN, f"{prefix}{reason}; no OCR engine installed", hint, probe)
        if images:
            reason = f"Digital PDF; {format_pages(images)} {'is a full-page image' if len(images) == 1 else 'are full-page images'}"
        else:
            reason = "Digital PDF with a full text layer"
        return AutoDecision(EngineKind.MARKITDOWN, f"{prefix}{reason}", probe=probe)

    if from_url:
        # Web pages and YouTube links: MarkItDown has the converters, and gets
        # the original URL and content-type hints from the fetch.
        return AutoDecision(EngineKind.MARKITDOWN, "URL: web page")

    ext = Path(path).suffix.lower()
    image_engine = next(
        (e for e in ocr_engines if ext in ENGINE_IMAGE_EXTENSIONS.get(e, OCR_IMAGE_EXTENSIONS)), None
    )
    if image_engine is not None:
        return AutoDecision(image_engine, "Image file: OCR needed")
    route = AUTO_ROUTES.get(ext, ROUTE_MARKITDOWN)
    if route == ROUTE_OCR:
        hint = (
            f"{install_name} would read the text in this image. Install it in Settings."
            if ocr_installable
            else ""
        )
        return AutoDecision(EngineKind.MARKITDOWN, "Image file: no OCR engine installed", hint)
    if route == ROUTE_MARKIT:
        return AutoDecision(EngineKind.MARKIT, f"{FORMAT_LABELS.get(ext, ext.lstrip('.').upper())}: Markit")
    label = FORMAT_LABELS.get(ext)
    if label:
        return AutoDecision(EngineKind.MARKITDOWN, f"{label}: MarkItDown's dedicated converter")
    return AutoDecision(EngineKind.MARKITDOWN, "Native MarkItDown format")


def should_escalate(
    decision: AutoDecision,
    report: QualityReport | None,
    ocr_engines: list[EngineKind],
) -> EngineKind | None:
    """The OCR engine to retry with, or None. Only ever called on the Auto path.

    Retries when a text-layer engine (MarkItDown, Markit) produced the result,
    the check warned, and its suggestion is the OCR engine that is installed.
    Never retries when Auto had already chosen an OCR engine (it ran, or failed
    and fell back), and never installs anything.
    """
    if not ocr_engines or report is None or not report.warning:
        return None
    best = ocr_engines[0]
    if decision.engine in ocr_engines:
        return None
    if report.engine not in (EngineKind.MARKITDOWN.value, EngineKind.MARKIT.value):
        return None
    if report.suggestion != best.value:
        return None
    return best


def pick_result(
    first: tuple[str, QualityReport | None],
    second: tuple[str, QualityReport | None],
) -> int:
    """0 to keep the first (text-layer) result, 1 to keep the second (OCR) one.

    Coverage is measured on pages with a text layer only, so it cannot credit
    the OCR engine for reading scanned pages. When the first result left
    scanned pages unread, the OCR result wins as long as it found more text.
    Otherwise the OCR result is kept unless its coverage is lower by more than
    KEEP_OCR_MARGIN.
    """
    r1, r2 = first[1], second[1]
    if r2 is None:
        return 1
    if r1 is None:
        return 1
    if r1.scan_pages and r2.output_tokens > r1.output_tokens:
        return 1
    if r1.coverage is None or r2.coverage is None:
        return 1 if r2.output_tokens >= r1.output_tokens else 0
    return 1 if r2.coverage >= r1.coverage - KEEP_OCR_MARGIN else 0


# ─── Orchestration ───────────────────────────────────────────────────────────
LaneProvider = Callable[[EngineKind], Any]  # returns an async context manager


class _NoLane:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


def _no_lane(_engine: EngineKind) -> _NoLane:
    return _NoLane()


def _availability(path: str) -> tuple[list[EngineKind], str | None, bool]:
    engines = ocr_engines_usable()
    target = None if engines else ocr_install_target()
    return engines, target, is_pdf(path)


def _enrichment_usable() -> bool:
    """Whether the code/formula model can be loaded by the engine that will run."""
    try:
        from app.core.engine_manager import EngineManager

        if not EngineManager.get_instance().is_pack_installed("docling"):
            # Source mode resolves the model through the Hugging Face cache,
            # exactly as explicit Docling does there.
            return True
        from app.core.addon_manager import AddonManager

        return bool(AddonManager.get_instance().get_status("code_enrichment").get("usable"))
    except Exception:
        return False


def _apply_enrichment_policy(options: ConversionOptions, decision: AutoDecision | None) -> None:
    """Run Docling without enrichment, and say so, when the model is missing.

    Explicit Docling refuses the job instead (EnrichmentModelUnavailableError);
    Auto is the user letting InkDoc choose, so it converts and reports it.
    """
    wants = options.docling_code_enrichment or options.docling_formula_enrichment
    if wants and not _enrichment_usable():
        options.docling_code_enrichment = False
        options.docling_formula_enrichment = False
        if decision is not None and "enrichment" not in decision.reason:
            decision.reason += " (without code/formula enrichment: the recognition model isn't installed)"


def _convert_once(
    item: QueueItem,
    options: ConversionOptions,
    engine: EngineKind,
    fetched: FetchedSource | None,
) -> str:
    from app.core import converter

    if fetched is not None:
        file_item = converter.fetched_file_item(item, fetched, engine)
    else:
        file_item = dataclasses.replace(item, engine=engine)
    return converter.convert_local(file_item, options, fetched=fetched)


def _safe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:200] or type(exc).__name__


async def _convert_with_fallback(
    item: QueueItem,
    options: ConversionOptions,
    engine: EngineKind,
    *,
    lane: LaneProvider,
    fetched: FetchedSource | None,
    job: JobHandle,
) -> str:
    """Convert with `engine` in its lane; if a non-MarkItDown engine fails, use MarkItDown.

    The fallback applies whatever the fallback_to_markitdown setting says:
    choosing Auto means letting InkDoc choose. It runs in the default lane,
    after the failed engine's lane has been released.
    """
    job.set_phase("converting", f"Converting with {engine_label(engine)}…", engine=engine.value)
    try:
        async with lane(engine):
            job.raise_if_cancelled()
            return await asyncio.to_thread(_convert_once, item, options, engine, fetched)
    except ConversionCancelledError:
        raise
    except Exception as exc:
        if engine == EngineKind.MARKITDOWN:
            raise
        logger.warning("Auto: %s failed, falling back to MarkItDown: %s", engine_label(engine), exc)
        options.fallback_occurred = True
        options.fallback_reason = str(exc)
        job.set_phase("converting", "Converting with MarkItDown…", engine=EngineKind.MARKITDOWN.value)
        async with lane(EngineKind.MARKITDOWN):
            job.raise_if_cancelled()
            return await asyncio.to_thread(
                _convert_once, item, options, EngineKind.MARKITDOWN, fetched
            )


async def _convert_split(
    item: QueueItem,
    options: ConversionOptions,
    segments: list[PageSegment],
    *,
    path: str,
    lane: LaneProvider,
    job: JobHandle,
    read_by: dict[int, EngineKind] | None = None,
) -> str:
    """Convert each run of pages with its engine and join the results in page order.

    Docling's runs go first (each as a PDF of just those pages), then
    GLM-OCR's (reading those pages of the original file), so on a machine with
    little memory each engine starts once. A run whose engine fails is read by
    the next one down (GLM-OCR, then Docling, then MarkItDown), and the
    response says so. Each run takes its engine's lane only while it converts.
    `read_by`, if given, gets the engine that read each page.
    """
    import shutil
    import tempfile

    from app.core.pdf_pages import page_runs, write_page_subset
    from app.core.queue_model import SourceKind

    texts: dict[int, str] = {}
    tmpdir = tempfile.mkdtemp(prefix="inkdoc-split-")

    def notes(more: list[str]) -> None:
        options.engine_warnings = [*options.engine_warnings, *more]

    async def subset(pages: list[int]) -> QueueItem:
        # A PDF of just these pages (pdfium lock held inside; milliseconds).
        sub = os.path.join(tmpdir, f"pages-{pages[0]}-{pages[-1]}.pdf")
        if not os.path.exists(sub):
            await asyncio.to_thread(write_page_subset, path, pages, sub)
        return QueueItem(source=sub, kind=SourceKind.FILE, display_name=item.display_name, engine=EngineKind.DOCLING)

    async def run(engine: EngineKind, run_item: QueueItem, pages: list[int], run_options: ConversionOptions) -> str:
        job.set_phase(
            "converting", f"Reading page{'s' if len(pages) > 1 else ''} {page_runs(pages)} with {engine_label(engine)}…",
            engine=engine.value,
        )
        async with lane(engine):
            job.raise_if_cancelled()
            return await asyncio.to_thread(_convert_once, run_item, run_options, engine, None)

    try:
        order = [s for s in segments if s.engine == EngineKind.DOCLING] + [
            s for s in segments if s.engine == EngineKind.GLM_OCR
        ]
        for seg in order:
            job.raise_if_cancelled()
            chain = [seg.engine] + ([EngineKind.DOCLING] if seg.engine == EngineKind.GLM_OCR else []) + [
                EngineKind.MARKITDOWN
            ]
            last_error = ""
            for attempt, engine in enumerate(chain):
                if engine == EngineKind.GLM_OCR:
                    run_item = QueueItem(source=path, kind=SourceKind.FILE, display_name=item.display_name,
                                         engine=EngineKind.GLM_OCR)
                    run_options = dataclasses.replace(options, page_subset=list(seg.pages))
                else:
                    run_item = await subset(seg.pages)
                    run_options = dataclasses.replace(options, page_subset=None)
                try:
                    texts[seg.pages[0]] = await run(engine, run_item, seg.pages, run_options)
                    if read_by is not None:
                        read_by.update(dict.fromkeys(seg.pages, engine))
                    options.engine_warnings = run_options.engine_warnings
                    if attempt:
                        options.fallback_occurred = True
                        options.fallback_reason = last_error
                        notes([
                            f"Pages {page_runs(seg.pages)}: {engine_label(chain[0])} failed ({last_error}); "
                            f"read with {engine_label(engine)} instead."
                        ])
                    break
                except ConversionCancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Auto page split: %s failed on pages %s: %s",
                                   engine_label(engine), page_runs(seg.pages), exc)
                    last_error = _safe_error(exc)
                    if engine == EngineKind.MARKITDOWN:
                        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    parts = [texts.get(seg.pages[0], "").strip() for seg in segments]
    return "\n\n".join(p for p in parts if p) + "\n"


async def run_auto(
    item: QueueItem,
    options: ConversionOptions,
    *,
    lane: LaneProvider = _no_lane,
    probe_lane: Any = None,
    fetched: FetchedSource | None = None,
    job: JobHandle | None = None,
) -> str:
    """Route, convert, check and maybe re-convert one item. Returns the Markdown to keep.

    Args:
        lane: engine -> async context manager capping that engine's concurrency
            (the server passes get_conversion_semaphore). Entered in async code
            before any thread is used for that engine.
        probe_lane: async context manager serialising pdfium work, so a probe
            waiting its turn holds no thread either.
        fetched: the downloaded file when `item` is a URL; its temp file must
            outlive this call (the check reads it).
        job: progress/cancel handle; cancellation stops before the next phase.

    Sets options.auto_decision, options.engine_used (concrete) and
    options.quality_report. Saves nothing: the caller saves the result once.
    """
    job = job or NULL_JOB
    probe_lane = probe_lane if probe_lane is not None else _NoLane()
    path = str(fetched.path) if fetched is not None else item.source

    job.set_phase("routing", "Choosing an engine…")
    ocr_engines, install_target, pdf = await asyncio.to_thread(_availability, path)
    docling_usable_now = EngineKind.DOCLING in ocr_engines
    installable = install_target is not None
    install_name = engine_label(EngineKind(install_target)) if install_target else "Docling"

    probe: PdfProbe | None = None
    if pdf:
        async with probe_lane:
            job.raise_if_cancelled()
            probe = await asyncio.to_thread(probe_pdf, path)

    # The "Auto quality" steps on for this conversion (app/core/thorough_passes.py),
    # judged on what is installed. GLM-OCR's time limit applies below, where it
    # would read whole pages; reading a table area takes seconds.
    available = {e.value for e in ocr_engines}
    at_thorough = level_rank(options.auto_quality) >= level_rank(LEVEL_THOROUGH)
    if pdf and at_thorough and await asyncio.to_thread(layout_model_usable):
        available.add(LAYOUT_MODEL)
    plan = PassPlan(options.auto_quality, available=available, disabled=options.passes_off)
    log = PassLog(plan)

    # A mixed PDF is read page by page (Docling for text pages, GLM-OCR for
    # the rest), with GLM-OCR's time limit counted on its pages only.
    split: list[PageSegment] | None = None
    read_by: dict[int, EngineKind] = {}  # page -> the engine that read it, for a page split
    reread = pdf and plan.on("ocr_layer_pages")
    if pdf and plan.on("page_split"):
        split = plan_page_split(probe, ocr_engines, reread_ocr_layer=reread)
        if split is None:
            log.ran("page_split")  # not a mix of text and scanned pages
        else:
            ocr_pages = sum(len(s.pages) for s in split if s.engine == EngineKind.GLM_OCR)
            estimate = await asyncio.to_thread(glm_estimate_for_pages, ocr_pages)
            if estimate is not None and estimate > GLM_OCR_TIME_LIMIT_S:
                split = None
                log.skipped("page_split", f"GLM-OCR would need over {GLM_OCR_TIME_LIMIT_S / 60:.0f} minutes")
                if reread:
                    log.skipped("ocr_layer_pages", f"GLM-OCR would need over {GLM_OCR_TIME_LIMIT_S / 60:.0f} minutes")
            else:
                log.ran("page_split", ocr_pages)
                if reread:
                    glm_read = {p for s in split if s.engine == EngineKind.GLM_OCR for p in s.pages}
                    log.ran("ocr_layer_pages", len(glm_read & set(ocr_layer_pages(probe))))

    # Auto runs its own fallback (below), in the right lane.
    options.allow_engine_fallback = False
    if split is not None:
        decision = split_decision(split, probe)  # type: ignore[arg-type]
        options.auto_decision = decision
        await asyncio.to_thread(_apply_enrichment_policy, options, decision)
        markdown = await _convert_split(item, options, split, path=path, lane=lane, job=job, read_by=read_by)
        used = decision.engine
        if plan.on("digital_layout"):
            log.skipped("digital_layout", "the page split chose the engines")
    else:
        # GLM-OCR only for files it should finish within the time limit; else Docling.
        gate_note = ""
        if EngineKind.GLM_OCR in ocr_engines:
            estimate = await asyncio.to_thread(glm_estimate_seconds, path, probe, pdf)
            ocr_engines, gate_note = glm_time_gate(ocr_engines, estimate)

        decision = decide(
            path,
            ocr_engines=ocr_engines,
            ocr_installable=installable,
            probe=probe,
            pdf=pdf,
            from_url=fetched is not None,
            install_name=install_name,
        )
        if gate_note and decision.engine != EngineKind.GLM_OCR:
            needed_ocr = decision.engine in OCR_ENGINE_ORDER or "no OCR engine" in decision.reason
            if needed_ocr:
                decision.reason += f" ({gate_note})"
                if decision.engine == EngineKind.MARKITDOWN and not decision.hint:
                    decision.hint = gate_note + "."
        if reread and probe is not None:
            # A PDF mostly of OCR'd scans: GLM-OCR reads it whole (within its time limit).
            if ocr_layer_pages(probe) and EngineKind.GLM_OCR not in ocr_engines and gate_note:
                log.skipped("ocr_layer_pages", "GLM-OCR would take too long")
            else:
                log.ran("ocr_layer_pages", thorough_ocr_layer_route(decision, probe, EngineKind.GLM_OCR in ocr_engines))
        if pdf and probe is not None and plan.on("digital_layout"):
            routed_from = decision.engine
            thorough_digital_route(decision, docling_usable_now, EngineKind.GLM_OCR in ocr_engines)
            if decision.engine != routed_from:
                log.ran("digital_layout", 1)
            elif decision.engine == EngineKind.MARKITDOWN and "Digital PDF" in decision.reason:
                log.skipped("digital_layout", "GLM-OCR would take too long and Docling isn't installed")
            else:
                log.ran("digital_layout")  # not a digital PDF: Auto's own choice stands
        options.auto_decision = decision
        if decision.engine == EngineKind.DOCLING:
            await asyncio.to_thread(_apply_enrichment_policy, options, decision)

        # Docling's worker stops a conversion after 3 minutes: at Thorough a long PDF goes in parts.
        thorough = plan.level == LEVEL_THOROUGH
        chunks = docling_chunks(probe) if thorough and decision.engine == EngineKind.DOCLING else None
        if chunks is not None:
            markdown = await _convert_split(item, options, chunks, path=path, lane=lane, job=job)
        else:
            markdown = await _convert_with_fallback(
                item, options, decision.engine, lane=lane, fetched=fetched, job=job
            )
        used = options.engine_used or decision.engine

    report: QualityReport | None = None
    if pdf and options.quality_check:
        job.raise_if_cancelled()
        job.set_phase("checking", "Checking…")
        if probe is None:
            report = QualityReport(checked=False, skipped_reason=SKIP_UNREADABLE, engine=used.value)
        else:
            ocr_engine = ocr_engines[0] if ocr_engines else None
            report = await asyncio.to_thread(
                assess, path, markdown, used,
                probe=probe, ocr_engine=ocr_engine, ocr_installable=installable,
                ocr_install_engine=install_target or "docling",
            )

    alt = should_escalate(decision, report, ocr_engines) if not options.fallback_occurred else None
    if alt is not None and report is not None:
        job.raise_if_cancelled()
        job.set_phase("retrying", f"Retrying with {engine_label(alt)}…", engine=alt.value)
        escalation: dict[str, Any] = {
            "from": used.value,
            "from_missing_pct": report.missing_pct,
            "from_scan_pages": len(report.scan_pages),
            "to": alt.value,
            "kept": used.value,
            "error": None,
        }
        alt_options = dataclasses.replace(
            options,
            engine=alt,
            fallback_occurred=False,
            fallback_reason="",
            engine_used=None,
            quality_report=None,
        )
        if alt == EngineKind.DOCLING:
            await asyncio.to_thread(_apply_enrichment_policy, alt_options, None)
        try:
            async with lane(alt):
                job.raise_if_cancelled()
                alt_markdown = await asyncio.to_thread(_convert_once, item, alt_options, alt, fetched)
            alt_used = alt_options.engine_used or alt
            job.set_phase("checking", "Checking…")
            alt_report = await asyncio.to_thread(
                assess, path, alt_markdown, alt_used,
                probe=probe, ocr_engine=alt, ocr_installable=False,
            )
            if pick_result((used.value, report), (alt_used.value, alt_report)) == 1:
                markdown, report, used = alt_markdown, alt_report, alt_used
                escalation["kept"] = alt_used.value
        except ConversionCancelledError:
            raise
        except Exception as exc:
            # Keep the first result and its warning; say why the retry did not help.
            logger.warning("Auto: retry with %s failed: %s", engine_label(alt), exc)
            escalation["error"] = _safe_error(exc)
        decision.escalated = True
        decision.escalation = escalation

    # The steps that edit a result run once, on the result kept.
    converted = markdown
    if split is not None:
        glm_pages = [page for page, engine in read_by.items() if engine == EngineKind.GLM_OCR]
    elif used == EngineKind.GLM_OCR and probe is not None:
        glm_pages = list(range(1, probe.page_count + 1))
    else:
        glm_pages = []
    if pdf and plan.on("polish"):
        if probe is None:
            log.skipped("polish", "the PDF could not be read")
        elif used in (EngineKind.MARKITDOWN, EngineKind.MARKIT):
            log.ran("polish")  # they copy the text layer as it is
        else:
            markdown = await asyncio.to_thread(_polish, markdown, probe, used, options)
            log.ran("polish", sum(options.polish_counts))
    if pdf and plan.relevant():
        markdown = await _check_tables(
            path, markdown, probe, options, lane=lane, job=job, plan=plan, log=log, glm_pages=glm_pages,
            used=used, glm_usable=EngineKind.GLM_OCR in ocr_engines,
        )
    if markdown != converted and report is not None and report.checked and probe is not None:
        # The words are the same, but the check describes the saved text.
        ocr_engine = ocr_engines[0] if ocr_engines else None
        report = await asyncio.to_thread(
            assess, path, markdown, used,
            probe=probe, ocr_engine=ocr_engine, ocr_installable=installable,
            ocr_install_engine=install_target or "docling",
        )

    options.engine_used = used
    options.quality_report = report
    return markdown


async def _check_tables(
    path: str,
    markdown: str,
    probe: PdfProbe | None,
    options: ConversionOptions,
    *,
    lane: LaneProvider,
    job: JobHandle,
    plan: PassPlan,
    log: PassLog,
    glm_pages: Collection[int] = (),
    used: EngineKind | None = None,
    glm_usable: bool = False,
) -> str:
    """The table stage for Auto: check, and replace what the engine got wrong.

    The finder runs in the MarkItDown worker, so the inspection takes
    MarkItDown's lane. Tables found from word positions are read a second
    time by GLM-OCR, in GLM-OCR's lane, before anything is written (the step
    needs GLM-OCR installed). Then the tables of the scanned pages GLM-OCR
    read (`glm_pages`) are read again one by one. Which of this runs is
    `plan`'s to say; what each step did goes into `log` and the report's `passes`.
    """
    from app.core.table_check import TableSummary
    from app.core.table_stage import (
        SKIP_SWITCHED_OFF,
        confirm_tables,
        finish_tables,
        inspect_tables,
        pending_confirmations,
    )

    if plan.finder_needed():
        async with lane(EngineKind.MARKITDOWN):
            job.raise_if_cancelled()
            inspection = await asyncio.to_thread(
                inspect_tables, path, markdown, probe, detectors=plan.detectors(), job=job, skip_pages=glm_pages
            )
        if plan.on("borderless_tables") and pending_confirmations(inspection):
            from app.core.engines.glm_ocr_engine import read_table_region

            async with lane(EngineKind.GLM_OCR):
                job.raise_if_cancelled()
                await asyncio.to_thread(confirm_tables, inspection, read_table_region, job=job)
        result, summary = await asyncio.to_thread(
            finish_tables, inspection, repair_allowed=True, kinds=plan.table_kinds()
        )
        _log_table_passes(log, plan, inspection)
    else:
        inspection = None
        result, summary = markdown, TableSummary(checked=False, skipped_reason=SKIP_SWITCHED_OFF)

    if plan.on("scanned_tables"):
        result = await _scanned_tables(path, result, probe, summary, lane=lane, job=job, log=log, glm_pages=glm_pages)
        if summary.repaired and summary.skipped_reason == SKIP_SWITCHED_OFF:
            # Only this step ran, and it wrote tables: the report is about them.
            summary.checked, summary.skipped_reason = True, ""

    result = await _after_tables(
        path, result, probe, options, summary, inspection,
        lane=lane, job=job, plan=plan, log=log, used=used, glm_usable=glm_usable,
    )
    amps, words = options.polish_counts
    if amps or words:
        summary.characters_restored = amps + words
        summary.details.append(
            f"Put back {amps + words} of the PDF's characters the engine had simplified "
            "('&', dashes and quotes)."
        )
    summary.passes = log.to_list()
    options.table_report = summary
    return result


async def _scanned_tables(
    path: str,
    markdown: str,
    probe: PdfProbe | None,
    summary: Any,
    *,
    lane: LaneProvider,
    job: JobHandle,
    log: PassLog,
    glm_pages: Collection[int],
) -> str:
    """The scanned_tables step (app/core/scanned_tables.py): the layout model finds the
    tables in MarkItDown's lane (it runs in that worker), GLM-OCR reads each in its lane.
    An addition: a failure leaves the Markdown as it was (a cancel still stops the job)."""
    from app.core.engines.layout_model import MODEL_FILE
    from app.core.engines.markitdown_worker_client import find_layout_in_worker
    from app.core.scanned_tables import (
        find_scanned_tables,
        read_scanned_tables,
        target_pages,
        write_scanned_tables,
    )

    if probe is None:
        log.skipped("scanned_tables", "the PDF could not be read")
        return markdown
    try:
        pages = target_pages(probe, glm_pages)
        if not pages:
            log.ran("scanned_tables")  # GLM-OCR read no scanned page
            return markdown
        location = await asyncio.to_thread(layout_model_location)
        if location is None:
            log.skipped("scanned_tables", "needs the layout model")  # removed since the conversion started
            return markdown
        folder, digests = location

        def detect(source: str, numbers: list[int], detect_job: Any) -> dict:
            return find_layout_in_worker(source, numbers, str(folder), digests.get(MODEL_FILE, ""), detect_job)

        async with lane(EngineKind.MARKITDOWN):
            job.raise_if_cancelled()
            found = await asyncio.to_thread(find_scanned_tables, path, pages, detect, job=job)
        if found.skipped:
            log.skipped("scanned_tables", found.skipped)
            return markdown
        if found.areas:
            from app.core.engines.glm_ocr_engine import read_table_region

            async with lane(EngineKind.GLM_OCR):
                job.raise_if_cancelled()
                await asyncio.to_thread(read_scanned_tables, found, read_table_region, job=job)
        # Pure, but it reads the whole Markdown: off the event loop.
        result, written, details = await asyncio.to_thread(write_scanned_tables, markdown, found)
    except ConversionCancelledError:
        raise
    except Exception as exc:
        logger.warning("Tables on scanned pages skipped (%s)", type(exc).__name__)
        log.skipped("scanned_tables", "it failed")
        return markdown
    summary.repaired.extend(written)
    summary.details.extend(details)
    log.ran("scanned_tables", sum(w["tables"] for w in written))
    return result


# ─── The steps after the tables ──────────────────────────────────────────────
# What runs once the tables are settled, in PASSES order: the text of figures
# and the values of charts drawn in the PDF, pages an engine lost read again,
# paragraphs put back, lines joined, checkboxes marked, pictures and formulas
# read by GLM-OCR, and last the PDF's bold, italics and raised text. Each is
# an addition: a failure leaves the Markdown as the step before left it.
_TEXT_STEPS = ("figure_text", "chart_data", "dropped_paragraphs", "broken_lines", "checkboxes", "formatting")


def _detail(summary: Any, count: int, one: str, many: str) -> None:
    if count:
        summary.details.append(one.format(n=count) if count == 1 else many.format(n=count))


async def _after_tables(
    path: str,
    markdown: str,
    probe: PdfProbe | None,
    options: ConversionOptions,
    summary: Any,
    inspection: Any,
    *,
    lane: LaneProvider,
    job: JobHandle,
    plan: PassPlan,
    log: PassLog,
    used: EngineKind | None,
    glm_usable: bool,
) -> str:
    from app.core import figure_text as ft
    from app.core.text_polish import restore_formatting

    pages = inspection.found.get("pages", []) if inspection is not None and inspection.found else []
    wanted = [p.name for p in PASSES if p.stage == "after_tables" and plan.on(p.name)]
    if not wanted:
        return markdown
    if not pages:
        for name in wanted:
            if name != "page_recheck":
                log.skipped(name, _unread_reason(inspection))
    result = markdown
    failed = ""

    async def step(name: str, work: Callable[[str], tuple[str, int]]) -> int:
        """Run one pure step off the event loop; on failure keep the text and say so."""
        nonlocal result, failed
        if name not in wanted or not pages:
            return 0
        if failed:
            log.skipped(name, "a step before it failed")
            return 0
        try:
            result, changed = await asyncio.to_thread(work, result)
        except Exception as exc:  # an addition, never a reason to fail
            logger.warning("Step %s skipped: %s", name, exc)
            failed = name
            log.skipped(name, "it failed")
            return 0
        log.ran(name, changed)
        return changed

    charts = 0
    if "figure_text" in wanted or "chart_data" in wanted:
        first = "figure_text" if "figure_text" in wanted else "chart_data"

        def figures(md: str) -> tuple[str, int]:
            nonlocal charts
            out, filled, charts = ft.recover_figures(
                md, pages, charts="chart_data" in wanted, text="figure_text" in wanted,
            )
            return out, filled if first == "figure_text" else charts

        changed = await step(first, figures)
        filled = changed if first == "figure_text" else 0
        if first == "figure_text" and "chart_data" in wanted and pages:
            if failed:
                log.skipped("chart_data", "a step before it failed")
            else:
                log.ran("chart_data", charts)
        if filled:
            summary.figures_filled = filled
            _detail(summary, filled, "Added the text of {n} figure from the PDF.", "Added the text of {n} figures from the PDF.")
        _detail(summary, charts, "Measured {n} chart from its drawing in the PDF.", "Measured {n} charts from their drawing in the PDF.")

    if "page_recheck" in wanted:
        result = await _recheck_pages(path, result, probe, options, summary, lane=lane, job=job, log=log,
                                      used=used, glm_usable=glm_usable)

    restored = await step("dropped_paragraphs", lambda md: ft.recover_dropped_paragraphs(md, pages))
    if restored:
        summary.paragraphs_restored = restored
        _detail(summary, restored, "Put back {n} paragraph of the PDF the engine left out.",
                "Put back {n} paragraphs of the PDF the engine left out.")
    joined = await step("broken_lines", lambda md: ft.join_broken_lines(md, pages))
    _detail(summary, joined, "Joined {n} line the engine had broken off its sentence.",
            "Joined {n} lines the engine had broken off their sentences.")
    marked = await step("checkboxes", lambda md: ft.mark_checkboxes(md, pages))
    _detail(summary, marked, "Marked {n} checkbox drawn in the PDF.", "Marked {n} checkboxes drawn in the PDF.")

    if "picture_text" in wanted and pages and not failed:
        result = await _read_regions(path, result, pages, "picture_text", summary, lane=lane, job=job, log=log)
    if "formulas" in wanted and pages and not failed:
        result = await _read_regions(path, result, pages, "formulas", summary, lane=lane, job=job, log=log)

    styled = await step("formatting", lambda md: restore_formatting(md, pages))
    _detail(summary, styled, "Put back {n} of the PDF's bold, italic or raised words.",
            "Put back {n} of the PDF's bold, italic or raised words.")
    return result


async def _read_regions(
    path: str, markdown: str, pages: list[dict], name: str, summary: Any,
    *, lane: LaneProvider, job: JobHandle, log: PassLog,
) -> str:
    """picture_text and formulas: find the areas, have GLM-OCR read each in its lane, write. An addition."""
    from app.core import figure_text as ft
    from app.core.engines.glm_ocr_engine import read_formula_region, read_text_region

    try:
        if name == "picture_text":
            areas = await asyncio.to_thread(ft.picture_areas, markdown, pages)
            read = read_text_region
        else:
            wanted = await asyncio.to_thread(ft.formula_pages, markdown, pages)
            if not wanted:
                log.ran(name)
                return markdown
            layout = await _layout_regions(path, sorted(wanted), lane=lane, job=job)
            if layout is None:
                log.skipped(name, "needs the layout model")
                return markdown
            areas = await asyncio.to_thread(ft.formula_areas, markdown, pages, layout)
            read = read_formula_region
        if not areas:
            log.ran(name)
            return markdown
        async with lane(EngineKind.GLM_OCR):
            for index, area in enumerate(areas):
                job.raise_if_cancelled()
                what = "picture" if name == "picture_text" else "formula"
                job.set_phase("checking", f"Reading {what} {index + 1} of {len(areas)} with GLM-OCR…", engine="glm_ocr")
                area.reading = await asyncio.to_thread(read, path, area.page, area.bbox)
        if name == "picture_text":
            result, written = await asyncio.to_thread(ft.write_picture_text, markdown, areas)
            _detail(summary, written, "Read the text in {n} picture with GLM-OCR.", "Read the text in {n} pictures with GLM-OCR.")
        else:
            result, written = await asyncio.to_thread(ft.write_formulas, markdown, areas, pages)
            _detail(summary, written, "Read {n} formula with GLM-OCR.", "Read {n} formulas with GLM-OCR.")
    except ConversionCancelledError:
        raise
    except Exception as exc:
        logger.warning("Step %s skipped (%s)", name, type(exc).__name__)
        log.skipped(name, "it failed")
        return markdown
    log.ran(name, written)
    return result


async def _layout_regions(path: str, pages: list[int], *, lane: LaneProvider, job: JobHandle) -> dict | None:
    """The layout model's regions on `pages`, in MarkItDown's lane (it runs in that worker)."""
    from app.core.engines.layout_model import MODEL_FILE
    from app.core.engines.markitdown_worker_client import find_layout_in_worker

    location = await asyncio.to_thread(layout_model_location)
    if location is None:
        return None
    folder, digests = location
    async with lane(EngineKind.MARKITDOWN):
        job.raise_if_cancelled()
        found = await asyncio.to_thread(find_layout_in_worker, path, pages, str(folder), digests.get(MODEL_FILE, ""), job)
    return None if found.get("error") else found


# ─── Pages an engine lost (the page_recheck step) ────────────────────────────
# A page the missing-text check finds mostly missing after every other step is
# read again on its own: by MarkItDown, which copies the text layer, and by
# GLM-OCR when it is installed and the page is worth its time. The reading
# whose page holds more of the PDF's text is kept, written after what the
# engine wrote for that page, only when it adds at least RECHECK_MIN_GAIN.
RECHECK_MAX_PAGES = 8
RECHECK_MIN_GAIN = 0.15
RECHECK_GOOD = 0.9             # a reading this complete needs no second engine


async def _recheck_pages(
    path: str, markdown: str, probe: PdfProbe | None, options: ConversionOptions, summary: Any,
    *, lane: LaneProvider, job: JobHandle, log: PassLog, used: EngineKind | None, glm_usable: bool,
) -> str:
    import shutil
    import tempfile

    from app.core.pdf_pages import write_page_subset
    from app.core.quality_check import insert_page_text
    from app.core.queue_model import SourceKind

    if probe is None or used is None:
        log.skipped("page_recheck", "the PDF could not be read")
        return markdown
    try:
        report = await asyncio.to_thread(assess, path, markdown, used, probe=probe)
        if report is None or not report.checked or not report.low_pages:
            log.ran("page_recheck")
            return markdown
        coverage = {page: share for page, share, _words in report.page_coverages}
        engines = [e for e in (EngineKind.MARKITDOWN, EngineKind.GLM_OCR) if e != used]
        if EngineKind.GLM_OCR in engines:
            estimate = await asyncio.to_thread(glm_estimate_for_pages, 1)
            if not glm_usable or (estimate is not None and estimate > GLM_OCR_TIME_LIMIT_S / RECHECK_MAX_PAGES):
                engines.remove(EngineKind.GLM_OCR)
        texts = {p.index + 1: p.text for p in probe.pages}
        rechecked: list[int] = []
        tmpdir = tempfile.mkdtemp(prefix="inkdoc-recheck-")
        try:
            for page in report.low_pages[:RECHECK_MAX_PAGES]:
                best: tuple[float, str, EngineKind] | None = None
                for engine in engines:
                    job.raise_if_cancelled()
                    job.set_phase("retrying", f"Reading page {page} again with {engine_label(engine)}…", engine=engine.value)
                    run_options = dataclasses.replace(
                        options, engine=engine, engine_used=None, quality_report=None, table_report=None,
                        engine_warnings=[], page_subset=[page] if engine == EngineKind.GLM_OCR else None,
                    )
                    if engine == EngineKind.GLM_OCR:
                        item = QueueItem(source=path, kind=SourceKind.FILE, display_name="", engine=engine)
                    else:
                        sub = os.path.join(tmpdir, f"page-{page}.pdf")
                        if not os.path.exists(sub):
                            await asyncio.to_thread(write_page_subset, path, [page], sub)
                        item = QueueItem(source=sub, kind=SourceKind.FILE, display_name="", engine=engine)
                    try:
                        async with lane(engine):
                            job.raise_if_cancelled()
                            text = await asyncio.to_thread(_convert_once, item, run_options, engine, None)
                    except ConversionCancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("Page %d: reading again with %s failed: %s", page, engine_label(engine), exc)
                        continue
                    trial = await asyncio.to_thread(insert_page_text, markdown, page, text, engine.value, texts)
                    check = await asyncio.to_thread(assess, path, trial, used, probe=probe)
                    share = next((s for p, s, _w in (check.page_coverages if check else []) if p == page), 0.0)
                    if share - coverage.get(page, 0.0) >= RECHECK_MIN_GAIN and (best is None or share > best[0]):
                        best = (share, trial, engine)
                    if share >= RECHECK_GOOD:
                        break
                if best is not None:
                    markdown = best[1]
                    rechecked.append(page)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except ConversionCancelledError:
        raise
    except Exception as exc:
        logger.warning("Pages read again skipped (%s): %s", type(exc).__name__, exc)
        log.skipped("page_recheck", "it failed")
        return markdown
    log.ran("page_recheck", len(rechecked))
    if rechecked:
        summary.details.append(
            f"Read {format_pages(rechecked)} again: the engine had left out much of {'its' if len(rechecked) == 1 else 'their'} text."
        )
    return markdown


def _unread_reason(inspection: Any) -> str:
    """Why the finder has nothing for the steps that build on it."""
    from app.core.table_stage import SKIP_UNREADABLE

    early = inspection.early if inspection is not None else None
    if early is None:
        return "the table finder found nothing to read"
    if early.checked:
        return "the PDF has no text pages"
    return "the PDF could not be read" if early.skipped_reason == SKIP_UNREADABLE else "the table check failed"


def _log_table_passes(log: PassLog, plan: PassPlan, inspection: Any) -> None:
    """What the table check and the finder's steps did: tables written, per step."""
    names = [p.name for p in PASSES if p.table_kinds and plan.on(p.name)]
    if inspection.early is not None and not inspection.early.checked:
        for name in names:
            log.skipped(name, _unread_reason(inspection))
        return
    owner = {kind: p.name for p in PASSES for kind in p.table_kinds}
    written = dict.fromkeys(names, 0)
    for band in inspection.bands:
        if band.repaired:
            for table in band.tables:
                name = owner.get(table.get("kind", "lines"))
                if name in written:
                    written[name] += 1
    for name, count in written.items():
        log.ran(name, count)


def run_auto_blocking(
    item: QueueItem,
    options: ConversionOptions,
    *,
    fetched: FetchedSource | None = None,
) -> str:
    """run_auto for synchronous callers (convert_item from a script or worker thread).

    No lanes: a direct caller has no concurrency to manage. Must not be called
    from a thread that is already running an event loop.
    """
    return asyncio.run(run_auto(item, options, lane=_no_lane, fetched=fetched))
