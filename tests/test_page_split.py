"""Auto's page split for mixed PDFs (Auto quality: Thorough).

The plan is pure and tested with probes of hand-written PDFs; the orchestration
is tested with stand-in engines, so no model is needed.
"""
from __future__ import annotations

import asyncio
import os

import pypdfium2 as pdfium
import pytest

import app.core.auto_engine as auto_engine
from app.core.auto_engine import PageSegment, plan_page_split, run_auto
from app.core.converter import ConversionOptions
from app.core.jobs import ConversionCancelledError, JobRegistry
from app.core.pdf_pages import page_runs, write_page_subset
from app.core.pdf_probe import probe_pdf
from app.core.queue_model import EngineKind, QueueItem, SourceKind
from tests._pdf_fixtures import image_page, prose_page, write_pdf

BOTH = [EngineKind.GLM_OCR, EngineKind.DOCLING]
D, G = EngineKind.DOCLING, EngineKind.GLM_OCR


def _mixed(tmp_path, kinds: str):
    """'t' a text page, 's' a scanned page (CCITT image), in order."""
    pages = [prose_page(60, i) if k == "t" else image_page("ccitt") for i, k in enumerate(kinds)]
    return str(write_pdf(tmp_path / f"{kinds}.pdf", pages))


# ─── Plan ────────────────────────────────────────────────────────────────────
def test_text_pages_go_to_docling_and_scans_to_glm_ocr(tmp_path):
    probe = probe_pdf(_mixed(tmp_path, "ttsstt"))
    assert plan_page_split(probe, BOTH) == [PageSegment(D, [1, 2]), PageSegment(G, [3, 4]), PageSegment(D, [5, 6])]


@pytest.mark.parametrize("kinds, engines, why", [
    ("tttt", BOTH, "nothing needs OCR"),
    ("ttss", [EngineKind.DOCLING], "GLM-OCR not usable"),
    ("ttss", [EngineKind.GLM_OCR], "Docling not usable"),
    ("tsss", BOTH, "only one text page"),
    ("tstststststst", BOTH, "too many runs"),
])
def test_no_split_when_it_would_not_pay(tmp_path, kinds, engines, why):
    assert plan_page_split(probe_pdf(_mixed(tmp_path, kinds)), engines) is None, why


def test_no_split_for_a_partly_probed_or_unreadable_pdf(tmp_path):
    probe = probe_pdf(_mixed(tmp_path, "ttss"))
    probe.truncated = True
    assert plan_page_split(probe, BOTH) is None
    assert plan_page_split(None, BOTH) is None


def test_page_runs():
    assert page_runs([1, 2, 3, 5, 7, 8]) == "1-3, 5, 7-8"
    assert page_runs([4]) == "4" and page_runs([]) == ""


# ─── Sub-PDFs ────────────────────────────────────────────────────────────────
def test_a_subset_pdf_holds_exactly_those_pages(tmp_path):
    src = _mixed(tmp_path, "ttst")
    dest = tmp_path / "sub.pdf"
    write_page_subset(src, [2, 4], dest)
    doc = pdfium.PdfDocument(str(dest))
    try:
        assert len(doc) == 2
        texts = [doc[i].get_textpage().get_text_range() for i in range(2)]
    finally:
        doc.close()
    assert texts[0].startswith(prose_page(60, 1)["lines"][0][:20])
    os.remove(dest)  # closed: Windows can delete it

    with pytest.raises(ValueError):
        write_page_subset(src, [9], tmp_path / "bad.pdf")
    assert not (tmp_path / "bad.pdf").exists()


# ─── Orchestration (stand-in engines) ────────────────────────────────────────
@pytest.fixture
def engines(monkeypatch, tmp_path):
    calls: list[dict] = []
    failing: set[EngineKind] = set()

    def fake_convert(item, options, engine, fetched):
        pages = list(options.page_subset) if engine == G and options.page_subset else None
        if engine != G:
            doc = pdfium.PdfDocument(item.source)
            pages = len(doc)
            doc.close()
        calls.append({"engine": engine, "source": item.source, "pages": pages})
        if engine in failing:
            raise RuntimeError(f"{engine.value} broke")
        options.engine_used = engine
        return f"[{engine.value} read {pages}]"

    monkeypatch.setattr(auto_engine, "_convert_once", fake_convert)

    async def no_table_stage(path, markdown, *args, **kwargs):  # tested in test_table_stage.py
        return markdown

    monkeypatch.setattr(auto_engine, "_check_tables", no_table_stage)
    monkeypatch.setattr(auto_engine, "ocr_engines_usable", lambda: list(BOTH))
    monkeypatch.setattr(auto_engine, "glm_estimate_for_pages", lambda n: 10.0 * n)
    monkeypatch.setattr(auto_engine, "glm_estimate_seconds", lambda *a: 10.0)
    splits = tmp_path / "splits"
    splits.mkdir()
    import tempfile

    real = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="", **kw: real(prefix=prefix, dir=splits))
    return calls, failing, splits


def _run(path: str, level: str = "thorough", job=None):
    item = QueueItem(source=path, kind=SourceKind.FILE, display_name="mixed.pdf", engine=EngineKind.AUTO)
    options = ConversionOptions(engine=EngineKind.AUTO, auto_quality=level, quality_check=False)
    return asyncio.run(run_auto(item, options, job=job)), options


def test_docling_runs_first_then_glm_ocr_and_pages_are_joined_in_order(tmp_path, engines):
    calls, _, splits = engines
    md, options = _run(_mixed(tmp_path, "ttsstt"))
    assert [(c["engine"], c["pages"]) for c in calls] == [(D, 2), (D, 2), (G, [3, 4])]
    assert md == "[docling read 2]\n\n[glm_ocr read [3, 4]]\n\n[docling read 2]\n"
    decision = options.auto_decision
    assert decision.segments == [
        {"engine": "docling", "pages": "1-2"}, {"engine": "glm_ocr", "pages": "3-4"}, {"engine": "docling", "pages": "5-6"},
    ]
    assert options.engine_used == D and decision.reason.startswith("Mixed PDF: 4 text pages read with Docling")
    assert list(splits.iterdir()) == [], "temporary page PDFs are removed"


def test_balanced_converts_the_file_whole(tmp_path, engines):
    calls, _, _ = engines
    _run(_mixed(tmp_path, "ttsstt"), level="balanced")
    assert [c["engine"] for c in calls] == [G] and calls[0]["pages"] is None


def test_a_glm_ocr_time_over_the_limit_means_no_split(tmp_path, engines, monkeypatch):
    calls, _, _ = engines
    monkeypatch.setattr(auto_engine, "glm_estimate_for_pages", lambda n: 9999.0)
    _, options = _run(_mixed(tmp_path, "ttsstt"))
    assert options.auto_decision.segments is None


def test_a_failed_glm_ocr_run_is_read_by_docling_and_reported(tmp_path, engines):
    calls, failing, _ = engines
    failing.add(G)
    md, options = _run(_mixed(tmp_path, "ttss"))
    assert [(c["engine"], c["pages"]) for c in calls] == [(D, 2), (G, [3, 4]), (D, 2)]
    assert md == "[docling read 2]\n\n[docling read 2]\n"
    assert options.fallback_occurred
    assert any("Pages 3-4: GLM-OCR failed (glm_ocr broke); read with Docling instead." in w for w in options.engine_warnings)


def test_cancel_stops_between_runs(tmp_path, engines):
    calls, _, splits = engines
    registry = JobRegistry()
    job = registry.start("split-cancel-1")
    real = auto_engine._convert_once

    def cancel_after_first(*args):
        result = real(*args)
        registry.cancel("split-cancel-1")
        return result

    auto_engine._convert_once = cancel_after_first
    try:
        with pytest.raises(ConversionCancelledError):
            _run(_mixed(tmp_path, "ttss"), job=job)
    finally:
        auto_engine._convert_once = real
    assert len(calls) == 1
    assert list(splits.iterdir()) == []
