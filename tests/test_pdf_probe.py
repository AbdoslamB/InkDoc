"""Tests for app/core/pdf_probe.py: text layer, image coverage and the scan classifier."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.pdf_probe import is_pdf, probe_pdf  # noqa: E402
from tests._pdf_fixtures import image_page, prose_page, text_page, write_pdf  # noqa: E402


def _digital(n: int) -> list[dict]:
    return [prose_page(60, offset=i * 7) for i in range(n)]


def test_page_count_and_text(tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf", [text_page(["Hello world from page one"]), prose_page(40)])
    probe = probe_pdf(pdf)
    assert probe is not None
    assert probe.page_count == 2
    assert len(probe.pages) == 2
    assert "Hello world from page one" in probe.pages[0].text
    assert probe.pages[0].char_count == len("Helloworldfrompageone")
    assert not probe.truncated
    assert probe.scan_pages == [] and probe.image_pages == []


def test_image_only_detection(tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf", [prose_page(), image_page("rgb"), prose_page()])
    probe = probe_pdf(pdf)
    image = probe.pages[1]
    assert image.image_only and image.char_count == 0 and image.image_coverage == 1.0
    assert not probe.pages[0].image_only


def test_small_image_is_not_image_only(tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf", [image_page("rgb", width_fraction=0.3)])
    probe = probe_pdf(pdf)
    assert not probe.pages[0].image_only
    assert 0.25 < probe.pages[0].image_coverage < 0.35


def test_scanner_codecs_are_scan_likely(tmp_path):
    for kind in ("ccitt", "jbig2"):
        pdf = write_pdf(tmp_path / f"{kind}.pdf", _digital(3) + [image_page(kind)] + _digital(3))
        probe = probe_pdf(pdf)
        assert probe.scan_pages == [4], (kind, probe.pages[3])
        assert "scanner_codec" in probe.pages[3].scan_evidence


def test_grayscale_and_bilevel_full_page_are_scan_likely(tmp_path):
    for kind in ("gray", "bilevel"):
        pdf = write_pdf(tmp_path / f"{kind}.pdf", _digital(3) + [image_page(kind)] + _digital(3))
        probe = probe_pdf(pdf)
        assert probe.scan_pages == [4], kind
        assert "bilevel_or_gray_full_page" in probe.pages[3].scan_evidence


def test_color_cover_photo_is_not_a_scan(tmp_path):
    """A full-bleed colour photo in front of a digital report is a plain image page."""
    for kind in ("dct", "rgb"):
        pdf = write_pdf(tmp_path / f"{kind}.pdf", [image_page(kind)] + _digital(5))
        probe = probe_pdf(pdf)
        assert probe.scan_pages == [], kind
        assert probe.image_pages == [1], kind


def test_consecutive_image_pages_are_scan_likely(tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf", _digital(4) + [image_page("rgb"), image_page("rgb")] + _digital(4))
    probe = probe_pdf(pdf)
    assert probe.scan_pages == [5, 6]
    assert all("image_page_run" in probe.pages[i].scan_evidence for i in (4, 5))


def test_scanner_producer_is_evidence(tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf", [image_page("rgb")] + _digital(5), producer="ScanSnap Manager #S1300")
    probe = probe_pdf(pdf)
    assert probe.scan_pages == [1]
    assert "producer" in probe.pages[0].scan_evidence


def test_mostly_image_pages_is_a_scan(tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf", [image_page("rgb"), prose_page(), image_page("rgb")])
    probe = probe_pdf(pdf)
    assert probe.scan_pages == [1, 3]


def test_ocr_layer_detection(tmp_path):
    lines = ["This scanned page carries an invisible OCR text layer with enough words."]
    pdf = write_pdf(tmp_path / "a.pdf", [image_page("rgb", lines=lines), prose_page()])
    probe = probe_pdf(pdf)
    page = probe.pages[0]
    assert page.ocr_layer and not page.image_only and not page.scan_likely


def test_image_inside_form_xobject_counts_in_page_space(tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf", [image_page("rgb", in_form=True)] + _digital(3))
    probe = probe_pdf(pdf)
    assert probe.pages[0].image_coverage == 1.0
    assert probe.pages[0].image_only


def test_truncation_at_max_pages_and_time_budget(tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf", _digital(6))
    probe = probe_pdf(pdf, max_pages=4)
    assert probe.truncated and len(probe.pages) == 4 and probe.page_count == 6
    probe = probe_pdf(pdf, time_budget_s=-1)
    assert probe.truncated and probe.pages == [] and probe.page_count == 6


def test_unreadable_input_returns_none(tmp_path):
    encrypted = write_pdf(tmp_path / "enc.pdf", _digital(1), encrypt=True)
    assert probe_pdf(encrypted) is None
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4\nthis is not a pdf at all")
    assert probe_pdf(corrupt) is None
    assert probe_pdf(tmp_path / "missing.pdf") is None


def test_file_is_closed_after_probe(tmp_path):
    """On Windows an open handle would stop the server deleting its temp upload."""
    pdf = write_pdf(tmp_path / "a.pdf", _digital(2) + [image_page("ccitt")])
    assert probe_pdf(pdf) is not None
    os.remove(pdf)
    assert not pdf.exists()
    bad = write_pdf(tmp_path / "enc.pdf", _digital(1), encrypt=True)
    assert probe_pdf(bad) is None
    os.remove(bad)


def test_is_pdf_by_extension_or_magic(tmp_path):
    no_ext = tmp_path / "download"
    no_ext.write_bytes(b"%PDF-1.7\n...")
    assert is_pdf(no_ext)
    html = tmp_path / "page.html"
    html.write_text("<html></html>")
    assert not is_pdf(html)
    assert is_pdf(tmp_path / "named.pdf")  # by extension, even before it exists
    assert not is_pdf(tmp_path / "missing")


def test_new_modules_are_lazy():
    """No pypdfium2 at import, and the server does not import the new modules at startup."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import app.core.pdf_probe, app.core.quality_check, app.core.auto_engine\n"
        "assert 'pypdfium2' not in sys.modules, 'pypdfium2 imported at module level'\n"
        "for m in [m for m in list(sys.modules) if m.startswith('app.core.')]:\n"
        "    del sys.modules[m]\n"
        "import app.server.server\n"
        "loaded = [m for m in ('app.core.pdf_probe', 'app.core.quality_check', 'app.core.auto_engine', 'pypdfium2') if m in sys.modules]\n"
        "assert not loaded, loaded\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
