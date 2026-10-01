"""Tests for app/core/auto_engine.py.

The decisions (decide, should_escalate, pick_result) are pure and are tested
with synthetic probes and reports: no files, threads or servers. run_auto is
tested with fake lanes and a fake converter.
"""
from __future__ import annotations

import asyncio
import enum
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.core.auto_engine as auto  # noqa: E402
from app.core.auto_engine import (  # noqa: E402
    AutoDecision,
    decide,
    pick_result,
    run_auto,
    should_escalate,
)
from app.core.converter import ConversionOptions  # noqa: E402
from app.core.pdf_probe import PageProbe, PdfProbe  # noqa: E402
from app.core.quality_check import QualityReport  # noqa: E402
from app.core.queue_model import EngineKind, QueueItem, SourceKind  # noqa: E402
from tests._pdf_fixtures import image_page, prose_page, write_pdf  # noqa: E402

MD, DOCLING, MARKIT = EngineKind.MARKITDOWN, EngineKind.DOCLING, EngineKind.MARKIT


def probe(*kinds: str) -> PdfProbe:
    """kinds per page: 'text', 'scan' (scan-likely), 'image' (plain image page)."""
    pages = []
    for i, kind in enumerate(kinds):
        image_only = kind in ("scan", "image")
        pages.append(PageProbe(index=i, text="" if image_only else "words " * 40,
                               char_count=0 if image_only else 200,
                               image_coverage=1.0 if image_only else 0.0,
                               image_only=image_only, scan_likely=kind == "scan"))
    return PdfProbe(page_count=len(pages), pages=pages, producer="", truncated=False, elapsed_ms=1)


# ─── decide(): one test per routing-table row ────────────────────────────────

def test_scanned_pdf_goes_to_ocr_engine():
    d = decide("a.pdf", ocr_engines=[DOCLING], probe=probe("text", "scan", "scan"), pdf=True)
    assert d.engine == DOCLING and d.reason == "2 of 3 pages are scanned images" and d.hint == ""


def test_one_scanned_page_of_ten_routes_straight_to_ocr():
    d = decide("a.pdf", ocr_engines=[DOCLING], probe=probe(*(["text"] * 9 + ["scan"])), pdf=True)
    assert d.engine == DOCLING


def test_many_plain_image_pages_go_to_ocr():
    d = decide("a.pdf", ocr_engines=[DOCLING], probe=probe("image", "text", "image", "text", "text"), pdf=True)
    assert d.engine == DOCLING and d.reason == "2 of 5 pages are full-page images"


def test_lone_cover_photo_in_short_document_stays_on_markitdown():
    d = decide("a.pdf", ocr_engines=[DOCLING], probe=probe("image", "text", "text", "text"), pdf=True)
    assert d.engine == MD


def test_scanned_pdf_without_ocr_engine_uses_markitdown_with_hint():
    d = decide("a.pdf", ocr_engines=[], ocr_installable=True, probe=probe("scan", "scan"), pdf=True)
    assert d.engine == MD
    assert d.hint == "Docling would read the 2 scanned pages. Install it in Settings."


def test_no_install_hint_where_docling_is_unsupported():
    d = decide("a.pdf", ocr_engines=[], ocr_installable=False, probe=probe("scan"), pdf=True)
    assert d.engine == MD and d.hint == ""


def test_digital_pdf_and_lone_cover_photo_stay_on_markitdown():
    d = decide("a.pdf", ocr_engines=[DOCLING], probe=probe("text", "text"), pdf=True)
    assert d.engine == MD and d.reason == "Digital PDF with a full text layer"
    d = decide("a.pdf", ocr_engines=[DOCLING], probe=probe("image", *["text"] * 9), pdf=True)
    assert d.engine == MD and d.reason == "Digital PDF; page 1 is a full-page image"


def test_unreadable_pdf_goes_to_markitdown():
    d = decide("a.pdf", ocr_engines=[DOCLING], probe=None, pdf=True)
    assert d.engine == MD and "unreadable" in d.reason


@pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"])
def test_images_go_to_ocr(ext):
    assert decide(f"x{ext}", ocr_engines=[DOCLING], pdf=False).engine == DOCLING
    d = decide(f"x{ext}", ocr_engines=[], ocr_installable=True, pdf=False)
    assert d.engine == MD and "Install it in Settings" in d.hint


def test_gif_stays_on_markitdown():
    """Docling's image pipeline does not accept GIF."""
    assert decide("x.gif", ocr_engines=[DOCLING], pdf=False).engine == MD


@pytest.mark.parametrize("ext", [".epub", ".ipynb", ".rss", ".atom"])
def test_formats_with_markitdown_converters(ext):
    d = decide(f"x{ext}", ocr_engines=[DOCLING], pdf=False)
    assert d.engine == MD and "dedicated converter" in d.reason


@pytest.mark.parametrize("ext", [".yaml", ".yml", ".xml"])
def test_formats_routed_to_markit(ext):
    assert decide(f"x{ext}", ocr_engines=[DOCLING], pdf=False).engine == MARKIT


@pytest.mark.parametrize("ext", [".docx", ".xlsx", ".pptx", ".html", ".csv", ".json", ".txt", ".md", ".zip", ".mp3"])
def test_everything_else_goes_to_markitdown(ext):
    assert decide(f"x{ext}", ocr_engines=[DOCLING], pdf=False).engine == MD


def test_urls():
    assert decide("tmp.html", ocr_engines=[DOCLING], pdf=False, from_url=True).engine == MD
    d = decide("tmp.bin", ocr_engines=[DOCLING], probe=probe("scan"), pdf=True, from_url=True)
    assert d.engine == DOCLING and d.reason.startswith("URL: ")


def test_ocr_engine_order_is_respected():
    class Fake(str, enum.Enum):
        GLM_OCR = "glm_ocr"

    d = decide("a.pdf", ocr_engines=[Fake.GLM_OCR, DOCLING], probe=probe("scan"), pdf=True)
    assert d.engine == Fake.GLM_OCR


def test_decide_probes_real_files(tmp_path):
    pdf = write_pdf(tmp_path / "s.pdf", [prose_page(), image_page("ccitt"), prose_page()])
    assert decide(str(pdf), ocr_engines=[DOCLING]).engine == DOCLING
    digital = write_pdf(tmp_path / "d.pdf", [image_page("dct"), prose_page(), prose_page(), prose_page()])
    assert decide(str(digital), ocr_engines=[DOCLING]).engine == MD


# ─── should_escalate / pick_result ───────────────────────────────────────────

def report(engine=MD, *, warning=True, suggestion="docling", coverage=0.8, scan=(), tokens=100):
    return QualityReport(checked=True, coverage=coverage, missing_pct=None if coverage is None else round((1 - coverage) * 100),
                         warning=warning, suggestion=suggestion, scan_pages=list(scan),
                         engine=engine.value, output_tokens=tokens)


def test_escalates_only_when_check_fails_and_ocr_usable():
    md_decision = AutoDecision(MD, "Digital PDF")
    assert should_escalate(md_decision, report(), [DOCLING]) == DOCLING
    assert should_escalate(md_decision, report(warning=False), [DOCLING]) is None
    assert should_escalate(md_decision, report(), []) is None
    assert should_escalate(md_decision, report(suggestion="install_docling"), [DOCLING]) is None
    assert should_escalate(md_decision, None, [DOCLING]) is None
    # Auto already chose the OCR engine (it ran, or failed and fell back): no retry.
    assert should_escalate(AutoDecision(DOCLING, "scan"), report(), [DOCLING]) is None
    assert should_escalate(md_decision, report(DOCLING, suggestion="markitdown"), [DOCLING]) is None


def test_pick_result_keeps_better_coverage():
    assert pick_result(("markitdown", report(coverage=0.80)), ("docling", report(DOCLING, coverage=0.95))) == 1
    assert pick_result(("markitdown", report(coverage=0.95)), ("docling", report(DOCLING, coverage=0.94))) == 1  # near tie
    assert pick_result(("markitdown", report(coverage=0.95)), ("docling", report(DOCLING, coverage=0.80))) == 0


def test_pick_result_credits_ocr_for_scanned_pages():
    first = report(coverage=0.99, scan=[3, 4], tokens=500)
    assert pick_result(("markitdown", first), ("docling", report(DOCLING, coverage=0.93, tokens=900))) == 1
    assert pick_result(("markitdown", first), ("docling", report(DOCLING, coverage=0.60, tokens=10))) == 0


# ─── Availability ────────────────────────────────────────────────────────────

class _FakeManager:
    def __init__(self, base: Path, *, pack=True, busy=False, status="installed"):
        self.base, self.pack, self.busy, self.status = base, pack, busy, status

    def is_install_in_progress(self, _name):
        return self.busy

    def is_pack_installed(self, _name):
        return self.pack

    def get_engine_dir(self, _name):
        return self.base

    def get_engine_status(self, _name):
        from app.core.engine_manifest import EngineStatus
        return EngineStatus(self.status)


def _with_manager(mgr):
    return patch("app.core.engine_manager.EngineManager.get_instance", return_value=mgr)


def test_pack_without_models_is_not_usable(tmp_path):
    (tmp_path / "models").mkdir()
    with _with_manager(_FakeManager(tmp_path)):
        assert auto.docling_usable() is False
    (tmp_path / "models" / "layout").mkdir()
    (tmp_path / "models" / "layout" / "model.safetensors").write_bytes(b"x")
    with _with_manager(_FakeManager(tmp_path)):
        assert auto.docling_usable() is True
    with _with_manager(_FakeManager(tmp_path, busy=True)):
        assert auto.docling_usable() is False


def test_docling_removed_mid_session_routes_to_markitdown(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "m.bin").write_bytes(b"x")
    mgr = _FakeManager(tmp_path)
    with _with_manager(mgr):
        assert auto.ocr_engines_usable() == [DOCLING]
        mgr.pack, mgr.status = False, "installable"   # removed in Settings
        assert auto.ocr_engines_usable() == []
        assert auto.ocr_installable() is True


def test_source_mode_needs_models_in_hf_cache(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    monkeypatch.delenv("DOCLING_ARTIFACTS_PATH", raising=False)
    with _with_manager(_FakeManager(tmp_path, pack=False)):
        assert auto.docling_usable() is False
        (hub / "models--docling-project--docling-layout-heron").mkdir()
        assert auto.docling_usable() is True


def test_availability_never_raises():
    with patch("app.core.engine_manager.EngineManager.get_instance", side_effect=RuntimeError("x")):
        assert auto.docling_usable() is False
        assert auto.ocr_installable() is False


# ─── run_auto ────────────────────────────────────────────────────────────────

class Lanes:
    """Fake lane provider that records which lane each conversion ran in."""

    def __init__(self):
        self.held: list[str] = []
        self.active: dict[str, int] = {}

    def __call__(self, engine):
        lanes = self
        name = "docling" if engine == DOCLING else "default"

        class _Lane:
            async def __aenter__(self):
                lanes.held.append(name)
                lanes.active[name] = lanes.active.get(name, 0) + 1

            async def __aexit__(self, *exc):
                lanes.active[name] -= 1

        return _Lane()


def _run(coro):
    return asyncio.run(coro)


def _fake_converter(outputs: dict, calls: list, lanes: Lanes | None = None, fail: set = frozenset()):
    def fake(file_item, options, *, fetched=None):
        engine = file_item.engine
        calls.append((engine, threading.current_thread().name, dict(lanes.active) if lanes else None))
        if engine in fail:
            raise RuntimeError(f"{engine.value} exploded")
        options.engine_used = engine
        return outputs[engine]
    return fake


@pytest.fixture
def scanned_pdf(tmp_path):
    return write_pdf(tmp_path / "scan.pdf", [prose_page(80, 1), image_page("ccitt"), prose_page(80, 3)])


@pytest.fixture
def digital_pdf(tmp_path):
    return write_pdf(tmp_path / "doc.pdf", [prose_page(80, 1), prose_page(80, 9), prose_page(80, 17)])


def _item(path) -> QueueItem:
    return QueueItem(source=str(path), kind=SourceKind.FILE, display_name=Path(path).name, engine=EngineKind.AUTO)


def _patch_ocr(engines):
    # Docling is the engine install hints name; GLM-OCR stays out of it so the
    # result does not depend on what is installed on the test machine.
    return patch.multiple(
        auto,
        ocr_engines_usable=lambda: list(engines),
        ocr_installable=lambda: not engines,
        docling_installable=lambda: not engines,
        glm_ocr_installable=lambda: False,
    )


def test_run_auto_routes_scan_to_docling_in_docling_lane(scanned_pdf):
    calls, lanes = [], Lanes()
    options = ConversionOptions(engine=EngineKind.AUTO)
    with _patch_ocr([DOCLING]), patch("app.core.converter.convert_local",
                                      _fake_converter({DOCLING: "docling text"}, calls, lanes)):
        md = _run(run_auto(_item(scanned_pdf), options, lane=lanes))
    assert md == "docling text"
    assert [c[0] for c in calls] == [DOCLING]
    assert calls[0][2] == {"docling": 1}            # held the Docling lane, not a default slot
    assert "MainThread" not in calls[0][1]
    assert options.engine_used == DOCLING and options.auto_decision.engine == DOCLING
    assert options.quality_report is not None and not options.quality_report.scan_pages


def test_run_auto_digital_pdf_single_pass(digital_pdf):
    from tests._pdf_fixtures import words

    calls = []
    good = "\n".join([words(80, 1), words(80, 9), words(80, 17)])
    options = ConversionOptions(engine=EngineKind.AUTO)
    with _patch_ocr([DOCLING]), patch("app.core.converter.convert_local", _fake_converter({MD: good}, calls)):
        md = _run(run_auto(_item(digital_pdf), options, lane=Lanes()))
    assert md == good and [c[0] for c in calls] == [MD]
    assert not options.auto_decision.escalated and not options.quality_report.warning


def test_run_auto_escalates_and_keeps_better_result(digital_pdf):
    from tests._pdf_fixtures import words

    calls = []
    partial = words(80, 1)                                    # pages 2 and 3 missing
    full = "\n".join([words(80, 1), words(80, 9), words(80, 17)])
    options = ConversionOptions(engine=EngineKind.AUTO)
    with _patch_ocr([DOCLING]), patch("app.core.converter.convert_local",
                                      _fake_converter({MD: partial, DOCLING: full}, calls)):
        md = _run(run_auto(_item(digital_pdf), options, lane=Lanes()))
    assert [c[0] for c in calls] == [MD, DOCLING]
    assert md == full and options.engine_used == DOCLING
    esc = options.auto_decision.escalation
    assert options.auto_decision.escalated and esc["from"] == "markitdown" and esc["kept"] == "docling"
    assert esc["from_missing_pct"] > 10 and esc["error"] is None
    assert not options.quality_report.warning


def test_run_auto_escalation_failure_keeps_markitdown(digital_pdf):
    from tests._pdf_fixtures import words

    calls = []
    options = ConversionOptions(engine=EngineKind.AUTO)
    with _patch_ocr([DOCLING]), patch("app.core.converter.convert_local",
                                      _fake_converter({MD: words(80, 1)}, calls, fail={DOCLING})):
        md = _run(run_auto(_item(digital_pdf), options, lane=Lanes()))
    assert md == words(80, 1) and options.engine_used == MD
    esc = options.auto_decision.escalation
    assert esc["kept"] == "markitdown" and "docling exploded" in esc["error"]
    assert options.quality_report.warning  # the warning stays


def test_run_auto_initial_docling_failure_falls_back_even_with_setting_off(scanned_pdf):
    calls, lanes = [], Lanes()
    options = ConversionOptions(engine=EngineKind.AUTO)
    with _patch_ocr([DOCLING]), patch("app.core.converter.convert_local",
                                      _fake_converter({MD: "md text"}, calls, lanes, fail={DOCLING})), \
         patch("app.core.engine_manager.EngineManager.get_settings", return_value={"fallback_to_markitdown": False}):
        md = _run(run_auto(_item(scanned_pdf), options, lane=lanes))
    assert md == "md text"
    assert [c[0] for c in calls] == [DOCLING, MD]
    assert calls[1][2] == {"docling": 0, "default": 1}  # fallback ran after leaving the Docling lane
    assert options.fallback_occurred and "docling exploded" in options.fallback_reason
    assert options.engine_used == MD
    assert options.allow_engine_fallback is False      # Docling's own fallback was disabled


def test_run_auto_without_ocr_engine_never_calls_docling(scanned_pdf):
    calls = []
    options = ConversionOptions(engine=EngineKind.AUTO)
    with _patch_ocr([]), patch("app.core.converter.convert_local", _fake_converter({MD: "partial"}, calls)):
        _run(run_auto(_item(scanned_pdf), options, lane=Lanes()))
    assert [c[0] for c in calls] == [MD]
    assert options.auto_decision.hint.startswith("Docling would read")
    assert options.quality_report.suggestion == "install_docling"


def test_run_auto_drops_enrichment_when_model_missing(scanned_pdf):
    seen = {}

    def fake(file_item, options, *, fetched=None):
        seen["code"] = options.docling_code_enrichment
        options.engine_used = file_item.engine
        return "text"

    options = ConversionOptions(engine=EngineKind.AUTO, docling_code_enrichment=True)
    with _patch_ocr([DOCLING]), patch.object(auto, "_enrichment_usable", return_value=False), \
         patch("app.core.converter.convert_local", fake):
        _run(run_auto(_item(scanned_pdf), options, lane=Lanes()))
    assert seen["code"] is False
    assert "without code/formula enrichment" in options.auto_decision.reason


def test_run_auto_quality_check_disabled(digital_pdf):
    options = ConversionOptions(engine=EngineKind.AUTO, quality_check=False)
    with _patch_ocr([DOCLING]), patch("app.core.converter.convert_local", _fake_converter({MD: "x"}, [])):
        _run(run_auto(_item(digital_pdf), options, lane=Lanes()))
    assert options.quality_report is None and not options.auto_decision.escalated


def test_run_auto_cancel_stops_before_next_phase(digital_pdf):
    from app.core.jobs import ConversionCancelledError, JobRegistry

    registry = JobRegistry()
    job = registry.start("job-cancel-test")
    calls = []

    def fake(file_item, options, *, fetched=None):
        calls.append(file_item.engine)
        registry.cancel("job-cancel-test")  # user clicks Cancel while MarkItDown runs
        options.engine_used = file_item.engine
        return "partial"

    options = ConversionOptions(engine=EngineKind.AUTO)
    with _patch_ocr([DOCLING]), patch("app.core.converter.convert_local", fake), \
            pytest.raises(ConversionCancelledError):
        _run(run_auto(_item(digital_pdf), options, lane=Lanes(), job=job))
    assert calls == [MD]  # no Docling retry after the cancel


def test_run_auto_never_downloads(scanned_pdf):
    """No network: patch huggingface_hub's download entry points to fail loudly."""
    def boom(*_a, **_k):
        raise AssertionError("Auto attempted a model download")

    targets = {}
    try:
        import huggingface_hub  # noqa: F401
        targets = {"huggingface_hub.hf_hub_download": boom, "huggingface_hub.snapshot_download": boom}
    except ImportError:
        pass
    patches = [patch(name, side_effect=fn) for name, fn in targets.items()]
    for p in patches:
        p.start()
    try:
        options = ConversionOptions(engine=EngineKind.AUTO)
        with _patch_ocr([]), patch("app.core.converter.convert_local", _fake_converter({MD: "x"}, [])):
            _run(run_auto(_item(scanned_pdf), options, lane=Lanes()))
    finally:
        for p in patches:
            p.stop()


def test_non_pdf_runs_no_check(tmp_path):
    f = tmp_path / "notes.yaml"
    f.write_text("a: 1")
    options = ConversionOptions(engine=EngineKind.AUTO)
    calls = []
    with _patch_ocr([DOCLING]), patch("app.core.converter.convert_local", _fake_converter({MARKIT: "yaml md"}, calls)):
        md = _run(run_auto(_item(f), options, lane=Lanes()))
    assert md == "yaml md" and calls[0][0] == MARKIT and options.quality_report is None


def test_convert_item_with_auto_runs_blocking(tmp_path):
    """The AGENTS.md example (convert_item from a script) works with engine=auto."""
    from app.core.converter import convert_item

    f = tmp_path / "plain.txt"
    f.write_text("Hello from a plain text file")
    item = QueueItem(source=str(f), kind=SourceKind.FILE, display_name="plain.txt", engine=EngineKind.AUTO)
    options = ConversionOptions(engine=EngineKind.AUTO)
    with _patch_ocr([]):
        md = convert_item(item, options)
    assert "Hello from a plain text file" in md
    assert options.engine_used == MD and options.auto_decision.engine == MD
