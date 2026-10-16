"""The table stage end to end: probe, finder, check, repair (app/core/table_stage.py)."""
from __future__ import annotations

import asyncio

import pytest

from app.core.jobs import ConversionCancelledError, JobRegistry
from app.core.markdown_tables import find_table_blocks
from app.core.pdf_probe import probe_pdf
from app.core.table_finder import find_tables
from app.core.table_stage import (
    checks_tables,
    confirm_tables,
    finish_tables,
    grids_agree,
    inspect_tables,
    normalize_level,
    pending_confirmations,
    run_table_stage,
    wide_table_hints,
)
from tests._pdf_fixtures import write_pdf
from tests._table_fixtures import FIXTURES, side_by_side

MERGED = (
    "Quarterly summary\n"
    "| Region | Q1 | Q2 | Product | Units | Price |\n"
    "| --- | --- | --- | --- | --- | --- |\n"
    "| North | 120 | 135 | Alpha | 12 | 4.50 |\n"
    "| South | 98 | 101 | Beta | 7 | 9.99 |\n"
    "| East | 143 | 150 | Gamma | 30 | 1.25 |\n"
    "Figures are unaudited and subject to revision.\n"
)


def _in_process(path, pages, _job, hints=None, deep=False):
    return find_tables(path, pages, hints=hints, deep=deep)


def _pair(tmp_path):
    pages, truth = side_by_side(True)
    pdf = write_pdf(tmp_path / "pair.pdf", pages)
    return str(pdf), truth


# ─── Probe ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name, candidate", [
    ("side_by_side_ruled", True), ("shapes_in_cells", True), ("merged_header", True),
    ("table_in_prose", True), ("framed_text", False), ("plain_prose", False),
    ("side_by_side_borderless", False),
])
def test_the_probe_flags_only_pages_with_drawn_tables(name, candidate, tmp_path):
    pages, _ = FIXTURES[name]()
    probe = probe_pdf(write_pdf(tmp_path / f"{name}.pdf", pages))
    assert (probe.table_pages == [1]) is candidate


# ─── Levels ──────────────────────────────────────────────────────────────────
def test_levels():
    assert normalize_level("balanced") == "balanced"
    assert normalize_level("BALANCED") == "fast" and normalize_level(None) == "fast"
    assert not checks_tables("fast") and checks_tables("balanced") and checks_tables("thorough")


# ─── The stage ───────────────────────────────────────────────────────────────
def test_repair_rebuilds_the_merged_pair(tmp_path):
    pdf, truth = _pair(tmp_path)
    out, summary = run_table_stage(pdf, MERGED, probe_pdf(pdf), repair_allowed=True, find=_in_process)
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [t["rows"] for t in truth]
    assert summary.checked and summary.tables_found == 2 and not summary.warning
    assert summary.repaired[0]["issue"] == "merged"


def test_a_merged_borderless_pair_is_split_through_the_engines_own_table(tmp_path):
    pages, truth = FIXTURES["side_by_side_borderless"]()
    pdf = str(write_pdf(tmp_path / "pair.pdf", pages))
    probe = probe_pdf(pdf)
    assert probe.table_pages == []  # nothing drawn: only the engine's wide table points here
    assert wide_table_hints(MERGED, probe) == {1: [[
        ["Region", "Q1", "Q2", "Product", "Units", "Price"],
        ["North", "120", "135", "Alpha", "12", "4.50"],
        ["South", "98", "101", "Beta", "7", "9.99"],
        ["East", "143", "150", "Gamma", "30", "1.25"],
    ]]}
    out, summary = run_table_stage(pdf, MERGED, probe, repair_allowed=True, find=_in_process)
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == [t["rows"] for t in truth]
    assert summary.repaired == [{"page": 1, "tables": 2, "issue": "merged", "source": "pdf_words"}]
    assert "source=pdf-words" in out


def test_narrow_tables_and_tables_from_elsewhere_give_no_hints(tmp_path):
    pages, _ = FIXTURES["side_by_side_borderless"]()
    probe = probe_pdf(str(write_pdf(tmp_path / "pair.pdf", pages)))
    assert wide_table_hints("| a | b |\n|---|---|\n| 1 | 2 |", probe) == {}
    assert wide_table_hints("| w | x | y | z |\n|---|---|---|---|\n| 1 | 2 | 3 | 4 |", probe) == {}


# ─── Thorough: tables without lines, confirmed by a second reading ───────────
# Docling's reading of the borderless pair: no table, the cells as loose lines.
LOOSE_PAIR = (
    "## Quarterly summary\n\nRegion Q1 Q2 Product Units Price\n\nNorth 120 135 Alpha 12 4.50\n\n"
    "South\n\n98\n\n101\n\nBeta\n\n7\n\n9.99\n\nEast 143 150 Gamma 30 1.25\n\n"
    "Figures are unaudited and subject to revision.\n"
)


def _borderless(tmp_path):
    pages, truth = FIXTURES["side_by_side_borderless"]()
    pdf = str(write_pdf(tmp_path / "pair.pdf", pages))
    return pdf, [t["rows"] for t in truth]


def test_tables_without_lines_are_written_only_when_a_second_reading_agrees(tmp_path):

    pdf, truth = _borderless(tmp_path)
    insp = inspect_tables(pdf, LOOSE_PAIR, probe_pdf(pdf), deep=True, find=_in_process)
    assert len(pending_confirmations(insp)) == 2
    readings = {0: truth[0], 1: [["产品", "Units", "Price"], *truth[1][1:]]}  # one cell read wrong
    calls = []

    def reader(path, page, bbox):
        calls.append((page, bbox))
        return readings[len(calls) - 1]

    assert confirm_tables(insp, reader) == 2
    out, summary = finish_tables(insp, repair_allowed=True)
    assert [b.rows for b in find_table_blocks(out.split("\n"))] == truth  # the PDF's own text: "Product"
    assert out.count("checked=text-layer,glm-ocr") == 2
    assert "confirmed by a GLM-OCR reading" in summary.details[-1]
    assert out.rstrip().endswith("Figures are unaudited and subject to revision.")


def test_without_agreement_nothing_is_written_and_nothing_is_a_warning(tmp_path):

    pdf, truth = _borderless(tmp_path)
    for reader in (
        lambda *_a: [["Region", "Q1"], ["North", "120"]],  # another shape
        lambda *_a: None,                                    # no table read at all
        lambda *_a: (_ for _ in ()).throw(RuntimeError("server gone")),
    ):
        insp = inspect_tables(pdf, LOOSE_PAIR, probe_pdf(pdf), deep=True, find=_in_process)
        assert confirm_tables(insp, reader) == 0
        out, summary = finish_tables(insp, repair_allowed=True)
        assert out == LOOSE_PAIR
        assert summary.warning is False and summary.issues == []
        assert any("table without lines came out as text" in line for line in summary.details)


def test_without_a_second_reader_word_tables_are_never_written(tmp_path):
    pdf, _ = _borderless(tmp_path)
    out, summary = run_table_stage(pdf, LOOSE_PAIR, probe_pdf(pdf), repair_allowed=True, deep=True, find=_in_process)
    assert out == LOOSE_PAIR and not summary.repaired and not summary.warning


def test_a_table_the_engine_got_right_is_not_read_again(tmp_path):

    pages, truth = FIXTURES["wide_borderless"]()
    pdf = str(write_pdf(tmp_path / "wide.pdf", pages))
    from app.core.markdown_tables import render_pipe_table

    md = "Monthly units\n\n" + "\n".join(render_pipe_table(truth[0]["rows"])) + "\n"
    insp = inspect_tables(pdf, md, probe_pdf(pdf), deep=True, find=_in_process)
    assert pending_confirmations(insp) == []


def test_confirmations_stop_at_the_limit_and_on_cancel(tmp_path):

    pdf, truth = _borderless(tmp_path)
    insp = inspect_tables(pdf, LOOSE_PAIR, probe_pdf(pdf), deep=True, find=_in_process)
    assert confirm_tables(insp, lambda *_a: truth[0], limit=1) == 1
    registry = JobRegistry()
    job = registry.start("confirm-cancel-1")
    registry.cancel("confirm-cancel-1")
    insp = inspect_tables(pdf, LOOSE_PAIR, probe_pdf(pdf), deep=True, find=_in_process)
    with pytest.raises(ConversionCancelledError):
        confirm_tables(insp, lambda *_a: truth[0], job=job)


def test_grids_agree():

    grid = [["a", "b"], ["1", "2"], ["3", "4"], ["5", "6"], ["7", "8"]]
    assert grids_agree(grid, [row[:] for row in grid])
    assert grids_agree(grid, [["a", "b"], ["1", "2"], ["3", "4"], ["5", "6"], ["7", "X"]])  # 9 of 10
    assert not grids_agree(grid, [["a", "b"], ["1", "2"], ["3", "4"], ["5", "Y"], ["7", "X"]])  # 8 of 10
    assert not grids_agree(grid, grid[:-1]) and not grids_agree(grid, None)


def test_check_only_reports_and_keeps_the_text(tmp_path):
    pdf, _ = _pair(tmp_path)
    out, summary = run_table_stage(pdf, MERGED, probe_pdf(pdf), repair_allowed=False, find=_in_process)
    assert out == MERGED
    assert summary.warning and "came out as one" in summary.message


def test_a_document_without_drawn_tables_costs_no_finder_call(tmp_path):
    pages, _ = FIXTURES["plain_prose"]()
    pdf = str(write_pdf(tmp_path / "prose.pdf", pages))

    def finder(*_args, **_kwargs):
        raise AssertionError("the finder must not run")

    out, summary = run_table_stage(pdf, "text", probe_pdf(pdf), repair_allowed=True, find=finder)
    assert out == "text" and summary.checked and summary.tables_found == 0


def test_failures_leave_the_markdown_alone(tmp_path):
    pdf, _ = _pair(tmp_path)
    probe = probe_pdf(pdf)

    def broken(*_args, **_kwargs):
        raise RuntimeError("worker gone")

    assert run_table_stage(pdf, MERGED, probe, repair_allowed=True, find=broken)[1].skipped_reason == "error"
    assert run_table_stage(pdf, MERGED, probe, repair_allowed=True,
                           find=lambda *_a, **_k: {"pages": [], "error": "bad pdf"})[1].skipped_reason == "error"
    out, summary = run_table_stage(pdf, MERGED, None, repair_allowed=True, find=_in_process)
    assert out == MERGED and summary.skipped_reason == "unreadable"


def test_a_cancelled_job_stops_before_the_finder(tmp_path):
    pdf, _ = _pair(tmp_path)
    registry = JobRegistry()
    job = registry.start("job-12345678")
    registry.cancel("job-12345678")
    with pytest.raises(ConversionCancelledError):
        run_table_stage(pdf, MERGED, probe_pdf(pdf), repair_allowed=True, job=job, find=_in_process)


def test_the_finder_runs_in_the_markitdown_worker(tmp_path):
    from app.core.engines.markitdown_worker_client import MarkItDownWorker, find_tables_in_worker

    pdf, truth = _pair(tmp_path)
    try:
        result = find_tables_in_worker(pdf, [1])
        worker = MarkItDownWorker.get_instance()
        assert worker.is_running(), "answered by the worker, not in this process"
    finally:
        MarkItDownWorker.get_instance().shutdown()
    rows = [t["rows"] for band in result["pages"][0]["bands"] for t in band["tables"]]
    assert rows == [t["rows"] for t in truth]


# ─── Auto ────────────────────────────────────────────────────────────────────
def _auto(pdf: str, level: str):
    from app.core.auto_engine import run_auto
    from app.core.converter import ConversionOptions
    from app.core.queue_model import EngineKind, QueueItem, SourceKind

    item = QueueItem(source=pdf, kind=SourceKind.FILE, display_name="pair.pdf", engine=EngineKind.AUTO)
    options = ConversionOptions(engine=EngineKind.AUTO, auto_quality=level)
    return asyncio.run(run_auto(item, options)), options


def test_auto_thorough_asks_glm_ocr_for_a_second_reading_and_only_then(tmp_path, monkeypatch):
    import app.core.auto_engine as auto_engine
    import app.core.engines.glm_ocr_engine as glm_engine
    from app.core.queue_model import EngineKind

    pdf, truth = _borderless(tmp_path)
    monkeypatch.setattr(auto_engine, "ocr_engines_usable", lambda: [EngineKind.GLM_OCR, EngineKind.DOCLING])
    monkeypatch.setattr(auto_engine, "glm_estimate_seconds", lambda *a: 10.0)

    def engine_wrote_loose_lines(item, options, engine, fetched):
        options.engine_used = engine
        return LOOSE_PAIR

    monkeypatch.setattr(auto_engine, "_convert_once", engine_wrote_loose_lines)
    reads = []

    def glm_reads(path, page, bbox):
        reads.append(bbox)
        return truth[len(reads) - 1]

    monkeypatch.setattr(glm_engine, "read_table_region", glm_reads)
    try:
        md, options = _auto(pdf, "thorough")
        balanced_md, _ = _auto(pdf, "balanced")
    finally:
        from app.core.engines.markitdown_worker_client import MarkItDownWorker

        MarkItDownWorker.get_instance().shutdown()
    assert len(reads) == 2, "one second reading per table, at Thorough only"
    assert [b.rows for b in find_table_blocks(md.split("\n"))] == truth
    assert options.table_report.repaired[0]["source"] == "pdf_words"
    assert balanced_md == LOOSE_PAIR


def test_auto_balanced_rebuilds_tables_and_fast_does_nothing(tmp_path, monkeypatch):
    import app.core.auto_engine as auto_engine

    monkeypatch.setattr(auto_engine, "ocr_engines_usable", lambda: [])
    pdf, truth = _pair(tmp_path)
    try:
        fast_md, fast = _auto(pdf, "fast")
        balanced_md, balanced = _auto(pdf, "balanced")
    finally:
        from app.core.engines.markitdown_worker_client import MarkItDownWorker

        MarkItDownWorker.get_instance().shutdown()
    assert fast.table_report is None
    assert len(find_table_blocks(fast_md.split("\n"))) == 1  # MarkItDown's merged table, as before
    assert [b.rows for b in find_table_blocks(balanced_md.split("\n"))] == [t["rows"] for t in truth]
    assert balanced.table_report.repaired and balanced.quality_report.checked
    assert "<!-- inkdoc:table page=1 source=pdf-lines" in balanced_md
