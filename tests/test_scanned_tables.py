"""Tables on scanned pages (app/core/scanned_tables.py).

The layout model and GLM-OCR are stand-ins: what the step decides from their
answers is what is tested here.
"""
from __future__ import annotations

import pytest

from app.core.jobs import ConversionCancelledError, JobRegistry
from app.core.markdown_tables import find_table_blocks
from app.core.pdf_probe import probe_pdf
from app.core.scanned_tables import (
    Area,
    ScannedTables,
    find_scanned_tables,
    is_grid,
    read_scanned_tables,
    target_pages,
    write_scanned_tables,
)
from tests._pdf_fixtures import image_page, prose_page, write_pdf

LEFT = [["Region", "Q1"], ["North", "120"], ["South", "98"]]
RIGHT = [["Product", "Units"], ["Alpha", "12"], ["Beta", "7"]]
MERGED = "\n".join([
    "## Quarterly summary", "",
    "| Region | Q1 | Product | Units |", "|---|---|---|---|",
    "| North | 120 | Alpha | 12 |", "| South | 98 | Beta | 7 |", "",
    "Figures are unaudited.",
])
COMMENT = "<!-- inkdoc:table page=2 source=glm-ocr checked=layout,page-reading -->"


def _found(*readings: tuple[int, list[list[str]] | None]) -> ScannedTables:
    return ScannedTables(path="doc.pdf", areas=[
        Area(page=page, bbox=[50.0 + 250 * i, 100.0, 250.0 + 250 * i, 200.0], rows=rows)
        for i, (page, rows) in enumerate(readings)
    ])


# ─── Which pages ─────────────────────────────────────────────────────────────
def test_only_pages_without_a_text_layer_that_glm_ocr_read(tmp_path):
    probe = probe_pdf(str(write_pdf(tmp_path / "mixed.pdf", [prose_page(60), image_page("ccitt"), image_page("ccitt")])))
    assert target_pages(probe, [1, 2, 3]) == [2, 3], "page 1 has its own text: the table check reads it"
    assert target_pages(probe, [3]) == [3], "page 2 was read by another engine"
    assert target_pages(probe, []) == []


def test_a_reading_must_be_a_grid():
    assert is_grid(LEFT)
    assert not is_grid(None) and not is_grid([["only", "a header"]])
    assert not is_grid([["one column"], ["a"], ["b"]])
    assert not is_grid([["a", "b"], ["c"]]), "every row as wide"
    assert not is_grid([["a", "", ""], ["", "", ""], ["", "", "b"]]), "mostly empty"


# ─── Writing ─────────────────────────────────────────────────────────────────
def test_side_by_side_tables_written_as_one_become_two():
    found = _found((2, LEFT), (2, RIGHT))
    out, written, details = write_scanned_tables(MERGED, found)
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [LEFT, RIGHT]
    assert out.count(COMMENT) == 2
    assert out.startswith("## Quarterly summary") and out.endswith("Figures are unaudited.")
    assert written == [{"page": 2, "tables": 2, "issue": "scanned", "source": "glm_ocr"}]
    assert details == ["Page 2: rebuilt 2 tables from GLM-OCR's reading of each table on its own "
                       "(found by the layout model)."]
    assert all(a.written for a in found.areas)


def test_a_stacked_header_glm_ocr_shifted_on_the_page_is_put_right():
    # As GLM-OCR read a scanned page: the year row shifted under the wrong columns.
    page = "\n".join([
        "| Region | Revenue | Costs | Revenue | Costs |", "| :--- | :--- | :--- | :--- | :--- |",
        "| 2023 | 2024 | 2023 | 2024 | 2023 |", "| North | 410 | 455 | 300 | 310 |", "| South | 380 | 390 | 290 | 285 |",
    ])
    crop = [["Region", "Revenue 2023", "Revenue 2024", "Costs 2023", "Costs 2024"],
            ["North", "410", "455", "300", "310"], ["South", "380", "390", "290", "285"]]
    out, written, _ = write_scanned_tables(page, _found((2, crop)))
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [crop], "the same words, put right"
    assert written == [{"page": 2, "tables": 1, "issue": "scanned", "source": "glm_ocr"}]


def test_an_html_table_is_replaced_too():
    html = MERGED.replace(
        "| Region | Q1 | Product | Units |\n|---|---|---|---|\n| North | 120 | Alpha | 12 |\n| South | 98 | Beta | 7 |",
        "<table><tr><td>Region</td><td>Q1</td><td>Product</td><td>Units</td></tr>"
        "<tr><td>North</td><td>120</td><td>Alpha</td><td>12</td></tr>"
        "<tr><td>South</td><td>98</td><td>Beta</td><td>7</td></tr></table>",
    )
    out, written, _ = write_scanned_tables(html, _found((2, LEFT), (2, RIGHT)))
    assert "<table>" not in out and written[0]["tables"] == 2


@pytest.mark.parametrize("readings, note", [
    ([(2, [["Region", "Q1", "Product", "Units"], ["North", "120", "Alpha", "12"], ["South", "98", "Beta", "7"]])],
     "GLM-OCR's table was already right"),
    ([(2, [["Name", "Age"], ["Ann", "31"], ["Bob", "42"]])],
     "it doesn't match what GLM-OCR wrote for the page"),
    ([(2, LEFT)], "writing it would leave out part of what GLM-OCR wrote there"),
    ([(2, LEFT), (3, RIGHT)], "it matches a table on another page too"),
    ([(2, [["Region", "Q1"], ["North", "120"], ["South"]])], "GLM-OCR's reading of it is not a full grid"),
])
def test_a_reading_that_could_be_wrong_changes_nothing(readings, note):
    found = _found(*readings)
    out, written, details = write_scanned_tables(MERGED, found)
    assert out == MERGED and written == []
    assert any(note in line for line in details), details
    assert not any(a.written for a in found.areas)


def test_a_reading_in_another_script_is_not_written():
    # Nine cells of ten agree, so only the script check stands between GLM-OCR's
    # "产品" for "Product" (seen on real crops) and the saved file.
    page = "| Product | Units |\n|---|---|\n| Alpha | 12 |\n| Beta | 7 |\n| Gamma | 30 |\n| Delta | 4 |"
    reading = [["产品", "Units"], ["Alpha", "12"], ["Beta", "7"], ["Gamma", "30"], ["Delta", "4"]]
    out, written, details = write_scanned_tables(page, _found((2, reading)))
    assert out == page and written == []
    assert details == ["Page 2: a table was left as GLM-OCR wrote it: GLM-OCR read it in another script on its own."]
    latin = [["Product", "Units"], ["Alpha", "12"], ["Beta", "7"], ["Gamma", "30"], ["Delta", "4"], ["Echo", "5"]]
    assert write_scanned_tables(page + "\n| Echo | 5 |", _found((2, latin)))[1] == [], "already right"


# As GLM-OCR often reads a scanned table: plain lines of its words.
INVENTORY = [["Category", "Item", "Stock"], ["Fruit", "Apples", "40"], ["Fruit", "Pears", "25"], ["Dairy", "Milk", "12"]]
LOOSE = "Inventory\n\nCategory Item Stock\nFruit Apples 40\nPears 25\nDairy Milk 12\n\nCounted on Friday."


def test_a_table_glm_ocr_wrote_as_plain_lines_becomes_a_table():
    out, written, _ = write_scanned_tables(LOOSE, _found((2, INVENTORY)))
    assert out.startswith("Inventory\n\n" + COMMENT) and out.endswith("\n\nCounted on Friday.")
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [INVENTORY]
    assert written == [{"page": 2, "tables": 1, "issue": "scanned", "source": "glm_ocr"}]


def test_a_paragraph_with_other_words_is_left_alone():
    prose = ("Inventory\n\nThe count on Friday found these: Category Item Stock Fruit Apples 40 Pears 25 Dairy Milk 12,"
             " which matches what the shop recorded for the month before.")
    out, written, details = write_scanned_tables(prose, _found((2, INVENTORY)))
    assert out == prose and written == []
    assert details == ["Page 2: a table was left as GLM-OCR wrote it: writing it would leave out part of what "
                       "GLM-OCR wrote there."]


def test_headings_comments_code_and_contents_are_never_taken_for_a_table():
    contents = [["1", "Introduction", "3"], ["2", "Results by region", "7"], ["3", "Costs", "12"]]
    toc = "Contents\n\n1 Introduction ... 3\n2 Results by region ... 7\n3 Costs ... 12"
    assert write_scanned_tables(toc, _found((2, contents)))[0] == toc, "dot leaders: a contents list"
    for marked in ("## Category Item Stock Fruit Apples 40 Pears 25 Dairy Milk 12",
                   "<!-- Category Item Stock Fruit Apples 40 Pears 25 Dairy Milk 12 -->",
                   "```\nCategory Item Stock\nFruit Apples 40\nPears 25\nDairy Milk 12\n```"):
        assert write_scanned_tables(marked, _found((2, INVENTORY)))[0] == marked, marked


# ─── Finding and reading ─────────────────────────────────────────────────────
def test_the_layout_models_answer_becomes_areas_in_reading_order():
    answer = {"pages": [{"page": 2, "skipped": "", "tables": [
        {"bbox": [50, 100, 250, 200], "score": 0.9}, {"bbox": [300, 100, 500, 200], "score": 0.8}]},
        {"page": 3, "skipped": "rotated", "tables": []}], "truncated": True, "error": ""}
    found = find_scanned_tables("doc.pdf", [2, 3], lambda path, pages, job: answer)
    assert [(a.page, a.bbox[0]) for a in found.areas] == [(2, 50.0), (2, 300.0)]
    assert found.truncated and not found.skipped


@pytest.mark.parametrize("detect", [
    lambda path, pages, job: {"pages": [], "error": "ValueError: the layout model file does not match"},
    lambda path, pages, job: (_ for _ in ()).throw(RuntimeError("worker gone")),
])
def test_a_failing_layout_model_is_a_skip_not_an_error(detect):
    found = find_scanned_tables("doc.pdf", [2], detect)
    assert found.skipped == "the layout model failed" and found.areas == []


def test_readings_stop_at_the_limit_and_failures_are_noted():
    found = _found((2, None), (2, None), (2, None), (3, None))
    answers = iter([LEFT, None, RuntimeError("server down")])

    def read(path, page, bbox):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    read_scanned_tables(found, read, limit=3)
    assert found.areas[0].rows == LEFT and found.areas[0].note == ""
    assert found.areas[1].note == "GLM-OCR read no table there"
    assert found.areas[2].note == "the reading failed"
    assert found.areas[3].note == "more tables than one conversion reads"


def test_no_time_left_means_no_more_readings():
    found = _found((2, None), (2, None))
    read_scanned_tables(found, lambda *a: pytest.fail("not read"), time_limit_s=-1)
    assert {a.note for a in found.areas} == {"there was no time left to read it"}


def test_a_cancelled_job_stops_the_step():
    registry = JobRegistry()
    job = registry.start("scan-cancel-1")
    registry.cancel("scan-cancel-1")
    with pytest.raises(ConversionCancelledError):
        find_scanned_tables("doc.pdf", [2], lambda *a: pytest.fail("not run"), job=job)
    with pytest.raises(ConversionCancelledError):
        read_scanned_tables(_found((2, None)), lambda *a: pytest.fail("not read"), job=job)
