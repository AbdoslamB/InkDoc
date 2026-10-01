"""One cheap CPU pass over a PDF, shared by the missing-text check and Auto.

Reads each page's embedded text layer and the placement of its images with
pdfium, without rendering anything. Two features use the result:

- app/core/quality_check.py compares a conversion's output with the text
  layer, and skips pages whose text is inside an image;
- app/core/auto_engine.py routes scanned PDFs to an OCR engine.

Telling scans from cover photos
-------------------------------
A page with no text and a full-page image is not necessarily a scan. Annual
reports, brochures and theses often open with a full-bleed photo that has
nothing to read. Treating it as a scan would warn on, and route to Docling,
a large share of perfectly ordinary documents. So an image-only page is
`scan_likely` only with at least one piece of scan evidence:

  1. the image uses a scanner/fax codec (CCITTFaxDecode, JBIG2Decode);
  2. the image is 1-bit or 8-bit grayscale and covers most of the page;
  3. it is part of a run of consecutive image-only pages;
  4. the document's producer names scanner software;
  5. most of the document's pages are image-only.

Anything else is a plain `image page`: reported as information, never a
warning on its own. All of it is read from the image dictionaries
(PdfImage.get_filters / get_metadata), so no image is decoded.

Contract: never raises. Any failure (encrypted, corrupt, not a PDF) returns
None and logs at WARNING; callers treat None as "unknown". pypdfium2 is imported
inside functions only, so importing this module costs nothing at startup.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ─── Classification thresholds ───────────────────────────────────────────────
# Provisional values from the plan (plan_for_new_features.md, 3.1); confirm or
# replace them with scripts/calibrate_quality_check.py on a real corpus.
IMAGE_ONLY_MAX_CHARS = 25       # fewer non-whitespace chars than this = "no text"
IMAGE_ONLY_MIN_COVERAGE = 0.5   # ...and images over at least half the page
OCR_LAYER_MIN_COVERAGE = 0.9    # text on top of a page-sized image = OCR'd scan
SCAN_GRAY_MIN_COVERAGE = 0.85   # a bilevel/gray image this large looks scanned
SCAN_RUN_MIN_PAGES = 2          # consecutive image-only pages
SCAN_DOC_MIN_FRACTION = 0.5     # share of image-only pages that makes a document a scan
SCAN_CODECS = frozenset({"CCITTFaxDecode", "JBIG2Decode", "CCF", "JBIG2"})
# Lowercased substrings of the Producer/Creator metadata written by scanner
# software. "scan" is deliberately broad (ScanSnap, CamScanner, "Scanned by…").
SCANNER_PRODUCERS = ("scan", "paper capture", "kofax", "naps2", "captiva")

DEFAULT_MAX_PAGES = 500
DEFAULT_TIME_BUDGET_S = 2.0
# How deep to follow Form XObjects when looking for images. Scanner output and
# some PDF writers wrap the page image in a form; deeper nesting is rare.
FORM_MAX_DEPTH = 3

# pdfium is not thread-safe, and the default conversion lane runs two jobs at
# once. Every probe holds this lock. The server additionally serialises probes
# in async code (its probe lane) so a waiting probe never occupies a thread.
# Source-mode, in-process Docling also uses pdfium without this lock; that path
# is development-only, so it is accepted rather than wrapped.
_PDFIUM_LOCK = threading.Lock()


def pdfium_lock() -> threading.Lock:
    """The process-wide pdfium lock, for other pdfium users (GLM-OCR page rendering).

    Hold it per call, never across a long job: GLM-OCR takes it once per page
    it renders, so a probe waiting behind a 30-page OCR job waits for one page,
    not the whole document.
    """
    return _PDFIUM_LOCK


def count_pages(path: str | os.PathLike[str]) -> int | None:
    """Page count of a PDF, or None when it cannot be opened. Never raises.

    Cheap (no page is loaded); used for the GLM-OCR time estimate.
    """
    try:
        import pypdfium2 as pdfium
    except Exception:  # pragma: no cover - ships with markitdown[pdf]
        return None
    with _PDFIUM_LOCK:
        doc = None
        try:
            doc = pdfium.PdfDocument(str(path))
            return len(doc)
        except Exception as exc:
            logger.debug("Could not count pages of %s: %s", os.path.basename(str(path)), exc)
            return None
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass


@dataclass
class PageProbe:
    index: int                 # 0-based
    text: str                  # embedded text layer (pdfium get_text_range)
    char_count: int            # non-whitespace characters
    image_coverage: float      # 0..1, sum of image areas clipped to the page, capped
    image_only: bool           # char_count < 25 and image_coverage >= 0.5
    scan_likely: bool = False  # image_only AND scan evidence: text almost certainly inside the image
    ocr_layer: bool = False    # char_count >= 25 and image_coverage >= 0.9: OCR'd scan
    scan_evidence: list[str] = field(default_factory=list)  # why scan_likely (for logs/tests)
    # Per-page evidence gathered while reading; folded into scan_likely at the end.
    _codec: bool = field(default=False, repr=False)
    _gray_full: bool = field(default=False, repr=False)


@dataclass
class PdfProbe:
    page_count: int
    pages: list[PageProbe]     # probed pages (may be fewer than page_count)
    producer: str              # document Producer/Creator metadata, lowercased
    truncated: bool            # hit max_pages or the time budget
    elapsed_ms: int

    @property
    def scan_pages(self) -> list[int]:
        """1-based numbers of scan-likely pages."""
        return [p.index + 1 for p in self.pages if p.scan_likely]

    @property
    def image_pages(self) -> list[int]:
        """1-based numbers of image-only pages that are NOT scan-likely (covers, full-page figures)."""
        return [p.index + 1 for p in self.pages if p.image_only and not p.scan_likely]

    @property
    def pages_probed(self) -> int:
        return len(self.pages)


def is_pdf(path: str | os.PathLike[str]) -> bool:
    """True for a .pdf path, or any file whose header carries the %PDF- marker.

    The PDF spec allows the marker anywhere in the first 1024 bytes, and URL
    downloads often arrive without a .pdf name, so both are checked.
    """
    try:
        if str(path).lower().endswith(".pdf"):
            return True
        with open(path, "rb") as fh:
            return b"%PDF-" in fh.read(1024)
    except OSError:
        return False


def _compose(outer: tuple[float, ...], inner: tuple[float, ...]) -> tuple[float, ...]:
    """Matrix product mapping inner-space points through `inner`, then `outer`."""
    a1, b1, c1, d1, e1, f1 = inner
    a2, b2, c2, d2, e2, f2 = outer
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _transform_rect(m: tuple[float, ...], rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    a, b, c, d, e, f = m
    left, bottom, right, top = rect
    xs, ys = [], []
    for x, y in ((left, bottom), (left, top), (right, bottom), (right, top)):
        xs.append(a * x + c * y + e)
        ys.append(b * x + d * y + f)
    return min(xs), min(ys), max(xs), max(ys)


_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _iter_images(page, pdfium, raw):
    """Yield (image object, bounds in page space) for every image, following forms.

    pdfium reports the bounds of an object inside a Form XObject in the form's
    own space, not the page's (a full-page scan wrapped in a form scaled 2x
    reports half its real size). Each form's matrix is composed on the way down
    so every image comes back in page coordinates. Objects are visited through
    the raw API so text and path objects, which can number in the tens of
    thousands on a chart-heavy page, cost one type check each.
    """

    def walk(container, is_form: bool, ctm: tuple[float, ...], depth: int):
        if is_form:
            count = raw.FPDFFormObj_CountObjects(container)
        else:
            count = raw.FPDFPage_CountObjects(container)
        for i in range(max(count, 0)):
            obj = raw.FPDFFormObj_GetObject(container, i) if is_form else raw.FPDFPage_GetObject(container, i)
            if not obj:
                continue
            kind = raw.FPDFPageObj_GetType(obj)
            if kind == raw.FPDF_PAGEOBJ_IMAGE:
                helper = pdfium.PdfObject(obj, page=page, pdf=page.pdf, level=depth)
                try:
                    bounds = helper.get_bounds()
                except Exception:
                    continue
                yield helper, (_transform_rect(ctm, bounds) if depth else bounds)
            elif kind == raw.FPDF_PAGEOBJ_FORM and depth < FORM_MAX_DEPTH - 1:
                helper = pdfium.PdfObject(obj, page=page, pdf=page.pdf, level=depth)
                try:
                    own = tuple(float(v) for v in helper.get_matrix().get())
                except Exception:
                    own = _IDENTITY
                yield from walk(obj, True, _compose(ctm, own), depth + 1)

    yield from walk(page.raw, False, _IDENTITY, 0)


def _probe_page(page, index: int, pdfium, raw) -> PageProbe:
    textpage = page.get_textpage()
    try:
        text = textpage.get_text_range() or ""
    finally:
        textpage.close()
    char_count = sum(1 for ch in text if not ch.isspace() and ch not in "￾\x02")

    width, height = page.get_size()
    page_area = max(width * height, 1.0)
    covered = 0.0
    images: list[tuple[object, float]] = []
    for img, (left, bottom, right, top) in _iter_images(page, pdfium, raw):
        # Clip to the page box; overlapping images are rare enough to just sum.
        w = max(0.0, min(right, width) - max(left, 0.0))
        h = max(0.0, min(top, height) - max(bottom, 0.0))
        share = (w * h) / page_area
        covered += share
        images.append((img, share))
    coverage = min(covered, 1.0)

    image_only = char_count < IMAGE_ONLY_MAX_CHARS and coverage >= IMAGE_ONLY_MIN_COVERAGE
    result = PageProbe(
        index=index,
        text=text,
        char_count=char_count,
        image_coverage=round(coverage, 4),
        image_only=image_only,
        ocr_layer=char_count >= IMAGE_ONLY_MAX_CHARS and coverage >= OCR_LAYER_MIN_COVERAGE,
    )
    if not image_only:
        return result

    # Scan evidence, only for image-only pages. Filters are read from the image
    # dictionary. Metadata can be slow on some images (pdfium may decode to learn
    # the bit depth, crbug.com/pdfium/1928), so it is read only for an image
    # large enough to matter, and only when the codec has not settled it.
    for img, share in images:
        try:
            filters = set(img.get_filters())
        except Exception:
            filters = set()
        if filters & SCAN_CODECS:
            result._codec = True
            break
        if share >= SCAN_GRAY_MIN_COVERAGE:
            try:
                meta = img.get_metadata()
                bpp = int(meta.bits_per_pixel)
                space = int(meta.colorspace)
            except Exception:
                continue
            gray_spaces = {
                raw.FPDF_COLORSPACE_DEVICEGRAY,
                raw.FPDF_COLORSPACE_CALGRAY,
                raw.FPDF_COLORSPACE_ICCBASED,  # 8 bpp ICC = one component = gray
            }
            if bpp == 1 or (bpp == 8 and space in gray_spaces):
                result._gray_full = True
                break
    return result


def _classify_scans(pages: list[PageProbe], producer: str) -> None:
    """Fold per-page and document-level evidence into scan_likely."""
    if not pages:
        return
    image_only = [p for p in pages if p.image_only]
    if not image_only:
        return
    doc_level: list[str] = []
    if any(token in producer for token in SCANNER_PRODUCERS):
        doc_level.append("producer")
    if len(image_only) / len(pages) >= SCAN_DOC_MIN_FRACTION:
        doc_level.append("most_pages_image_only")

    # Runs of consecutive image-only pages (by page index, so a run is never
    # broken by a page the probe skipped).
    in_run: set[int] = set()
    run: list[int] = []
    for p in pages:
        if p.image_only and run and p.index == run[-1] + 1:
            run.append(p.index)
        else:
            if len(run) >= SCAN_RUN_MIN_PAGES:
                in_run.update(run)
            run = [p.index] if p.image_only else []
    if len(run) >= SCAN_RUN_MIN_PAGES:
        in_run.update(run)

    for p in image_only:
        evidence = list(doc_level)
        if p._codec:
            evidence.append("scanner_codec")
        if p._gray_full:
            evidence.append("bilevel_or_gray_full_page")
        if p.index in in_run:
            evidence.append("image_page_run")
        p.scan_evidence = evidence
        p.scan_likely = bool(evidence)


def probe_pdf(
    path: str | os.PathLike[str],
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
) -> PdfProbe | None:
    """Probe `path`. Returns None when the PDF cannot be read; never raises.

    Stops after `max_pages` pages or `time_budget_s` seconds and returns what it
    has with truncated=True. pdfium extracts text in milliseconds per page, so
    the default budget covers several hundred pages.
    """
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw
    except Exception as exc:  # pragma: no cover - pypdfium2 ships with markitdown[pdf]
        logger.warning("PDF probe unavailable (pypdfium2 not importable): %s", exc)
        return None

    with _PDFIUM_LOCK:
        # Timed from here: the one-off import and any wait for the lock are not
        # the document's cost.
        start = time.perf_counter()
        doc = None
        try:
            doc = pdfium.PdfDocument(str(path))
            page_count = len(doc)
            producer = ""
            try:
                meta = doc.get_metadata_dict()
                producer = f"{meta.get('Producer', '')} {meta.get('Creator', '')}".strip().lower()
            except Exception:
                producer = ""

            pages: list[PageProbe] = []
            truncated = False
            for index in range(page_count):
                if index >= max_pages or (time.perf_counter() - start) > time_budget_s:
                    truncated = True
                    break
                page = doc[index]
                try:
                    pages.append(_probe_page(page, index, pdfium, raw))
                finally:
                    page.close()

            _classify_scans(pages, producer)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            logger.debug(
                "Probed %d/%d pages of %s in %d ms", len(pages), page_count, path, elapsed_ms
            )
            return PdfProbe(
                page_count=page_count,
                pages=pages,
                producer=producer,
                truncated=truncated,
                elapsed_ms=elapsed_ms,
            )
        except Exception as exc:
            logger.warning("PDF probe could not read %s: %s", os.path.basename(str(path)), exc)
            return None
        finally:
            # Close explicitly: on Windows an open handle would stop the server
            # deleting its temporary copy of the upload.
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass
