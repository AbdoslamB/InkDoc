"""Comparing an engine's tables with the PDF's, and repairing them (app/core/table_check.py).

Pure: the finder's JSON is written out by hand, so no PDF is involved.
"""
from __future__ import annotations

from app.core.markdown_tables import find_table_blocks, line_tokens, render_pipe_table
from app.core.table_check import (
    LOOSE,
    MERGED,
    MISMATCH,
    MISSING,
    OK,
    PICTURE,
    SHAPES,
    SPLIT,
    check,
    repair,
    same_table,
    summarize,
)

LEFT = [["Region", "Q1"], ["North", "120"], ["South", "98"]]
RIGHT = [["Product", "Units"], ["Alpha", "12"], ["Beta", "7"]]


def _table(rows, *, verified=True, reason="", captions=None, words=None):
    flat = [c for row in rows for c in row] + list(captions or [])
    return {
        "bbox": [0, 0, 1, 1],
        "captions": list(captions or []),
        "rows": rows,
        "source": "pdf_lines",
        "verified": verified,
        "reason": reason,
        "merged_cells": 0,
        "shape_cells": 0,
        "words": words if words is not None else [t for c in flat for t in line_tokens(c)],
    }


def _found(*bands, page=1):
    return {"pages": [{"page": page, "skipped": "", "bands": [dict(b) for b in bands]}]}


def _band(*tables, above="Quarterly summary", below="Figures are unaudited."):
    return {"top": 0, "bottom": 1, "above": above, "below": below, "tables": list(tables)}


def _md(*parts: str) -> str:
    return "\n".join(parts)


MERGED_TABLE = _md(
    "| Region | Q1 | Product | Units |",
    "| --- | --- | --- | --- |",
    "| North | 120 | Alpha | 12 |",
    "| South | 98 | Beta | 7 |",
)


def _run(markdown, found, *, allowed=True):
    lines, results = check(markdown, found)
    out = repair(lines, results) if allowed else markdown
    return out, results


# ─── Classification ──────────────────────────────────────────────────────────
def test_side_by_side_tables_merged_into_one_are_split_again():
    md = _md("Quarterly summary", MERGED_TABLE, "Figures are unaudited.")
    out, results = _run(md, _found(_band(_table(LEFT), _table(RIGHT))))
    assert results[0].issue == MERGED and results[0].repaired
    assert "2 side-by-side tables came out as one" in results[0].message
    blocks = find_table_blocks(out.split("\n"))
    assert [b.rows for b in blocks] == [LEFT, RIGHT]
    assert out.count("<!-- inkdoc:table page=1 source=pdf-lines checked=text-layer -->") == 2
    assert out.startswith("Quarterly summary\n") and out.rstrip().endswith("Figures are unaudited.")


def test_a_right_table_is_left_exactly_as_the_engine_wrote_it():
    md = _md("Quarterly summary", "", "|Region|Q1|", "|-|-|", "|North|120|", "|South|98|", "", "Figures are unaudited.")
    out, results = _run(md, _found(_band(_table(LEFT))))
    assert results[0].issue == OK and not results[0].repaired
    assert out == md


def test_loose_lines_become_a_table():
    md = _md("Quarterly summary", "", "Region", "", "North", "", "South", "", "Q1", "120", "98", "", "Figures are unaudited.")
    out, results = _run(md, _found(_band(_table(LEFT))))
    assert results[0].issue == LOOSE and results[0].repaired
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [LEFT]
    assert "Figures are unaudited." in out and "Quarterly summary" in out


def test_a_picture_placeholder_is_replaced_by_the_table():
    md = _md("## Quarterly summary", "", "<!-- image -->", "", "Figures are unaudited.")
    out, results = _run(md, _found(_band(_table(LEFT))))
    assert results[0].issue == PICTURE and results[0].repaired
    assert "<!-- image -->" not in out
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [LEFT]


def test_a_dropped_table_is_put_back_between_its_neighbours():
    md = _md("Quarterly summary", "", "Figures are unaudited.")
    out, results = _run(md, _found(_band(_table(LEFT))))
    assert results[0].issue == MISSING and results[0].repaired
    lines = out.split("\n")
    assert lines.index("Quarterly summary") < next(i for i, ln in enumerate(lines) if ln.startswith("| Region"))
    assert out.rstrip().endswith("Figures are unaudited.")


def test_a_table_at_the_end_of_the_page_uses_only_the_line_above():
    md = _md("Quarterly summary", "", "<!-- image -->", "")
    out, results = _run(md, _found(_band(_table(LEFT), below="")))
    assert results[0].issue == PICTURE and results[0].repaired


def test_shapes_left_out_are_put_back():
    expected = [["Task", "Status"], ["Design", "[green shape]"], ["Build", "[red shape]"]]
    md = _md("Quarterly summary", "| Task | Status |", "|---|---|", "| Design | |", "| Build | |", "Figures are unaudited.")
    table = _table(expected, words=["task", "status", "design", "build"])
    out, results = _run(md, _found(_band(table)))
    assert results[0].issue == SHAPES and results[0].repaired
    assert "[green shape]" in out


def test_split_and_mismatch_are_reported():
    two = _md("Quarterly summary", "| Region | Q1 |", "|---|---|", "| North | 120 |", "",
              "| Region | Q1 |", "|---|---|", "| South | 98 |", "Figures are unaudited.")
    _, results = _run(two, _found(_band(_table(LEFT, words=["region", "q1", "north", "120", "south", "98", "region", "q1"])))
                      , allowed=False)
    assert results[0].issue == SPLIT
    wrong = _md("Quarterly summary", "| Region | Q1 |", "|---|---|", "| North | 98 |", "| South | 120 |", "Figures are unaudited.")
    _, results = _run(wrong, _found(_band(_table(LEFT))), allowed=False)
    assert results[0].issue == MISMATCH
    assert results[0].message == "Page 1: a table's rows or columns don't match the PDF."


# ─── Safety ──────────────────────────────────────────────────────────────────
def test_an_unverified_table_is_reported_and_never_written():
    md = _md("Quarterly summary", MERGED_TABLE, "Figures are unaudited.")
    out, results = _run(md, _found(_band(_table(LEFT), _table(RIGHT, verified=False, reason="some of the table's text is outside its cells"))))
    assert results[0].issue == MERGED and not results[0].repaired
    assert results[0].not_repaired == "some of the table's text is outside its cells"
    assert out == md


def test_nothing_but_the_tables_own_words_is_ever_replaced():
    # The table's words are interleaved with a sentence that is not part of it:
    # the band can't be located without cutting that sentence, so it is left alone.
    md = _md("Quarterly summary", "Region North South", "This sentence is not in the table.", "Q1 120 98",
             "Figures are unaudited.")
    out, results = _run(md, _found(_band(_table(LEFT))))
    assert not results[0].repaired
    assert out == md


def test_a_table_cut_by_the_slice_is_not_touched():
    # One Markdown table holds this band's rows and another band's: replacing
    # only part of it would leave half a table behind.
    md = _md("Quarterly summary", "| Region | Q1 |", "|---|---|", "| North | 120 |", "| South | 98 |",
             "| Alpha | 12 |", "| Beta | 7 |", "Figures are unaudited.")
    out, results = _run(md, _found(_band(_table(LEFT))))
    assert not results[0].repaired
    assert out == md


def test_a_header_shared_by_tables_on_several_pages_does_not_confuse_them():
    p1 = [["Name", "Role"], ["Ada", "Engineer"]]
    p2 = [["Name", "Role"], ["Alan", "Mathematician"]]
    md = _md("Staff list Staff list (continued)", "", "<!-- image -->", "", *render_pipe_table(p2))
    found = {"pages": [
        {"page": 1, "skipped": "", "bands": [_band(_table(p1), above="Staff list", below="")]},
        {"page": 2, "skipped": "", "bands": [_band(_table(p2), above="Staff list (continued)", below="")]},
    ]}
    out, results = _run(md, found)
    assert [r.issue for r in results] == [PICTURE, OK]
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [p1, p2]


def test_a_one_word_anchor_counts_only_when_it_is_unique():
    found = _found(_band(_table(LEFT), above="Inventory", below=""))
    md_unique = _md("Inventory", "", "<!-- image -->")
    assert _run(md_unique, found)[1][0].repaired
    md_twice = _md("Inventory", "", "<!-- image -->", "", "Inventory notes follow.")
    assert not _run(md_twice, found)[1][0].repaired


def test_side_by_side_tables_split_apart_by_a_note_are_found_one_by_one():
    # Docling's reading of a borderless pair: every cell its own paragraph, and
    # the note under the tables placed between them.
    md = _md("Quarterly summary", "", "Region Q1", "", "North", "120", "South", "98", "",
             "Figures are unaudited.", "", "Product Units", "Alpha", "12", "Beta", "7")
    out, results = _run(md, _found(_band(_table(LEFT), _table(RIGHT))))
    assert [(r.issue, r.repaired) for r in results] == [(LOOSE, True), (LOOSE, True)]
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [LEFT, RIGHT]
    assert "Figures are unaudited." in out  # the note is kept, where the engine put it


def test_two_tables_put_back_at_the_same_line_keep_their_order():
    # Two bands of one page, both dropped, both anchored right before the note.
    first = [["Region", "Q1"], ["North", "120"]]
    second = [["Product", "Units"], ["Alpha", "12"]]
    md = _md("Figures are unaudited.")
    found = _found(_band(_table(first), above="", below="Figures are unaudited."),
                   _band(_table(second), above="", below="Figures are unaudited."))
    out, results = _run(md, found)
    assert [r.repaired for r in results] == [True, True]
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [first, second]


def test_a_table_whose_step_is_off_is_left_as_the_engine_wrote_it():
    found = _found(_band(_table(LEFT), _table(RIGHT)))
    lines, results = check(MERGED_TABLE, found)
    assert repair(lines, results, kinds={"rating_grids"}) == MERGED_TABLE
    assert not results[0].repaired and results[0].not_repaired == "that step is switched off"
    lines, results = check(MERGED_TABLE, found)
    assert repair(lines, results, kinds={"lines"}) != MERGED_TABLE and results[0].repaired


def test_check_only_never_changes_the_text():
    md = _md("Quarterly summary", MERGED_TABLE, "Figures are unaudited.")
    out, results = _run(md, _found(_band(_table(LEFT), _table(RIGHT))), allowed=False)
    assert out == md and results[0].issue == MERGED


# ─── Comparing ───────────────────────────────────────────────────────────────
def test_a_one_word_difference_in_a_long_cell_is_still_the_same_table_but_never_rebuilt():
    long_cell = ("Save over $100 billion annually through grid optimization, forecasting and operational "
                 "efficiency improvements in power plants. Scaled applications could avoid up to 1.5 Gt of "
                 "CO2 by 2035")
    table = [["Area", "Benefit"], ["Electrons", long_cell]]
    engine = long_cell.replace("CO2", "CO ₂")  # a subscript read apart
    md = _md("Quarterly summary", "| Area | Benefit |", "|---|---|", f"| Electrons | {engine} |",
             "Figures are unaudited.")
    out, results = _run(md, _found(_band(_table(table))))
    assert results[0].issue == OK and out == md
    # Numbers and short cells must match exactly.
    assert not same_table([["A", "B"], ["x", "120"]], [["A", "B"], ["x", "12"]])
    # A table found only roughly is compared, never replaced.
    wrong = _md("Quarterly summary", "| Area | Benefit |", "|---|---|", f"| Electrons | {engine} extra |",
                "| Other | row |", "Figures are unaudited.")
    out, results = _run(wrong, _found(_band(_table(table))))
    assert not results[0].repaired and out == wrong


def test_same_table_accepts_stacked_headers_but_not_a_blank_merged_header():
    expected = [["Region", "Revenue 2023", "Revenue 2024"], ["North", "410", "455"]]
    stacked = [["Region", "Revenue", "Revenue"], ["", "2023", "2024"], ["North", "410", "455"]]
    docling = [["Region", "Revenue - 2023", "Revenue - 2024"], ["North", "410", "455"]]
    blank = [["Region", "Revenue", ""], ["", "2023", "2024"], ["North", "410", "455"]]
    assert same_table(expected, stacked)
    assert same_table(expected, docling)
    assert not same_table(expected, blank)
    assert not same_table(expected, [["Region", "Revenue 2023"], ["North", "410"]])


# ─── Summary ─────────────────────────────────────────────────────────────────
def test_summary_lists_what_was_rebuilt_and_what_was_left():
    md = _md("Quarterly summary", MERGED_TABLE, "Figures are unaudited.")
    lines, results = check(md, _found(_band(_table(LEFT), _table(RIGHT))))
    repair(lines, results)
    summary = summarize(results, [1], repair_allowed=True)
    data = summary.to_dict()
    assert data["tables_found"] == 2 and data["warning"] is False
    assert data["repaired"] == [{"page": 1, "tables": 2, "issue": "merged", "source": "pdf_lines"}]
    assert any("rebuilt 2 tables from the PDF's ruling lines" in line for line in data["details"])

    lines, results = check(md, _found(_band(_table(LEFT), _table(RIGHT))))
    summary = summarize(results, [1], repair_allowed=False)
    assert summary.warning is True
    assert summary.message == "Page 1: 2 side-by-side tables came out as one."
    assert summary.issues[0]["why"] == "the result was only checked"
