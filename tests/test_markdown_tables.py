"""Reading and writing Markdown tables (app/core/markdown_tables.py)."""
from __future__ import annotations

from app.core.markdown_tables import (
    GridCell,
    cell_key,
    find_table_blocks,
    parse_html_table,
    render_pipe_table,
    split_pipe_row,
    to_markdown_grid,
)


def _grid(rows: list[list[str | None]], spans: dict | None = None) -> tuple[list[GridCell], int, int]:
    spans = spans or {}
    cells = [
        GridCell(r, c, *spans.get((r, c), (1, 1)), text=text)
        for r, row in enumerate(rows)
        for c, text in enumerate(row)
        if text is not None
    ]
    return cells, len(rows), max(len(r) for r in rows)


# ─── InkDoc's rules for merged cells ─────────────────────────────────────────
def test_a_stacked_header_is_flattened_and_a_rowspan_header_kept_once():
    cells, n, m = _grid(
        [["Region", "Revenue", None, "Costs", None],
         [None, "2023", "2024", "2023", "2024"],
         ["North", "410", "455", "300", "310"]],
        {(0, 0): (2, 1), (0, 1): (1, 2), (0, 3): (1, 2)},
    )
    captions, rows = to_markdown_grid(cells, n, m)
    assert captions == []
    assert rows == [
        ["Region", "Revenue 2023", "Revenue 2024", "Costs 2023", "Costs 2024"],
        ["North", "410", "455", "300", "310"],
    ]


def test_a_rowspan_in_the_body_is_repeated_in_every_row():
    cells, n, m = _grid(
        [["Category", "Item"], ["Fruit", "Apples"], [None, "Pears"]],
        {(1, 0): (2, 1)},
    )
    assert to_markdown_grid(cells, n, m)[1] == [["Category", "Item"], ["Fruit", "Apples"], ["Fruit", "Pears"]]


def test_a_colspan_in_the_body_is_repeated_across_its_columns():
    cells, n, m = _grid([["A", "B", "C"], ["x", "both", None]], {(1, 1): (1, 2)})
    assert to_markdown_grid(cells, n, m)[1] == [["A", "B", "C"], ["x", "both", "both"]]


def test_a_full_width_top_row_is_a_caption_and_a_full_width_body_row_a_section():
    cells, n, m = _grid(
        [["Table 1: Sales", None, None],
         ["Item", "Q1", "Q2"],
         ["Europe", None, None],
         ["Bikes", "3", "4"]],
        {(0, 0): (1, 3), (2, 0): (1, 3)},
    )
    captions, rows = to_markdown_grid(cells, n, m)
    assert captions == ["Table 1: Sales"]
    assert rows == [["Item", "Q1", "Q2"], ["Europe", "", ""], ["Bikes", "3", "4"]]


def test_spans_past_the_grid_are_clipped_and_odd_input_never_raises():
    cells = [GridCell(0, 0, 9, 9, "big"), GridCell(5, 5, 1, 1, "outside")]
    # One cell covering the whole grid is a caption and nothing else.
    assert to_markdown_grid(cells, 2, 2) == (["big"], [])
    assert to_markdown_grid([], 0, 0) == ([], [])
    assert to_markdown_grid([GridCell(0, 0, text="a")], 1, 1) == ([], [["a"]])


def test_cell_text_is_one_line():
    cells, n, m = _grid([["Step", "Text"], ["1", "Unpack the device\nand  check"]])
    assert to_markdown_grid(cells, n, m)[1][1] == ["1", "Unpack the device and check"]


# ─── Writing ─────────────────────────────────────────────────────────────────
def test_render_escapes_pipes_and_pads_columns():
    lines = render_pipe_table([["Name", "Note"], ["a|b", "x"]])
    assert lines[0] == "| Name | Note |"
    assert lines[1] == "| ---- | ---- |"
    assert lines[2] == "| a\\|b | x    |"
    assert split_pipe_row(lines[2]) == ["a|b", "x"]


def test_render_then_read_round_trips():
    rows = [["Region", "Q1"], ["North", "120"], ["South", ""]]
    block = find_table_blocks(render_pipe_table(rows))
    assert len(block) == 1 and block[0].rows == rows


# ─── Reading ─────────────────────────────────────────────────────────────────
def test_finds_pipe_tables_with_and_without_outer_pipes_and_glm_separators():
    lines = [
        "Intro",
        "| A | B |",
        "| :--- | :--- |",
        "| 1 | 2 |",
        "",
        "C | D",
        "--- | ---",
        "3 | 4",
        "Outro",
    ]
    blocks = find_table_blocks(lines)
    assert [(b.start, b.end, b.kind) for b in blocks] == [(1, 4, "pipe"), (5, 8, "pipe")]
    assert blocks[0].rows == [["A", "B"], ["1", "2"]]
    assert blocks[1].rows == [["C", "D"], ["3", "4"]]


def test_a_line_with_a_pipe_but_no_separator_is_not_a_table():
    assert find_table_blocks(["a | b", "c | d"]) == []


def test_html_tables_with_spans_are_read_with_repetition():
    markup = (
        '<table border="1"><tr><th rowspan="2">Region</th><th colspan="2">Revenue</th></tr>'
        "<tr><td>2023</td><td>2024</td></tr><tr><td>North</td><td>4&amp;1</td><td>4<br>55</td></tr></table>"
    )
    assert parse_html_table(markup) == [
        ["Region", "Revenue", "Revenue"],
        ["Region", "2023", "2024"],
        ["North", "4&1", "4 55"],
    ]
    blocks = find_table_blocks(["before", markup, "after"])
    assert len(blocks) == 1 and blocks[0].kind == "html" and blocks[0].n_cols == 3


def test_a_multi_line_html_table_is_one_block():
    lines = ["<table>", "<tr><td>a</td><td>b</td></tr>", "</table>", "next"]
    blocks = find_table_blocks(lines)
    assert (blocks[0].start, blocks[0].end) == (0, 3)
    assert blocks[0].rows == [["a", "b"]]


def test_cell_key_ignores_markdown_and_case():
    assert cell_key("**Total**") == cell_key("total")
    assert cell_key("4.50") == "4 50"
