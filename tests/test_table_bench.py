"""The table benchmark's scoring (scripts/table_bench.py): pure, no engines."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.table_bench as tb  # noqa: E402
from app.core.markdown_tables import render_pipe_table  # noqa: E402

LEFT = [["Region", "Q1"], ["North", "120"], ["South", "98"]]
RIGHT = [["Product", "Units"], ["Alpha", "12"], ["Beta", "7"]]
TRUTH = [{"page": 1, "rows": LEFT}, {"page": 1, "rows": RIGHT}]


def _md(*tables):
    return "\n\n".join("\n".join(render_pipe_table(t)) for t in tables)


def test_perfect_output_scores_full_marks():
    s = tb.score(TRUTH, _md(LEFT, RIGHT), source_words={"region", "q1", "north", "120", "south", "98",
                                                         "product", "units", "alpha", "12", "beta", "7"})
    assert s == {"expected": 2, "found": 2, "exact": 2, "cells_hit": 12, "cells_total": 12,
                 "count_right": True, "invented_cells": 0}


def test_a_merged_pair_scores_no_exact_table_and_a_wrong_count():
    merged = [a + b for a, b in zip(LEFT, RIGHT, strict=True)]
    s = tb.score(TRUTH, _md(merged))
    assert s["exact"] == 0 and s["found"] == 1 and not s["count_right"]
    # The left table's cells sit in the merged table's first columns, so they
    # count; the right table is consumed and scores nothing.
    assert s["cells_hit"] == 6 and s["cells_total"] == 12


def test_invented_cells_are_words_the_pdf_does_not_have():
    s = tb.score([{"page": 1, "rows": LEFT}], _md([["Region", "Q1"], ["North", "120"], ["产品", "98"]]),
                 source_words={"region", "q1", "north", "120", "south", "98"})
    assert s["invented_cells"] == 1
    # Shape labels describe what is drawn; they are not invented text.
    s = tb.score([], _md([["Task", "Status"], ["Build", "[red shape]"], ["Test", "[3 blue shapes]"]]),
                 source_words={"task", "status", "build", "test"})
    assert s["invented_cells"] == 0


def test_a_stacked_header_is_not_penalised():
    expected = [["Region", "Revenue 2023"], ["North", "410"]]
    stacked = [["Region", "Revenue"], ["", "2023"], ["North", "410"]]
    assert tb.cell_accuracy(expected, stacked) == (4, 4)


def test_a_scan_copy_has_no_text_layer(tmp_path):
    from app.core.pdf_probe import probe_pdf
    from tests._pdf_fixtures import write_pdf
    from tests._table_fixtures import FIXTURES

    pages, _ = FIXTURES["repeated_header_pages"]()
    original = write_pdf(tmp_path / "doc.pdf", pages)
    import pypdfium2 as pdfium

    scan = tb.scan_copy(original, tmp_path / "scan.pdf")
    probe = probe_pdf(str(scan))
    assert probe.page_count == 2 and all(p.image_only and p.char_count == 0 for p in probe.pages)
    doc = pdfium.PdfDocument(str(scan))
    try:
        assert [round(v) for v in doc[0].get_size()] == [612, 792], "same page size as the original"
    finally:
        doc.close()


def test_summary_totals():
    rows = [tb.score(TRUTH, _md(LEFT, RIGHT)), tb.score(TRUTH, _md(LEFT))]
    s = tb.summarize(rows)
    assert s["documents"] == 2 and s["tables"] == 4 and s["exact"] == 3
    assert s["count_right_pct"] == 50.0
