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
from tests._pdf_fixtures import image_page, prose_page, write_pdf
from tests._table_fixtures import FIXTURES

BOTH = {"docling", "glm_ocr"}
EVERYTHING = BOTH | {"layout_model"}
STAGE_ORDER = [tp.STAGE_ROUTE, tp.STAGE_POLISH, tp.STAGE_TABLES, tp.STAGE_FINDER, tp.STAGE_SCANNED,
               tp.STAGE_AFTER_TABLES]


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


def test_thorough_with_every_engine_and_add_on_runs_every_step():
    plan = tp.PassPlan("thorough", EVERYTHING)
    assert all(plan.on(name) for name in tp.PASS_NAMES)
    assert plan.detectors() == [tp.DETECT_RATING_GRIDS, tp.DETECT_BORDERLESS, tp.DETECT_SEGMENTS]
    assert plan.table_kinds() == {tp.TABLE_KIND_LINES, tp.TABLE_KIND_HINT_SPLIT, tp.TABLE_KIND_RATING,
                                  tp.TABLE_KIND_BORDERLESS}


@pytest.mark.parametrize("available, layout, split, borderless", [
    (set(), "needs Docling or GLM-OCR", "needs Docling and GLM-OCR", "needs GLM-OCR"),
    ({"docling"}, "", "needs Docling and GLM-OCR", "needs GLM-OCR"),
    ({"glm_ocr"}, "", "needs Docling and GLM-OCR", ""),
    (BOTH, "", "", ""),
])
def test_steps_that_need_an_engine_say_which(available, layout, split, borderless):
    plan = tp.PassPlan("thorough", available)
    assert plan.reason("digital_layout") == layout
    assert plan.reason("page_split") == split
    # Tables without lines are written only once GLM-OCR agrees: without it,
    # the finder doesn't look for them at all.
    assert plan.reason("borderless_tables") == borderless
    assert (tp.DETECT_BORDERLESS in plan.detectors()) == (not borderless)
    assert plan.on("polish") and plan.on("figure_text")  # no engine needed


def test_tables_on_scanned_pages_need_glm_ocr_and_the_layout_model():
    assert tp.PassPlan("thorough", BOTH).reason("scanned_tables") == "needs GLM-OCR and the layout model"
    assert tp.PassPlan("thorough", {"glm_ocr", "layout_model"}).on("scanned_tables")
    assert tp.ENGINE_NAMES["layout_model"] == "the layout model"


def test_a_switched_off_step_costs_nothing_and_says_so():
    segment_steps = {p.name for p in tp.PASSES if p.needs_segments}
    plan = tp.PassPlan("thorough", BOTH, disabled=segment_steps | {"rating_grids"})
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


def test_known_names_keep_only_steps_once_in_run_order():
    assert tp.known_names(["polish", "nope", "digital_layout", "polish"]) == ("digital_layout", "polish")
    assert tp.known_names({"figure_text"}) == ("figure_text",)
    for junk in (None, "polish", 3, {"polish": True}):
        assert tp.known_names(junk) == (), junk


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


D, G, M = EngineKind.DOCLING, EngineKind.GLM_OCR, EngineKind.MARKITDOWN


@pytest.fixture
def auto(monkeypatch, tmp_path):
    """run(doc, reply, ...): Auto with stand-in engines and the table finder in this process.

    `doc` is a fixture name or a list of pages; `reply` the Markdown every
    engine returns, or {engine: Markdown}; `engines` the OCR engines installed
    (Docling alone by default); `layout` whether the layout model add-on is.
    """
    asked: list[list[str]] = []
    state: dict = {"reply": "", "engines": [D], "layout": False, "runs": 0}

    def fake_convert(item, options, engine, fetched):
        options.engine_used = engine
        reply = state["reply"]
        return reply[engine] if isinstance(reply, dict) else reply

    def finder(path, pages, _job, **kwargs):
        asked.append(list(kwargs.get("detectors") or []))
        return find_tables(path, pages, **kwargs)

    monkeypatch.setattr(auto_engine, "_convert_once", fake_convert)
    monkeypatch.setattr(auto_engine, "ocr_engines_usable", lambda: list(state["engines"]))
    monkeypatch.setattr(auto_engine, "layout_model_usable", lambda: state["layout"])
    monkeypatch.setattr(auto_engine, "glm_estimate_seconds", lambda *a: 10.0)
    monkeypatch.setattr(auto_engine, "glm_estimate_for_pages", lambda n: 10.0 * n)
    monkeypatch.setattr(table_stage, "_find_in_worker", finder)

    def run(doc, reply, level="thorough", off=(), engines=(D,), quality_check=False, layout=False):
        pages = FIXTURES[doc]()[0] if isinstance(doc, str) else doc
        state["runs"] += 1
        pdf = str(write_pdf(tmp_path / f"doc{state['runs']}.pdf", pages))
        state.update(reply=reply, engines=list(engines), layout=layout)
        item = QueueItem(source=pdf, kind=SourceKind.FILE, display_name="doc.pdf", engine=EngineKind.AUTO)
        options = ConversionOptions(
            engine=EngineKind.AUTO, auto_quality=level, passes_off=tuple(off), quality_check=quality_check
        )
        markdown = asyncio.run(run_auto(item, options))
        report = options.table_report
        return markdown, options, {e["name"]: e for e in (report.passes if report else [])}

    return run, asked


def test_auto_reports_what_each_step_did(auto):
    run, asked = auto
    markdown, options, passes = run("rating_grid", EMPTY_GRID)
    assert "source=pdf-shapes" in markdown and "| Equities | | | |" not in markdown
    assert asked == [[tp.DETECT_RATING_GRIDS, tp.DETECT_SEGMENTS]]
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
    assert asked == [[tp.DETECT_SEGMENTS]], "the finder never looks for markers"
    assert passes["rating_grids"] == {"name": "rating_grids", "ran": False, "changed": 0, "skipped": "switched off"}


def test_tables_without_lines_need_glm_ocr_to_confirm_them(auto, monkeypatch):
    import app.core.engines.glm_ocr_engine as glm_ocr_engine

    run, asked = auto
    markdown, _options, passes = run("side_by_side_borderless", LOOSE_PAIR)
    assert "inkdoc:table" not in markdown
    assert passes["borderless_tables"]["skipped"] == "needs GLM-OCR"
    assert tp.DETECT_BORDERLESS not in asked[-1], "not looked for when they can't be confirmed"

    truth = [t["rows"] for t in FIXTURES["side_by_side_borderless"]()[1]]
    readings: list[tuple[int, list[float]]] = []

    def read_table_region(path, page, bbox):
        readings.append((page, bbox))
        return truth[len(readings) - 1]

    monkeypatch.setattr(glm_ocr_engine, "read_table_region", read_table_region)
    markdown, _options, passes = run("side_by_side_borderless", LOOSE_PAIR, engines=(G, D))
    assert len(readings) == 2
    assert markdown.count("checked=text-layer,glm-ocr") == 2
    assert passes["borderless_tables"] == {"name": "borderless_tables", "ran": True, "changed": 2, "skipped": ""}


def test_the_polish_applies_to_the_result_auto_keeps(auto):
    # MarkItDown first (the layout step is off), its result misses most of the
    # text, so Auto converts again with Docling and keeps that: the polish has
    # to run on Docling's result, not on the one thrown away.
    run, _asked = auto
    page = prose_page(120, extra_lines=["Research by Fenner & Smith and partners"])
    full = "\n\n".join(page["lines"])
    reply = {M: page["lines"][0], D: full.replace("&", "&amp;")}
    markdown, options, passes = run([page], reply, off={"digital_layout"}, quality_check=True)
    assert options.auto_decision.escalated and options.engine_used == D
    assert "Fenner & Smith" in markdown and "&amp;" not in markdown
    assert passes["polish"]["changed"] == 1 and options.table_report.characters_restored == 1


# GLM-OCR's reading of a scanned page with two tables side by side: one table.
MERGED_SCAN = "\n".join([
    "## Quarterly summary", "",
    "| Region | Q1 | Product | Units |", "|---|---|---|---|",
    "| North | 120 | Alpha | 12 |", "| South | 98 | Beta | 7 |", "",
    "Figures are unaudited.",
])
LEFT = [["Region", "Q1"], ["North", "120"], ["South", "98"]]
RIGHT = [["Product", "Units"], ["Alpha", "12"], ["Beta", "7"]]


def test_tables_on_a_scanned_page_are_read_one_by_one(auto, monkeypatch, tmp_path):
    import app.core.engines.glm_ocr_engine as glm_ocr_engine
    import app.core.engines.markitdown_worker_client as worker_client

    asked: list[tuple] = []

    def find_layout(path, pages, model_dir, sha256, job=None):
        asked.append((pages, model_dir, sha256))
        boxes = [[50.0, 100.0, 250.0, 200.0], [300.0, 100.0, 500.0, 200.0]]
        return {"pages": [{"page": 1, "skipped": "", "tables": [{"bbox": b, "score": 0.9} for b in boxes]}],
                "error": ""}

    monkeypatch.setattr(auto_engine, "layout_model_location", lambda: (tmp_path, {"model.onnx": "ab" * 32}))
    monkeypatch.setattr(worker_client, "find_layout_in_worker", find_layout)
    monkeypatch.setattr(glm_ocr_engine, "read_table_area", lambda path, page, bbox, detail="normal":
                        glm_ocr_engine.TableReading(LEFT if bbox[0] < 200 else RIGHT))
    run, _asked = auto
    markdown, options, passes = run([image_page("ccitt")], MERGED_SCAN, engines=(G,), layout=True)
    assert options.engine_used == G
    assert asked == [([1], str(tmp_path), "ab" * 32)], "only the scanned page GLM-OCR read"
    assert markdown.count("<!-- inkdoc:table page=1 source=glm-ocr checked=layout,page-reading -->") == 2
    assert "| Region | Q1 | Product | Units |" not in markdown and markdown.endswith("Figures are unaudited.")
    assert passes["scanned_tables"] == {"name": "scanned_tables", "ran": True, "changed": 2, "skipped": ""}
    assert {"page": 1, "tables": 2, "issue": "scanned", "source": "glm_ocr"} in options.table_report.repaired

    # Without the layout model the page stays as GLM-OCR read it.
    markdown, _options, passes = run([image_page("ccitt")], MERGED_SCAN, engines=(G,), layout=False)
    assert markdown == MERGED_SCAN
    assert passes["scanned_tables"]["skipped"] == "needs GLM-OCR and the layout model"


def test_a_failing_scanned_tables_step_leaves_the_page_as_glm_ocr_read_it(auto, monkeypatch, tmp_path):
    import app.core.scanned_tables as scanned_tables

    def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(auto_engine, "layout_model_location", lambda: (tmp_path, {"model.onnx": "ab" * 32}))
    monkeypatch.setattr(scanned_tables, "find_scanned_tables", broken)
    run, _asked = auto
    markdown, _options, passes = run([image_page("ccitt")], MERGED_SCAN, engines=(G,), layout=True)
    assert markdown == MERGED_SCAN
    assert passes["scanned_tables"]["skipped"] == "it failed"


MERGED_RULED = "\n".join([
    "## Quarterly summary", "",
    "| Region | Q1 | Q2 | Product | Units | Price |", "|---|---|---|---|---|---|",
    "| North | 120 | 135 | Alpha | 12 | 4.50 |", "| South | 98 | 101 | Beta | 7 | 9.99 |",
    "| East | 143 | 150 | Gamma | 30 | 1.25 |", "", "Figures are unaudited and subject to revision.",
])


def test_a_switched_off_table_check_gives_no_warning(auto):
    run, _asked = auto
    _markdown, options, _passes = run("side_by_side_ruled", MERGED_RULED)
    assert options.table_report.repaired, "on: the merged pair is rebuilt"
    markdown, options, passes = run("side_by_side_ruled", MERGED_RULED, off={"table_check"})
    assert markdown == MERGED_RULED
    report = options.table_report
    assert report.warning is False and report.issues == [], "no warning from a check that is off"
    assert passes["table_check"]["skipped"] == "switched off"


def test_a_switched_off_step_keeps_its_reason_through_a_page_split(auto):
    run, _asked = auto
    pages = [prose_page(60), prose_page(60, 1), image_page("ccitt")]
    _markdown, options, passes = run(pages, "text", engines=(G, D), off={"digital_layout"})
    assert options.auto_decision.segments, "the PDF was split by page"
    assert passes["digital_layout"]["skipped"] == "switched off"


def test_a_failing_addition_costs_nothing_but_itself(auto, monkeypatch):
    import app.core.figure_text as figure_text

    def broken(markdown, pages, **_switches):
        raise RuntimeError("bug")

    monkeypatch.setattr(figure_text, "recover_figures", broken)
    run, _asked = auto
    markdown, _options, passes = run("rating_grid", EMPTY_GRID)
    assert "source=pdf-shapes" in markdown, "the tables written before it are kept"
    assert passes["figure_text"]["skipped"] == "it failed"
    assert passes["dropped_paragraphs"]["skipped"] == "a step before it failed"
    assert passes["formatting"]["skipped"] == "a step before it failed"


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
