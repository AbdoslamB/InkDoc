"""Tables in Markdown: find them, read them, and write them InkDoc's way.

Pure functions, no I/O, shared by the table check (app/core/table_check.py),
the table finder that runs in the MarkItDown worker (app/core/table_finder.py)
and GLM-OCR's table crops.

InkDoc's rules for a table it writes itself (merged cells come out as
Markdown, not HTML):

- A merged cell's text is repeated in every row and column it covers, so each
  row still reads on its own ("Fruit | Pears | 25").
- Stacked header rows are flattened into one: a header cell that spans
  several columns is prefixed to each column under it ("Revenue 2023").
- A row with one cell spanning the whole width is a caption when it is at the
  top (written as a line above the table), and a section row in the body
  (its text in the first column, the other columns empty).
- Multi-line cell text is joined with spaces; "|" is escaped.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# ─── Grid model ──────────────────────────────────────────────────────────────


@dataclass
class GridCell:
    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    text: str = ""


def clean_cell_text(text: str) -> str:
    """One line of text: whitespace and line breaks collapsed to single spaces."""
    return " ".join((text or "").split())


def to_markdown_grid(cells: list[GridCell], n_rows: int, n_cols: int) -> tuple[list[str], list[list[str]]]:
    """(caption lines, rows) for a grid of possibly merged cells, by the rules above.

    Positions no cell covers are empty strings. The first returned row is the
    header. Never raises on odd input: spans are clipped to the grid.
    """
    if n_rows <= 0 or n_cols <= 0:
        return [], []
    owner: list[list[GridCell | None]] = [[None] * n_cols for _ in range(n_rows)]
    for cell in cells:
        for r in range(cell.row, min(n_rows, cell.row + max(1, cell.rowspan))):
            for c in range(cell.col, min(n_cols, cell.col + max(1, cell.colspan))):
                if owner[r][c] is None:
                    owner[r][c] = cell

    def full_width(r: int) -> GridCell | None:
        first = owner[r][0]
        if first is not None and n_cols > 1 and all(owner[r][c] is first for c in range(n_cols)):
            return first
        return None

    captions: list[str] = []
    start = 0
    while start < n_rows and full_width(start) is not None:
        text = clean_cell_text(full_width(start).text)  # type: ignore[union-attr]
        if text and (not captions or captions[-1] != text):
            captions.append(text)
        start += 1
    if start >= n_rows:
        return captions, []

    def spans_columns(r: int) -> bool:
        return any(
            cell is not None and cell.colspan > 1 and full_width(r) is None
            for cell in owner[r]
        )

    # Stacked header: the top rows with a cell spanning columns, plus the row under them.
    header_end = start
    while header_end < n_rows and spans_columns(header_end):
        header_end += 1
    if header_end > start and header_end < n_rows:
        header_end += 1
    header_rows = range(start, max(header_end, start + 1))

    header: list[str] = []
    for c in range(n_cols):
        parts: list[str] = []
        seen: set[int] = set()
        for r in header_rows:
            cell = owner[r][c]
            if cell is None or id(cell) in seen:
                continue
            seen.add(id(cell))
            text = clean_cell_text(cell.text)
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        header.append(" ".join(parts))

    rows = [header]
    for r in range(header_rows.stop, n_rows):
        wide = full_width(r)
        if wide is not None:
            rows.append([clean_cell_text(wide.text)] + [""] * (n_cols - 1))
            continue
        rows.append([clean_cell_text(cell.text) if cell is not None else "" for cell in owner[r]])
    return captions, rows


def render_pipe_table(rows: list[list[str]]) -> list[str]:
    """GFM pipe-table lines; the first row is the header. Columns padded for reading."""
    if not rows:
        return []
    n_cols = max(len(r) for r in rows)
    norm = [[_escape_cell(r[c]) if c < len(r) else "" for c in range(n_cols)] for r in rows]
    widths = [max(3, min(60, max(len(r[c]) for r in norm))) for c in range(n_cols)]

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(cell.ljust(widths[c]) for c, cell in enumerate(cells)) + " |"

    out = [line(norm[0]), "| " + " | ".join("-" * w for w in widths) + " |"]
    out.extend(line(r) for r in norm[1:])
    return out


def _escape_cell(text: str) -> str:
    return clean_cell_text(text).replace("\\", "\\\\").replace("|", "\\|")


# ─── Finding tables in Markdown ──────────────────────────────────────────────

_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_HTML_TABLE_OPEN = re.compile(r"<table\b", re.I)
_HTML_TABLE_CLOSE = re.compile(r"</table\s*>", re.I)


@dataclass
class TableBlock:
    start: int                      # first line (0-based)
    end: int                        # one past the last line
    kind: str                       # "pipe" | "html"
    rows: list[list[str]] = field(default_factory=list)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)


def split_pipe_row(line: str) -> list[str]:
    """Cells of one pipe-table row. "\\|" is a literal pipe, not a separator."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def find_table_blocks(lines: list[str]) -> list[TableBlock]:
    """Every pipe table and HTML <table> in `lines`, in order."""
    blocks: list[TableBlock] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if _HTML_TABLE_OPEN.search(line):
            j = i
            while j < n and not _HTML_TABLE_CLOSE.search(lines[j]):
                j += 1
            end = min(j + 1, n)
            rows = parse_html_table("\n".join(lines[i:end]))
            blocks.append(TableBlock(i, end, "html", rows))
            i = end
            continue
        if "|" in line and i + 1 < n and _SEPARATOR_RE.match(lines[i + 1]) and "-" in lines[i + 1]:
            rows = [split_pipe_row(line)]
            j = i + 2
            while j < n and lines[j].strip() and "|" in lines[j]:
                rows.append(split_pipe_row(lines[j]))
                j += 1
            blocks.append(TableBlock(i, j, "pipe", rows))
            i = j
            continue
        i += 1
    return blocks


class _HtmlTableParser(HTMLParser):
    """Collects the cells of the first <table>, with rowspan/colspan."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[GridCell] = []
        self._row = -1
        self._occupied: dict[tuple[int, int], bool] = {}
        self._col = 0
        self._cell: GridCell | None = None
        self._buf: list[str] = []
        self._depth = 0
        self.n_cols = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._depth += 1
            return
        if self._depth != 1:
            return
        if tag == "tr":
            self._finish_cell()
            self._row += 1
            self._col = 0
        elif tag in ("td", "th"):
            self._finish_cell()
            if self._row < 0:
                self._row = 0
            while self._occupied.get((self._row, self._col)):
                self._col += 1
            spans = {k.lower(): v for k, v in attrs}
            self._cell = GridCell(
                self._row, self._col,
                rowspan=_span(spans.get("rowspan")), colspan=_span(spans.get("colspan")),
            )
            for r in range(self._cell.row, self._cell.row + self._cell.rowspan):
                for c in range(self._cell.col, self._cell.col + self._cell.colspan):
                    self._occupied[(r, c)] = True
            self._col += self._cell.colspan
            self.n_cols = max(self.n_cols, self._col)
            self._buf = []
        elif tag == "br" and self._cell is not None:
            self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            if self._depth == 1:
                self._finish_cell()
            self._depth -= 1
        elif self._depth == 1 and tag in ("td", "th", "tr"):
            self._finish_cell()

    def handle_data(self, data: str) -> None:
        if self._cell is not None and self._depth >= 1:
            self._buf.append(data)

    def _finish_cell(self) -> None:
        if self._cell is not None:
            self._cell.text = clean_cell_text(html.unescape("".join(self._buf)))
            self.cells.append(self._cell)
            self._cell = None
            self._buf = []

    @property
    def n_rows(self) -> int:
        return max((c.row + c.rowspan for c in self.cells), default=0)


def _span(value: str | None) -> int:
    try:
        return max(1, min(100, int(str(value).strip())))
    except (TypeError, ValueError):
        return 1


def parse_html_cells(markup: str) -> tuple[list[GridCell], int, int]:
    """The cells of the first HTML table in `markup`, and its row and column counts."""
    parser = _HtmlTableParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    parser._finish_cell()
    return parser.cells, parser.n_rows, parser.n_cols


def parse_html_table(markup: str) -> list[list[str]]:
    """Rows of the first HTML table, merged cells repeated (no header flattening)."""
    cells, n_rows, n_cols = parse_html_cells(markup)
    grid = [[""] * n_cols for _ in range(n_rows)]
    for cell in cells:
        for r in range(cell.row, min(n_rows, cell.row + cell.rowspan)):
            for c in range(cell.col, min(n_cols, cell.col + cell.colspan)):
                grid[r][c] = cell.text
    return grid


# ─── Comparing text ──────────────────────────────────────────────────────────


def line_tokens(text: str) -> list[str]:
    """Comparable words of a Markdown line: the missing-text check's own tokens."""
    from app.core.quality_check import normalize_markdown, tokenize

    return tokenize(normalize_markdown(text))


def cell_key(text: str) -> str:
    """A cell's text reduced to its tokens, for comparing two readings of a table."""
    return " ".join(line_tokens(text))
