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
    ([(2, LEFT), (3, RIGHT)], "it doesn't match what GLM-OCR wrote for the page"),
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


def test_a_note_under_a_plain_lines_table_stays():
    page = LOOSE.replace("Dairy Milk 12\n", "Dairy Milk 12\nSource: Ministry of Agriculture, 2023\n")
    out, written, _ = write_scanned_tables(page, _found((2, INVENTORY)))
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [INVENTORY]
    assert "\nSource: Ministry of Agriculture, 2023\n" in out, "only the table's own lines are replaced"
    assert written[0]["tables"] == 1


def test_a_line_with_other_words_inside_the_table_stops_it():
    page = LOOSE.replace("Pears 25\n", "Pears 25\nprices checked by hand\n")
    out, written, details = write_scanned_tables(page, _found((2, INVENTORY)))
    assert out == page and written == []
    assert any("would leave out part of what GLM-OCR wrote there" in line for line in details)


def test_the_same_table_on_two_pages_is_written_on_each():
    page = LOOSE.replace("\n\nCounted on Friday.", "\n\nPage two\n\n" + LOOSE.split("\n\n")[1])
    out, written, _ = write_scanned_tables(page, _found((2, INVENTORY), (3, INVENTORY)))
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [INVENTORY, INVENTORY]
    assert written == [{"page": 2, "tables": 1, "issue": "scanned", "source": "glm_ocr"},
                       {"page": 3, "tables": 1, "issue": "scanned", "source": "glm_ocr"}]


def test_symbols_that_stand_for_letters_are_not_another_script():
    # The micro and ohm signs on the page, Greek mu and omega in the crop: the same letters.
    page = "| Part | Value |\n|---|---|\n| R1 | 4.7 kΩ |\n| C1 | 10 µF |\n| C2 | 22 µF |\n| Note | n/a |"
    crop = [["Part", "Value"], ["R1", "4.7 kΩ"], ["C1", "10 μF"], ["C2", "22 μF"], ["Note", "n/a"]]
    out, written, details = write_scanned_tables(page, _found((2, crop)))
    assert not any("another script" in line for line in details), details


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


def test_pages_the_layout_model_skipped_are_named():
    answer = {"pages": [{"page": 4, "skipped": "rotated", "tables": []}, {"page": 5, "skipped": "missing", "tables": []}],
              "truncated": False, "error": ""}
    found = find_scanned_tables("doc.pdf", [4, 5], lambda path, pages, job: answer)
    assert found.unread_pages == [(4, "rotated")]
    assert write_scanned_tables("text", found)[2] == ["Page 4: not looked at for tables: the page is rotated."]


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


# ─── A scan read cell by cell (an image file): one row per paragraph ─────────
CELLS = [["No", "Name", "Unit", "Price"], ["75", "Дюбелі варіант", "шт", "0,66 778,80"],
         ["76", "Грунтовка", "т", "14292,53 1364,42"], ["77", "Ксилол нафтовий", "т", "11374,17 0,48"]]
BY_CELL = ("Програмний комплекс\n\nNo Name Unit Price\n\n75\nДюбели вариант\nшт\n0,66\n778,80\n\n"
           "76\nГрунтовака\nТ\n14292,53\n1364,42\n\n77\nКсилол нафтовий\nT\n11374,17\n0,48\n\nКінець кошторису")


def test_a_table_written_one_row_per_paragraph_is_rebuilt():
    # The crop's reading spells some words better, has "0.66" for "0,66" and one digit wrong.
    crop = [row[:] for row in CELLS]
    crop[1][3] = "0.66 778.80"
    crop[2][3] = "14292,53 1384,42"
    out, written, _ = write_scanned_tables(BY_CELL, _found((1, crop)))
    assert written == [{"page": 1, "tables": 1, "issue": "scanned", "source": "glm_ocr"}]
    rows = find_table_blocks(out.split("\n"))[0].rows
    assert rows == CELLS, "comma decimals as on the page, and the page's digit where they disagree"
    assert out.startswith("Програмний комплекс\n\n") and out.endswith("\n\nКінець кошторису"), \
        "the lines around the table stay"


def test_a_single_paragraph_still_wins_over_a_run():
    # A table GLM-OCR wrote in one paragraph is matched there, as before; the run is never needed.
    one = "Intro words here\n\nNo Name Unit Price\n75 Alpha шт 12\n76 Beta т 40\n\nKept note"
    reading = [["No", "Name", "Unit", "Price"], ["75", "Alpha", "шт", "12"], ["76", "Beta", "т", "40"]]
    out, written, _ = write_scanned_tables(one, _found((1, reading)))
    assert written and out.startswith("Intro words here\n\n<!-- inkdoc:table") and out.endswith("\n\nKept note")


def test_in_a_run_a_line_of_other_words_between_rows_stops_it():
    note = BY_CELL.replace("\n\n76\n", "\n\nThis note is about something else entirely here\n\n76\n")
    out, written, details = write_scanned_tables(note, _found((1, CELLS)))
    assert out == note and written == []
    assert any("leave out part" in line for line in details)


def test_point_decimals_follow_the_page_only_when_it_writes_commas():
    from app.core.scanned_tables import decimal_marks, page_numbers

    assert decimal_marks([["0.66", "v2.10.6"]], "1,5 2,25 3,75 4,00") == [["0,66", "v2.10.6"]]
    assert decimal_marks([["0.66"]], "1.5 2.25 3,75") == [["0.66"]]
    assert page_numbers([["1384,42", "9999"]], "1364,42 and 1386,42") == [["1384,42", "9999"]], \
        "two close numbers on the page: which one is not known"
    assert page_numbers([["1384,42"]], "1364,42") == [["1364,42"]]


# ─── High detail: a dense table read whole ───────────────────────────────────
DENSE_HEADER = ["Town", "Name", "Copies", "Days", "Width", "Font", "Mat"]
DENSE = [DENSE_HEADER] + [[f"{n} 000", f"Paper {n}", f"{n} 300", "6", "46", "P", "+"] for n in range(1, 7)]


def test_a_row_shifted_by_a_column_is_moved_back():
    from app.core.scanned_tables import align_columns

    shifted = [row[:] for row in DENSE]
    shifted[3] = ["3 000", "3 300", "6", "46", "P", "+", ""]        # the name was dropped: values one left
    shifted[5] = ["", "Paper 5", "5 300", "6", "46", "P", "+"]       # an empty town is not a shift
    fixed, moved = align_columns(shifted)
    assert fixed[3] == ["3 000", "", "3 300", "6", "46", "P", "+"]
    assert fixed[5] == shifted[5] and moved == 1
    assert align_columns(DENSE[:3]) == (DENSE[:3], 0), "too few rows to know the columns"


def test_high_detail_writes_a_table_that_holds_the_page_reading():
    # The page's reading of a dense table: names and few numbers, run together.
    page = "Directory\n\n" + "\n".join(f"Paper {n} {n} 000" for n in range(1, 7)) + "\n\nEnd"
    for detail, written in (("normal", False), ("high", True)):
        found = ScannedTables(path="scan.png", detail=detail, areas=[Area(page=1, bbox=[0, 0, 1, 1], rows=DENSE)])
        out, report, _details = write_scanned_tables(page, found)
        assert bool(report) is written, detail
    assert find_table_blocks(out.split("\n"))[0].rows[1][:3] == ["1 000", "Paper 1", "1 300"]
    assert "<!-- inkdoc:table page=1 source=glm-ocr checked=layout,high-detail -->" in out
    assert out.startswith("Directory\n\n") and out.endswith("\n\nEnd")


def test_a_too_large_reading_is_offered_for_high_detail():
    from app.core.engines.glm_ocr_engine import TableReading
    from app.core.scanned_tables import high_detail_pages, high_detail_seconds

    found = ScannedTables(path="scan.png", areas=[Area(page=2, bbox=[0, 0, 1, 1]), Area(page=3, bbox=[0, 0, 1, 1])])
    replies = iter([TableReading(None, too_large=True, seconds=40.0), TableReading(DENSE, seconds=10.0)])
    read_scanned_tables(found, lambda path, page, bbox: next(replies))
    assert high_detail_pages(found) == [2] and high_detail_seconds(found) == 100.0
    assert found.areas[0].note.startswith("it is too large") and found.areas[1].rows == DENSE


def test_high_detail_never_drops_what_the_page_reading_had():
    page = "Directory\n\n" + "\n".join(f"Paper {n} {n} 000" for n in range(1, 7)) + "\nA note the table does not have\n\nEnd"
    found = ScannedTables(path="scan.png", detail="high", areas=[Area(page=1, bbox=[0, 0, 1, 1], rows=DENSE)])
    out, report, _details = write_scanned_tables(page, found)
    assert out == page and report == []
