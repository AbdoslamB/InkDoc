"""Auto engine: pick the engine per file, then verify the result.

"Auto" is a routing mode, not a converter. For each file it:

1. probes PDFs cheaply (app/core/pdf_probe.py) and picks a concrete engine:
   MarkItDown for digital documents, the best installed OCR engine for scans
   and images, Markit for the few formats MarkItDown has no converter for;
2. converts with that engine, in that engine's lane;
3. runs the missing-text check on PDFs (app/core/quality_check.py);
4. if the check says an OCR engine would do better and one is installed,
   converts again with it and keeps the better of the two results.

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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.jobs import NULL_JOB, ConversionCancelledError, JobHandle
from app.core.pdf_probe import PdfProbe, is_pdf, probe_pdf
from app.core.quality_check import (
    SKIP_UNREADABLE,
    QualityReport,
    assess,
    format_pages,
)
from app.core.queue_model import EngineKind, QueueItem

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

    def to_dict(self, engine_used: EngineKind | None = None) -> dict[str, Any]:
        return {
            "chosen": (engine_used or self.engine).value,
            "routed_to": self.engine.value,
            "reason": self.reason,
            "hint": self.hint,
            "escalated": self.escalated,
            "escalation": self.escalation,
        }


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
    installable = install_target is not None
    install_name = engine_label(EngineKind(install_target)) if install_target else "Docling"

    probe: PdfProbe | None = None
    if pdf:
        async with probe_lane:
            job.raise_if_cancelled()
            probe = await asyncio.to_thread(probe_pdf, path)

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
    options.auto_decision = decision
    # Auto runs its own fallback (below), in the right lane.
    options.allow_engine_fallback = False
    if decision.engine == EngineKind.DOCLING:
        await asyncio.to_thread(_apply_enrichment_policy, options, decision)

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

    options.engine_used = used
    options.quality_report = report
    return markdown


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
