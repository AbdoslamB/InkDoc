"""Tables on scanned pages ("Auto quality" Thorough: the scanned_tables step).

GLM-OCR reads a scanned page whole, and a table often comes out as plain
lines of its words, merged with the one beside it, or with its cells shifted.
This step finds each table on such a page with the layout model (an add-on,
run in the MarkItDown worker: app/core/engines/layout_model.py) and has
GLM-OCR read that area on its own ("Table Recognition:"). The reading takes
the place of what GLM-OCR wrote for that table, a table or a paragraph of its
words, only when all of these hold:

- it is a grid: at least two rows and two columns, every row as wide, at
  least half its cells filled;
- at least AGREE_SHARE of its words are in that place, so it is the same
  table read again, and it uses no script that place doesn't;
- the readings that take that place hold at least AGREE_SHARE of its words,
  so nothing GLM-OCR wrote there is lost;
- it differs from a table GLM-OCR wrote there.

Words, not whole cells, because the structure is what may differ: a stacked
header GLM-OCR shifted on the page comes back as "Revenue 2023" from the crop.
A paragraph is a run of non-blank lines that are not headings, comments,
code or table rows; nothing is ever added where GLM-OCR wrote other words.
A paragraph with dot leaders is a contents list, never a table, as for the
table finder on text pages.

GLM-OCR's output has no page boundaries, so a reading is matched to its place
by its words. Two tables side by side that GLM-OCR wrote as one are written
as two. Each table written says where it came from:
<!-- inkdoc:table page=N source=glm-ocr checked=layout,page-reading -->

Three steps, each taking its engine's lane in app/core/auto_engine.py:
find_scanned_tables (the layout model) -> read_scanned_tables (GLM-OCR) ->
write_scanned_tables (pure). A failure leaves the Markdown as it was; a
cancelled job raises ConversionCancelledError.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

from app.core.jobs import NULL_JOB, ConversionCancelledError
from app.core.markdown_tables import cell_key, find_table_blocks, line_tokens
from app.core.table_check import table_lines

logger = logging.getLogger(__name__)

AGREE_SHARE = 0.9           # as the table check's second readings (app/core/table_stage.py)
MIN_FILLED = 0.5
MAX_TABLES = 12             # read per conversion; TIME_LIMIT_S is what bounds a slow computer
TIME_LIMIT_S = 300.0
SOURCE = "glm_ocr"
CHECKED = "layout,page-reading"
ISSUE = "scanned"
TEXT_PAGE_MIN_CHARS = 25    # a page with fewer characters has no usable text layer (table_stage)

# detect(path, pages, job) -> the layout model's JSON (app/core/engines/layout_model.py)
Detector = Callable[[str, list[int], Any], dict]
# read(path, page, bbox) -> GLM-OCR's rows for that area, or None
Reader = Callable[[str, int, list[float]], "list[list[str]] | None"]


@dataclass
class Area:
    """One table the layout model found, and what became of it."""

    page: int
    bbox: list[float]
    rows: list[list[str]] | None = None
    written: bool = False
    note: str = ""              # why it was not written


@dataclass
class ScannedTables:
    """What find_scanned_tables found, carried to read_scanned_tables and write_scanned_tables."""

    path: str
    areas: list[Area] = field(default_factory=list)
    skipped: str = ""           # why nothing was looked for or read
    truncated: bool = False


def target_pages(probe: Any, read_by_glm: Collection[int]) -> list[int]:
    """The pages GLM-OCR read that have no usable text layer, 1-based."""
    text = {p.index + 1 for p in probe.pages if not p.image_only and p.char_count >= TEXT_PAGE_MIN_CHARS}
    return sorted(p for p in set(read_by_glm) if p not in text)


def find_scanned_tables(path: str, pages: list[int], detect: Detector, *, job: Any = None) -> ScannedTables:
    """Where the tables are on `pages`, by the layout model. Blocking."""
    job = job or NULL_JOB
    found = ScannedTables(path=path)
    job.raise_if_cancelled()
    job.set_phase("checking", "Finding tables on scanned pages…")
    try:
        result = detect(path, pages, job)
    except ConversionCancelledError:
        raise
    except Exception as exc:
        logger.warning("Finding tables on scanned pages failed: %s", exc)
        result = {"error": str(exc)}
    if result.get("error"):
        logger.warning("Layout model: %s", result["error"])
        found.skipped = "the layout model failed"
        return found
    found.truncated = bool(result.get("truncated"))
    found.areas = [
        Area(page=int(p["page"]), bbox=[float(v) for v in t["bbox"]])
        for p in result.get("pages", [])
        for t in p.get("tables", [])
    ]
    return found


def read_scanned_tables(
    found: ScannedTables,
    read: Reader,
    *,
    job: Any = None,
    limit: int = MAX_TABLES,
    time_limit_s: float = TIME_LIMIT_S,
) -> None:
    """GLM-OCR's reading of each area on its own (at most `limit`, within `time_limit_s`). Blocking."""
    job = job or NULL_JOB
    started = time.monotonic()
    total = min(len(found.areas), limit)
    for index, area in enumerate(found.areas):
        if index >= limit:
            area.note = "more tables than one conversion reads"
            continue
        job.raise_if_cancelled()
        if time.monotonic() - started > time_limit_s:
            area.note = "there was no time left to read it"
            continue
        job.set_phase("checking", f"Reading table {index + 1} of {total} with GLM-OCR…", engine="glm_ocr")
        try:
            area.rows = read(found.path, area.page, area.bbox)
        except ConversionCancelledError:
            raise
        except Exception as exc:
            logger.warning("Reading a table on page %d failed: %s", area.page, exc)
            area.note = "the reading failed"
            continue
        if area.rows is None:
            area.note = "GLM-OCR read no table there"


def is_grid(rows: list[list[str]] | None) -> bool:
    if not rows or len(rows) < 2:
        return False
    width = len(rows[0])
    if width < 2 or any(len(row) != width for row in rows):
        return False
    filled = sum(1 for row in rows for cell in row if cell.strip())
    return filled >= MIN_FILLED * width * len(rows)


def _words(rows: list[list[str]]) -> Counter[str]:
    return Counter(word for row in rows for cell in row for word in line_tokens(cell))


def _share(part: Counter[str], whole: Counter[str]) -> float:
    """How much of `part` is in `whole`, counting repeats."""
    total = sum(part.values())
    return sum(min(n, whole[k]) for k, n in part.items()) / total if total else 0.0


def scripts(texts: list[str]) -> set[str]:
    """The writing systems of the letters in `texts` ("LATIN", "CJK", "CYRILLIC", ...)."""
    return {unicodedata.name(ch, "?").split(" ")[0] for text in texts for ch in text if ch.isalpha()}


def _cells(rows: list[list[str]]) -> list[str]:
    return [cell for row in rows for cell in row]


def _same(a: list[list[str]], b: list[list[str]]) -> bool:
    return [[cell_key(c) for c in row] for row in a] == [[cell_key(c) for c in row] for row in b]


@dataclass
class _Place:
    """Where GLM-OCR may have written a table: a table, or a paragraph of plain lines."""

    start: int
    end: int
    texts: list[str]                        # its cells, or its lines
    rows: list[list[str]] | None = None     # None for a paragraph

    def __post_init__(self) -> None:
        self.words = Counter(word for text in self.texts for word in line_tokens(text))
        self.scripts = scripts(self.texts)


_NOT_TEXT = ("#", "<!--", "|", "<")  # headings, comments, table and HTML lines
_FENCES = ("```", "~~~")
_LEADER = re.compile(r"(?:\.\s?){3,}|…")    # "Introduction ..... 3": a contents line


def _code(lines: list[str]) -> set[int]:
    """The lines of fenced code blocks, fences included."""
    inside, found = False, set()
    for i, line in enumerate(lines):
        fence = line.lstrip().startswith(_FENCES)
        if fence or inside:
            found.add(i)
        if fence:
            inside = not inside
    return found


def _places(lines: list[str]) -> list[_Place]:
    places = [_Place(b.start, b.end, _cells(b.rows), b.rows) for b in find_table_blocks(lines)]
    taken = {i for p in places for i in range(p.start, p.end)} | _code(lines)

    def text(i: int) -> bool:
        return i not in taken and bool(lines[i].strip()) and not lines[i].lstrip().startswith(_NOT_TEXT)

    i = 0
    while i < len(lines):
        end = i
        while end < len(lines) and text(end):
            end += 1
        if end > i and not any(_LEADER.search(line) for line in lines[i:end]):
            places.append(_Place(i, end, lines[i:end]))
        i = max(end, i + 1)
    return places


def write_scanned_tables(markdown: str, found: ScannedTables) -> tuple[str, list[dict], list[str]]:
    """(Markdown, tables written per page, detail lines). Pure."""
    lines = markdown.split("\n")
    places = _places(lines)
    groups: dict[int, list[Area]] = {}
    for area in found.areas:
        if area.rows is None:
            continue
        if not is_grid(area.rows):
            area.note = "GLM-OCR's reading of it is not a full grid"
            continue
        words = _words(area.rows)
        share, best = max(((_share(words, p.words), i) for i, p in enumerate(places)), default=(0.0, -1))
        if share < AGREE_SHARE:
            area.note = "it doesn't match what GLM-OCR wrote for the page"
        elif not scripts(_cells(area.rows)) <= places[best].scripts:
            area.note = "GLM-OCR read it in another script on its own"
        else:
            groups.setdefault(best, []).append(area)

    replacements: list[tuple[int, int, list[str]]] = []
    for index, areas in groups.items():
        place = places[index]
        if len({a.page for a in areas}) > 1:
            note = "it matches a table on another page too"
        elif _share(place.words, sum((_words(a.rows) for a in areas), Counter())) < AGREE_SHARE:
            note = "writing it would leave out part of what GLM-OCR wrote there"
        elif place.rows is not None and len(areas) == 1 and _same(areas[0].rows, place.rows):
            note = "GLM-OCR's table was already right"
        else:
            new: list[str] = []
            for area in areas:
                table = {"rows": area.rows, "source": SOURCE, "checked": CHECKED}
                new += [*table_lines(table, area.page), ""]
                area.written = True
            replacements.append((place.start, place.end, new[:-1]))
            continue
        for area in areas:
            area.note = note
    for start, end, new in sorted(replacements, reverse=True):
        lines[start:end] = new

    written = Counter(a.page for a in found.areas if a.written)
    report = [{"page": page, "tables": n, "issue": ISSUE, "source": SOURCE} for page, n in sorted(written.items())]
    details = [
        f"Page {page}: rebuilt {n} table{'s' if n != 1 else ''} from GLM-OCR's reading of each "
        "table on its own (found by the layout model)."
        for page, n in sorted(written.items())
    ]
    left = sorted({(a.page, a.note) for a in found.areas if a.note})
    details += [f"Page {page}: a table was left as GLM-OCR wrote it: {note}." for page, note in left]
    if found.truncated:
        details.append("Some scanned pages were not looked at: the layout model reached its time limit.")
    return "\n".join(lines), report, details
