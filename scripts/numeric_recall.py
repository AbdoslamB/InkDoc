"""Numeric recall: which of a PDF's numbers a conversion kept, lost or changed, and where they were.

Development-only, used by scripts/pdf_diagnose.py, scripts/table_bench.py and
scripts/bench_replay.py. The reference is the PDF's own text layer, so the
scores say "text-layer reference": they cannot judge a page without one, and an
OCR layer someone else made is only as good as that OCR. Truth written by a
person is the benchmark corpus's job (benchmarks/README.md).

A number is what the missing-text check counts as one (app/core/quality_check.py):
a token of digits after quality_check.normalize_text, on both sides, so
"1,234.56" is the tokens 1, 234 and 56. A misread digit then shows as one token
missing and another extra, and the benchmark and the product never disagree
about what the PDF's numbers are.

    pdf = pdf_numbers("file.pdf", tables=found)   # found: the table finder's result, for zones
    report = compare(pdf, markdown_numbers(markdown))

compare() counts with multisets (the order of the output never costs recall),
then places each loss with difflib on the two reading-order sequences, so a
repeated 0 or 100 is charged to the occurrence whose neighbours went missing
with it, not to the first one in the file:

  missing   in the PDF more often than in the output; each charged occurrence
            has its page, box, zone and kind: "dropped" (nothing stands in its
            place) or "changed" (another number does, e.g. a misread digit)
  extra     in the output more often than in the PDF (precision)
  moved     found, but not in the PDF's order (reading order)

Zones: "margin" (the top and bottom share figure_text treats as page headers
and footers; never counted against recall), "table_ruled", "table_borderless",
"table_grid" (the table finder's kinds), "figure" (inside an embedded image),
"prose", or "unknown" when a page's positions could not be read.
"""
from __future__ import annotations

import difflib
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ZONE_MARGIN = "margin"
ZONE_PROSE = "prose"
ZONE_FIGURE = "figure"
ZONE_UNKNOWN = "unknown"
# The table finder's kinds (app/core/thorough_passes.py), by zone.
TABLE_ZONES = {
    "lines": "table_ruled",
    "hint_split": "table_borderless",
    "borderless_tables": "table_borderless",
    "rating_grids": "table_grid",
}
DROPPED = "dropped"
CHANGED = "changed"
EXAMPLES = 20
CONTEXT_CHARS = 30


@dataclass
class PdfNumber:
    value: str
    page: int                                          # 1-based
    bbox: tuple[float, float, float, float] | None     # (x0, top, x1, bottom), points from the top-left
    zone: str
    context: str = ""


@dataclass
class MdNumber:
    value: str
    context: str = ""


@dataclass
class PdfNumbers:
    numbers: list[PdfNumber]
    pages: int
    no_text_pages: list[int] = field(default_factory=list)   # nothing to compare: scanned or empty
    no_positions: list[int] = field(default_factory=list)    # tokens read, boxes not (zone "unknown")


# ─── Tokens with their place in the raw text ─────────────────────────────────
def _sub(chars: list[str], src: list[int], pattern: Any, repl: str | tuple[int, ...]) -> tuple[list[str], list[int]]:
    """pattern.sub over the characters, keeping each kept character's raw index.

    `repl` is "" (drop the match), " " (one space) or the groups to keep (r"\\1\\2").
    """
    text = "".join(chars)
    out_c: list[str] = []
    out_s: list[int] = []
    pos = 0
    for m in pattern.finditer(text):
        out_c += chars[pos:m.start()]
        out_s += src[pos:m.start()]
        if repl == " ":
            out_c.append(" ")
            out_s.append(src[m.start()])
        elif isinstance(repl, tuple):
            for g in repl:
                s, e = m.span(g)
                out_c += chars[s:e]
                out_s += src[s:e]
        pos = m.end()
    return out_c + chars[pos:], out_s + src[pos:]


def _expand(chars: list[str], src: list[int], fn: Any, group_marks: bool) -> tuple[list[str], list[int]]:
    """Apply `fn` to each character (with its combining marks when `group_marks`), one raw index per result."""
    out_c: list[str] = []
    out_s: list[int] = []
    i = 0
    while i < len(chars):
        j = i + 1
        if group_marks:
            # NFKC composes a letter with the marks after it, and Hangul jamo with each other.
            while j < len(chars) and (unicodedata.combining(chars[j]) or "ᅠ" <= chars[j] <= "ᇿ"):
                j += 1
        for ch in fn("".join(chars[i:j])):
            out_c.append(ch)
            out_s.append(src[i])
        i = j
    return out_c, out_s


def mapped_normalize(raw: str) -> tuple[str, list[int]]:
    """quality_check.normalize_text(raw), and for each character the index in `raw` it came from.

    The same steps in the same order; tests/test_numeric_recall.py holds the
    two to the same text, and pdf_numbers() checks every page's digit tokens
    against normalize_text's before trusting a position.
    """
    from app.core import quality_check as qc

    chars, src = list(raw), list(range(len(raw)))
    chars, src = _expand(chars, src, lambda s: s.replace("­", ""), False)
    chars, src = _sub(chars, src, qc._PDFIUM_HYPHEN_RE, "")
    chars, src = _expand(chars, src, lambda s: unicodedata.normalize("NFKC", s), True)
    chars, src = _sub(chars, src, qc._WORD_HYPHEN_RE, (1, 2))
    chars, src = _sub(chars, src, qc._CONTROL_RE, " ")
    chars, src = _expand(chars, src, str.lower, False)
    return "".join(chars), src


def _digit_tokens(text: str) -> list[tuple[str, int, int]]:
    """(token, start, end) for each number token, as quality_check.tokenize finds them."""
    from app.core.quality_check import _TOKEN_RE

    return [(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(text) if m.group().isdigit()]


def _reference_digits(raw: str) -> list[str]:
    from app.core.quality_check import normalize_text, tokenize

    return [t for t in tokenize(normalize_text(raw)) if t.isdigit()]


def _snippet(text: str, start: int, end: int) -> str:
    return " ".join(text[max(0, start - CONTEXT_CHARS):end + CONTEXT_CHARS].split())


def markdown_numbers(markdown: str) -> list[MdNumber]:
    """The output's numbers in order, after quality_check.normalize_markdown (comments, tags,
    link targets and data URIs are not text)."""
    from app.core.quality_check import normalize_markdown

    text = normalize_markdown(markdown)
    return [MdNumber(tok, _snippet(text, s, e)) for tok, s, e in _digit_tokens(text)]


# ─── The PDF's numbers, with zones ───────────────────────────────────────────
def table_boxes(found: dict | None) -> dict[int, list[tuple[tuple[float, float, float, float], str]]]:
    """{page: [(bbox, zone)]} from the table finder's result (table_finder.find_tables)."""
    boxes: dict[int, list] = {}
    for page in (found or {}).get("pages", []):
        for band in page.get("bands", []):
            for t in band.get("tables", []):
                if t.get("bbox"):
                    zone = TABLE_ZONES.get(t.get("kind", "lines"), "table_ruled")
                    boxes.setdefault(page["page"], []).append((tuple(t["bbox"]), zone))
    return boxes


def _inside(box: tuple[float, float, float, float], x: float, y: float) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def zone_of(bbox: tuple[float, float, float, float] | None, height: float, tables: list, images: list) -> str:
    """The zone of one number's box: margin, then table, then figure, else prose."""
    from app.core.figure_text import MARGIN_SHARE

    if bbox is None:
        return ZONE_UNKNOWN
    x0, top, x1, bottom = bbox
    # figure_text's own test for a page header or footer line, applied to this box.
    if top < MARGIN_SHARE * height or bottom > (1 - MARGIN_SHARE) * height:
        return ZONE_MARGIN
    cx, cy = (x0 + x1) / 2, (top + bottom) / 2
    for box, zone in tables:
        if _inside(box, cx, cy):
            return zone
    if any(_inside(box, cx, cy) for box in images):
        return ZONE_FIGURE
    return ZONE_PROSE


def _page_numbers(page: Any, number: int, pdfium: Any, raw: Any, tables: list) -> tuple[list[PdfNumber], bool, bool]:
    """(numbers, has text, positions read) for one page."""
    from app.core.pdf_probe import _iter_images

    textpage = page.get_textpage()
    try:
        text = textpage.get_text_range() or ""
        width, height = page.get_size()
        reference = _reference_digits(text)
        if not text.strip():
            return [], False, True
        norm, src = mapped_normalize(text)
        tokens = _digit_tokens(norm)
        boxes: list[tuple[float, float, float, float] | None]
        if [t for t, _s, _e in tokens] == reference and textpage.count_chars() == len(text):
            boxes = []
            for _tok, s, e in tokens:
                rects = [textpage.get_charbox(i) for i in sorted({src[k] for k in range(s, e)})]
                left = min(r[0] for r in rects)
                right = max(r[2] for r in rects)
                # pdfium's boxes are (left, bottom, right, top) from the bottom-left corner.
                boxes.append((left, height - max(r[3] for r in rects), right, height - min(r[1] for r in rects)))
            positioned = True
        else:
            tokens = [(t, 0, 0) for t in reference]
            boxes = [None] * len(tokens)
            positioned = False
        images = [
            (left, height - top, right, height - bottom)
            for _img, (left, bottom, right, top) in _iter_images(page, pdfium, raw, None)
        ]
        out = []
        for (tok, s, e), bbox in zip(tokens, boxes, strict=True):
            context = _snippet(text, src[s], src[e - 1] + 1) if positioned else ""
            out.append(PdfNumber(tok, number, bbox, zone_of(bbox, height, tables, images), context))
        return out, True, positioned
    finally:
        textpage.close()


def pdf_numbers(path: str | Path, tables: dict | None = None) -> PdfNumbers:
    """The text layer's numbers in reading order, page by page, each with its zone.

    `tables` is the table finder's result for any pages (table_finder.find_tables);
    without it no number is in a table zone. Holds pdfium's lock, as the probe does.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    from app.core.pdf_probe import pdfium_lock

    by_page = table_boxes(tables)
    result = PdfNumbers(numbers=[], pages=0)
    with pdfium_lock():
        doc = pdfium.PdfDocument(str(path))
        try:
            result.pages = len(doc)
            for index in range(len(doc)):
                page = doc[index]
                try:
                    found, has_text, positioned = _page_numbers(page, index + 1, pdfium, raw, by_page.get(index + 1, []))
                finally:
                    page.close()
                result.numbers += found
                if not has_text:
                    result.no_text_pages.append(index + 1)
                elif not positioned:
                    result.no_positions.append(index + 1)
        finally:
            doc.close()
    return result


# ─── Comparing (pure) ────────────────────────────────────────────────────────
def _pick(candidates: list[tuple[int, int]], n: int) -> set[int]:
    """The `n` indices with the lowest (tier, index)."""
    return {i for _tier, i in sorted(candidates)[:n]}


def compare(pdf: PdfNumbers | list[PdfNumber], md: list[MdNumber]) -> dict[str, Any]:
    """Recall, precision and reading order of `md` against the PDF's numbers; where each loss was."""
    info = pdf if isinstance(pdf, PdfNumbers) else PdfNumbers(numbers=list(pdf), pages=0)
    ref = info.numbers
    a = [n.value for n in ref]
    b = [m.value for m in md]
    in_pdf, in_md = Counter(a), Counter(b)
    found = in_pdf & in_md

    # Where each value sits in the alignment of the two sequences.
    pdf_op: list[tuple] = [()] * len(a)
    md_op: list[tuple] = [()] * len(b)
    for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        tag, i1, i2, j1, j2 = op
        for i in range(i1, i2):
            pdf_op[i] = op
        for j in range(j1, j2):
            md_op[j] = op

    def tier(tag: str, value: str, across: list[str]) -> int:
        # Nothing on the other side here first; then a stretch where the value is
        # absent; last a stretch holding it (a local reshuffle, so likely moved).
        if tag in ("delete", "insert"):
            return 0
        return 2 if value in across else 1

    unmatched_pdf: dict[str, list[tuple[int, int]]] = {}
    for i, (tag, _i1, _i2, j1, j2) in enumerate(pdf_op):
        if tag != "equal":
            unmatched_pdf.setdefault(a[i], []).append((tier(tag, a[i], b[j1:j2]), i))
    unmatched_md: dict[str, list[tuple[int, int]]] = {}
    for j, (tag, i1, i2, _j1, _j2) in enumerate(md_op):
        if tag != "equal":
            unmatched_md.setdefault(b[j], []).append((tier(tag, b[j], a[i1:i2]), j))

    missing: set[int] = set()
    for value, cands in unmatched_pdf.items():
        missing |= _pick(cands, in_pdf[value] - found[value])
    extra: set[int] = set()
    for value, cands in unmatched_md.items():
        extra |= _pick(cands, in_md[value] - found[value])
    moved = [i for cands in unmatched_pdf.values() for _t, i in cands if i not in missing]

    counted = [i for i, n in enumerate(ref) if n.zone != ZONE_MARGIN]
    lost = sorted(i for i in missing if ref[i].zone != ZONE_MARGIN)
    examples, by_zone, by_kind, by_page = [], Counter(), Counter(), Counter()
    zone_pages: dict[str, set[int]] = {}
    for i in lost:
        tag, _i1, _i2, j1, j2 = pdf_op[i]
        became = [b[j] for j in range(j1, j2) if j in extra] if tag == "replace" else []
        kind = CHANGED if became else DROPPED
        n = ref[i]
        by_zone[n.zone] += 1
        by_kind[kind] += 1
        by_page[n.page] += 1
        zone_pages.setdefault(n.zone, set()).add(n.page)
        if len(examples) < EXAMPLES:
            ex = {"value": n.value, "page": n.page, "zone": n.zone, "kind": kind, "context": n.context}
            if became:
                ex["became"] = became
            examples.append(ex)
    total = len(counted)
    scanned = bool(info.no_text_pages) and info.pages > 0
    return {
        "reference": "text-layer",
        "numbers": total,
        "found": total - len(lost),
        "recall": round(1 - len(lost) / total, 4) if total else None,
        # Numbers on pages without a text layer are in the output but not in the
        # reference, so precision means nothing there.
        "precision": None if scanned or not b else round(1 - len(extra) / len(b), 4),
        "in_order": round(sum(1 for i in counted if pdf_op[i][0] == "equal") / total, 4) if total else None,
        "missing": len(lost),
        "extra": len(extra),
        "moved": sum(1 for i in moved if ref[i].zone != ZONE_MARGIN),
        "margin_numbers": len(ref) - total,
        "margin_missing": sum(1 for i in missing if ref[i].zone == ZONE_MARGIN),
        "missing_by_zone": dict(sorted(by_zone.items())),
        "missing_by_kind": dict(sorted(by_kind.items())),
        "missing_by_page": {p: by_page[p] for p in sorted(by_page)},
        "missing_pages_by_zone": {z: sorted(p) for z, p in sorted(zone_pages.items())},
        "examples": examples,
        "extra_examples": [{"value": md[j].value, "context": md[j].context} for j in sorted(extra)[:EXAMPLES // 2]],
        "no_text_pages": list(info.no_text_pages),
        "no_positions": list(info.no_positions),
    }


def summary_line(r: dict[str, Any] | None) -> str:
    """'412 numbers, 97.3% found (11 missing: table_ruled 5, prose 6; 3 changed), 2 extra, 12 out of order'."""
    if not r:
        return "not measured"
    if not r["numbers"]:
        return "no numbers in the text layer"
    zones = ", ".join(f"{z} {n}" for z, n in r["missing_by_zone"].items())
    changed = r["missing_by_kind"].get(CHANGED, 0)
    lost = f" ({r['missing']} missing: {zones}{f'; {changed} changed' if changed else ''})" if r["missing"] else ""
    extra = "" if r["precision"] is None else f", {r['extra']} extra"
    return f"{r['numbers']} numbers, {100 * r['recall']:.1f}% found{lost}{extra}, {r['moved']} out of order"
