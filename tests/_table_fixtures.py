"""Synthetic table pages: the hard cases the table stage exists for.

Each fixture returns (pages, truth). `pages` goes to _pdf_fixtures.write_pdf;
`truth` lists the tables a correct conversion contains, in reading order, as
{"page": 1-based page, "rows": [[cell, ...], ...]} with merged cells already
repeated and stacked header rows already flattened (InkDoc's Markdown rules,
app/core/markdown_tables.py). scripts/table_bench.py scores engines against
the same truth.

Drawn with raw PDF operators in PDF coordinates (origin bottom-left, points).
Every table cell is its own stroked rectangle when ruled, which is how most
PDF writers draw borders and how merged cells come out naturally.
"""
from __future__ import annotations

from tests._pdf_fixtures import PAGE_H, drawn_page, prose_page, text_op, words

LINE = 11  # leading of multi-line cell text


def grid_table(
    x0: float,
    y_top: float,
    cells: list[list[str | None]],
    col_widths: list[float],
    row_h: float = 20,
    *,
    spans: dict[tuple[int, int], tuple[int, int]] | None = None,
    ruled: bool = True,
    shapes: dict[tuple[int, int], list[tuple[tuple[float, float, float], float, float]]] | None = None,
    size: float = 9,
) -> list[str]:
    """Operators drawing one table.

    cells: rows of cell text; None for a position covered by a merged cell.
    spans: {(row, col): (rowspan, colspan)} for merged cells.
    shapes: {(row, col): [((r, g, b), width, height), ...]} filled rectangles
    drawn side by side in the cell (status squares, mini bar charts).
    Cell text may contain "\\n" for several lines.
    """
    spans = spans or {}
    shapes = shapes or {}
    ops: list[str] = ["0 0 0 RG 0.6 w"]
    for r, row in enumerate(cells):
        for c, text in enumerate(row):
            if text is None:
                continue
            rs, cs = spans.get((r, c), (1, 1))
            x = x0 + sum(col_widths[:c])
            w = sum(col_widths[c:c + cs])
            top = y_top - r * row_h
            h = rs * row_h
            if ruled:
                ops.append(f"{x:.2f} {top - h:.2f} {w:.2f} {h:.2f} re S")
            for i, line in enumerate(text.split("\n")):
                if line:
                    ops.append(text_op(x + 4, top - 13 - i * LINE, line, size))
            cursor = x + 4 + (len(text) * size * 0.55 + 6 if text else 0)
            for (rgb, sw, sh) in shapes.get((r, c), []):
                ops.append(f"{rgb[0]} {rgb[1]} {rgb[2]} rg {cursor:.2f} {top - h + 4:.2f} {sw} {sh} re f 0 g")
                cursor += sw + 4
    return ops


def _page(ops: list[str], above: list[str] | None = None) -> dict:
    """A drawn page with a heading line at the top of the page."""
    head = [text_op(40, PAGE_H - 50 - i * 12, line, 12 if i == 0 else 9) for i, line in enumerate(above or [])]
    return drawn_page(head + ops)


LEFT = [["Region", "Q1", "Q2"], ["North", "120", "135"], ["South", "98", "101"], ["East", "143", "150"]]
RIGHT = [["Product", "Units", "Price"], ["Alpha", "12", "4.50"], ["Beta", "7", "9.99"], ["Gamma", "30", "1.25"]]
NOTE = "Figures are unaudited and subject to revision."


def side_by_side(ruled: bool = True) -> tuple[list[dict], list[dict]]:
    ops = grid_table(50, 700, LEFT, [70, 70, 70], ruled=ruled)
    ops += grid_table(330, 700, RIGHT, [70, 70, 70], ruled=ruled)
    ops.append(text_op(50, 580, NOTE))
    return [_page(ops, ["Quarterly summary"])], [{"page": 1, "rows": LEFT}, {"page": 1, "rows": RIGHT}]


GREEN, RED, AMBER, BLUE = (0, 0.6, 0), (0.8, 0, 0), (0.95, 0.75, 0), (0.2, 0.3, 0.8)


def shapes_in_cells() -> tuple[list[dict], list[dict]]:
    cells = [["Task", "Status", "Trend"], ["Design", "", ""], ["Build", "", ""], ["Test", "", ""]]
    bars = lambda *hs: [(BLUE, 8, h) for h in hs]  # noqa: E731
    shapes = {
        (1, 1): [(GREEN, 12, 12)], (2, 1): [(RED, 12, 12)], (3, 1): [(AMBER, 12, 12)],
        (1, 2): bars(6, 10, 14), (2, 2): bars(14, 10, 6), (3, 2): bars(5, 5, 14),
    }
    ops = grid_table(50, 700, cells, [110, 110, 110], row_h=24, shapes=shapes)
    ops.append(text_op(50, 590, "Green = on track, amber = at risk, red = late."))
    truth = [["Task", "Status", "Trend"],
             ["Design", "[green shape]", "[3 blue shapes]"],
             ["Build", "[red shape]", "[3 blue shapes]"],
             ["Test", "[yellow shape]", "[3 blue shapes]"]]
    return [_page(ops, ["Project status"])], [{"page": 1, "rows": truth}]


def merged_header() -> tuple[list[dict], list[dict]]:
    """A two-level header: "Revenue" and "Costs" each span two year columns."""
    cells = [
        ["Region", "Revenue", None, "Costs", None],
        [None, "2023", "2024", "2023", "2024"],
        ["North", "410", "455", "300", "310"],
        ["South", "380", "390", "290", "285"],
    ]
    spans = {(0, 0): (2, 1), (0, 1): (1, 2), (0, 3): (1, 2)}
    ops = grid_table(50, 700, cells, [80, 70, 70, 70, 70], spans=spans)
    truth = [["Region", "Revenue 2023", "Revenue 2024", "Costs 2023", "Costs 2024"],
             ["North", "410", "455", "300", "310"],
             ["South", "380", "390", "290", "285"]]
    return [_page(ops, ["Regional results"])], [{"page": 1, "rows": truth}]


def rowspan_body() -> tuple[list[dict], list[dict]]:
    """A category cell spanning the rows of its items."""
    cells = [
        ["Category", "Item", "Stock"],
        ["Fruit", "Apples", "40"],
        [None, "Pears", "25"],
        ["Dairy", "Milk", "12"],
    ]
    ops = grid_table(50, 700, cells, [90, 90, 70], spans={(1, 0): (2, 1)})
    truth = [["Category", "Item", "Stock"], ["Fruit", "Apples", "40"], ["Fruit", "Pears", "25"],
             ["Dairy", "Milk", "12"]]
    return [_page(ops, ["Inventory"])], [{"page": 1, "rows": truth}]


def multiline_cells() -> tuple[list[dict], list[dict]]:
    cells = [["Step", "Description"], ["1", "Unpack the device\nand check the box"], ["2", "Connect the cable"]]
    ops = grid_table(50, 700, cells, [60, 200], row_h=28)
    truth = [["Step", "Description"], ["1", "Unpack the device and check the box"], ["2", "Connect the cable"]]
    return [_page(ops, ["Setup"])], [{"page": 1, "rows": truth}]


def table_in_prose() -> tuple[list[dict], list[dict]]:
    """One ordinary table between paragraphs: engines usually get this right."""
    above = ["Annual report"] + [words(10, i * 10) for i in range(4)]
    rows = [["Year", "Revenue", "Profit"], ["2022", "1200", "140"], ["2023", "1350", "170"]]
    ops = grid_table(50, 650, rows, [80, 80, 80])
    ops += [text_op(40, 560 - i * 11, words(10, 40 + i * 10)) for i in range(4)]
    return [_page(ops, above)], [{"page": 1, "rows": rows}]


def framed_text() -> tuple[list[dict], list[dict]]:
    """A paragraph in a box: ruled, but not a table."""
    ops = ["0 0 0 RG 50 600 400 80 re S"]
    ops += [text_op(60, 665 - i * 11, words(12, i * 12)) for i in range(5)]
    return [_page(ops, ["Notice"])], []


def repeated_header_pages() -> tuple[list[dict], list[dict]]:
    """The same table header on two pages, different bodies."""
    p1 = [["Name", "Role"], ["Ada", "Engineer"], ["Grace", "Admiral"]]
    p2 = [["Name", "Role"], ["Alan", "Mathematician"], ["Linus", "Developer"]]
    pages = [
        _page(grid_table(50, 700, p1, [100, 120]), ["Staff list"]),
        _page(grid_table(50, 700, p2, [100, 120]), ["Staff list (continued)"]),
    ]
    return pages, [{"page": 1, "rows": p1}, {"page": 2, "rows": p2}]


def wide_borderless() -> tuple[list[dict], list[dict]]:
    """One six-column table without lines, columns evenly spaced: not two tables."""
    rows = [["Month", "North", "South", "East", "West", "Total"],
            ["Jan", "12", "15", "9", "11", "47"],
            ["Feb", "14", "13", "10", "12", "49"]]
    ops = grid_table(50, 700, rows, [70] * 6, ruled=False)
    return [_page(ops, ["Monthly units"])], [{"page": 1, "rows": rows}]


def two_column_prose() -> tuple[list[dict], list[dict]]:
    """A two-column article: every line has two aligned blocks of text, but no table."""
    ops = []
    for i in range(14):
        y = 720 - i * 11
        ops.append(text_op(40, y, words(9, i * 9), 7))
        ops.append(text_op(320, y, words(9, 100 + i * 9), 7))
    return [_page(ops, ["Annual review"])], []


def dotted_contents() -> tuple[list[dict], list[dict]]:
    """A table of contents with dot leaders: aligned, but not a table to rebuild."""
    entries = [("1", "Introduction", "3"), ("2", "Results by region", "7"), ("3", "Costs", "12"),
               ("4", "Outlook", "15")]
    ops = []
    for i, (num, title, page) in enumerate(entries):
        y = 700 - i * 14
        ops.append(text_op(50, y, num))
        ops.append(text_op(80, y, title + " " + "." * 30))
        ops.append(text_op(400, y, page))
    return [_page(ops, ["Contents"])], []


def label_form() -> tuple[list[dict], list[dict]]:
    """Label/value pairs on two columns: a form, not a table."""
    pairs = [("Name: Ada Lovelace", "Date: 10 Dec 1815"), ("Role: Analyst", "Team: Engines"),
             ("City: London", "Phone: 555 0100")]
    ops = []
    for i, (left, right) in enumerate(pairs):
        y = 700 - i * 14
        ops += [text_op(50, y, left), text_op(300, y, right)]
    return [_page(ops, ["Applicant"])], []


RATING_POSITIONS = [320, 343, 366, 389, 412]
LIGHT_GREEN, ORANGE, YELLOW, ARROW_RED = (0.58, 0.76, 0.12), (0.96, 0.5, 0.13), (0.99, 0.84, 0.29), (0.89, 0.09, 0.22)


def rating_grid_ops(rows: list[tuple[str, int, tuple, int | None]], top: float = 300) -> list[str]:
    """A rating grid like a CIO view: five grey dots per row, a coloured marker on one,
    sometimes a red arrow pointing left; headings above, labels on the left.

    rows: (label, marker position 0-4, colour, arrow position or None).
    """
    y_of = lambda r: PAGE_H - (top + 14 * r)  # noqa: E731  (PDF y of row r's centre)
    head_y = PAGE_H - (top - 14)
    ops = [
        text_op(240, head_y - 2, "Asset Class", 7),
        text_op(312, head_y - 2, "Underweight", 6),
        text_op(358, head_y - 2, "Neutral", 6),
        text_op(386, head_y - 2, "Overweight", 6),
        text_op(355, head_y + 6, "CIO View", 6),
    ]
    for r, (label, pos, colour, arrow) in enumerate(rows):
        y = y_of(r)
        ops.append(text_op(240, y - 2.5, label, 7))
        for x in RATING_POSITIONS:
            ops.append(f"0.52 0.52 0.52 rg {x - 1.5} {y - 1.5} 3 3 re f 0 g")
        x = RATING_POSITIONS[pos]
        ops.append(f"{colour[0]} {colour[1]} {colour[2]} rg {x - 4.5} {y - 4.5} 9 9 re f 0 g")
        if arrow is not None:
            ax = RATING_POSITIONS[arrow]
            ops.append(f"{ARROW_RED[0]} {ARROW_RED[1]} {ARROW_RED[2]} rg "
                       f"{ax + 4} {y - 4} m {ax - 4} {y} l {ax + 4} {y + 4} l h f 0 g")
    return ops


def rating_grid() -> tuple[list[dict], list[dict]]:
    rows = [("Equities", 3, LIGHT_GREEN, None), ("Bonds", 1, ORANGE, 2), ("Cash", 2, YELLOW, None)]
    truth = [["Asset Class", "CIO View", "Scale"],
             ["Equities", "Overweight", "○ ○ ○ ● ○ (green)"],
             ["Bonds", "Underweight", "○ ● ◀ ○ ○ (orange)"],
             ["Cash", "Neutral", "○ ○ ● ○ ○ (yellow)"]]
    return [_page(rating_grid_ops(rows), ["Asset Class Weightings"])], [{"page": 1, "rows": truth}]


def plain_prose() -> tuple[list[dict], list[dict]]:
    return [prose_page(120)], []


FIXTURES = {
    "side_by_side_ruled": lambda: side_by_side(True),
    "side_by_side_borderless": lambda: side_by_side(False),
    "shapes_in_cells": shapes_in_cells,
    "merged_header": merged_header,
    "rowspan_body": rowspan_body,
    "multiline_cells": multiline_cells,
    "table_in_prose": table_in_prose,
    "framed_text": framed_text,
    "repeated_header_pages": repeated_header_pages,
    "wide_borderless": wide_borderless,
    "two_column_prose": two_column_prose,
    "dotted_contents": dotted_contents,
    "label_form": label_form,
    "rating_grid": rating_grid,
    "plain_prose": plain_prose,
}
