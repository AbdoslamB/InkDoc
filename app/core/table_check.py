"""Compare an engine's tables with the PDF's own, and swap in the ones it got wrong.

Pure: works on the Markdown text and on the JSON from app/core/table_finder.py.
No PDF is opened here.

For every band of tables the finder reported (one table, or several side by
side), in document order:

1. Locate it in the Markdown. First as a run of consecutive lines made only of
   the band's own words (holding most of them); failing that, between the
   text lines just above and below it in the PDF, when the band's words are
   nowhere else (a table an engine dropped or drew as a picture).
2. Compare what is there with the PDF's tables: same number of tables, same
   columns, same body rows. Header styling is not compared: one header row or
   two stacked ones are both right.
3. Report what is wrong: merged, split, loose lines, missing, a picture
   placeholder, shapes left out, or rows and columns that differ.
4. Repair (Auto only): replace the located lines with the PDF's tables, only
   when every table in the band was verified against the text layer and the
   lines being replaced hold nothing but the band's own words. Each table it
   writes is preceded by an HTML comment saying where it came from.

A band that cannot be located safely is reported and left alone.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.core.markdown_tables import (
    TableBlock,
    cell_key,
    find_table_blocks,
    line_tokens,
    render_pipe_table,
)

MIN_LOCATE_COVERAGE = 0.6      # a run of lines must hold this share of a band's words
LINE_TOLERANCE = 0.1           # a long line may differ from the table in this share of its words...
TOLERANT_LINE_TOKENS = 8       # ...when it has at least this many (short lines must match exactly)
LONG_CELL_TOKENS = 8           # cells this long compare as alike at...
CELL_SIMILARITY = 0.9          # ...this share of shared words; shorter cells (numbers) must be equal
MAX_ELSEWHERE_COVERAGE = 0.3   # an anchor slice is used only if the words are not elsewhere
ANCHOR_TOKENS = 8              # words of an anchor line matched in the Markdown
ANCHOR_MIN_TOKENS = 2

OK = ""
MERGED = "merged"
SPLIT = "split"
LOOSE = "loose"
MISSING = "missing"
PICTURE = "picture"
SHAPES = "shapes"
MISMATCH = "mismatch"

SOURCE_LABELS = {
    "pdf_lines": "the PDF's ruling lines",
    "pdf_words": "the positions of the PDF's words",
    "pdf_shapes": "the PDF's drawn rating markers",
    "glm_ocr": "a GLM-OCR reading of the table",
}

SHAPE_LABEL_RE = re.compile(r"\[(?:\d+ )?(?:[a-z]+ )?(?:shapes?|images?)(?::[^\]]*)?\]")
_PLACEHOLDER_RE = re.compile(r"<!--\s*image\s*-->", re.I)


@dataclass
class BandResult:
    page: int
    tables: list[dict]
    start: int | None = None          # located lines [start, end) in the Markdown
    end: int | None = None
    how: str = ""                     # "words" | "anchors" | ""
    issue: str = OK
    message: str = ""
    repaired: bool = False
    not_repaired: str = ""            # why a found issue was left as it is

    @property
    def located(self) -> bool:
        return self.start is not None

    @property
    def verified(self) -> bool:
        return bool(self.tables) and all(t.get("verified") for t in self.tables)

    @property
    def source(self) -> str:
        sources = {t.get("source", "pdf_lines") for t in self.tables}
        return sources.pop() if len(sources) == 1 else "mixed"


# ─── Locating ────────────────────────────────────────────────────────────────
def _band_words(band: dict) -> Counter[str]:
    """The band's words as the PDF's text layer has them (what a reading must contain)."""
    words: Counter[str] = Counter()
    for table in band.get("tables", []):
        words.update(table.get("words") or [])
    return words


def _band_allowance(band: dict) -> Counter[str]:
    """The most of each word a faithful reading may contain.

    A merged cell's text is legitimately repeated (InkDoc's own grid does it,
    Docling writes "Revenue - 2023 | Revenue - 2024"), so the allowance is the
    larger of the text layer's count and the repeated grid's, captions included.
    """
    allow = _band_words(band)
    for table in band.get("tables", []):
        grid: Counter[str] = Counter()
        for text in list(table.get("captions") or []) + [c for row in table.get("rows", []) for c in row]:
            grid.update(line_tokens(text))
        for token, count in grid.items():
            if count > allow[token]:
                allow[token] = count
    return allow


def _covered(seen: Counter[str], need: Counter[str], skip: set[str] = frozenset()) -> int:  # type: ignore[assignment]
    return sum(min(c, need[t]) for t, c in seen.items() if t not in skip)


def _odd_words(line: Counter[str], remaining: Counter[str]) -> int:
    return sum(max(0, c - max(0, remaining[t])) for t, c in line.items())


def _tolerated(line: Counter[str], odd: int) -> int:
    """How many words a line may have that the table doesn't (a subscript read apart, a dropped dash)."""
    size = sum(line.values())
    return int(LINE_TOLERANCE * size) if size >= TOLERANT_LINE_TOKENS else 0


def _best_run(
    toks: list[list[str]], allow: Counter[str], need: Counter[str], start: int
) -> tuple[int, int, Counter[str]]:
    """(first line, end line, words seen) of the run of lines made of `allow` covering most of `need`.

    A long line may hold a few words the table doesn't have (LINE_TOLERANCE),
    so a one-word difference doesn't hide a table the engine wrote; whether
    the run fits exactly is checked again before anything is replaced.
    """
    best: tuple[int, int, Counter[str]] = (start, start, Counter())
    best_covered = 0
    n = len(toks)
    i = start
    while i < n:
        if not toks[i]:
            i += 1
            continue
        remaining = Counter(allow)
        seen: Counter[str] = Counter()
        last = i
        j = i
        while j < n:
            line = Counter(toks[j])
            if line:
                odd = _odd_words(line, remaining)
                if odd > _tolerated(line, odd):
                    break
                remaining.subtract(line)
                seen.update(line)
                last = j
            j += 1
        covered = _covered(seen, need)
        if covered > best_covered:
            best, best_covered = (i, last + 1, seen), covered
        i = last + 1 if seen else i + 1
    return best


def _find_anchor(toks: list[list[str]], anchor: list[str], start: int, stop: int, *, last: bool) -> int | None:
    """Line index in [start, stop) whose words contain `anchor` in order; the first or last one.

    A one-word anchor (a short heading such as "Inventory") counts only when
    that word is on exactly one line from `start` on, so it cannot point at
    the wrong place.
    """
    if not anchor:
        return None
    k = len(anchor)
    hits = []
    for i in range(max(0, start), min(stop, len(toks))):
        line = toks[i]
        if any(line[j:j + k] == anchor for j in range(len(line) - k + 1)):
            hits.append(i)
            if not last and k >= ANCHOR_MIN_TOKENS:
                return i
    if not hits:
        return None
    if k < ANCHOR_MIN_TOKENS:
        everywhere = [i for i in range(max(0, start), len(toks)) if anchor[0] in toks[i]]
        return hits[0] if len(everywhere) == 1 else None
    return hits[-1] if last else hits[0]


def _fits(toks: list[list[str]], start: int, end: int, words: Counter[str]) -> bool:
    """Lines [start, end) hold no word from outside the band (what replacing them may delete).

    Repeats of the band's own words are fine: an engine that copied a merged
    cell into every column ("Alternative Investments*" four times) loses
    nothing when the copies go.
    """
    return all(t in words for i in range(start, end) for t in toks[i])


def _expand_to_blocks(start: int, end: int, blocks: list[TableBlock]) -> tuple[int, int]:
    """Grow [start, end) so it never cuts a Markdown table in two."""
    changed = True
    while changed:
        changed = False
        for b in blocks:
            if b.start < end and b.end > start and (b.start < start or b.end > end):
                start, end = min(start, b.start), max(end, b.end)
                changed = True
    return start, end


def _locate(toks, lines, blocks, band: dict, cursor: int, shared: set[str]) -> tuple[int, int, str] | None:
    """Lines [start, end) holding the band in the Markdown, and how they were found; None if unsure.

    `shared` holds words other bands have too (a header repeated on every
    page): they say nothing about whether this band is there.
    """
    need = _band_words(band)
    allow = _band_allowance(band)
    total = sum(need.values())
    if not total:
        return None
    s, e, seen = _best_run(toks, allow, need, cursor)
    if _covered(seen, need) >= MIN_LOCATE_COVERAGE * total:
        s, e = _expand_to_blocks(s, e, blocks)
        # Exact: only the band's own words, so it may be replaced. Otherwise it
        # can still be compared (and found right), never replaced.
        return s, e, ("words" if _fits(toks, s, e, allow) else "words-approx")
    own_total = sum(c for t, c in need.items() if t not in shared)
    own_covered = _covered(seen, need, shared)
    if own_total and own_covered > MAX_ELSEWHERE_COVERAGE * own_total:
        return None  # partly there, partly not: too ambiguous to touch
    if not own_total and _covered(seen, need) > MAX_ELSEWHERE_COVERAGE * total:
        return None

    # The band's words are (almost) nowhere: find where it should have been,
    # between the PDF's text lines just above and below it. A missing anchor
    # (the table starts or ends the page) reaches only across lines without
    # words: blank lines and picture placeholders.
    n = len(toks)
    below = line_tokens(band.get("below", ""))[:ANCHOR_TOKENS]
    above = line_tokens(band.get("above", ""))[-ANCHOR_TOKENS:]
    b = _find_anchor(toks, below, cursor, n, last=False)
    a = _find_anchor(toks, above, cursor, n if b is None else b, last=True)
    if a is None and b is None:
        return None
    if a is None:
        s = e = b  # type: ignore[assignment]
        while s > cursor and not toks[s - 1]:
            s -= 1
    else:
        s = a + 1
        if b is None:
            e = s
            while e < n and not toks[e]:
                e += 1
        else:
            e = b
    if s > e or not _fits(toks, s, e, allow):
        return None
    if any(bl.start < e and bl.end > s for bl in blocks if not (s <= bl.start and bl.end <= e)):
        return None
    return s, e, "anchors"


# ─── Comparing ───────────────────────────────────────────────────────────────
def _keys(rows: list[list[str]], n_cols: int, strip_shapes: bool = False) -> list[list[str]]:
    out = []
    for row in rows:
        cells = [(SHAPE_LABEL_RE.sub("", c) if strip_shapes else c) for c in row]
        keys = [cell_key(c) for c in cells] + [""] * (n_cols - len(cells))
        out.append(keys[:n_cols])
    return out


def _cells_alike(a: str, b: str) -> bool:
    """Equal, or both long and sharing CELL_SIMILARITY of their words (numbers must be equal)."""
    if a == b:
        return True
    ta, tb = Counter(a.split()), Counter(b.split())
    size = max(sum(ta.values()), sum(tb.values()))
    if min(sum(ta.values()), sum(tb.values())) < LONG_CELL_TOKENS:
        return False
    return sum((ta & tb).values()) >= CELL_SIMILARITY * size


def _rows_alike(a: list[list[str]], b: list[list[str]]) -> bool:
    return len(a) == len(b) and all(
        len(x) == len(y) and all(_cells_alike(p, q) for p, q in zip(x, y, strict=True))
        for x, y in zip(a, b, strict=True)
    )


def same_table(expected: list[list[str]], found: list[list[str]], *, strip_shapes: bool = False) -> bool:
    """Same columns and body rows, and every column under its own header words.

    The header may be one row or several stacked ("Revenue | Revenue" over
    "2023 | 2024" is right); a merged header cell left blank over a column
    ("Revenue |" over "2023 | 2024") is not, because the second column has
    lost its "Revenue".
    """
    if not expected or not found:
        return False
    n_cols = max(len(r) for r in expected)
    if max(len(r) for r in found) != n_cols:
        return False
    exp = _keys(expected, n_cols, strip_shapes)
    got = _keys(found, n_cols)
    body = exp[1:]
    if len(got) < len(exp):
        return False
    if body and not _rows_alike(got[len(got) - len(body):], body):
        return False
    head_rows = got[:len(got) - len(body)]
    for c in range(n_cols):
        want = set(exp[0][c].split())
        have = {t for row in head_rows for t in row[c].split()}
        if not want <= have:
            return False
    return True


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _classify(band: dict, lines: list[str], start: int, end: int) -> tuple[str, str]:
    page = band.get("page", 0)
    tables = band.get("tables", [])
    expected = [t["rows"] for t in tables]
    blocks = find_table_blocks(lines[start:end])
    n = len(expected)
    a_table = _plural(n, "a table", f"{n} tables")
    if not blocks:
        text = "\n".join(lines[start:end])
        if _PLACEHOLDER_RE.search(text):
            return PICTURE, f"Page {page}: {a_table} came out as a picture placeholder, without {_plural(n, 'its', 'their')} text."
        if any(line_tokens(ln) for ln in lines[start:end]):
            return LOOSE, f"Page {page}: {a_table} came out as loose lines of text."
        return MISSING, f"Page {page}: {a_table} {_plural(n, 'is', 'are')} missing from the result."
    found = [b.rows for b in blocks]
    if len(found) == n and all(same_table(e, f) for e, f in zip(expected, found, strict=True)):
        return OK, ""
    if len(found) == n and all(same_table(e, f, strip_shapes=True) for e, f in zip(expected, found, strict=True)):
        return SHAPES, f"Page {page}: the shapes in {_plural(n, 'a table', 'the tables')}' cells were left out."
    if n >= 2 and len(found) < n:
        return MERGED, f"Page {page}: {n} side-by-side tables came out as one."
    if len(found) > n:
        return SPLIT, f"Page {page}: {a_table} came out split into pieces."
    return MISMATCH, f"Page {page}: {_plural(n, 'a table', 'tables')}' rows or columns don't match the PDF."


def check(markdown: str, finder_result: dict[str, Any]) -> tuple[list[str], list[BandResult]]:
    """(the Markdown's lines, one result per band) without changing anything."""
    lines = markdown.split("\n")
    toks = [line_tokens(ln) for ln in lines]
    blocks = find_table_blocks(lines)
    bands = [
        {**band, "page": page.get("page", 0)}
        for page in finder_result.get("pages", [])
        for band in page.get("bands", [])
    ]
    owners: Counter[str] = Counter()
    for band in bands:
        owners.update(set(_band_words(band)))
    results: list[BandResult] = []
    cursor = 0

    def unlocated(band: dict) -> BandResult:
        result = BandResult(page=band["page"], tables=band.get("tables", []))
        result.issue = MISMATCH
        result.message = (
            f"Page {band['page']}: InkDoc couldn't match "
            f"{_plural(len(result.tables), 'a table', 'the tables')} with the result."
        )
        result.not_repaired = "its place in the result could not be found"
        return result

    for band in bands:
        shared = {t for t in _band_words(band) if owners[t] > 1}
        located = _locate(toks, lines, blocks, band, cursor, shared)
        if located is not None:
            result = BandResult(page=band["page"], tables=band.get("tables", []))
            result.start, result.end, result.how = located
            result.issue, result.message = _classify(band, lines, result.start, result.end)
            cursor = result.end
            results.append(result)
            continue
        tables = band.get("tables", [])
        if len(tables) < 2:
            results.append(unlocated(band))
            continue
        # Side by side, but not together in the result (an engine may put a
        # note between them): each table on its own, left to right.
        for table in tables:
            single = {**band, "tables": [table]}
            siblings: Counter[str] = Counter()
            for other in tables:
                if other is not table:
                    siblings.update(set(other.get("words") or []))
            own_shared = {t for t in _band_words(single) if owners[t] > 1 or siblings[t]}
            spot = _locate(toks, lines, blocks, single, cursor, own_shared)
            if spot is None:
                results.append(unlocated(single))
                continue
            result = BandResult(page=band["page"], tables=[table])
            result.start, result.end, result.how = spot
            result.issue, result.message = _classify(single, lines, result.start, result.end)
            cursor = result.end
            results.append(result)
    return lines, results


# ─── Repairing ───────────────────────────────────────────────────────────────
def table_lines(table: dict, page: int) -> list[str]:
    """The Markdown InkDoc writes for one table: provenance comment, captions, pipe table."""
    source = table.get("source", "pdf_lines").replace("_", "-")
    checked = table.get("checked", "text-layer")
    out = [f"<!-- inkdoc:table page={page} source={source} checked={checked} -->"]
    for caption in table.get("captions") or []:
        out += [caption, ""]
    out += render_pipe_table(table["rows"])
    return out


def repair(lines: list[str], results: list[BandResult]) -> str:
    """Replace each repairable band's lines with the PDF's tables; marks results repaired."""
    out = list(lines)
    # Last first, so earlier positions stay valid; of two insertions at the same
    # line, the later band goes in first and the earlier one lands before it.
    located = [(r.start or 0, i, r) for i, r in enumerate(results) if r.located]
    for _start, _index, result in sorted(located, key=lambda t: (t[0], t[1]), reverse=True):
        if result.issue == OK:
            continue
        if result.how == "words-approx":
            result.not_repaired = "the result's text there differs slightly from the PDF's table"
            continue
        if not result.verified:
            reasons = {t.get("reason") for t in result.tables if not t.get("verified")}
            result.not_repaired = "; ".join(sorted(r for r in reasons if r)) or "it could not be checked"
            continue
        block: list[str] = []
        for table in result.tables:
            if block:
                block.append("")
            block += table_lines(table, result.page)
        start, end = result.start or 0, result.end or 0
        if any(t.get("source") == "pdf_shapes" for t in result.tables):
            # The engine drew each marker as a picture next to its empty table:
            # those placeholders are what InkDoc has just written out.
            while start > 0 and (not out[start - 1].strip() or _PLACEHOLDER_RE.fullmatch(out[start - 1].strip())):
                start -= 1
            while end < len(out) and (not out[end].strip() or _PLACEHOLDER_RE.fullmatch(out[end].strip())):
                end += 1
        if start > 0 and out[start - 1].strip():
            block.insert(0, "")
        if end < len(out) and out[end].strip():
            block.append("")
        out[start:end] = block
        result.repaired = True
    return "\n".join(out)


@dataclass
class TableSummary:
    """What happened to a document's tables, for the response and the Details list."""

    checked: bool = False
    skipped_reason: str = ""
    pages_checked: list[int] = field(default_factory=list)
    tables_found: int = 0
    repaired: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    warning: bool = False
    message: str = ""
    details: list[str] = field(default_factory=list)
    figures_filled: int = 0           # Thorough: figures given their text from the PDF
    paragraphs_restored: int = 0      # Thorough: whole paragraphs the engine dropped, put back
    characters_restored: int = 0      # Thorough: '&', dashes and quotes put back

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "skipped_reason": self.skipped_reason,
            "pages_checked": list(self.pages_checked),
            "tables_found": self.tables_found,
            "repaired": list(self.repaired),
            "issues": list(self.issues),
            "warning": self.warning,
            "message": self.message,
            "details": list(self.details),
            "figures_filled": self.figures_filled,
            "paragraphs_restored": self.paragraphs_restored,
            "characters_restored": self.characters_restored,
        }


def summarize(results: list[BandResult], pages_checked: list[int], *, repair_allowed: bool) -> TableSummary:
    summary = TableSummary(checked=True, pages_checked=sorted(pages_checked))
    summary.tables_found = sum(len(r.tables) for r in results)
    for r in results:
        n = len(r.tables)
        if r.repaired:
            summary.repaired.append({"page": r.page, "tables": n, "issue": r.issue, "source": r.source})
            confirmed = any("glm-ocr" in str(t.get("checked", "")) for t in r.tables)
            summary.details.append(
                f"Page {r.page}: rebuilt {_plural(n, 'a table', f'{n} tables')} from "
                f"{SOURCE_LABELS.get(r.source, 'the PDF')} ({r.issue.replace('_', ' ')}); "
                + ("confirmed by a GLM-OCR reading of the same area." if confirmed
                   else "checked against the PDF's text.")
            )
        elif r.issue != OK and not r.located and r.tables and not r.verified:
            # Not placed in the result, and InkDoc couldn't vouch for its own
            # reading either (a grid that may be a chart's): a note, not a warning.
            summary.details.append(f"{r.message} Left as it is: the PDF's grid there could not be checked.")
        elif r.issue != OK and r.tables and all(t.get("confirm") and not t.get("verified") for t in r.tables):
            # Found only from word positions and not confirmed: maybe not a
            # table at all, so a note rather than a warning.
            reasons = sorted({t.get("reason", "") for t in r.tables if t.get("reason")})
            summary.details.append(
                f"Page {r.page}: what looks like a table without lines came out as text; left as it is"
                + (f" ({'; '.join(reasons)})." if reasons else ".")
            )
        elif r.issue != OK:
            why = r.not_repaired or ("" if repair_allowed else "the result was only checked")
            summary.issues.append({"page": r.page, "tables": n, "issue": r.issue, "message": r.message, "why": why})
            summary.details.append(f"{r.message} Left as it is: {why}." if why else r.message)
    if summary.tables_found:
        pages = sorted({r.page for r in results})
        summary.details.insert(0, (
            f"Compared {summary.tables_found} table{'s' if summary.tables_found != 1 else ''} with the PDF "
            f"on page{'s' if len(pages) != 1 else ''} {', '.join(str(p) for p in pages[:10])}"
            f"{', …' if len(pages) > 10 else ''}."
        ))
    if summary.issues:
        summary.warning = True
        first = summary.issues[0]["message"]
        more = len(summary.issues) - 1
        summary.message = first + (f" ({more} more in Details.)" if more else "")
    return summary
