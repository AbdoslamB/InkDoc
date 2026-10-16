"""The registry of Auto quality steps (app/core/thorough_passes.py), and Auto following it.

The plan and the log are pure; Auto is run with a stand-in engine and the
table finder in this process, so no model or worker is needed.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

import app.core.auto_engine as auto_engine
import app.core.table_stage as table_stage
from app.core import thorough_passes as tp
from app.core.auto_engine import run_auto
from app.core.converter import ConversionOptions
from app.core.queue_model import EngineKind, QueueItem, SourceKind
from app.core.table_finder import find_tables
from tests._pdf_fixtures import write_pdf
from tests._table_fixtures import FIXTURES

BOTH = {"docling", "glm_ocr"}
STAGE_ORDER = [tp.STAGE_ROUTE, tp.STAGE_POLISH, tp.STAGE_TABLES, tp.STAGE_FINDER, tp.STAGE_AFTER_TABLES]


def test_steps_are_unique_described_and_in_stage_order():
    assert len(tp.PASS_NAMES) == len(set(tp.PASS_NAMES))
    stages = [STAGE_ORDER.index(p.stage) for p in tp.PASSES]
    assert stages == sorted(stages), "PASSES is the order steps run in"
    for p in tp.PASSES:
        assert p.title and p.description and p.level in (tp.LEVEL_BALANCED, tp.LEVEL_THOROUGH)
        assert p.cost in ("low", "medium", "high")
        assert set(p.needs_all) | set(p.needs_any) <= set(tp.ENGINE_NAMES)


def test_fast_runs_nothing_and_balanced_only_the_table_check():
    assert tp.PassPlan("fast", BOTH).relevant() == []
    plan = tp.PassPlan("balanced", BOTH)
    assert [p.name for p in plan.relevant()] == ["table_check"]
    assert plan.on("table_check") and not plan.on("figure_text")
    assert plan.reason("figure_text") == "runs at Thorough and above"
    assert plan.detectors() == [] and plan.finder_needed()
    assert tp.PassPlan("nonsense", BOTH).level == "fast"


def test_thorough_with_both_engines_runs_every_step():
    plan = tp.PassPlan("thorough", BOTH)
    assert all(plan.on(name) for name in tp.PASS_NAMES)
    assert plan.detectors() == [tp.DETECT_RATING_GRIDS, tp.DETECT_BORDERLESS, tp.DETECT_SEGMENTS]
    assert plan.table_kinds() == {tp.TABLE_KIND_LINES, tp.TABLE_KIND_HINT_SPLIT, tp.TABLE_KIND_RATING,
                                  tp.TABLE_KIND_BORDERLESS}


@pytest.mark.parametrize("available, layout, split", [
    (set(), "needs Docling or GLM-OCR", "needs Docling and GLM-OCR"),
    ({"docling"}, "", "needs Docling and GLM-OCR"),
    ({"glm_ocr"}, "", "needs Docling and GLM-OCR"),
    (BOTH, "", ""),
])
def test_steps_that_need_an_engine_say_which(available, layout, split):
    plan = tp.PassPlan("thorough", available)
    assert plan.reason("digital_layout") == layout
    assert plan.reason("page_split") == split
    assert plan.on("polish") and plan.on("figure_text")  # no engine needed


def test_a_switched_off_step_costs_nothing_and_says_so():
    plan = tp.PassPlan("thorough", BOTH, disabled={"figure_text", "dropped_paragraphs", "rating_grids"})
    assert plan.reason("figure_text") == "switched off"
    assert plan.detectors() == [tp.DETECT_BORDERLESS], "no segments without a step that uses them"
    assert tp.TABLE_KIND_RATING not in plan.table_kinds()
    # The finder still runs for the table check; with everything off it doesn't.
    assert plan.finder_needed()
    off = tp.PassPlan("thorough", BOTH, disabled=set(tp.PASS_NAMES))
    assert not off.finder_needed() and off.detectors() == [] and off.table_kinds() == frozenset()
    # Only the finder's extras on: the finder runs, but writes no ruled table.
    extras = tp.PassPlan("thorough", BOTH, disabled={"table_check"})
    assert extras.finder_needed() and tp.TABLE_KIND_LINES not in extras.table_kinds()


def test_the_log_reports_every_step_of_the_level():
    log = tp.PassLog(tp.PassPlan("thorough", {"docling"}, disabled={"polish"}))
    log.ran("table_check", 2)
    log.ran("table_check", 1)
    log.ran("figure_text")
    log.skipped("rating_grids", "nothing to read")
    entries = {e["name"]: e for e in log.to_list()}
    assert list(entries) == list(tp.PASS_NAMES)
    assert entries["table_check"] == {"name": "table_check", "ran": True, "changed": 3, "skipped": ""}
    assert entries["figure_text"]["ran"] and entries["figure_text"]["changed"] == 0
    assert entries["polish"]["skipped"] == "switched off"
    assert entries["page_split"]["skipped"] == "needs Docling and GLM-OCR"
    assert entries["rating_grids"]["skipped"] == "nothing to read"
    log.ran("not_a_step")  # unknown names are ignored, never an error
    assert tp.PassLog(tp.PassPlan("fast")).to_list() == []


def test_catalogue_lists_every_step():
    cat = tp.catalogue()
    assert [c["name"] for c in cat] == list(tp.PASS_NAMES)
    assert {"name", "title", "description", "level", "needs_all", "needs_any", "cost"} <= set(cat[0])


def test_the_registry_imports_nothing_but_the_standard_library():
    tree = ast.parse(Path(tp.__file__).read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert modules <= {"__future__", "collections.abc", "dataclasses", "typing"}


# ─── Auto runs what the plan says ────────────────────────────────────────────
# What a layout engine makes of the fixtures: the rating grid's cells empty,
# the borderless pair as loose lines.
EMPTY_GRID = "\n".join([
    "## Asset Class Weightings", "", "| Asset Class | Underweight | CIO View Neutral | Overweight |",
    "|---|---|---|---|", "| Equities | | | |", "| Bonds | | | |", "| Cash | | | |", "",
])
LOOSE_PAIR = (
    "## Quarterly summary\n\nRegion Q1 Q2 Product Units Price\n\nNorth 120 135 Alpha 12 4.50\n\n"
    "South\n\n98\n\n101\n\nBeta\n\n7\n\n9.99\n\nEast 143 150 Gamma 30 1.25\n\n"
    "Figures are unaudited and subject to revision.\n"
)


@pytest.fixture
def auto(monkeypatch, tmp_path):
    """run(fixture, engine reply, level, off) with Docling installed (a stand-in) and no GLM-OCR."""
    asked: list[list[str]] = []
    reply = {"text": ""}

    def fake_convert(item, options, engine, fetched):
        options.engine_used = engine
        return reply["text"]

    def finder(path, pages, _job, **kwargs):
        asked.append(list(kwargs.get("detectors") or []))
        return find_tables(path, pages, **kwargs)

    monkeypatch.setattr(auto_engine, "_convert_once", fake_convert)
    monkeypatch.setattr(auto_engine, "ocr_engines_usable", lambda: [EngineKind.DOCLING])
    monkeypatch.setattr(table_stage, "_find_in_worker", finder)

    def run(fixture: str, text: str, level: str = "thorough", off=()):
        pages, _ = FIXTURES[fixture]()
        pdf = str(write_pdf(tmp_path / f"{fixture}.pdf", pages))
        reply["text"] = text
        item = QueueItem(source=pdf, kind=SourceKind.FILE, display_name="doc.pdf", engine=EngineKind.AUTO)
        options = ConversionOptions(
            engine=EngineKind.AUTO, auto_quality=level, passes_off=tuple(off), quality_check=False
        )
        markdown = asyncio.run(run_auto(item, options))
        report = options.table_report
        return markdown, options, {e["name"]: e for e in (report.passes if report else [])}

    return run, asked


def test_auto_reports_what_each_step_did(auto):
    run, asked = auto
    markdown, options, passes = run("rating_grid", EMPTY_GRID)
    assert "source=pdf-shapes" in markdown and "| Equities | | | |" not in markdown
    assert asked == [[tp.DETECT_RATING_GRIDS, tp.DETECT_BORDERLESS, tp.DETECT_SEGMENTS]]
    assert list(passes) == list(tp.PASS_NAMES)
    assert passes["digital_layout"] == {"name": "digital_layout", "ran": True, "changed": 1, "skipped": ""}
    assert options.engine_used == EngineKind.DOCLING
    assert passes["page_split"]["skipped"] == "needs Docling and GLM-OCR"
    assert passes["rating_grids"]["ran"] and passes["rating_grids"]["changed"] == 1
    assert passes["table_check"]["ran"] and passes["table_check"]["changed"] == 0
    assert options.table_report.to_dict()["passes"] == list(passes.values())


def test_a_step_switched_off_is_not_run_and_says_so(auto):
    run, asked = auto
    markdown, _options, passes = run("rating_grid", EMPTY_GRID, off={"rating_grids"})
    assert "| Equities | | | |" in markdown and "source=pdf-shapes" not in markdown
    assert asked == [[tp.DETECT_BORDERLESS, tp.DETECT_SEGMENTS]], "the finder never looks for markers"
    assert passes["rating_grids"] == {"name": "rating_grids", "ran": False, "changed": 0, "skipped": "switched off"}


def test_tables_without_lines_wait_for_glm_ocr(auto):
    run, _asked = auto
    markdown, _options, passes = run("side_by_side_borderless", LOOSE_PAIR)
    assert "inkdoc:table" not in markdown
    assert passes["borderless_tables"]["skipped"] == "GLM-OCR isn't installed to confirm them"


def test_a_failing_addition_costs_nothing_but_itself(auto, monkeypatch):
    import app.core.figure_text as figure_text

    def broken(markdown, pages):
        raise RuntimeError("bug")

    monkeypatch.setattr(figure_text, "recover_figure_text", broken)
    run, _asked = auto
    markdown, _options, passes = run("rating_grid", EMPTY_GRID)
    assert "source=pdf-shapes" in markdown, "the tables written before it are kept"
    assert passes["figure_text"]["skipped"] == "it failed"
    assert passes["dropped_paragraphs"]["skipped"] == "a step before it failed"


def test_with_every_step_off_nothing_is_looked_at(auto):
    run, asked = auto
    markdown, options, passes = run("rating_grid", EMPTY_GRID, off=tp.PASS_NAMES)
    assert markdown == EMPTY_GRID and asked == []
    assert options.engine_used == EngineKind.MARKITDOWN, "no layout engine for a digital PDF"
    assert options.table_report.skipped_reason == "switched_off"
    assert {e["skipped"] for e in passes.values()} == {"switched off"}


def test_balanced_runs_only_the_table_check(auto):
    run, asked = auto
    _markdown, options, passes = run("rating_grid", EMPTY_GRID, level="balanced")
    assert list(passes) == ["table_check"] and passes["table_check"]["ran"]
    assert all(detectors == [] for detectors in asked)
    assert options.engine_used == EngineKind.MARKITDOWN
    _markdown, options, passes = run("rating_grid", EMPTY_GRID, level="fast")
    assert options.table_report is None and passes == {}
