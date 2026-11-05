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
- in a paragraph, only the lines made entirely of the readings' words are
  replaced, and only when no other line sits between them: a note or a
  source line next to the table stays as it is. A table GLM-OCR wrote one
  row per paragraph (a scan read cell by cell, blank lines between rows)
  is such a paragraph too: paragraphs with only blank lines between them
  count as one place, after the single paragraphs. Two readings of the same
  scan spell words differently ("вариант" and "варіант"), so in such a run a
  word counts when one there is alike (SIMILAR_WORD), a line is the table's
  when most of its words are, and a number the two readings disagree on by
  one digit keeps the page's reading: nothing InkDoc had is traded for a guess;
- a table GLM-OCR wrote is replaced only when the readings hold at least
  AGREE_SHARE of its words (the rest are the same cells read slightly
  differently), and only when they differ from it.

Words, not whole cells, because the structure is what may differ: a stacked
header GLM-OCR shifted on the page comes back as "Revenue 2023" from the crop.
A reading that writes decimals with a point where the page's reading writes
them with a comma ("0.66" for "0,66") takes the page's comma first.

The pages can be an image file's frames (Auto, an image GLM-OCR read): the
layout model and GLM-OCR then read its pixels.

High detail. A table too dense for GLM-OCR to finish (its answer ran out of
room) is noted (`Area.too_large`), and Auto offers to read it again in high
detail when the computer can (glm_ocr_manager.high_detail_available): the
area whole, at about its own size, with a larger context. Such a reading is
mended before it is written: a row GLM-OCR shifted by a column, where it
dropped an empty cell, is moved back to where its values' kinds fit the
table's columns (`align_columns`). And as the page's own reading of a dense
table has few of its numbers, the reading is written when it holds what
GLM-OCR wrote there (AGREE_SHARE of the place's words, loosely): nothing is
lost, and every cell is still GLM-OCR's reading of the scan.
A paragraph is a run of non-blank lines that are not headings, comments,
code or table rows. A paragraph with dot leaders is a contents list, never a
table, as for the table finder on text pages.

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
CHECKED_HIGH = "layout,high-detail"   # written because it holds the page's reading (high detail)
ISSUE = "scanned"
TEXT_PAGE_MIN_CHARS = 25    # a page with fewer characters has no usable text layer (table_stage)
# Why the layout model skipped a page, as the detail line says it.
UNREAD = {"rotated": "the page is rotated", "error": "the layout model failed on it"}

# detect(path, pages, job) -> the layout model's JSON (app/core/engines/layout_model.py)
Detector = Callable[[str, list[int], Any], dict]
# read(path, page, bbox) -> GLM-OCR's rows for that area, or None; or a reading with them
# (glm_ocr_engine.TableReading: rows, too_large, seconds)
Reader = Callable[[str, int, list[float]], Any]


@dataclass
class Area:
    """One table the layout model found, and what became of it."""

    page: int
    bbox: list[float]
    rows: list[list[str]] | None = None
    written: bool = False
    note: str = ""              # why it was not written
    too_large: bool = False     # GLM-OCR's answer ran out of room: high detail may read it
    seconds: float = 0.0        # how long the reading took


@dataclass
class ScannedTables:
    """What find_scanned_tables found, carried to read_scanned_tables and write_scanned_tables."""

    path: str
    areas: list[Area] = field(default_factory=list)
    skipped: str = ""           # why nothing was looked for or read
    truncated: bool = False
    unread_pages: list[tuple[int, str]] = field(default_factory=list)  # (page, why) the model skipped
    detail: str = "normal"      # "high": read whole at full size (an offer the user took)


def target_pages(probe: Any, read_by_glm: Collection[int]) -> list[int]:
    """The pages GLM-OCR read that have no usable text layer (none, or an earlier OCR's), 1-based."""
    text = {
        p.index + 1 for p in probe.pages
        if not p.image_only and not getattr(p, "ocr_layer", False) and p.char_count >= TEXT_PAGE_MIN_CHARS
    }
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
        result = {"error": f"{type(exc).__name__}: {exc}"}
    if result.get("error"):
        # The type only at WARNING: the message may hold the user's folder names.
        logger.warning("The layout model failed (%s)", str(result["error"]).split(":", 1)[0])
        logger.debug("Layout model: %s", result["error"])
        found.skipped = "the layout model failed"
        return found
    found.truncated = bool(result.get("truncated"))
    pages = result.get("pages", [])
    found.areas = [Area(page=int(p["page"]), bbox=[float(v) for v in t["bbox"]]) for p in pages for t in p.get("tables", [])]
    found.unread_pages = [(int(p["page"]), p["skipped"]) for p in pages if p.get("skipped") in UNREAD]
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
            reading = read(found.path, area.page, area.bbox)
        except ConversionCancelledError:
            raise
        except Exception as exc:
            logger.warning("Reading a table on page %d failed: %s", area.page, exc)
            area.note = "the reading failed"
            continue
        if hasattr(reading, "rows"):
            area.rows = reading.rows
            area.too_large = bool(getattr(reading, "too_large", False))
            area.seconds = float(getattr(reading, "seconds", 0.0) or 0.0)
        else:
            area.rows = reading
        if area.too_large:
            area.note = (
                "it is too large for GLM-OCR to read in one go even in high detail" if found.detail == "high"
                else "it is too large for GLM-OCR to read in one go at normal detail"
            )
        elif area.rows is None:
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
    """The writing systems of the letters in `texts` ("LATIN", "CJK", "CYRILLIC", ...).

    NFKC first, so symbols that stand for letters (º, µ, the ohm and kelvin
    signs) count as those letters, as they do for the words.
    """
    return {
        unicodedata.name(ch, "?").split(" ")[0]
        for text in texts for ch in unicodedata.normalize("NFKC", text) if ch.isalpha()
    }


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
    run: bool = False                       # paragraphs with only blank lines between them

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

    paragraphs: list[_Place] = []
    i = 0
    while i < len(lines):
        end = i
        while end < len(lines) and text(end):
            end += 1
        if end > i and not any(_LEADER.search(line) for line in lines[i:end]):
            paragraphs.append(_Place(i, end, lines[i:end]))
        i = max(end, i + 1)
    # Runs of paragraphs with only blank lines between them: a table written one row per
    # paragraph. After the single paragraphs, so a table in one paragraph still matches there.
    runs: list[_Place] = []
    run: list[_Place] = []
    for paragraph in [*paragraphs, None]:
        if paragraph is not None and run and not any(lines[k].strip() for k in range(run[-1].end, paragraph.start)):
            run.append(paragraph)
            continue
        if len(run) > 1:
            runs.append(_Place(run[0].start, run[-1].end, [t for p in run for t in p.texts], run=True))
        run = [paragraph] if paragraph is not None else []
    return places + paragraphs + runs


def _own_lines(lines: list[str], place: _Place, words: Counter[str]) -> tuple[int, int] | None:
    """The lines of a paragraph made only of `words`, from the first to the last such line;
    None when there are none, or when a line with other words sits between them (blank lines
    between paragraphs are neither)."""
    def is_own(line: str) -> bool:
        tokens = line_tokens(line)
        if not place.run:
            return all(w in words for w in tokens)
        # A line with words is the table's by its words: a heading's numbers ("ABK-5 (2.10.6)")
        # can all be in a table's column numbers.
        lettered = [w for w in tokens if any(ch.isalpha() for ch in w)] or tokens
        if not lettered:
            return True  # a unit of one letter ("т"), as a paragraph's line with no words is
        held = sum(1 for w in lettered if w in words or any(_alike(w, k) for k in words))
        return held >= OWN_LINE_SHARE * len(lettered)

    own = [is_own(lines[i]) if lines[i].strip() else None for i in range(place.start, place.end)]
    if True not in own:
        return None
    first, last = own.index(True), len(own) - 1 - own[::-1].index(True)
    if False in own[first:last + 1]:
        return None
    return place.start + first, place.start + last + 1


SIMILAR_WORD = 0.6            # two readings of a word of WORD_MIN_LETTERS or more this alike are one word
WORD_MIN_LETTERS = 4
OWN_LINE_SHARE = 0.5           # in a run, a line with this share of the reading's words is the table's
# Latin letters a scan's reading writes for the Cyrillic ones they look like ("M3" for "м3").
_LOOKALIKE = str.maketrans("abcehikmoptxy", "авсенікмортху")


def _alike(a: str, b: str) -> bool:
    import difflib

    if a.translate(_LOOKALIKE) == b.translate(_LOOKALIKE):
        return True
    return (
        a.isalpha() and b.isalpha() and min(len(a), len(b)) >= WORD_MIN_LETTERS
        and difflib.SequenceMatcher(None, a, b).ratio() >= SIMILAR_WORD
    )


def _loose_share(part: Counter[str], whole: Counter[str]) -> float:
    """_share, where a word left over counts when `whole` has one alike left over (numbers: exactly)."""
    total = sum(part.values())
    if not total:
        return 0.0
    left = Counter({k: n - min(n, part.get(k, 0)) for k, n in whole.items()})
    held = sum(min(n, whole[k]) for k, n in part.items())
    for word, n in part.items():
        for _ in range(n - min(n, whole[word])):
            other = next((k for k, m in left.items() if m > 0 and _alike(word, k)), None)
            if other is None:
                break
            left[other] -= 1
            held += 1
    return held / total


def _share_in(place: _Place, words: Counter[str]) -> float:
    return _loose_share(words, place.words) if place.run else _share(words, place.words)


_NUMBER = re.compile(r"\d[\d,.x×]*\d|\d")


def page_numbers(rows: list[list[str]], page_text: str) -> list[list[str]]:
    """A reading's numbers the page's reading has one digit apart, as the page has them.

    Only when exactly one number of the page's is that close: two readings of a
    scan that disagree keep the one InkDoc already wrote.
    """
    known = Counter(_NUMBER.findall(page_text))

    def mend(match: re.Match[str]) -> str:
        number = match.group(0)
        if known[number]:
            return number
        close = {
            k for k in known
            if len(k) == len(number)
            and sum(1 for a, b in zip(k, number, strict=True) if a != b) == 1
            and all(a == b or (a.isdigit() and b.isdigit()) for a, b in zip(k, number, strict=True))
        }
        return close.pop() if len(close) == 1 else number

    return [[_NUMBER.sub(mend, cell) for cell in row] for row in rows]


def _match(area: Area, places: list[_Place], groups: dict[int, list[Area]]) -> int | None:
    """The place holding the area's words: the best share, the earliest on a tie, never one
    another page's reading has taken. None when no place holds AGREE_SHARE of them.
    A run of paragraphs only when no single table or paragraph does."""
    words = _words(area.rows or [])
    for runs in (False, True):
        ranked = sorted((-_share_in(p, words), i) for i, p in enumerate(places) if p.run == runs)
        for negative_share, index in ranked:
            if -negative_share < AGREE_SHARE:
                break
            if all(a.page == area.page for a in groups.get(index, [])):
                return index
    return None


def _held_lines(lines: list[str], place: _Place, areas: list[Area]) -> tuple[int, int] | str:
    """High detail: the lines of the place the readings hold, as one stretch."""
    words = sum((_words(a.rows or []) for a in areas), Counter())

    def held(line: str) -> bool:
        tokens = [w for w in line_tokens(line) if any(ch.isalpha() for ch in w)] or line_tokens(line)
        hit = sum(1 for w in tokens if w in words or any(_alike(w, k) for k in words))
        return not tokens or hit >= OWN_LINE_SHARE * len(tokens)

    own = [held(lines[i]) if lines[i].strip() else None for i in range(place.start, place.end)]
    if True not in own:
        return "writing it would leave out part of what GLM-OCR wrote there"
    first, last = own.index(True), len(own) - 1 - own[::-1].index(True)
    if False in own[first:last + 1]:
        return "writing it would leave out part of what GLM-OCR wrote there"
    return place.start + first, place.start + last + 1


def high_detail_pages(found: ScannedTables) -> list[int]:
    """The pages with a table too large to read at normal detail (what Auto may offer)."""
    return sorted({a.page for a in found.areas if a.too_large})


def high_detail_seconds(found: ScannedTables) -> float:
    """About how long reading those tables in high detail takes: 2.5 times the normal attempt each."""
    return sum(max(60.0, 2.5 * a.seconds) for a in found.areas if a.too_large)


def _replacement(lines: list[str], place: _Place, areas: list[Area]) -> tuple[int, int] | str:
    """The lines the areas' readings take the place of, or why they don't."""
    words = sum((_words(a.rows or []) for a in areas), Counter())
    if place.rows is None:
        span = _own_lines(lines, place, words)
        if span is None:
            return "writing it would leave out part of what GLM-OCR wrote there"
        start, end = span
        there = Counter(w for line in lines[start:end] for w in line_tokens(line))
        if (_loose_share if place.run else _share)(words, there) < AGREE_SHARE:
            return "it doesn't match what GLM-OCR wrote for the page"
        return start, end
    if _share(place.words, words) < AGREE_SHARE:
        return "writing it would leave out part of what GLM-OCR wrote there"
    if len(areas) == 1 and _same(areas[0].rows or [], place.rows):
        return "GLM-OCR's table was already right"
    return place.start, place.end


_COMMA_DECIMAL = re.compile(r"(?<![\d.,])\d+,\d+(?![\d.,])")
_POINT_DECIMAL = re.compile(r"(?<![\d.,])(\d+)\.(\d+)(?![\d.,])")
DECIMAL_STYLE_RATIO = 5        # the page writes comma decimals this many times more often than point ones


def decimal_marks(rows: list[list[str]], page_text: str) -> list[list[str]]:
    """A reading's "0.66" as "0,66" when the page's reading writes its decimals with a comma."""
    commas = len(_COMMA_DECIMAL.findall(page_text))
    points = len(_POINT_DECIMAL.findall(page_text))
    if commas < 3 or commas < DECIMAL_STYLE_RATIO * points:
        return rows
    return [[_POINT_DECIMAL.sub(r"\1,\2", cell) for cell in row] for row in rows]


# ─── High detail: rows shifted by a column ───────────────────────────────────
_KIND_NUMBER = re.compile(r"^[-−+]?\d[\d\s.,/x×]*%?$")
_KIND_CODE = re.compile(r"^[A-ZÄÖÜ]{1,2}$")
_KIND_MARK = re.compile(r"^[+○●◯·∙•*×✓✗-]$|^\$\\cdot\$$")
COLUMN_KIND_SHARE = 0.6        # a column is of a kind when this share of its filled cells are
SHIFT_MAX = 2


def _kind(cell: str) -> str:
    text = cell.strip()
    if not text:
        return ""
    if _KIND_MARK.match(text):
        return "mark"
    if _KIND_NUMBER.match(text):
        return "number"
    if _KIND_CODE.match(text):
        return "code"
    return "text"


def align_columns(rows: list[list[str]]) -> tuple[list[list[str]], int]:
    """(rows, rows moved): a body row whose values sit a column or two off is moved back.

    Each column's kind (numbers, one-letter codes, marks such as "+") is what most of
    its cells are. A row moves only when the move puts strictly more of its values in
    a column of their kind, and it only moves across empty cells, so no value is lost.
    """
    if len(rows) < 4 or len({len(r) for r in rows}) != 1:
        return rows, 0
    width = len(rows[0])
    body = rows[1:]
    kinds: list[str] = []
    for c in range(width):
        seen = Counter(_kind(r[c]) for r in body if _kind(r[c]))
        kind, n = seen.most_common(1)[0] if seen else ("", 0)
        kinds.append(kind if n >= COLUMN_KIND_SHARE * max(1, sum(seen.values())) else "")

    def fit(row: list[str]) -> int:
        return sum(1 for c, cell in enumerate(row) if cell.strip() and kinds[c] and _kind(cell) == kinds[c])

    def shifted(row: list[str], at: int, by: int) -> list[str] | None:
        """The row with its cells from column `at` moved `by` columns (right > 0), across empty cells only."""
        if by > 0:
            if any(cell.strip() for cell in row[width - by:]):
                return None
            return row[:at] + [""] * by + row[at:width - by]
        if any(cell.strip() for cell in row[at:at - by]):
            return None
        return row[:at] + row[at - by:] + [""] * (-by)

    def merged(row: list[str], at: int) -> list[str] | None:
        """Text GLM-OCR split into the next column (a long name's second line) joined back to its cell."""
        if at < 1 or kinds[at - 1] != "text" or _kind(row[at]) != "text" or kinds[at] == "text":
            return None
        return row[:at - 1] + [f"{row[at - 1]} {row[at]}".strip()] + row[at + 1:] + [""]

    out = [rows[0]]
    moved = 0
    for row in body:
        best, best_fit = row, fit(row)
        for at in range(width):
            candidates = [shifted(row, at, by) for by in [b for k in range(1, SHIFT_MAX + 1) for b in (k, -k)]]
            candidates.append(merged(row, at))
            for candidate in candidates:
                if candidate is not None and fit(candidate) > best_fit:
                    best, best_fit = candidate, fit(candidate)
        moved += best is not row
        out.append(best)
    # Columns left empty from top to bottom (where shifted values were) are not columns.
    keep = [c for c in range(width) if any(r[c].strip() for r in out)]
    if len(keep) < width:
        out = [[r[c] for c in keep] for r in out]
    return out, moved


def _holding(area: Area, places: list[_Place], groups: dict[int, list[Area]]) -> int | None:
    """High detail: the place whose words the reading holds (nothing GLM-OCR wrote there is lost)."""
    words = _words(area.rows or [])
    ranked = sorted(
        (-_loose_share(p.words, words), i) for i, p in enumerate(places)
        if p.rows is None and sum(p.words.values()) >= MIN_HELD_WORDS
    )
    for negative_share, index in ranked:
        if -negative_share < AGREE_SHARE:
            return None
        if all(a.page == area.page for a in groups.get(index, [])):
            return index
    return None


MIN_HELD_WORDS = 20            # a place this small is a caption, not where a dense table was written


def write_scanned_tables(markdown: str, found: ScannedTables) -> tuple[str, list[dict], list[str]]:
    """(Markdown, tables written per page, detail lines). Pure."""
    lines = markdown.split("\n")
    high = found.detail == "high"
    for area in found.areas:
        if area.rows is not None:
            area.rows = decimal_marks(area.rows, markdown)
            if high:
                area.rows, _moved = align_columns(area.rows)
    readings = [a for a in found.areas if a.rows is not None]
    places = _places(lines) if readings else []
    groups: dict[int, list[Area]] = {}
    holding: set[int] = set()      # places taken by holding (high detail), not by matching
    for area in readings:
        if not is_grid(area.rows):
            area.note = "GLM-OCR's reading of it is not a full grid"
            continue
        index = _match(area, places, groups)
        held = False
        if index is None and high:
            index = _holding(area, places, groups)
            held = index is not None
        if index is None:
            area.note = "it doesn't match what GLM-OCR wrote for the page"
        elif not scripts(_cells(area.rows)) <= places[index].scripts:
            area.note = "GLM-OCR read it in another script on its own"
        else:
            groups.setdefault(index, []).append(area)
            if held:
                holding.add(index)

    replacements: list[tuple[int, int, list[str]]] = []
    for index, areas in groups.items():
        if places[index].run:
            text = "\n".join(places[index].texts)
            for area in areas:
                area.rows = page_numbers(area.rows or [], text)
        target = (
            _held_lines(lines, places[index], areas) if index in holding else _replacement(lines, places[index], areas)
        )
        if isinstance(target, str):
            for area in areas:
                area.note = target
            continue
        new: list[str] = []
        for area in areas:
            table = {"rows": area.rows, "source": SOURCE, "checked": CHECKED_HIGH if index in holding else CHECKED}
            new += [*table_lines(table, area.page), ""]
            area.written = True
        replacements.append((*target, new[:-1]))
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
    details += [f"Page {page}: not looked at for tables: {UNREAD[why]}." for page, why in found.unread_pages]
    if found.truncated:
        details.append("Some scanned pages were not looked at: the layout model reached its page or time limit.")
    return "\n".join(lines), report, details
