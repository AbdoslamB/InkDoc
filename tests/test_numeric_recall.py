"""Numeric recall (scripts/numeric_recall.py): the product's numbers, placed losses, zones."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.numeric_recall as nr  # noqa: E402
from app.core.quality_check import normalize_text, tokenize  # noqa: E402

SAMPLES = [
    "Revenue 1,234.56 in 2023-24\r\nnext line",
    "soft­hyphen 12­34 and pdfium co￾\r\noperation 5\x026",
    "ligature ﬁnance, x² + y², １２３ full width",
    "control\x07chars 7\x0b8 and a decomposed été 9",
    "half ½ and 3⁄4 and ① circled",
    "Hangul 각 42",
]


@pytest.mark.parametrize("raw", SAMPLES)
def test_mapped_normalize_is_normalize_text(raw):
    text, src = nr.mapped_normalize(raw)
    assert text == normalize_text(raw)
    assert len(src) == len(text) and all(0 <= i < len(raw) for i in src)


@pytest.mark.parametrize("raw", [*SAMPLES, "ΑΣ 12 İ 34"])
def test_its_numbers_are_the_missing_text_checks(raw):
    # Final sigma and dotted capitals lower differently per character; numbers never do.
    text, _ = nr.mapped_normalize(raw)
    assert [t for t, _s, _e in nr._digit_tokens(text)] == [t for t in tokenize(normalize_text(raw)) if t.isdigit()]


def test_a_number_maps_back_to_its_raw_characters():
    raw = "Total １２ units"
    text, src = nr.mapped_normalize(raw)
    (tok, s, e), = nr._digit_tokens(text)
    assert tok == "12" and raw[src[s]:src[e - 1] + 1] == "１２"


def _pdf(*items):
    return [nr.PdfNumber(value, page, None, zone) for value, zone, page in
            ((i[0], i[1], i[2] if len(i) > 2 else 1) for i in items)]


def _md(*values):
    return [nr.MdNumber(v) for v in values]


def test_a_repeated_number_is_charged_where_its_neighbours_went_missing():
    pdf = _pdf(("10", "prose"), ("0", "table_ruled"), ("20", "table_ruled"), ("0", "table_ruled"),
               ("30", "prose"), ("0", "prose"), ("40", "prose"))
    r = nr.compare(pdf, _md("10", "0", "20", "0", "30", "40"))
    assert r["missing"] == 1 and r["missing_by_zone"] == {"prose": 1}
    assert r["recall"] == round(6 / 7, 4) and r["moved"] == 0


def test_a_dropped_column_and_a_misread_digit():
    pdf = _pdf(("120", "table_ruled"), ("135", "table_ruled"), ("98", "table_ruled"), ("101", "table_ruled"),
               ("143", "table_ruled"), ("150", "table_ruled"))
    r = nr.compare(pdf, _md("120", "98", "143", "158"))
    assert r["missing_by_kind"] == {"changed": 1, "dropped": 2}
    changed = [e for e in r["examples"] if e["kind"] == "changed"]
    assert changed[0]["value"] == "150" and changed[0]["became"] == ["158"]
    assert r["extra"] == 1 and r["precision"] == 0.75


def test_numbers_out_of_order_cost_no_recall():
    pdf = _pdf(("1", "prose"), ("2", "prose"), ("3", "prose"), ("7", "prose"), ("8", "prose"), ("9", "prose"))
    r = nr.compare(pdf, _md("7", "8", "9", "1", "2", "3"))
    assert r["recall"] == 1.0 and r["missing"] == 0 and r["moved"] == 3 and r["in_order"] == 0.5


def test_page_numbers_never_count_against_recall():
    pdf = _pdf(("5", "prose"), ("6", "prose"), ("1", "margin"), ("7", "prose", 2), ("2", "margin", 2))
    r = nr.compare(pdf, _md("5", "6", "7", "2"))
    assert r["numbers"] == 3 and r["recall"] == 1.0 and r["margin_missing"] == 1 and r["extra"] == 0


def test_precision_means_nothing_beside_a_page_without_text():
    info = nr.PdfNumbers(numbers=_pdf(("5", "prose")), pages=2, no_text_pages=[2])
    assert nr.compare(info, _md("5", "77"))["precision"] is None


def test_markup_is_not_text():
    md = "<!-- inkdoc:table page=3 source=pdf-lines -->\n![chart](img/fig2024.png) [see](https://x.org/a/99)\nvalue 42"
    assert [m.value for m in nr.markdown_numbers(md)] == ["42"]


def test_zones():
    tables = [((100.0, 200.0, 300.0, 300.0), "table_borderless")]
    images = [(100.0, 400.0, 300.0, 500.0)]
    assert nr.zone_of((150, 10, 160, 18), 792, tables, images) == "margin"
    assert nr.zone_of((150, 250, 160, 258), 792, tables, images) == "table_borderless"
    assert nr.zone_of((150, 450, 160, 458), 792, tables, images) == "figure"
    assert nr.zone_of((150, 600, 160, 608), 792, tables, images) == "prose"
    assert nr.zone_of(None, 792, tables, images) == "unknown"


def test_a_pdfs_numbers_have_boxes_and_zones(tmp_path):
    from tests._pdf_fixtures import write_pdf
    from tests._table_fixtures import side_by_side

    pages, _ = side_by_side()
    pdf = write_pdf(tmp_path / "pair.pdf", pages)
    found = {"pages": [{"page": 1, "bands": [{"tables": [{"bbox": [0, 0, 612, 792], "kind": "lines"}]}]}]}
    info = nr.pdf_numbers(pdf, found)
    assert info.no_positions == [] and info.no_text_pages == []
    values = [n.value for n in info.numbers]
    assert values[:6] == ["120", "135", "98", "101", "143", "150"]
    assert all(n.bbox is not None and n.zone in ("table_ruled", "margin") for n in info.numbers)
    first = info.numbers[0]
    assert "North 120" in first.context and first.bbox[0] < first.bbox[2] and first.bbox[1] < first.bbox[3]
    report = nr.compare(info, nr.markdown_numbers("North 120 135"))
    assert report["missing"] == report["numbers"] - 2
    assert "found" in nr.summary_line(report)
