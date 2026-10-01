"""Missing-text check: compare a PDF conversion with the PDF's own text layer.

After a PDF is converted, the Markdown is compared with the text pdfium reads
from the same file (app/core/pdf_probe.py). If too much of that text is absent
from the output, the result carries a warning such as "~18% of the source text
may be missing (pages 4, 7). Try Docling."

The reference is the PDF's embedded text layer, not ground truth, so messages
say "may be missing". Pages where the text layer cannot be trusted are left out
of the comparison: image-only pages (nothing to compare), OCR'd scans (the
layer is itself a guess) and CJK-heavy pages (word segmentation differs
between tools). Scanned pages are reported separately, and only for engines
that cannot read them.

What it catches, by engine:
- MarkItDown and Markit read the same text layer, so for them it mainly
  catches scanned pages (nothing to read), glued or garbled extraction
  ("(cid:12)" font junk) and form-layout losses;
- Docling rebuilds the page from layout analysis, so it catches dropped
  tables and regions.

Contract: read-only and best effort. assess() never raises and never changes
the saved file; a failure inside it yields checked=False with
skipped_reason="error". Returns None for anything that is not a PDF.
"""
from __future__ import annotations

import html
import logging
import re
import time
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.pdf_probe import PdfProbe, is_pdf, probe_pdf
from app.core.queue_model import EngineKind

logger = logging.getLogger(__name__)

# ─── Thresholds ──────────────────────────────────────────────────────────────
# Provisional values from the plan (3.2). Prajnya measured 61-83% coverage on
# pages that lost a table against 98-99% on prose pages, which is where these
# sit. Re-derive them with scripts/calibrate_quality_check.py on a real corpus
# and record the measured numbers next to each constant.
DOC_WARN = 0.90                 # warn when under 90% of the document's text is found
PAGE_WARN = 0.75                # ...or when one page is under 75%
PAGE_WARN_MIN_WORDS = 50        # pages shorter than this never warn on their own
REF_PAGE_MIN_WORDS = 30         # pages shorter than this are not compared at all
CJK_PAGE_FRACTION = 0.30        # pages with more CJK than this are skipped (v1)
GARBLED_TOKEN_FRACTION = 0.05   # "(cid:N)" share of output tokens that means garbled
EMPTY_OUTPUT_MAX_TOKENS = 10    # output this small counts as "nothing extracted"
HEADER_FOOTER_MIN_PAGES = 3     # a repeated edge line on this many pages...
HEADER_FOOTER_MIN_FRACTION = 0.30  # ...and this share of pages is a running header
DEFAULT_TIME_BUDGET_S = 1.0     # tokenising and counting; past it, skip
MAX_LISTED_PAGES = 5            # longer page lists become "4, 7, 9 and 6 more"

SKIP_NOT_PDF = "not_pdf"
SKIP_NO_TEXT_LAYER = "no_text_layer"
SKIP_TOO_LITTLE_TEXT = "too_little_text"
SKIP_UNREADABLE = "unreadable"
SKIP_TIMEOUT = "timeout"
SKIP_ERROR = "error"
SKIP_DISABLED = "disabled"

ENGINE_LABELS = {
    "markitdown": "MarkItDown",
    "docling": "Docling",
    "markit": "Markit",
    "glm_ocr": "GLM-OCR",
}


@dataclass
class QualityReport:
    checked: bool
    coverage: float | None = None        # 0..1 over reference pages
    missing_pct: int | None = None       # round((1 - coverage) * 100)
    page_count: int = 0
    pages_checked: int = 0               # reference pages compared
    low_pages: list[int] = field(default_factory=list)    # 1-based pages under PAGE_WARN
    scan_pages: list[int] = field(default_factory=list)   # scan-likely pages the engine could not read
    image_pages: list[int] = field(default_factory=list)  # other image-only pages (info only)
    garbled: bool = False                # output full of "(cid:N)" or U+FFFD
    warning: bool = False
    message: str = ""                    # user-facing, "" when no warning
    suggestion: str | None = None        # an engine ("docling", "glm_ocr", "markitdown"), "install_<engine>", or None
    skipped_reason: str = ""
    details: list[str] = field(default_factory=list)  # info lines for the Details disclosure
    engine: str = ""                     # engine whose output was assessed
    output_tokens: int = 0               # comparable words in the output
    # (1-based page, coverage, words) for every compared page. Used by
    # scripts/calibrate_quality_check.py to set thresholds; not sent to clients.
    page_coverages: list[tuple[int, float, int]] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("page_coverages", None)
        if self.coverage is not None:
            data["coverage"] = round(self.coverage, 3)
        return data


# ─── Text normalisation ──────────────────────────────────────────────────────
_TOKEN_RE = re.compile(r"[^\W_]+")
# pdfium marks a hyphen it removed at a line break with U+FFFE (some builds use
# \x02); the word continues straight after it.
_PDFIUM_HYPHEN_RE = re.compile("[￾\x02][ \t\r\n]*")
# A hyphen between word characters, across a line break or not. A compound
# word wrapped at its own hyphen ("co-\noperation") is indistinguishable from
# hyphenation, and one tool keeps the hyphen where another drops it, so both
# sides join every intra-word hyphen the same way.
_WORD_HYPHEN_RE = re.compile(r"(\w)-[ \t]*(?:\r?\n\s*)?(\w)")
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CID_RE = re.compile(r"\(cid:\d+\)")
_FFFD_RUN_RE = re.compile("�{3,}")

_MD_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_MD_DATA_URI_RE = re.compile(r"data:[^\s)\"']+")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")
_MD_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_MD_FENCE_RE = re.compile(r"(```|~~~)[^\n]*")
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~<])")
_MD_PUNCT_RE = re.compile(r"[#*_>~|`]")

_RTL_RE = re.compile("[֐-ࣿיִ-﷿ﹰ-﻿]")


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3040 <= cp <= 0x30FF       # Hiragana, Katakana
        or 0x3400 <= cp <= 0x4DBF    # CJK Extension A
        or 0x4E00 <= cp <= 0x9FFF    # CJK Unified Ideographs
        or 0xAC00 <= cp <= 0xD7AF    # Hangul syllables
        or 0xF900 <= cp <= 0xFAFF    # CJK Compatibility Ideographs
    )


def _cjk_fraction(text: str) -> float:
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return 0.0
    return sum(1 for ch in chars if _is_cjk(ch)) / len(chars)


def normalize_text(text: str) -> str:
    """Normalisation applied identically to the reference and the output."""
    text = text.replace("­", "")              # soft hyphens
    text = _PDFIUM_HYPHEN_RE.sub("", text)          # pdfium line-break hyphens
    text = unicodedata.normalize("NFKC", text)      # ligatures: "ﬁ" -> "fi"
    text = _WORD_HYPHEN_RE.sub(r"\1\2", text)       # "hyphen-\nated" -> "hyphenated"
    text = _CONTROL_RE.sub(" ", text)
    return text.lower()


def normalize_markdown(md: str) -> str:
    """Strip Markdown syntax that has no counterpart in the PDF, then normalise."""
    md = _MD_COMMENT_RE.sub(" ", md)               # <!-- image -->
    md = _MD_DATA_URI_RE.sub(" ", md)
    md = _MD_IMAGE_RE.sub(r" \1 ", md)             # keep alt text, drop the target
    md = _MD_LINK_TARGET_RE.sub("] ", md)          # [text](target) -> [text]
    md = _MD_TAG_RE.sub(" ", md)                   # <br>, <sup>, ...
    md = _MD_FENCE_RE.sub(" ", md)
    md = _MD_ESCAPE_RE.sub(r"\1", md)              # \_ -> _
    md = html.unescape(md)                         # &amp; -> &
    md = _CID_RE.sub(" ", md)                      # font junk is not text
    md = _MD_PUNCT_RE.sub(" ", md)
    return normalize_text(md)


def tokenize(text: str) -> list[str]:
    """Words of two or more characters, plus every number (a lost table is mostly numbers)."""
    return [t for t in _TOKEN_RE.findall(text) if len(t) >= 2 or t.isdigit()]


def _edge_key(line: str) -> str:
    return re.sub(r"\d", "#", line.strip())


def _strip_running_lines(pages: list[list[str]]) -> list[list[str]]:
    """Remove running headers and footers (they differ only in their page number).

    Docling drops them on purpose, so leaving them in the reference would count
    them as missing on every page. Only the first and last two lines of a page
    are candidates.
    """
    if len(pages) < HEADER_FOOTER_MIN_PAGES:
        return pages
    counts: Counter[str] = Counter()
    for lines in pages:
        edges = {_edge_key(ln) for ln in lines[:2] + lines[-2:] if ln.strip()}
        counts.update(edges)
    threshold = max(HEADER_FOOTER_MIN_PAGES, HEADER_FOOTER_MIN_FRACTION * len(pages))
    running = {key for key, n in counts.items() if n >= threshold}
    if not running:
        return pages
    out = []
    for lines in pages:
        n = len(lines)
        out.append([
            ln for i, ln in enumerate(lines)
            if not ((i < 2 or i >= n - 2) and _edge_key(ln) in running)
        ])
    return out


def _match_ratios(demand: Counter[str], output: Counter[str]) -> dict[str, float]:
    """For each reference token, the share of its occurrences found in the output.

    Each output occurrence counts once (supply), so a number repeated elsewhere
    in the document cannot hide a table that was lost. When the output has
    fewer copies than the reference, the shortfall is spread over every page
    using the token in proportion, rather than charged to whichever page comes
    last: common words are shared by all pages, and walking pages in order
    would always blame the final one. pdfium may return Arabic and Hebrew in
    visual order, so an RTL token's reverse also counts as supply.
    """
    ratios: dict[str, float] = {}
    for token, needed in demand.items():
        supply = output.get(token, 0)
        if _RTL_RE.search(token):
            rev = token[::-1]
            if rev != token:
                supply += output.get(rev, 0)
        ratios[token] = min(1.0, supply / needed)
    return ratios


# ─── Messages ────────────────────────────────────────────────────────────────
def format_pages(pages: list[int]) -> str:
    """'page 4', 'pages 4, 7' or 'pages 4, 7, 9 and 6 more'."""
    if not pages:
        return ""
    if len(pages) == 1:
        return f"page {pages[0]}"
    if len(pages) <= MAX_LISTED_PAGES:
        return "pages " + ", ".join(str(p) for p in pages)
    shown = ", ".join(str(p) for p in pages[:3])
    return f"pages {shown} and {len(pages) - 3} more"


def _engine_label(engine: str) -> str:
    return ENGINE_LABELS.get(engine, engine.replace("_", "-").upper() if engine else "this engine")


def _ocr_advice(
    ocr_engine: EngineKind | None,
    ocr_installable: bool,
    *,
    try_suffix: str,
    install_text: str,
    install_engine: str = "docling",
) -> tuple[str, str | None]:
    """The 'Try Docling' clause and its suggestion code, for a problem the OCR engine fixes.

    install_text names Docling; when the installable OCR engine is another one
    (GLM-OCR where Docling has no build), the name is swapped in.
    """
    if ocr_engine is not None:
        return f"Try {_engine_label(ocr_engine.value)}{try_suffix}.", ocr_engine.value
    if ocr_installable:
        if install_engine != "docling":
            install_text = install_text.replace("Docling", _engine_label(install_engine))
        return install_text, f"install_{install_engine}"
    return "", None


def _build_message(
    report: QualityReport,
    engine_used: str,
    ocr_engine: EngineKind | None,
    ocr_installable: bool,
    *,
    missing_pct: int | None,
    whole_document_scanned: bool,
    install_engine: str = "docling",
) -> tuple[str, str | None]:
    """One user-facing sentence (or two) and the suggestion code, most important cause first."""
    engine_label = _engine_label(engine_used)
    reads_text_layer = engine_used in (EngineKind.MARKITDOWN.value, EngineKind.MARKIT.value)

    def join(lead: str, advice: tuple[str, str | None]) -> tuple[str, str | None]:
        return (f"{lead} {advice[0]}".strip(), advice[1])

    if reads_text_layer and report.scan_pages:
        if whole_document_scanned and report.output_tokens <= EMPTY_OUTPUT_MAX_TOKENS:
            return join(
                "This PDF is scanned, so it has no text to extract.",
                _ocr_advice(ocr_engine, ocr_installable, install_engine=install_engine, try_suffix=" (OCR)",
                            install_text="Install Docling in Settings to read it with OCR."),
            )
        n, total = len(report.scan_pages), report.page_count
        noun = f"page{'s' if total != 1 else ''} {'is a scanned image' if n == 1 else 'are scanned images'}"
        lead = f"{n} of {total} {noun} with no text. {engine_label} can't read {'it' if n == 1 else 'them'}."
        return join(
            lead,
            _ocr_advice(ocr_engine, ocr_installable, install_engine=install_engine, try_suffix=" (OCR)",
                        install_text="Install Docling in Settings to read scanned pages."),
        )

    if report.garbled:
        lead = "Part of this PDF came out as unreadable characters (its fonts don't map to text)."
        if reads_text_layer:
            return join(
                lead,
                _ocr_advice(ocr_engine, ocr_installable, install_engine=install_engine, try_suffix="",
                            install_text="Docling may read it; install it in Settings."),
            )
        return (f"{lead} Try MarkItDown.", EngineKind.MARKITDOWN.value)

    if missing_pct is not None:
        where = f" ({format_pages(report.low_pages)})" if report.low_pages else ""
        lead = f"~{missing_pct}% of the source text may be missing{where}."
        if engine_used == EngineKind.DOCLING.value:
            return (
                f"{lead} Docling may have dropped a table or region. Try MarkItDown.",
                EngineKind.MARKITDOWN.value,
            )
        if engine_used == EngineKind.GLM_OCR.value:
            # A vision model can silently skip a table; the PDF's own text layer
            # (which this check compared against) is what MarkItDown reads.
            return (
                f"{lead} GLM-OCR may have skipped a table or region. Try MarkItDown.",
                EngineKind.MARKITDOWN.value,
            )
        return join(
            lead,
            _ocr_advice(ocr_engine, ocr_installable, install_engine=install_engine, try_suffix="",
                        install_text="Docling may read it better; install it in Settings."),
        )

    return ("", None)


def _plural_pages(pages: list[int], one: str, many: str) -> str:
    listed = format_pages(pages)
    return f"{listed[0].upper()}{listed[1:]} {one if len(pages) == 1 else many}"


# ─── Assessment ──────────────────────────────────────────────────────────────
def assess(
    source_path: str,
    markdown: str,
    engine_used: EngineKind | str,
    *,
    probe: PdfProbe | None = None,
    ocr_engine: EngineKind | None = None,
    ocr_installable: bool = False,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
    ocr_install_engine: str = "docling",
) -> QualityReport | None:
    """Compare `markdown` with the text layer of the PDF at `source_path`.

    Args:
        engine_used: the concrete engine that produced `markdown`.
        probe: a probe of the same file, reused so it is not read twice.
        ocr_engine: the best OCR engine usable right now (Docling today), or None.
        ocr_installable: an OCR engine could be installed from Settings. With
            neither, warnings carry no suggestion (e.g. a platform Docling does
            not support), rather than pointing at something the user cannot do.
        ocr_install_engine: which engine an install hint names ("docling" or
            "glm_ocr"); the suggestion becomes "install_<engine>".

    Returns None when the file is not a PDF. Never raises.
    """
    engine = engine_used.value if isinstance(engine_used, EngineKind) else str(engine_used)
    try:
        if probe is None:
            if not is_pdf(source_path):
                return None
            probe = probe_pdf(source_path)
        if probe is None:
            return QualityReport(checked=False, skipped_reason=SKIP_UNREADABLE, engine=engine)
        return _assess(
            markdown or "", engine, probe, ocr_engine, ocr_installable, time_budget_s, ocr_install_engine
        )
    except Exception as exc:
        logger.warning("Missing-text check failed: %s", exc)
        return QualityReport(checked=False, skipped_reason=SKIP_ERROR, engine=engine)


def _assess(
    markdown: str,
    engine: str,
    probe: PdfProbe,
    ocr_engine: EngineKind | None,
    ocr_installable: bool,
    time_budget_s: float,
    install_engine: str = "docling",
) -> QualityReport:
    start = time.perf_counter()
    report = QualityReport(checked=True, page_count=probe.page_count, engine=engine)
    report.image_pages = probe.image_pages
    reads_text_layer = engine in (EngineKind.MARKITDOWN.value, EngineKind.MARKIT.value)
    if reads_text_layer:
        report.scan_pages = probe.scan_pages

    # Output side.
    output_norm = normalize_markdown(markdown)
    output_tokens = tokenize(output_norm)
    report.output_tokens = len(output_tokens)
    cid_count = len(_CID_RE.findall(markdown))
    cid_share = cid_count / max(1, cid_count + len(output_tokens))
    report.garbled = cid_share > GARBLED_TOKEN_FRACTION or bool(_FFFD_RUN_RE.search(markdown))

    # Reference side: pages whose text layer can be trusted.
    ocr_pages: list[int] = []
    cjk_pages: list[int] = []
    candidates = []
    for page in probe.pages:
        if page.image_only:
            continue
        if page.ocr_layer:
            ocr_pages.append(page.index + 1)
            continue
        if _cjk_fraction(page.text) > CJK_PAGE_FRACTION:
            cjk_pages.append(page.index + 1)
            continue
        lines = [ln for ln in normalize_text(page.text).splitlines() if ln.strip()]
        candidates.append((page.index, lines))

    stripped = _strip_running_lines([lines for _, lines in candidates])
    reference: list[tuple[int, Counter[str]]] = []
    for (index, _), lines in zip(candidates, stripped, strict=True):
        tokens = tokenize("\n".join(lines))
        if len(tokens) >= REF_PAGE_MIN_WORDS:
            reference.append((index, Counter(tokens)))

    timed_out = False
    if reference:
        demand: Counter[str] = Counter()
        for _, page_tokens in reference:
            demand.update(page_tokens)
        ratios = _match_ratios(demand, Counter(output_tokens))
        total = 0
        covered = 0.0
        for index, page_tokens in reference:
            if time.perf_counter() - start > time_budget_s:
                timed_out = True
                break
            page_total = sum(page_tokens.values())
            page_covered = sum(count * ratios[token] for token, count in page_tokens.items())
            total += page_total
            covered += page_covered
            report.page_coverages.append((index + 1, round(page_covered / page_total, 4), page_total))
            if page_total >= PAGE_WARN_MIN_WORDS and page_covered / page_total < PAGE_WARN:
                report.low_pages.append(index + 1)
        if timed_out:
            report.checked = False
            report.skipped_reason = SKIP_TIMEOUT
            report.low_pages = []
        else:
            report.pages_checked = len(reference)
            report.coverage = covered / total if total else None
            if report.coverage is not None:
                report.missing_pct = round((1 - report.coverage) * 100)
    else:
        has_text = any(not p.image_only for p in probe.pages)
        report.skipped_reason = SKIP_TOO_LITTLE_TEXT if has_text else SKIP_NO_TEXT_LAYER

    # Verdict.
    coverage_warning = report.coverage is not None and (
        report.coverage < DOC_WARN or bool(report.low_pages)
    )
    report.warning = bool(report.scan_pages) or report.garbled or coverage_warning

    if report.warning:
        whole_doc = bool(probe.pages) and all(p.image_only for p in probe.pages)
        report.message, report.suggestion = _build_message(
            report,
            engine,
            ocr_engine,
            ocr_installable,
            # A small shortfall stays in the details; it is not what a warning about
            # scanned pages or garbled fonts should lead with.
            missing_pct=report.missing_pct if coverage_warning else None,
            whole_document_scanned=whole_doc,
            install_engine=install_engine,
        )

    # Details for the disclosure: what was compared and what was not.
    if report.coverage is not None:
        report.details.append(
            f"{round(report.coverage * 100)}% of the text layer found in the output "
            f"across {report.pages_checked} of {report.page_count} pages."
        )
    if report.image_pages:
        report.details.append(
            _plural_pages(report.image_pages, "is a full-page image", "are full-page images")
            + "; any text inside "
            + ("it" if len(report.image_pages) == 1 else "them")
            + " wasn't extracted."
        )
    if not reads_text_layer and probe.scan_pages:
        report.details.append(
            _plural_pages(probe.scan_pages, "is a scanned image", "are scanned images")
            + f", read with {_engine_label(engine)}'s OCR and not compared."
        )
    if ocr_pages:
        report.details.append(
            _plural_pages(ocr_pages, "has an OCR text layer and wasn't", "have an OCR text layer and weren't")
            + " compared."
        )
    if cjk_pages:
        report.details.append(
            _plural_pages(cjk_pages, "is mostly CJK text and wasn't", "are mostly CJK text and weren't")
            + " compared in this version."
        )
    if probe.truncated:
        report.details.append(
            f"Only the first {probe.pages_probed} of {probe.page_count} pages were checked."
        )
    if report.skipped_reason == SKIP_TIMEOUT:
        report.details.append("The comparison took too long and was skipped.")

    logger.debug(
        "Missing-text check: engine=%s coverage=%s low=%s scan=%s in %.1f ms",
        engine,
        report.coverage,
        report.low_pages,
        report.scan_pages,
        (time.perf_counter() - start) * 1000,
    )
    return report
