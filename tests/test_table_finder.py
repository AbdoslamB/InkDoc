"""Finding a PDF's tables from the PDF itself (app/core/table_finder.py)."""
from __future__ import annotations

import pytest

from app.core.table_finder import color_name, find_tables, shape_label
from tests._pdf_fixtures import drawn_page, text_op, write_pdf
from tests._table_fixtures import FIXTURES, grid_table, rating_grid, side_by_side

# Borderless tables have no ruling lines: the line strategy finds nothing there,
# and they are handled from the engine's own table (split_merged_table).
RULED = [name for name in FIXTURES if "borderless" not in name and "rating" not in name]


def _tables(result: dict) -> list[dict]:
    return [
        {"page": p["page"], "rows": t["rows"]}
        for p in result["pages"]
        for band in p["bands"]
        for t in band["tables"]
        if t["source"] == "pdf_lines"
    ]


@pytest.mark.parametrize("name", RULED)
def test_every_ruled_fixture_comes_out_as_its_truth(name, tmp_path):
    pages, truth = FIXTURES[name]()
    pdf = write_pdf(tmp_path / f"{name}.pdf", pages)
    result = find_tables(str(pdf), list(range(1, len(pages) + 1)))
    assert result["error"] == ""
    assert _tables(result) == truth
    for page in result["pages"]:
        for band in page["bands"]:
            assert all(t["verified"] for t in band["tables"]), band


def test_side_by_side_tables_share_one_band_with_anchors(tmp_path):
    pages, _ = side_by_side(True)
    pdf = write_pdf(tmp_path / "pair.pdf", pages)
    page = find_tables(str(pdf), [1])["pages"][0]
    assert len(page["bands"]) == 1
    band = page["bands"][0]
    assert [t["rows"][0][0] for t in band["tables"]] == ["Region", "Product"]  # left to right
    assert band["above"] == "Quarterly summary"
    assert band["below"].startswith("Figures are unaudited")
    words = band["tables"][0]["words"]
    assert "region" in words and "product" not in words


def test_text_outside_the_cells_makes_a_table_unverified(tmp_path):
    # An L-shaped table: no cell at the bottom right, but a word drawn there.
    ops = grid_table(50, 700, [["A1", "B1", "C1"], ["A2", "B2", "C2"], ["A3", "B3", None]], [60, 60, 60])
    ops.append(text_op(174, 647, "stray"))
    pdf = write_pdf(tmp_path / "l.pdf", [drawn_page(ops)])
    table = find_tables(str(pdf), [1])["pages"][0]["bands"][0]["tables"][0]
    assert table["verified"] is False
    assert "outside its cells" in table["reason"]


def test_a_mostly_empty_grid_is_not_trusted_and_a_chart_grid_is_no_table(tmp_path):
    # 3 of 9 cells with text: a form, reported but never used.
    rows = [["Name", "Date", "Signed"], ["", "", ""], ["", "", ""]]
    pdf = write_pdf(tmp_path / "form.pdf", [drawn_page(grid_table(50, 700, rows, [80, 80, 80]))])
    table = find_tables(str(pdf), [1])["pages"][0]["bands"][0]["tables"][0]
    assert table["verified"] is False and table["reason"] == "most cells are empty"
    # 1 of 9: a chart's gridlines, not a table at all.
    rows = [["Axis", "", ""], ["", "", ""], ["", "", ""]]
    pdf = write_pdf(tmp_path / "chart.pdf", [drawn_page(grid_table(50, 700, rows, [80, 80, 80]))])
    assert find_tables(str(pdf), [1])["pages"][0]["bands"] == []


def test_rotated_pages_are_skipped(tmp_path):
    pages, _ = side_by_side(True)
    pages[0]["rotate"] = 90
    pdf = write_pdf(tmp_path / "rotated.pdf", pages)
    assert find_tables(str(pdf), [1])["pages"] == [{"page": 1, "skipped": "rotated", "bands": []}]



def test_pages_drawn_too_densely_are_skipped_before_the_table_search(tmp_path, monkeypatch):
    # pdfplumber compares every vertical edge with every horizontal one, so a
    # crafted page could hold the check for hours; such a page is skipped.
    import app.core.table_finder as finder

    pages, _ = side_by_side(True)
    pdf = write_pdf(tmp_path / "dense.pdf", pages)
    assert finder.find_tables(str(pdf), [1])["pages"][0]["bands"], "an ordinary table page is read"

    monkeypatch.setattr(finder, "MAX_PAGE_EDGES", 3)
    assert finder.find_tables(str(pdf), [1])["pages"] == [{"page": 1, "skipped": "too_complex", "bands": []}]
    monkeypatch.setattr(finder, "MAX_PAGE_EDGES", 10_000)
    monkeypatch.setattr(finder, "MAX_PAGE_CHARS", 3)
    assert finder.find_tables(str(pdf), [1])["pages"][0]["skipped"] == "too_complex"


def test_never_raises_and_respects_its_limits(tmp_path):
    missing = find_tables(str(tmp_path / "nope.pdf"), [1])
    assert missing["pages"] == [] and missing["error"]
    pages, _ = side_by_side(True)
    pdf = write_pdf(tmp_path / "pair.pdf", pages)
    assert find_tables(str(pdf), [7, 0, -3])["pages"] == []  # pages that don't exist
    assert find_tables(str(pdf), [1], time_budget_s=-1)["truncated"] is True


# ─── Borderless tables an engine merged ──────────────────────────────────────
MERGED_ROWS = [
    ["Region", "Q1", "Q2", "Product", "Units", "Price"],
    ["North", "120", "135", "Alpha", "12", "4.50"],
    ["South", "98", "101", "Beta", "7", "9.99"],
    ["East", "143", "150", "Gamma", "30", "1.25"],
]


def test_a_merged_borderless_pair_is_split_at_the_pdf_gutter(tmp_path):
    pages, truth = FIXTURES["side_by_side_borderless"]()
    pdf = write_pdf(tmp_path / "pair.pdf", pages)
    result = find_tables(str(pdf), [], hints={1: [MERGED_ROWS]})
    bands = result["pages"][0]["bands"]
    assert len(bands) == 1
    tables = bands[0]["tables"]
    assert [t["rows"] for t in tables] == [t["rows"] for t in truth]
    assert all(t["source"] == "pdf_words" and t["verified"] for t in tables)
    assert bands[0]["above"] == "Quarterly summary"


def test_a_wide_table_without_a_gutter_is_not_split(tmp_path):
    pages, truth = FIXTURES["wide_borderless"]()
    pdf = write_pdf(tmp_path / "wide.pdf", pages)
    assert find_tables(str(pdf), [], hints={1: [truth[0]["rows"]]})["pages"][0]["bands"] == []


def test_a_split_must_keep_every_column_on_one_side(tmp_path):
    pages, _ = FIXTURES["side_by_side_borderless"]()
    pdf = write_pdf(tmp_path / "pair.pdf", pages)
    # The engine put a right-hand column between left-hand ones: no clean split.
    shuffled = [[r[0], r[3], r[1], r[2], r[4], r[5]] for r in MERGED_ROWS]
    assert find_tables(str(pdf), [], hints={1: [shuffled]})["pages"][0]["bands"] == []
    # A table whose words aren't on the page is left alone too.
    other = [["a", "b", "c", "d"], ["1", "2", "3", "4"]]
    assert find_tables(str(pdf), [], hints={1: [other]})["pages"][0]["bands"] == []


# ─── Borderless tables from word positions (Thorough) ────────────────────────
def _word_tables(result: dict) -> list[dict]:
    return [t for p in result["pages"] for b in p["bands"] for t in b["tables"] if t["source"] == "pdf_words"]


@pytest.mark.parametrize("name", list(FIXTURES))
def test_word_positions_find_borderless_tables_and_nothing_else(name, tmp_path):
    pages, truth = FIXTURES[name]()
    pdf = write_pdf(tmp_path / f"{name}.pdf", pages)
    found = _word_tables(find_tables(str(pdf), list(range(1, len(pages) + 1)), deep=True))
    expected = [t["rows"] for t in truth] if "borderless" in name else []
    assert [t["rows"] for t in found] == expected
    for table in found:
        assert table["verified"] is False and table["confirm"] is True, "never trusted on its own"


def test_word_tables_need_the_deep_flag(tmp_path):
    pages, _ = FIXTURES["side_by_side_borderless"]()
    pdf = write_pdf(tmp_path / "pair.pdf", pages)
    assert _word_tables(find_tables(str(pdf), [1])) == []


# ─── Detectors: each Thorough step asks for only what it uses ────────────────
def _sources(result: dict) -> list[tuple[str, str]]:
    return [(t["source"], t["kind"]) for p in result["pages"] for b in p["bands"] for t in b["tables"]]


def test_each_detector_finds_only_its_own(tmp_path):
    pages, _ = FIXTURES["side_by_side_borderless"]()
    pair = str(write_pdf(tmp_path / "pair.pdf", pages))
    words = find_tables(pair, [1], detectors=["borderless_tables"])
    assert _sources(words) == [("pdf_words", "borderless_tables")] * 2
    assert "segments" not in words["pages"][0]
    segments = find_tables(pair, [1], detectors=["segments"])
    assert _sources(segments) == [] and segments["pages"][0]["segments"]
    assert find_tables(pair, [1], detectors=["not_a_detector"])["pages"][0]["bands"] == []
    # deep is every detector at once.
    every = find_tables(pair, [1], detectors=["rating_grids", "borderless_tables", "segments"])
    deep = find_tables(pair, [1], deep=True)
    assert {k: v for k, v in every.items() if k != "elapsed_ms"} == {k: v for k, v in deep.items() if k != "elapsed_ms"}

    grid_pages, _ = rating_grid()
    grid = str(write_pdf(tmp_path / "rating.pdf", grid_pages))
    assert ("pdf_shapes", "rating_grids") in _sources(find_tables(grid, [1], detectors=["rating_grids"]))
    assert ("pdf_shapes", "rating_grids") not in _sources(find_tables(grid, [1], detectors=["borderless_tables"]))


def test_every_table_says_which_kind_it_is(tmp_path):
    # The kind decides which step may write it (app/core/thorough_passes.py).
    pages, _ = FIXTURES["table_in_prose"]()
    ruled = find_tables(str(write_pdf(tmp_path / "ruled.pdf", pages)), [1])
    assert {kind for _source, kind in _sources(ruled)} == {"lines"}
    pages, _ = FIXTURES["side_by_side_borderless"]()
    split = find_tables(str(write_pdf(tmp_path / "pair.pdf", pages)), [], hints={1: [MERGED_ROWS]})
    assert _sources(split) == [("pdf_words", "hint_split")] * 2


def test_a_wrapped_cell_continues_its_row(tmp_path):
    rows = [["Step", "Action", "Owner"], ["1", "Unpack the device", "Ops"], ["2", "Connect it", "IT"]]
    ops = grid_table(50, 700, rows, [60, 150, 80], ruled=False)
    # Row 1 (top at 680) has its first line at 667; its middle cell wraps to 656.
    ops.append(text_op(114, 700 - 20 - 13 - 11, "and check the box"))
    pdf = write_pdf(tmp_path / "wrap.pdf", [drawn_page(ops)])
    found = _word_tables(find_tables(str(pdf), [1], deep=True))
    assert [t["rows"] for t in found] == [[
        ["Step", "Action", "Owner"], ["1", "Unpack the device and check the box", "Ops"], ["2", "Connect it", "IT"],
    ]]


@pytest.mark.parametrize(
    "color, name",
    [
        ((0, 0.6, 0), "green"), ((0.8, 0, 0), "red"), ((0.95, 0.75, 0), "yellow"), ((1, 0.55, 0), "orange"),
        ((0.2, 0.3, 0.8), "blue"), ((0.5, 0.1, 0.7), "purple"), (0.5, "grey"), ((0,), "black"),
        ((1, 1, 1), "white"), ((0, 0, 0, 1), "black"), ((0, 1, 1, 0), "red"), ((0.55, 0.3, 0.1), "brown"),
        ("pattern", ""), (None, ""), ((1, 2), ""),
    ],
)
def test_color_names(color, name):
    assert color_name(color) == name


def test_shape_labels_say_what_is_drawn():
    assert shape_label(["green"]) == "[green shape]"
    assert shape_label(["blue"] * 3) == "[3 blue shapes]"
    assert shape_label(["red", "green"]) == "[2 shapes: red, green]"
    assert shape_label([""]) == "[shape]"
    assert shape_label([], images=1) == "[image]"
    assert shape_label(["red"], images=2) == "[red shape] [2 images]"
    assert shape_label([]) == ""
