"""The PDF diagnostic (scripts/pdf_diagnose.py): its findings, and one run of the real table stage."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.pdf_diagnose as pd  # noqa: E402
from app.core.markdown_tables import render_pipe_table  # noqa: E402
from app.core.thorough_passes import PASSES  # noqa: E402


def test_every_module_it_points_to_exists():
    named = {*pd.PROBE, *pd.ROUTING, *pd.QUALITY, *pd.FINDER, *pd.LOCATE}
    named |= {m for p in PASSES for m in p.modules}
    assert all(importlib.util.find_spec(m) is not None for m in named)


# ─── Findings (pure) ─────────────────────────────────────────────────────────
def _band(issue="merged", *, page=2, located=True, verified=True, repaired=False, why="", kind="lines"):
    step = pd._owner(kind)
    return {"page": page, "located": located, "how": "words", "issue": issue, "message": f"Page {page}: {issue}.",
            "repaired": repaired, "not_repaired": why,
            "tables": [{"kind": kind, "step": step, "source": "pdf_lines", "verified": verified,
                        "reason": "", "checked": "", "shape": [3, 3]}]}


def _report(*, left=(), engines=None, quality=None, steps_on=("table_check",), passes=(), pages=(),
            chosen="docling"):
    """A report whose Auto kept `chosen`, with `left` the bands still wrong in its output."""
    steps = [{"name": p.name, "title": p.title, "on": p.name in steps_on, "reason": "" if p.name in steps_on
              else "switched off", "modules": list(p.modules)} for p in PASSES]
    auto = {"decision": {"chosen": chosen}, "quality": quality or {"missing_pct": 0},
            "tables": {"passes": list(passes)}, "tables_left": {"bands": list(left)}}
    return {"level": "thorough", "pages": list(pages), "steps": steps, "engines": engines or {}, "auto": auto}


def test_a_table_whose_place_is_not_found_points_at_locating():
    f = pd.findings(_report(left=[_band(located=False, why="its place in the result could not be found")]))
    assert f[0]["severity"] == "problem" and f[0]["where"] == "auto" and f[0]["step"] == "table_check"
    assert "app.core.table_check" in f[0]["modules"] and f[0]["pages"] == [2]
    assert "its place in the result could not be found" in f[0]["what"]


def test_an_unverified_table_points_at_the_finder():
    f = pd.findings(_report(left=[_band(verified=False, why="a word lands outside the cells")]))
    assert f[0]["severity"] == "problem" and "app.core.table_finder" in f[0]["modules"]


def test_a_table_of_a_step_that_is_off_is_a_note():
    f = pd.findings(_report(left=[_band(kind="rating_grids", why="that step is switched off")]))
    assert [x["severity"] for x in f] == ["note"] and f[0]["step"] == "rating_grids"


def test_a_repaired_table_and_a_good_one_are_no_problem():
    f = pd.findings(_report(left=[_band(repaired=True), _band(issue="ok")]))
    assert [x["severity"] for x in f] == ["note"]


def test_the_same_trouble_on_several_pages_is_one_finding():
    left = [_band("split", page=p, why="the result's text there differs slightly") for p in (27, 8, 27)]
    f = pd.findings(_report(left=left))
    assert len(f) == 1 and f[0]["pages"] == [8, 27] and f[0]["count"] == 3
    assert "pages 8, 27 (3 times)" in pd._where(f[0])


def test_with_auto_the_engines_on_their_own_give_notes_only():
    engines = {
        "docling": {"quality": {"missing_pct": 0}, "tables": {"bands": []}},
        "markitdown": {"quality": {"missing_pct": 0, "low_pages": [3]},
                       "tables": {"bands": [_band(located=False, why="x")]}},
    }
    f = pd.findings(_report(engines=engines))
    assert f and all(x["severity"] == "note" for x in f)


def test_without_auto_every_engine_gives_problems():
    report = _report(engines={"markitdown": {"quality": {"missing_pct": 0, "low_pages": [3]}, "tables": {}}})
    report["auto"] = {}
    assert [x["severity"] for x in pd.findings(report)] == ["problem"]


def test_an_engine_that_fails_is_a_problem_and_a_docling_timeout_points_at_its_limit():
    engines = {"docling": {"error": "TimeoutError: Docling conversion timed out after 180 seconds.",
                           "timed_out": True},
               "glm_ocr": {"error": "RuntimeError: llama-server exited", "timed_out": False}}
    f = {x["where"]: x for x in pd.findings(_report(engines=engines))}
    assert f["docling"]["severity"] == f["glm_ocr"]["severity"] == "problem"
    assert {"app.core.engines.docling_worker_client", "app.core.auto_engine"} <= set(f["docling"]["modules"])
    assert "in parts" in f["docling"]["what"]
    assert f["glm_ocr"]["modules"] == ["app.core.engines.glm_ocr_engine"]


def test_a_step_waiting_for_an_install_is_a_note_and_one_switched_off_is_not():
    report = _report()
    for s in report["steps"]:
        s["needs_install"] = s["name"] == "borderless_tables"
    f = pd.findings(report)
    assert [(x["severity"], x["where"], x["step"]) for x in f] == [("note", "setup", "borderless_tables")]


def test_the_table_finder_and_glm_ocr_are_asked_each_question_once(tmp_path):
    calls = []

    def finder(path, pages, job, **kwargs):
        calls.append(pages)
        return {"pages": [{"page": 1, "bands": [{"tables": [{"rows": [["a"]], "verified": False}]}]}]}

    find = pd.remembered(finder, pd._finder_key)
    first = find("x.pdf", [1, 2], None, hints={}, detectors=["segments"])
    first["pages"][0]["bands"][0]["tables"][0]["verified"] = True  # the stage marks what it confirms
    second = find("x.pdf", [1, 2], None, hints={}, detectors=["segments"])
    assert calls == [[1, 2]] and second["pages"][0]["bands"][0]["tables"][0]["verified"] is False
    find("x.pdf", [1, 2], None, hints={1: [[["a"]]]}, detectors=["segments"])
    assert len(calls) == 2  # another engine's wide tables are another question


def test_an_engine_that_reads_more_than_autos_choice_is_a_routing_problem():
    engines = {"markitdown": {"quality": {"missing_pct": 30 - pd.BETTER_BY_PCT}, "tables": {}}}
    f = pd.findings(_report(engines=engines, quality={"missing_pct": 30}))
    assert any(x["where"] == "auto" and "app.core.auto_engine" in x["modules"] for x in f)
    engines["markitdown"]["quality"]["missing_pct"] = 30 - pd.BETTER_BY_PCT + 1
    assert not any(x["where"] == "auto" for x in pd.findings(_report(engines=engines, quality={"missing_pct": 30})))


def test_a_suggestion_of_an_engine_that_misses_more_is_a_problem():
    quality = {"missing_pct": 10, "warning": True, "message": "Try MarkItDown.", "suggestion": "markitdown",
               "low_pages": [26, 27], "pages": [{"page": 26, "coverage": 0.67}, {"page": 27, "coverage": 0.71}]}
    engines = {"markitdown": {"quality": {"missing_pct": 13, "pages": [{"page": 26, "coverage": 0.95},
                                                                       {"page": 27, "coverage": 0.6}]}}}
    f = [x for x in pd.findings(_report(engines=engines, quality=quality)) if "suggests" in x["what"]]
    assert len(f) == 1 and f[0]["modules"] == ["app.core.quality_check"]
    assert "misses 13%" in f[0]["what"] and "pages [26]" in f[0]["what"]
    engines["markitdown"]["quality"]["missing_pct"] = 8
    assert not [x for x in pd.findings(_report(engines=engines, quality=quality)) if "suggests" in x["what"]]


def test_only_a_step_that_broke_is_a_problem():
    passes = [{"name": "digital_layout", "ran": False, "changed": 0, "skipped": "the page split chose the engines"},
              {"name": "figure_text", "ran": False, "changed": 0, "skipped": "it failed"}]
    f = pd.findings(_report(passes=passes))
    assert [(x["severity"], x["step"]) for x in f] == [("problem", "figure_text")]


def test_an_image_page_without_scan_evidence_points_at_the_probe():
    page = {"page": 1, "kind": "picture", "chars": 0, "image_coverage": 1.0, "scan_evidence": [],
            "rule_lines": 0, "vector_boxes": 0, "table_candidate": False}
    f = pd.findings(_report(pages=[page]))
    assert f[0]["pages"] == [1] and f[0]["modules"] == ["app.core.pdf_probe"]


# ─── One run: the real probe, finder, check and repair ───────────────────────
def test_a_merged_pair_is_found_and_explained(tmp_path):
    from app.core.table_finder import find_tables
    from tests._pdf_fixtures import write_pdf
    from tests._table_fixtures import LEFT, RIGHT, side_by_side

    pages, _ = side_by_side()
    pdf = write_pdf(tmp_path / "pair.pdf", pages)
    merged = [a + b for a, b in zip(LEFT, RIGHT, strict=True)]
    text = "Quarterly summary\n\n" + "\n".join(render_pipe_table(merged))

    def convert(engine, _pdf, _options):
        assert engine == "markitdown"
        return text

    report = pd.diagnose(
        pdf, level="balanced", engines=["markitdown"], auto=False, out=tmp_path / "out", convert=convert,
        available=set(), find=lambda p, pages, _job, **kwargs: find_tables(p, pages, **kwargs),
    )
    assert report["pages"][0]["table_candidate"]
    assert [s["name"] for s in report["steps"]] == ["table_check"]
    bands = report["engines"]["markitdown"]["tables"]["bands"]
    assert [(b["issue"], b["repaired"], len(b["tables"])) for b in bands] == [("merged", True, 2)]
    assert any(f["step"] == "table_check" and "replaced" in f["what"] for f in report["findings"])
    saved = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert saved["engines"]["markitdown"]["tables"]["bands"] == bands
    assert (tmp_path / "out" / "markitdown.md").read_text(encoding="utf-8") == text
    assert "Where to look" in pd.render(report)


def test_the_tables_left_in_autos_output_are_checked(tmp_path):
    from app.core.table_finder import find_tables
    from tests._pdf_fixtures import write_pdf
    from tests._table_fixtures import LEFT, RIGHT, side_by_side

    pages, _ = side_by_side()
    pdf = write_pdf(tmp_path / "pair.pdf", pages)
    merged = [a + b for a, b in zip(LEFT, RIGHT, strict=True)]
    text = "Quarterly summary\n\n" + "\n".join(render_pipe_table(merged))

    report = pd.diagnose(
        pdf, level="balanced", engines=[], out=tmp_path / "out", convert=lambda engine, _pdf, _o: text,
        available=set(), find=lambda p, pages, _job, **kwargs: find_tables(p, pages, **kwargs),
    )
    bands = report["auto"]["tables_left"]["bands"]
    assert [(b["issue"], b["page"]) for b in bands] == [("merged", 1)]
    assert (tmp_path / "out" / "auto-balanced.md").read_text(encoding="utf-8") == text
    assert "tables in its output: 1 places" in pd.render(report)


def test_a_file_pdfium_cannot_read_is_reported(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7\nnot really\n")
    report = pd.diagnose(bad, engines=[], auto=False, out=tmp_path / "out", available=set())
    assert report["findings"][0]["modules"] == ["app.core.pdf_probe"]


@pytest.mark.parametrize("args", [["--engines", "tesseract"], ["--passes-off", "nonsense"]])
def test_unknown_names_are_refused(tmp_path, args):
    from tests._pdf_fixtures import prose_page, write_pdf

    pdf = write_pdf(tmp_path / "p.pdf", [prose_page()])
    with pytest.raises(SystemExit) as exc:
        pd.main([str(pdf), *args, "--out", str(tmp_path / "out")])
    assert exc.value.code == 2
