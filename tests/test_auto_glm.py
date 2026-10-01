"""Auto routing with GLM-OCR: order, image formats, the time limit, and the missing-text check."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.core.auto_engine as auto  # noqa: E402
from app.core.auto_engine import decide, format_duration, glm_time_gate, run_auto  # noqa: E402
from app.core.converter import ConversionOptions  # noqa: E402
from app.core.pdf_probe import PageProbe, PdfProbe  # noqa: E402
from app.core.quality_check import assess  # noqa: E402
from app.core.queue_model import EngineKind, QueueItem, SourceKind  # noqa: E402
from tests._pdf_fixtures import image_page, prose_page, write_pdf  # noqa: E402

GLM, DOCLING, MD = EngineKind.GLM_OCR, EngineKind.DOCLING, EngineKind.MARKITDOWN


def scan_probe(pages: int = 3) -> PdfProbe:
    return PdfProbe(
        page_count=pages,
        pages=[PageProbe(index=i, text="", char_count=0, image_coverage=1.0, image_only=True, scan_likely=True)
               for i in range(pages)],
        producer="", truncated=False, elapsed_ms=1,
    )


# ─── Pure ────────────────────────────────────────────────────────────────────

def test_glm_ocr_comes_first_when_usable():
    with patch.multiple(auto, glm_ocr_usable=lambda: True, docling_usable=lambda: True):
        assert auto.ocr_engines_usable() == [GLM, DOCLING]
    with patch.multiple(auto, glm_ocr_usable=lambda: True, docling_usable=lambda: False):
        assert auto.ocr_engines_usable() == [GLM]


def test_time_gate_keeps_glm_under_the_limit():
    engines, note = glm_time_gate([GLM, DOCLING], 120.0, 300.0)
    assert engines == [GLM, DOCLING] and note == ""
    assert glm_time_gate([GLM, DOCLING], None, 300.0) == ([GLM, DOCLING], "")


def test_time_gate_falls_back_to_docling_over_the_limit():
    engines, note = glm_time_gate([GLM, DOCLING], 1500.0, 300.0)
    assert engines == [DOCLING]
    assert "25 min" in note and "Docling was used" in note


def test_time_gate_without_docling_explains_how_to_run_glm():
    engines, note = glm_time_gate([GLM], 1500.0, 300.0)
    assert engines == []
    assert "Choose GLM-OCR" in note


def test_format_duration():
    assert format_duration(45) == "45 s"
    assert format_duration(360) == "6 min"
    assert format_duration(4200) == "1 h 10 min"
    assert format_duration(7200) == "2 h"


def test_scans_and_images_go_to_glm_first():
    assert decide("a.pdf", ocr_engines=[GLM, DOCLING], probe=scan_probe(), pdf=True).engine == GLM
    assert decide("photo.jpg", ocr_engines=[GLM, DOCLING], pdf=False).engine == GLM


def test_gif_goes_to_glm_but_not_to_docling():
    assert decide("scan.gif", ocr_engines=[GLM, DOCLING], pdf=False).engine == GLM
    assert decide("scan.gif", ocr_engines=[DOCLING], pdf=False).engine == MD


def test_install_hint_names_the_installable_engine():
    d = decide("a.pdf", ocr_engines=[], ocr_installable=True, probe=scan_probe(2), pdf=True, install_name="GLM-OCR")
    assert d.engine == MD and d.hint.startswith("GLM-OCR would read")


# ─── run_auto with the gate ──────────────────────────────────────────────────

class Lanes:
    def __init__(self):
        self.held: list[str] = []

    def __call__(self, engine):
        lanes = self

        class _Lane:
            async def __aenter__(self):
                lanes.held.append(engine.value)

            async def __aexit__(self, *exc):
                return None

        return _Lane()


def _run_with(tmp_path, *, engines, estimate, monkeypatch):
    pdf = write_pdf(tmp_path / "scan.pdf", [image_page("ccitt"), image_page("ccitt")])
    calls = []

    def fake_convert(file_item, options, *, fetched=None):
        calls.append(file_item.engine)
        options.engine_used = file_item.engine
        return "Recognised text " * 20

    monkeypatch.setattr(auto, "ocr_engines_usable", lambda: list(engines))
    monkeypatch.setattr(auto, "ocr_install_target", lambda: None if engines else "docling")
    monkeypatch.setattr(auto, "glm_estimate_seconds", lambda path, probe, pdf: estimate)
    options = ConversionOptions(engine=EngineKind.AUTO)
    item = QueueItem(source=str(pdf), kind=SourceKind.FILE, display_name="scan.pdf", engine=EngineKind.AUTO)
    lanes = Lanes()
    with patch("app.core.converter.convert_local", fake_convert):
        asyncio.run(run_auto(item, options, lane=lanes))
    return calls, options, lanes


def test_short_scan_runs_on_glm_in_its_lane(tmp_path, monkeypatch):
    calls, options, lanes = _run_with(tmp_path, engines=[GLM, DOCLING], estimate=60.0, monkeypatch=monkeypatch)
    assert calls == [GLM] and lanes.held == ["glm_ocr"]
    assert options.engine_used == GLM


def test_long_scan_falls_back_to_docling_and_says_why(tmp_path, monkeypatch):
    calls, options, _ = _run_with(tmp_path, engines=[GLM, DOCLING], estimate=1800.0, monkeypatch=monkeypatch)
    assert calls == [DOCLING]
    assert "GLM-OCR would take about 30 min" in options.auto_decision.reason
    assert options.auto_decision.engine == DOCLING


def test_long_scan_without_docling_uses_markitdown_with_a_tip(tmp_path, monkeypatch):
    calls, options, _ = _run_with(tmp_path, engines=[GLM], estimate=1800.0, monkeypatch=monkeypatch)
    assert calls == [MD]
    assert "Choose GLM-OCR" in options.auto_decision.hint


def test_estimate_uses_page_count(tmp_path):
    class M:
        def estimate_seconds(self, pages):
            return pages * 10.0

    with patch("app.core.glm_ocr_manager.GlmOcrManager.get_instance", return_value=M()):
        assert auto.glm_estimate_seconds("x.pdf", scan_probe(7), True) == 70.0
        assert auto.glm_estimate_seconds("x.pdf", None, True) is None


# ─── Missing-text check names GLM-OCR correctly ──────────────────────────────

@pytest.fixture
def digital_pdf(tmp_path):
    return write_pdf(tmp_path / "doc.pdf", [prose_page(120, 1), prose_page(120, 200)])


def test_glm_output_missing_text_suggests_markitdown_not_itself(digital_pdf):
    report = assess(str(digital_pdf), "only a few words", GLM, ocr_engine=GLM)
    assert report.warning
    assert "GLM-OCR may have skipped" in report.message
    assert report.suggestion == "markitdown"


def test_install_hint_can_name_glm(tmp_path):
    pdf = write_pdf(tmp_path / "s.pdf", [image_page("ccitt"), image_page("ccitt")])
    report = assess(str(pdf), "", MD, ocr_engine=None, ocr_installable=True, ocr_install_engine="glm_ocr")
    assert report.suggestion == "install_glm_ocr"
    assert "GLM-OCR" in report.message and "Docling" not in report.message
