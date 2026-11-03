"""Find a PDF's tables from the PDF itself: ruling lines, cells and text layer.

Runs in the MarkItDown worker (app/core/engines/markitdown_worker.py), which
already has pdfplumber loaded, so InkDoc's own process never imports it. The
worker writes the result as JSON; app/core/table_check.py compares it with an
engine's Markdown and app/core/table_stage.py decides what to do.

For each requested page:

- tables drawn with ruling lines (pdfplumber's line strategy): every cell's
  box, its text from the text layer, and merged cells, turned into InkDoc's
  Markdown grid (app/core/markdown_tables.py: merged cells repeated, stacked
  headers flattened);
- vector shapes and images inside a cell, named by their fill colour
  ("[green shape]", "[3 blue shapes]", "[image]"): what is drawn, never a
  guess at what it means;
- a check that the grid accounts for every word of the text layer inside the
  table ("verified"); a grid that misses text is reported, never used;
- borderless tables an engine merged: given the engine's own wide table as a
  hint, its columns are split where the PDF shows a gutter between them
  (split_merged_table), keeping the engine's cell text;
- tables side by side grouped into one band, read left to right, with the
  text lines just above and below the band as anchors for finding it in the
  engine's Markdown.

Never raises for a bad page: that page is reported as skipped.
"""
from __future__ import annotations

import colorsys
import logging
import re
import time
from collections.abc import Iterable
from typing import Any

from app.core.markdown_tables import GridCell, clean_cell_text, to_markdown_grid
from app.core.thorough_passes import (
    ALL_DETECTORS,
    DETECT_BORDERLESS,
    DETECT_RATING_GRIDS,
    DETECT_SEGMENTS,
    TABLE_KIND_BORDERLESS,
    TABLE_KIND_HINT_SPLIT,
    TABLE_KIND_LINES,
    TABLE_KIND_RATING,
)

logger = logging.getLogger(__name__)

DEFAULT_TIME_BUDGET_S = 20.0
MAX_PAGES = 60
# A page drawn with more than this many edges (4 per box, 1 per line or curve), or
# with more characters, is skipped as "too_complex": pdfplumber's table search
# compares every vertical edge with every horizontal one, so a crafted page could
# hold a conversion for hours. A 60x15 grid of boxed cells is about 3,600 edges.
MAX_PAGE_EDGES = 10_000
MAX_PAGE_CHARS = 60_000
LINE_TOLERANCE_PT = 3.0        # words whose tops differ by less are on one line
GUTTER_RATIO = 1.8             # a gap this many times the typical column gap...
GUTTER_MIN_PT = 20.0           # ...and at least this wide separates two tables
SIDE_PURITY = 0.9              # share of a column's words that must sit on one side
# Borderless tables found from word positions (Thorough only; app/core/table_stage.py).
COL_GAP_MIN_PT = 7.0           # a gap between words at least this wide...
COL_GAP_HEIGHT_RATIO = 0.8     # ...and this share of the line's height starts a new cell
WORD_TABLE_MIN_ROWS = 3        # header and two rows
WORD_TABLE_MIN_COLS = 3        # two columns of text are too often just a layout
WORD_TABLE_MAX_MEDIAN_CHARS = 25  # cells are short; prose is not
LINE_SPACING_RATIO = 2.2       # a gap taller than this many lines ends a table
ROW_MIN_FILL = 0.5             # a line filling fewer columns continues the row above
SEGMENT_GAP_MIN_PT = 3.0       # text segments (page_segments): a gap this wide...
SEGMENT_GAP_HEIGHT_RATIO = 0.45  # ...and this share of the line's height splits them
SNAP_PT = 2.0                  # cell edges closer than this are one grid line
WORD_TOLERANCE_PT = 1.0        # a word's centre may sit this far outside its cell
MAX_EMPTY_FRACTION = 0.6       # a "table" mostly empty is a layout grid or a form
MIN_TEXT_CELL_SHARE = 0.3      # fewer cells with text than this: a chart's grid, not a table
SHAPE_ONLY = re.compile(r"\[(?:\d+ )?(?:[a-z]+ )?(?:shapes?|images?)(?::[^\]]*)?\]")
SHAPE_MIN_PT = 1.5             # thinner than this is a line, not a shape
SHAPE_MAX_CELL_FRACTION = 0.7  # larger than this is the cell's background colour
BAND_OVERLAP = 0.3             # tables overlapping this much vertically sit side by side


# ─── Colours ─────────────────────────────────────────────────────────────────
def color_name(color: Any) -> str:
    """A plain colour name for a PDF fill colour (gray, RGB or CMYK); "" if unknown."""
    if isinstance(color, (int, float)):
        color = (color,)
    if not isinstance(color, (tuple, list)) or not color:
        return ""
    try:
        values = [max(0.0, min(1.0, float(v))) for v in color]
    except (TypeError, ValueError):
        return ""
    if len(values) == 1:
        r = g = b = values[0]
    elif len(values) == 3:
        r, g, b = values
    elif len(values) == 4:
        c, m, y, k = values
        r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
    else:
        return ""
    hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
    if val < 0.2:
        return "black"
    if sat < 0.18:
        return "white" if val > 0.92 else "grey"
    deg = hue * 360
    if deg < 15 or deg >= 345:
        name = "red"
    elif deg < 40:
        name = "brown" if val < 0.6 else "orange"
    elif deg < 70:
        name = "yellow"
    elif deg < 170:
        name = "green"
    elif deg < 200:
        name = "cyan"
    elif deg < 260:
        name = "blue"
    elif deg < 300:
        name = "purple"
    else:
        name = "pink"
    return name


def color_key(color: Any) -> str:
    """The exact colour as "#rrggbb" (gray and CMYK converted), for telling apart two blues; "" if unknown."""
    if isinstance(color, (int, float)):
        color = (color,)
    if not isinstance(color, (tuple, list)) or not color:
        return ""
    try:
        values = [max(0.0, min(1.0, float(v))) for v in color]
    except (TypeError, ValueError):
        return ""
    if len(values) == 1:
        rgb = values * 3
    elif len(values) == 3:
        rgb = values
    elif len(values) == 4:
        c, m, y, k = values
        rgb = [(1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)]
    else:
        return ""
    return "#" + "".join(f"{round(v * 255):02x}" for v in rgb)


def shape_label(colors: list[str], images: int = 0) -> str:
    """"[green shape]", "[3 blue shapes]", "[2 shapes: red, green]", "[image]"; "" for nothing."""
    parts: list[str] = []
    if colors:
        distinct = list(dict.fromkeys(c or "" for c in colors))
        n = len(colors)
        if len(distinct) == 1:
            name = f"{distinct[0]} " if distinct[0] else ""
            parts.append(f"[{name}shape]" if n == 1 else f"[{n} {name}shapes]")
        else:
            named = ", ".join(c for c in distinct if c)
            parts.append(f"[{n} shapes: {named}]" if named else f"[{n} shapes]")
    if images:
        parts.append("[image]" if images == 1 else f"[{images} images]")
    return " ".join(parts)


# ─── Geometry helpers ────────────────────────────────────────────────────────
def _snap(values: list[float]) -> list[float]:
    out: list[float] = []
    for v in sorted(values):
        if not out or v - out[-1] > SNAP_PT:
            out.append(v)
    return out


def _index(lines: list[float], value: float) -> int:
    return min(range(len(lines)), key=lambda i: abs(lines[i] - value))


def _inside(box: tuple[float, float, float, float], x: float, y: float, tol: float = 0.0) -> bool:
    x0, top, x1, bottom = box
    return x0 - tol <= x <= x1 + tol and top - tol <= y <= bottom + tol


def _tokens(texts: list[str]) -> list[str]:
    from app.core.quality_check import normalize_text, tokenize

    return tokenize(normalize_text(" ".join(texts)))


# ─── One table ───────────────────────────────────────────────────────────────
def _shapes_in(page: Any, box: tuple[float, float, float, float]) -> tuple[list[dict], list[dict]]:
    """Filled vector shapes and images whose box lies inside `box`."""
    x0, top, x1, bottom = box
    shapes = []
    for obj in list(getattr(page, "rects", []) or []) + list(getattr(page, "curves", []) or []):
        if not obj.get("fill"):
            continue
        ox0, otop, ox1, obottom = obj["x0"], obj["top"], obj["x1"], obj["bottom"]
        inside = ox0 >= x0 - 0.5 and ox1 <= x1 + 0.5 and otop >= top - 0.5 and obottom <= bottom + 0.5
        if inside and min(ox1 - ox0, obottom - otop) >= SHAPE_MIN_PT:
            shapes.append(obj)
    images = [
        img for img in (getattr(page, "images", []) or [])
        if img["x0"] >= x0 - 0.5 and img["x1"] <= x1 + 0.5 and img["top"] >= top - 0.5 and img["bottom"] <= bottom + 0.5
    ]
    return shapes, images


def _cell_label(cell_box: tuple[float, float, float, float], shapes: list[dict], images: list[dict]) -> str:
    cx0, ctop, cx1, cbottom = cell_box
    area = max(1.0, (cx1 - cx0) * (cbottom - ctop))
    colors: list[str] = []
    n_images = 0
    for obj in shapes:
        ox, oy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
        if not _inside(cell_box, ox, oy):
            continue
        if (obj["x1"] - obj["x0"]) * (obj["bottom"] - obj["top"]) > SHAPE_MAX_CELL_FRACTION * area:
            continue  # the cell's background colour
        colors.append(color_name(obj.get("non_stroking_color")))
    for img in images:
        if _inside(cell_box, (img["x0"] + img["x1"]) / 2, (img["top"] + img["bottom"]) / 2):
            n_images += 1
    return shape_label(colors, n_images)


def _table_dict(page: Any, table: Any) -> dict | None:
    """One pdfplumber table as InkDoc's grid, or None when it is not a real table."""
    texts = table.extract()
    boxes = [cell for row in table.rows for cell in row.cells if cell]
    if not boxes:
        return None
    xs = _snap([b[0] for b in boxes] + [b[2] for b in boxes])
    ys = _snap([b[1] for b in boxes] + [b[3] for b in boxes])
    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    if n_rows < 2 or n_cols < 2:
        return None
    bbox = tuple(float(v) for v in table.bbox)
    shapes, images = _shapes_in(page, bbox)  # type: ignore[arg-type]

    cells: list[GridCell] = []
    cell_boxes: list[tuple[float, float, float, float]] = []
    merged = shaped = empty = 0
    for i, row in enumerate(table.rows):
        for j, box in enumerate(row.cells):
            if box is None:
                continue
            box = tuple(float(v) for v in box)
            text = clean_cell_text((texts[i][j] if i < len(texts) and j < len(texts[i]) else "") or "")
            label = _cell_label(box, shapes, images)  # type: ignore[arg-type]
            if label:
                shaped += 1
                text = f"{text} {label}".strip()
            if not text:
                empty += 1
            r0, r1 = _index(ys, box[1]), _index(ys, box[3])
            c0, c1 = _index(xs, box[0]), _index(xs, box[2])
            if r1 - r0 > 1 or c1 - c0 > 1:
                merged += 1
            cells.append(GridCell(r0, c0, max(1, r1 - r0), max(1, c1 - c0), text))
            cell_boxes.append(box)  # type: ignore[arg-type]

    captions, rows = to_markdown_grid(cells, n_rows, n_cols)
    if len(rows) < 2:
        return None
    # A chart's gridlines and bars make a "table" of empty cells and shapes.
    texts = sum(1 for c in cells if SHAPE_ONLY.sub("", c.text).strip())
    if texts < MIN_TEXT_CELL_SHARE * len(cells):
        return None

    # Verified: every word of the text layer inside the table lies in a cell.
    reason = ""
    try:
        x0, top, x1, bottom = bbox
        clip = (max(0.0, x0), max(0.0, top), min(float(page.width), x1), min(float(page.height), bottom))
        words = page.within_bbox(clip).extract_words()
    except Exception as exc:  # odd page boxes: report rather than guess
        words = []
        reason = f"the table's text could not be read ({type(exc).__name__})"
    stray = [
        w for w in words
        if not any(_inside(b, (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2, WORD_TOLERANCE_PT)
                   for b in cell_boxes)
    ]
    if stray and not reason:
        reason = "some of the table's text is outside its cells"
    if not reason and cells and empty / len(cells) > MAX_EMPTY_FRACTION:
        reason = "most cells are empty"
    return {
        "bbox": [round(v, 2) for v in bbox],
        "captions": captions,
        "rows": rows,
        "source": "pdf_lines",
        "kind": TABLE_KIND_LINES,
        "verified": not reason,
        "reason": reason,
        "merged_cells": merged,
        "shape_cells": shaped,
        "words": _tokens([w["text"] for w in words]),
    }


# ─── Borderless tables an engine merged ──────────────────────────────────────
def _word_lines(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(word["top"] - lines[-1][0]["top"]) <= LINE_TOLERANCE_PT:
            lines[-1].append(word)
        else:
            lines.append([word])
    return lines


def _token_of(word: dict) -> list[str]:
    return _tokens([word["text"]])


def split_merged_table(words: list[dict], rows: list[list[str]], avoid: list[tuple]) -> list[dict]:
    """Split an engine's table at a gutter the PDF shows, or [] when it is one table.

    `rows` is the engine's table (header first); `words` the page's words with
    positions. The table's lines are found on the page by their words; a gap
    between columns much wider than the others is a gutter between two
    tables. The engine's columns are then split where their words fall on
    either side of it. Only a clean split is returned: every column on one
    side, each side at least two columns. The cells keep the engine's text.
    """
    from app.core.markdown_tables import line_tokens

    n_cols = max((len(r) for r in rows), default=0)
    if n_cols < 4:
        return []
    rows = [list(r) + [""] * (n_cols - len(r)) for r in rows]
    vocabulary = {t for row in rows for cell in row for t in line_tokens(cell)}
    if not vocabulary:
        return []

    # The table's lines: the longest run of page lines made only of its words.
    lines = _word_lines(words)
    best: list[list[dict]] = []
    run: list[list[dict]] = []
    for line in lines:
        toks = [t for w in line for t in _token_of(w)]
        if toks and all(t in vocabulary for t in toks):
            run.append(line)
            if len(run) > len(best):
                best = list(run)
        else:
            run = []
    region = [w for line in best for w in line]
    covered = {t for w in region for t in _token_of(w)}
    if len(covered) < 0.8 * len(vocabulary):
        return []
    x0 = min(w["x0"] for w in region)
    x1 = max(w["x1"] for w in region)
    top = min(w["top"] for w in region)
    bottom = max(w["bottom"] for w in region)
    for box in avoid:
        if box[0] < x1 and box[2] > x0 and box[1] < bottom and box[3] > top:
            return []  # a ruled table is there: the line strategy has it

    # Gaps between the horizontal extents the words cover.
    spans: list[list[float]] = []
    for w in sorted(region, key=lambda w: w["x0"]):
        if spans and w["x0"] <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], w["x1"])
        else:
            spans.append([w["x0"], w["x1"]])
    gaps = [(spans[i][1], spans[i + 1][0]) for i in range(len(spans) - 1)]
    if len(gaps) < 3:
        return []
    widths = sorted(b - a for a, b in gaps)
    typical = widths[len(widths) // 2]
    gutters = [(a, b) for a, b in gaps if b - a >= max(GUTTER_MIN_PT, GUTTER_RATIO * typical)]
    if not gutters:
        return []
    cuts = sorted((a + b) / 2 for a, b in gutters)

    def side_of(x: float) -> int:
        return sum(1 for c in cuts if x > c)

    # Which side each engine column's words are on. When the engine's rows and
    # the page's lines pair up one to one, a cell is looked up in its own line,
    # so a number that occurs on both sides can't vote for the wrong one.
    def positions_in(line_words: list[dict]) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        for w in line_words:
            for t in _token_of(w):
                found.setdefault(t, []).append((w["x0"] + w["x1"]) / 2)
        return found

    filled = [row for row in rows if any(cell.strip() for cell in row)]
    if len(filled) == len(best):
        per_row = [positions_in(line) for line in best]
    else:
        everywhere = positions_in(region)
        per_row = [everywhere] * len(filled)
    sides: list[int] = []
    for c in range(n_cols):
        votes = [
            side_of(x)
            for row, where in zip(filled, per_row, strict=True)
            for t in line_tokens(row[c])
            for x in where.get(t, [])
        ]
        if not votes:
            return []
        winner = max(set(votes), key=votes.count)
        if votes.count(winner) < SIDE_PURITY * len(votes):
            return []
        sides.append(winner)
    if sides != sorted(sides) or len(set(sides)) < 2:
        return []

    tables: list[dict] = []
    for side in sorted(set(sides)):
        cols = [c for c in range(n_cols) if sides[c] == side]
        if len(cols) < 2:
            return []
        part = [[row[c] for c in cols] for row in rows]
        part = [row for row in part if any(cell.strip() for cell in row)]
        if len(part) < 2:
            return []
        side_words = [w for w in region if side_of((w["x0"] + w["x1"]) / 2) == side]
        tables.append({
            "bbox": [
                round(min(w["x0"] for w in side_words), 2), round(min(w["top"] for w in side_words), 2),
                round(max(w["x1"] for w in side_words), 2), round(max(w["bottom"] for w in side_words), 2),
            ],
            "captions": [],
            "rows": part,
            "source": "pdf_words",
            "kind": TABLE_KIND_HINT_SPLIT,
            "verified": True,
            "reason": "",
            "merged_cells": 0,
            "shape_cells": 0,
            "words": _tokens([w["text"] for w in side_words]),
        })
    return tables


# ─── Borderless tables from word positions ───────────────────────────────────
def _chunks(line: list[dict], min_gap: float = COL_GAP_MIN_PT, ratio: float = COL_GAP_HEIGHT_RATIO) -> list[dict]:
    """A line's words grouped into cells: a wide gap starts a new one."""
    if not line:
        return []
    heights = sorted(w["bottom"] - w["top"] for w in line)
    gap_min = max(min_gap, ratio * heights[len(heights) // 2])
    chunks: list[dict] = []
    for w in sorted(line, key=lambda w: w["x0"]):
        if chunks and w["x0"] - chunks[-1]["x1"] < gap_min:
            chunk = chunks[-1]
            chunk["x1"] = max(chunk["x1"], w["x1"])
            chunk["bottom"] = max(chunk["bottom"], w["bottom"])
            chunk["words"].append(w)
        else:
            chunks.append({"x0": w["x0"], "x1": w["x1"], "top": w["top"], "bottom": w["bottom"], "words": [w]})
    for chunk in chunks:
        chunk["text"] = " ".join(w["text"] for w in chunk["words"])
    return chunks


def _column_bands(chunks: list[dict]) -> list[list[float]]:
    bands: list[list[float]] = []
    for c in sorted(chunks, key=lambda c: c["x0"]):
        if bands and c["x0"] <= bands[-1][1] + 1.0:
            bands[-1][1] = max(bands[-1][1], c["x1"])
        else:
            bands.append([c["x0"], c["x1"]])
    return bands


def _grid_from_run(run: list[list[dict]]) -> tuple[list[list[str]], list[list[float]]] | None:
    """Rows of cell text (header first) and the column bands, or None if not a table."""
    chunks = [c for line in run for c in line]
    bands = _column_bands(chunks)
    if len(bands) < WORD_TABLE_MIN_COLS:
        return None
    texts = [c["text"] for c in chunks]
    lengths = sorted(len(t) for t in texts)
    if lengths[len(lengths) // 2] > WORD_TABLE_MAX_MEDIAN_CHARS or any("...." in t for t in texts):
        return None  # prose or a table of contents
    rows: list[list[str]] = []
    for line in run:
        cells = [""] * len(bands)
        for c in line:
            col = next(i for i, b in enumerate(bands) if b[0] - 1.0 <= c["x0"] and c["x1"] <= b[1] + 1.0)
            cells[col] = f"{cells[col]} {c['text']}".strip()
        filled = sum(1 for cell in cells if cell)
        if rows and filled < ROW_MIN_FILL * len(bands):
            # A wrapped cell: its text continues the row above.
            rows[-1] = [f"{a} {b}".strip() for a, b in zip(rows[-1], cells, strict=True)]
        else:
            rows.append(cells)
    if len(rows) < WORD_TABLE_MIN_ROWS or sum(1 for c in rows[0] if c) < 2 / 3 * len(bands):
        return None
    return rows, bands


def find_word_tables(words: list[dict], avoid: list[tuple]) -> list[dict]:
    """Borderless tables from the page's word positions, never trusted on their own.

    Lines whose words fall into three or more aligned columns, three or more
    lines in a row, make a table; a wide gutter between its columns makes it
    two tables side by side. Each comes back verified=False with confirm=True:
    InkDoc writes it only when a second, independent reading of the same area
    (GLM-OCR) has the same shape (app/core/table_stage.py).
    """
    words = [
        w for w in words
        if not any(_inside(box, (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2) for box in avoid)
    ]
    lines = [_chunks(line) for line in _word_lines(words)]
    runs: list[list[list[dict]]] = []
    run: list[list[dict]] = []
    for line in lines:
        height = max(c["bottom"] for c in line) - min(c["top"] for c in line)
        if run:
            prev_bottom = max(c["bottom"] for c in run[-1])
            if min(c["top"] for c in line) - prev_bottom > LINE_SPACING_RATIO * max(height, 1.0):
                runs.append(run)
                run = []
        if len(line) >= 2 or (run and len(line) == 1):
            run.append(line)
        else:
            runs.append(run)
            run = []
    runs.append(run)

    tables: list[dict] = []
    for run in runs:
        while run and len(run[-1]) < 2:
            run = run[:-1]  # a trailing one-cell line is text after the table
        if len(run) < WORD_TABLE_MIN_ROWS:
            continue
        grid = _grid_from_run(run)
        if grid is None:
            continue
        rows, bands = grid
        # Side by side: a gap between columns much wider than the others.
        gaps = [bands[i + 1][0] - bands[i][1] for i in range(len(bands) - 1)]
        typical = sorted(gaps)[len(gaps) // 2]
        cuts = [
            i + 1 for i, g in enumerate(gaps)
            if len(gaps) >= 3 and g >= max(GUTTER_MIN_PT, GUTTER_RATIO * typical)
        ]
        edges = [0, *cuts, len(bands)]
        run_words = [w for line in run for c in line for w in c["words"]]
        for a, b in zip(edges, edges[1:], strict=False):
            if b - a < 2:
                continue  # a lone column beside a table: not a table of its own
            part = [row[a:b] for row in rows]
            part = [row for row in part if any(row)]
            x0, x1 = bands[a][0], bands[b - 1][1]
            side = [w for w in run_words if x0 - 1.0 <= w["x0"] and w["x1"] <= x1 + 1.0]
            if len(part) < 2 or not side:
                continue
            tables.append({
                "bbox": [
                    round(x0, 2), round(min(w["top"] for w in side), 2),
                    round(x1, 2), round(max(w["bottom"] for w in side), 2),
                ],
                "captions": [],
                "rows": part,
                "source": "pdf_words",
                "kind": TABLE_KIND_BORDERLESS,
                "verified": False,
                "confirm": True,
                "reason": "a table without lines is used only when a second reading agrees",
                "merged_cells": 0,
                "shape_cells": 0,
                "words": _tokens([w["text"] for w in side]),
            })
    return tables


# ─── Rating grids drawn with markers (Thorough) ──────────────────────────────
# A row of small identical dots is a scale (Underweight ... Overweight); a
# larger coloured shape on one dot is the rating, a triangle an arrow showing
# a change. Engines see empty cells: the rating is drawn, not written.
SCALE_DOT_MAX_PT = 5.0         # a scale dot is at most this wide
SCALE_MIN_POSITIONS = 3
SCALE_MIN_ROWS = 3
SCALE_MIN_DOTS = 9
MARKER_MIN_AREA_RATIO = 3.0    # a marker covers at least this many dots' area
MARKER_MAX_PT = 20.0
POSITION_TOLERANCE_PT = 2.5


def _filled_small(page: Any) -> list[dict]:
    out = []
    for obj in list(getattr(page, "curves", []) or []) + list(getattr(page, "rects", []) or []):
        if not obj.get("fill"):
            continue
        w, h = obj["x1"] - obj["x0"], obj["bottom"] - obj["top"]
        if 1.0 <= w <= MARKER_MAX_PT and 1.0 <= h <= MARKER_MAX_PT:
            out.append(obj)
    return out


def _cluster(values: list[float], tolerance: float) -> list[float]:
    centres: list[list[float]] = []
    for v in sorted(values):
        if centres and v - centres[-1][-1] <= tolerance:
            centres[-1].append(v)
        else:
            centres.append([v])
    return [sum(c) / len(c) for c in centres]


def _triangle_direction(obj: dict) -> str:
    """'◀', '▶', '▲' or '▼' for a filled triangle; '' for anything else."""
    pts = {(round(x, 1), round(y, 1)) for x, y in obj.get("pts") or []}
    if len(pts) != 3:
        return ""
    xs = sorted(p[0] for p in pts)
    ys = sorted(p[1] for p in pts)
    if abs(xs[0] - xs[1]) < 0.6 and xs[2] - xs[1] > 1:
        return "▶"
    if abs(xs[1] - xs[2]) < 0.6 and xs[1] - xs[0] > 1:
        return "◀"
    # pdfplumber's y grows downwards in "top", but "pts" are in PDF space (up).
    if abs(ys[0] - ys[1]) < 0.6 and ys[2] - ys[1] > 1:
        return "▲"
    if abs(ys[1] - ys[2]) < 0.6 and ys[1] - ys[0] > 1:
        return "▼"
    return ""


def _saturation(color: Any) -> float:
    if not isinstance(color, (tuple, list)) or len(color) != 3:
        return 0.0
    try:
        r, g, b = (float(v) for v in color)
    except (TypeError, ValueError):
        return 0.0
    return max(r, g, b) - min(r, g, b)


def _mid(line: list[dict]) -> float:
    return sum((w["top"] + w["bottom"]) / 2 for w in line) / len(line)


def _crosses(line: list[dict], words: list[dict], x0: float, x1: float) -> bool:
    """Text on this line continuing across [x0, x1]: a paragraph, not a label."""
    y = _mid(line)
    return any(abs((w["top"] + w["bottom"]) / 2 - y) < 2.5 and x0 <= w["x0"] <= x1 for w in words)


def _name_at(boxes: list[dict], x: float) -> str:
    """The heading over position `x`: the one spanning it, else the nearest."""
    for c in boxes:
        if c["x0"] - 2 <= x <= c["x1"] + 2:
            return c["text"]
    if not boxes:
        return ""
    return min(boxes, key=lambda c: abs((c["x0"] + c["x1"]) / 2 - x))["text"]


def find_marker_grids(page: Any, words: list[dict]) -> list[dict]:
    """Rating grids drawn with dots and markers, as tables: label, rating, scale."""
    shapes = _filled_small(page)
    kinds: dict[tuple, list[dict]] = {}
    for s in shapes:
        w, h = s["x1"] - s["x0"], s["bottom"] - s["top"]
        if w <= SCALE_DOT_MAX_PT and h <= SCALE_DOT_MAX_PT:
            kinds.setdefault((round(w), round(h), str(s.get("non_stroking_color"))), []).append(s)
    if not kinds:
        return []
    dots = max(kinds.values(), key=len)
    if len(dots) < SCALE_MIN_DOTS:
        return []
    dot_area = max(1.0, sum((d["x1"] - d["x0"]) * (d["bottom"] - d["top"]) for d in dots) / len(dots))
    cx = lambda o: (o["x0"] + o["x1"]) / 2  # noqa: E731
    cy = lambda o: (o["top"] + o["bottom"]) / 2  # noqa: E731

    columns = _cluster([cx(d) for d in dots], POSITION_TOLERANCE_PT)
    if len(columns) < SCALE_MIN_POSITIONS:
        return []
    gaps = [b - a for a, b in zip(columns, columns[1:], strict=False)]
    typical = sorted(gaps)[len(gaps) // 2]
    groups: list[list[float]] = [[columns[0]]]
    for gap, x in zip(gaps, columns[1:], strict=False):
        if gap > 2.5 * typical:
            groups.append([x])
        else:
            groups[-1].append(x)

    tables: list[dict] = []
    for positions in groups:
        if len(positions) < SCALE_MIN_POSITIONS:
            continue
        spacing_x = (positions[-1] - positions[0]) / (len(positions) - 1)
        mine = [d for d in dots if positions[0] - spacing_x / 2 <= cx(d) <= positions[-1] + spacing_x / 2]
        row_ys = [y for y in _cluster([cy(d) for d in mine], POSITION_TOLERANCE_PT)
                  if sum(1 for d in mine if abs(cy(d) - y) <= POSITION_TOLERANCE_PT) >= SCALE_MIN_POSITIONS]
        if len(row_ys) < SCALE_MIN_ROWS:
            continue
        spacing_y = min(b - a for a, b in zip(row_ys, row_ys[1:], strict=False))
        gx0, gx1 = positions[0] - spacing_x / 2, positions[-1] + spacing_x / 2

        # Row labels: the words left of the dots on each row's line, plus lines
        # continuing a label and the rows of the same list below the grid.
        left = [w for w in words if w["x1"] <= gx0 and w.get("upright", True)]
        # The label column is the text nearest the dots on each row's line,
        # not whatever else sits further left on the page (another table).
        nearest = []
        for y in row_ys:
            on_line = [w for w in left if abs((w["top"] + w["bottom"]) / 2 - y) <= spacing_y / 2]
            if on_line:
                nearest.append(max(_chunks(on_line), key=lambda c: c["x1"])["x0"])
        if not nearest:
            continue
        label_x0 = sorted(nearest)[len(nearest) // 2]
        # Words starting in the label column (a label may run on past the dots'
        # left edge only below the grid, "Alternative Investments*").
        column = [w for w in words if label_x0 - 3 <= w["x0"] < gx0 and w.get("upright", True)]
        lines = sorted((line for line in _word_lines(column) if line), key=lambda line: line[0]["top"])
        top_limit = row_ys[0] - spacing_y / 2
        bottom_row = row_ys[-1] + spacing_y / 2

        body: list[list[dict]] = []
        for line in lines:
            ly = _mid(line)
            if ly < top_limit:
                continue
            if body and ly - _mid(body[-1]) > 2.2 * spacing_y:
                break
            if ly > bottom_row and _crosses(line, words, gx0, gx1):
                break
            body.append(line)
        if not body:
            continue

        rows: list[dict] = []
        for line in body:
            ly = _mid(line)
            text = " ".join(w["text"] for w in sorted(line, key=lambda w: w["x0"]))
            near = min(row_ys, key=lambda y: abs(y - ly))
            if abs(near - ly) <= spacing_y * 0.6:
                if rows and rows[-1]["y"] == near:
                    rows[-1]["label"] += " " + text  # a label on two lines beside one row of dots
                    rows[-1]["words"] += list(line)
                else:
                    rows.append({"label": text, "y": near, "words": list(line)})
            elif rows and rows[-1]["y"] is not None and 0 < ly - rows[-1]["y"] < spacing_y:
                rows[-1]["label"] += " " + text      # a label on two lines
                rows[-1]["words"] += list(line)
            else:
                rows.append({"label": text, "y": None, "words": list(line)})

        # Headings above the grid: the line just above names the positions.
        above = [w for w in words if w["bottom"] <= top_limit + 1 and w["bottom"] >= top_limit - 3.5 * spacing_y
                 and w["x0"] >= label_x0 - 3 and w["x1"] <= gx1 + spacing_x]
        head_lines = sorted(_word_lines(above), key=lambda line: line[0]["top"])
        heading = head_lines[-1] if head_lines else []
        left_head = " ".join(w["text"] for w in heading if w["x1"] <= gx0) or "Item"
        name_boxes = _chunks([w for w in heading if w["x1"] > gx0], 3.0, 0.45) if heading else []
        # A line right above the position names ("CIO View") names the rating;
        # a title further up does not.
        view_head = "View"
        head_used = [heading] if heading else []
        if len(head_lines) >= 2:
            upper = head_lines[-2]
            height = max(w["bottom"] - w["top"] for w in heading)
            gap = min(w["top"] for w in heading) - max(w["bottom"] for w in upper)
            upper_in_grid = [w for w in upper if w["x0"] > gx0 - 2]
            if gap <= 0.8 * height and upper_in_grid:
                view_head = " ".join(w["text"] for w in sorted(upper_in_grid, key=lambda w: w["x0"]))
                head_used.append(upper_in_grid)

        dot_ids = {id(d) for d in mine}
        out_rows = [[left_head, view_head, "Scale"]]
        for row in rows:
            if row["y"] is None:
                out_rows.append([row["label"], "", ""])
                continue
            marks: dict[int, tuple[str, float, str]] = {}  # position -> (symbol, saturation, colour)
            for s in shapes:
                if id(s) in dot_ids or (s["x1"] - s["x0"]) * (s["bottom"] - s["top"]) < MARKER_MIN_AREA_RATIO * dot_area:
                    continue
                if abs(cy(s) - row["y"]) > spacing_y / 2 or not gx0 <= cx(s) <= gx1:
                    continue
                pos = min(range(len(positions)), key=lambda i: abs(positions[i] - cx(s)))
                arrow = _triangle_direction(s)
                sat = _saturation(s.get("non_stroking_color"))
                symbol = arrow or "●"
                current = marks.get(pos)
                if current is None or (symbol == "●" and (current[0] != "●" or sat > current[1])):
                    marks[pos] = (symbol, sat, color_name(s.get("non_stroking_color")))
            scale = " ".join(marks.get(i, ("○",))[0] for i in range(len(positions)))
            rating = [i for i, m in marks.items() if m[0] == "●"]
            view = _name_at(name_boxes, positions[rating[0]]) if rating else ""
            colour = marks[rating[0]][2] if rating else ""
            out_rows.append([row["label"], view, f"{scale} ({colour})" if colour else scale])
        if sum(1 for r in out_rows[1:] if r[1]) < SCALE_MIN_ROWS:
            continue
        region_words = [w for r in rows for w in r["words"]] + [w for line in head_used for w in line]
        x0 = min(w["x0"] for w in region_words)
        tables.append({
            "bbox": [round(x0, 2), round(min(w["top"] for w in region_words), 2),
                     round(gx1, 2), round(max(w["bottom"] for w in region_words), 2)],
            "captions": [],
            "rows": out_rows,
            "source": "pdf_shapes",
            "kind": TABLE_KIND_RATING,
            "verified": True,
            "reason": "",
            "merged_cells": 0,
            "shape_cells": sum(1 for r in out_rows[1:] if r[2]),
            "words": _tokens([w["text"] for w in region_words]),
        })
    return tables


def _overlaps(a: list[float], b: list[float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# ─── Bands and anchors ───────────────────────────────────────────────────────
def _bands(tables: list[dict]) -> list[list[dict]]:
    """Tables grouped by vertical overlap (side by side), bands top to bottom, tables left to right."""
    bands: list[list[dict]] = []
    for table in sorted(tables, key=lambda t: t["bbox"][1]):
        _, top, _, bottom = table["bbox"]
        placed = False
        for band in bands:
            btop = min(t["bbox"][1] for t in band)
            bbottom = max(t["bbox"][3] for t in band)
            overlap = min(bottom, bbottom) - max(top, btop)
            if overlap > BAND_OVERLAP * min(bottom - top, bbottom - btop):
                band.append(table)
                placed = True
                break
        if not placed:
            bands.append([table])
    for band in bands:
        band.sort(key=lambda t: t["bbox"][0])
    return bands


def _anchor_lines(page: Any, tables: list[dict]) -> list[dict]:
    """Text lines outside every table, top to bottom: {"text", "top", "bottom"}."""
    try:
        lines = page.extract_text_lines(return_chars=False)
    except Exception:
        return []
    out = []
    for line in lines:
        cx, cy = (line["x0"] + line["x1"]) / 2, (line["top"] + line["bottom"]) / 2
        if any(_inside(tuple(t["bbox"]), cx, cy) for t in tables):
            continue
        text = clean_cell_text(line.get("text", ""))
        if text:
            out.append({"text": text, "top": float(line["top"]), "bottom": float(line["bottom"])})
    out.sort(key=lambda ln: ln["top"])
    return out


def page_segments(words: list[dict]) -> list[dict]:
    """The page's text as positioned pieces: each line split where a wide gap separates columns.

    For finding what an engine left out (app/core/figure_text.py): a chart's
    labels, a sidebar. Upright text only; rotated axis titles read backwards.
    """
    upright = [w for w in words if w.get("upright", True)]
    out = []
    for line in _word_lines(upright):
        # Finer than a table's cells: a chart's small labels sit closer together.
        for chunk in _chunks(line, SEGMENT_GAP_MIN_PT, SEGMENT_GAP_HEIGHT_RATIO):
            out.append({
                "text": chunk["text"],
                "x0": round(chunk["x0"], 2), "x1": round(chunk["x1"], 2),
                "top": round(chunk["top"], 2), "bottom": round(chunk["bottom"], 2),
            })
    return out


# ─── What else is drawn and written on a page (for the steps after the tables) ─
# Exported with the segments, on the pages the finder reads for them:
# boxes (diagram panels, checkboxes, chart bars), the font styles of each line
# (bold, italic, raised), the images' places, chart paths and rotated labels.
# Bounded so a page drawn with tens of thousands of objects stays a small reply.
LAYOUT_MAX_BOXES = 800
LAYOUT_MAX_PATHS = 1500
LAYOUT_MAX_POINTS = 600
LAYOUT_MAX_STYLED_LINES = 400
BOX_MIN_PT = 3.0               # smaller than this both ways is a dot, not a box
SUP_SIZE_RATIO = 0.8           # a word this much smaller than its line's text...
SUP_RAISE_RATIO = 0.12         # ...whose bottom sits this share of the line's size higher is raised
_BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demi", re.I)
_ITALIC_FONT = re.compile(r"italic|oblique|(?:-|bold)it$", re.I)


def font_style(fontname: str) -> str:
    """"b", "i", "bi" or "" from a font's name ("ABCDEF+Arial-BoldItalicMT" -> "bi")."""
    name = str(fontname or "").split("+", 1)[-1]
    return ("b" if _BOLD_FONT.search(name) else "") + ("i" if _ITALIC_FONT.search(name) else "")


def _r(value: Any) -> float:
    return round(float(value), 2)


def _box_of(obj: dict, page_w: float, page_h: float) -> dict | None:
    x0, top, x1, bottom = (float(obj[k]) for k in ("x0", "top", "x1", "bottom"))
    w, h = x1 - x0, bottom - top
    if max(w, h) < BOX_MIN_PT or w >= 0.95 * page_w or h >= 0.95 * page_h:
        return None
    filled = bool(obj.get("fill"))
    return {
        "x0": _r(x0), "top": _r(top), "x1": _r(x1), "bottom": _r(bottom),
        "fill": color_name(obj.get("non_stroking_color")) if filled else "",
        "rgb": color_key(obj.get("non_stroking_color")) if filled else "",
        "stroke": bool(obj.get("stroke")),
    }


def _closed(points: list) -> bool:
    if len(points) < 4:
        return False
    (ax, ay), (bx, by) = points[0], points[-1]
    return abs(ax - bx) <= 1.0 and abs(ay - by) <= 1.0


def _size(word: dict) -> float:
    return float(word.get("size") or (word["bottom"] - word["top"]) or 1.0)


def _text_chunks(words: list[dict]) -> list[list[dict]]:
    """The page's words as line pieces (a line split at column gaps), raised words joined to theirs.

    A footnote number set small and high can sit too far up to share a line
    with its word; it is put back into the piece it touches.
    """
    chunks: list[list[dict]] = []
    for line in _word_lines(words):
        chunks += [c["words"] for c in _chunks(line, SEGMENT_GAP_MIN_PT, SEGMENT_GAP_HEIGHT_RATIO)]
    small_ids = {
        id(c) for c in chunks
        if len(c) <= 2 and all(_size(w) <= SUP_SIZE_RATIO * _median_size(chunks, c) for w in c)
    }
    pieces = [c for c in chunks if id(c) not in small_ids]
    loose = [c for c in chunks if id(c) in small_ids]
    for small in loose:
        home = None
        for piece in pieces:
            top = min(w["top"] for w in piece)
            bottom = max(w["bottom"] for w in piece)
            x0 = min(w["x0"] for w in piece)
            x1 = max(w["x1"] for w in piece)
            if all(top - 2 <= w["top"] and w["bottom"] <= bottom + 0.5 and x0 - 4 <= w["x0"] <= x1 + 4 for w in small):
                home = piece
                break
        if home is None:
            pieces.append(small)
        else:
            home.extend(small)
            home.sort(key=lambda w: w["x0"])
    return pieces


def _median_size(chunks: list[list[dict]], chunk: list[dict]) -> float:
    """The text size of the chunks around `chunk` (on its height, give or take a line)."""
    top = min(w["top"] for w in chunk)
    near = [_size(w) for c in chunks if c is not chunk for w in c if abs(w["top"] - top) <= 12]
    if not near:
        return 0.0
    near.sort()
    return near[len(near) // 2]


def _styled_lines(words: list[dict]) -> list[list[dict]]:
    """Line pieces with a bold, italic or raised word: [{"t": text, "s": "b"|"i"|"bi"|"sup"|""}, ...]."""
    out: list[list[dict]] = []
    for line in _text_chunks(words):
        sizes = sorted(_size(w) for w in line)
        size = sizes[len(sizes) // 2] or 1.0
        baseline = sorted(w["bottom"] for w in line)[len(line) // 2]
        runs: list[dict] = []
        for w in line:
            wsize = float(w.get("size") or size)
            raised = wsize <= SUP_SIZE_RATIO * size and w["bottom"] <= baseline - SUP_RAISE_RATIO * size
            style = "sup" if raised else font_style(w.get("fontname", ""))
            if runs and runs[-1]["s"] == style and style != "sup":
                runs[-1]["t"] += " " + w["text"]
            else:
                runs.append({"t": w["text"], "s": style})
        if any(r["s"] for r in runs) and len(runs) > 0:
            out.append(runs)
        if len(out) >= LAYOUT_MAX_STYLED_LINES:
            break
    return out


def _rotated_text(page: Any) -> list[dict]:
    """Words written sideways (a chart's year labels), in reading order: {"text", "x0", "x1", "top", "bottom"}."""
    chars = [c for c in page.chars if not c.get("upright", True) and str(c.get("text", "")).strip()]
    columns: list[list[dict]] = []
    for ch in sorted(chars, key=lambda c: (round(c["x0"]), c["top"])):
        last = columns[-1] if columns else None
        if last and abs(last[-1]["x0"] - ch["x0"]) <= 1.0 and ch["top"] - last[-1]["bottom"] <= 0.5 * (ch["bottom"] - ch["top"]) + 1:
            last.append(ch)
        else:
            columns.append([ch])
    out = []
    for column in columns:
        matrix = column[0].get("matrix") or (1, 0, 0, 1, 0, 0)
        ordered = sorted(column, key=lambda c: c["top"], reverse=float(matrix[1]) > 0)  # bottom-to-top text
        out.append({
            "text": "".join(c["text"] for c in ordered),
            "x0": _r(min(c["x0"] for c in column)), "x1": _r(max(c["x1"] for c in column)),
            "top": _r(min(c["top"] for c in column)), "bottom": _r(max(c["bottom"] for c in column)),
        })
    return out


def page_layout(page: Any, words: list[dict]) -> dict:
    """What the steps after the tables read besides the text segments: boxes, styles, images, paths."""
    page_w, page_h = float(page.width), float(page.height)
    boxes: list[dict] = []
    for rect in page.rects:
        box = _box_of(rect, page_w, page_h)
        if box is not None:
            boxes.append(box)
    paths: list[dict] = []
    for curve in list(page.curves) + list(page.lines):
        points = [(_r(x), _r(y)) for x, y in (curve.get("pts") or [])][:LAYOUT_MAX_POINTS]
        if len(points) < 2:
            continue
        if _closed(points):
            box = _box_of(curve, page_w, page_h)  # a rounded panel or a drawn checkbox
            if box is not None:
                boxes.append(box)
            continue
        if len(paths) < LAYOUT_MAX_PATHS:
            paths.append({
                "pts": points, "color": color_name(curve.get("stroking_color")),
                "rgb": color_key(curve.get("stroking_color")),
            })
    images = [
        {"x0": _r(i["x0"]), "top": _r(i["top"]), "x1": _r(i["x1"]), "bottom": _r(i["bottom"])}
        for i in page.images
    ]
    styled_words = [w for w in words if w.get("upright", True)]
    return {
        "boxes": boxes[:LAYOUT_MAX_BOXES],
        "styles": _styled_lines(styled_words),
        "images": images,
        "paths": paths,
        "vtext": _rotated_text(page),
    }


def _too_complex(page: Any) -> bool:
    """More drawing or text than any table page has (MAX_PAGE_EDGES, MAX_PAGE_CHARS)."""
    edges = 4 * len(page.rects) + len(page.lines) + len(page.curves)
    return edges > MAX_PAGE_EDGES or len(page.chars) > MAX_PAGE_CHARS


def _page_tables(
    page: Any, number: int, hints: list[list[list[str]]] | None = None,
    detectors: frozenset[str] = frozenset(),
) -> dict:
    rotation = int(getattr(page, "rotation", 0) or 0) % 360
    if rotation:
        return {"page": number, "skipped": "rotated", "bands": []}
    if _too_complex(page):
        return {"page": number, "skipped": "too_complex", "bands": []}
    tables = [t for t in (_table_dict(page, raw) for raw in page.find_tables()) if t is not None]
    extra: dict[str, Any] = {}
    if hints or detectors:
        words = page.extract_words()
        ruled = [tuple(t["bbox"]) for t in tables]
        for rows in hints or []:
            tables += split_merged_table(words, rows, ruled)
        if DETECT_RATING_GRIDS in detectors:
            grids = find_marker_grids(page, words)
            if grids:
                # A rating grid's rules can look like a table of empty cells.
                tables = [t for t in tables if not any(_overlaps(t["bbox"], g["bbox"]) for g in grids)]
                tables += grids
        if DETECT_BORDERLESS in detectors:
            tables += find_word_tables(words, [tuple(t["bbox"]) for t in tables])
        if DETECT_SEGMENTS in detectors:
            extra = {
                "width": round(float(page.width), 2),
                "height": round(float(page.height), 2),
                "segments": page_segments(words),
            }
            try:
                # Words split where the font changes, for the styles; the tables keep `words`.
                extra.update(page_layout(page, page.extract_words(extra_attrs=["fontname", "size"])))
            except Exception as exc:  # an extra: the segments still count
                logger.warning("Page layout skipped on page %d: %s", number, exc)
    if not tables:
        return {"page": number, "skipped": "", "bands": [], **extra}
    lines = _anchor_lines(page, tables)
    bands = []
    for band in _bands(tables):
        top = min(t["bbox"][1] for t in band)
        bottom = max(t["bbox"][3] for t in band)
        above = [ln for ln in lines if ln["bottom"] <= top + 1]
        below = [ln for ln in lines if ln["top"] >= bottom - 1]
        bands.append({
            "top": round(top, 2),
            "bottom": round(bottom, 2),
            "above": above[-1]["text"] if above else "",
            "below": below[0]["text"] if below else "",
            "tables": band,
        })
    return {"page": number, "skipped": "", "bands": bands, **extra}


# ─── Entry point ─────────────────────────────────────────────────────────────
def find_tables(
    path: str,
    pages: list[int],
    *,
    hints: dict[int, list[list[list[str]]]] | None = None,
    deep: bool = False,
    detectors: Iterable[str] | None = None,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
) -> dict:
    """Tables of the given 1-based pages of the PDF at `path`. Never raises.

    `hints` maps a page to tables an engine wrote for it (rows, header
    first): a borderless one that is really several tables side by side comes
    back split (source "pdf_words"); one that is a single table adds nothing.
    `detectors` names what else to look for on each page (app/core/thorough_passes.py):
    rating grids drawn with markers, borderless tables from word positions
    (unverified, to be confirmed), and positioned text segments; `deep` is
    all of them.

    {"pages": [{"page", "skipped", "bands": [...]}], "truncated": bool,
     "elapsed_ms": int, "error": str}. Stops after MAX_PAGES pages or
    `time_budget_s` seconds and says so with truncated=True.
    """
    hints = {int(k): v for k, v in (hints or {}).items()}
    looking_for = frozenset(ALL_DETECTORS if deep else ()) | frozenset(
        d for d in detectors or () if d in ALL_DETECTORS
    )
    pages = list(pages) + list(hints)
    started = time.perf_counter()
    result: dict[str, Any] = {"pages": [], "truncated": False, "elapsed_ms": 0, "error": ""}
    try:
        import pdfplumber
    except Exception as exc:  # pragma: no cover - ships with markitdown[pdf]
        result["error"] = f"pdfplumber is not available: {exc}"
        return result
    wanted = sorted({int(p) for p in pages if int(p) >= 1})
    try:
        with pdfplumber.open(path) as pdf:
            total = len(pdf.pages)
            for count, number in enumerate(wanted):
                if number > total:
                    continue
                if count >= MAX_PAGES or time.perf_counter() - started > time_budget_s:
                    result["truncated"] = True
                    break
                page = pdf.pages[number - 1]
                try:
                    result["pages"].append(_page_tables(page, number, hints.get(number), looking_for))
                except Exception as exc:
                    logger.warning("Table finder skipped page %d: %s", number, exc)
                    result["pages"].append({"page": number, "skipped": "error", "bands": []})
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return result
