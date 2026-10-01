"""GLM-OCR conversion engine: pages, request shape, budgets, guards, progress and cancel.

The llama-server is replaced by a fake that records each page image it is sent,
so these tests run without the 1.4 GB model.
"""
from __future__ import annotations

import base64
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.core.engines.glm_ocr_engine as ge  # noqa: E402
from app.core.converter import ConversionOptions, UserFacingConversionError  # noqa: E402
from app.core.engines.glm_ocr_server import GlmOcrServerError, OcrResult  # noqa: E402
from app.core.jobs import ConversionCancelledError, JobRegistry  # noqa: E402
from app.core.queue_model import EngineKind, QueueItem, SourceKind  # noqa: E402
from tests._pdf_fixtures import prose_page, write_pdf  # noqa: E402


class FakeServer:
    accel = "cpu"

    def __init__(self, replies=None, finish="stop"):
        self.replies = list(replies or [])
        self.finish = finish
        self.calls: list[dict] = []
        self.running = True
        self.before_call = None

    def is_running(self):
        return self.running

    def ocr(self, data_uri, prompt, max_tokens, *, repeat_penalty=None):
        if self.before_call:
            self.before_call(len(self.calls))
        png = base64.b64decode(data_uri.split(",", 1)[1])
        img = Image.open(io.BytesIO(png))
        img.load()
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens, "repeat_penalty": repeat_penalty,
                           "size": img.size, "image": img, "uri_prefix": data_uri[:22]})
        text = self.replies.pop(0) if self.replies else f"Page text {len(self.calls)}"
        return OcrResult(text=text, finish_reason=self.finish, prompt_tokens=0, completion_tokens=10, seconds=0.01)

    def take_notices(self):
        return []


class FakeManager:
    def __init__(self, long_side=1600, installed=True, usable=True):
        self.long_side, self.installed, self.usable = long_side, installed, usable
        self.measured = []

    def is_installed(self):
        return self.installed

    def is_usable(self):
        return self.usable

    def render_long_side(self):
        return self.long_side

    def record_measurement(self, spp, pages, accel=None):
        self.measured.append((spp, pages))


@pytest.fixture
def fake(monkeypatch):
    server, manager = FakeServer(), FakeManager()
    monkeypatch.setattr("app.core.engines.glm_ocr_server.GlmOcrServer.get_instance", lambda: server)
    monkeypatch.setattr("app.core.glm_ocr_manager.GlmOcrManager.get_instance", lambda: manager)
    return server, manager


def convert(path: Path, options: ConversionOptions | None = None) -> tuple[str, ConversionOptions]:
    opts = options or ConversionOptions(engine=EngineKind.GLM_OCR)
    item = QueueItem(source=str(path), kind=SourceKind.FILE, display_name=path.name, engine=EngineKind.GLM_OCR)
    return ge.convert_with_glm_ocr(item, opts), opts


# ─── Pure helpers ────────────────────────────────────────────────────────────

def test_output_budget_clamps_and_refuses_overflow():
    assert ge.output_budget(8192, 2000) == 4096
    assert ge.output_budget(8192, 5500) == 8192 - 5500 - 64
    assert ge.output_budget(8192, 7500) is None


def test_scaled_size_never_upscales_more_than_twice():
    assert ge.scaled_size(800, 1200, 1600) == (1067, 1600)
    assert ge.scaled_size(100, 50, 1600) == (200, 100)
    assert ge.scaled_size(4000, 3000, 1600) == (1600, 1200)


def test_repetition_detection_and_cut():
    looped = "Intro line\n" + "| a | b |\n" * 12
    cut = ge.find_repetition_start(looped)
    assert cut is not None and looped[:cut].count("| a | b |") == 1
    chars = "Total: " + "ha" * 60
    cut = ge.find_repetition_start(chars)
    assert cut is not None and chars[:cut].endswith("haha"[:2]) and len(chars[:cut]) < 20
    assert ge.find_repetition_start("A normal page\nwith different lines\nand a table\n| 1 | 2 |\n| 3 | 4 |") is None


def test_code_fence_wrapping_is_stripped():
    assert ge.clean_page_output("```markdown\n# Title\ntext\n```") == "# Title\ntext"
    assert ge.clean_page_output("  plain  ") == "plain"


# ─── Conversion ──────────────────────────────────────────────────────────────

def test_pdf_pages_are_rendered_sent_and_joined(fake, tmp_path):
    server, manager = fake
    pdf = write_pdf(tmp_path / "doc.pdf", [prose_page(30, 1), prose_page(30, 2), prose_page(30, 3)])
    md, _ = convert(pdf)
    assert len(server.calls) == 3
    assert md == "Page text 1\n\nPage text 2\n\nPage text 3\n"
    for call in server.calls:
        assert call["prompt"] == "Text Recognition:"
        assert 1024 <= call["max_tokens"] <= 4096
        assert call["uri_prefix"] == "data:image/png;base64,"
        assert max(call["size"]) == 1600
    assert manager.measured, "real speed feeds the estimate"


def test_unsupported_format_is_explained(fake, tmp_path):
    doc = tmp_path / "notes.docx"
    doc.write_bytes(b"PK\x03\x04 not an image")
    with pytest.raises(ge.GlmOcrUnsupportedFormat) as err:
        convert(doc)
    assert str(err.value).startswith("Unsupported format")
    assert "MarkItDown" in str(err.value)
    assert isinstance(err.value, UserFacingConversionError)


def test_password_protected_pdf_gets_a_clear_message(fake, tmp_path):
    pdf = write_pdf(tmp_path / "locked.pdf", [prose_page()], encrypt=True)
    with pytest.raises(UserFacingConversionError, match="password-protected"):
        convert(pdf)


def test_multi_frame_tiff_is_one_page_per_frame(fake, tmp_path):
    server, _ = fake
    frames = [Image.new("RGB", (400, 600), c) for c in ("white", "gray")]
    tif = tmp_path / "scan.tiff"
    frames[0].save(tif, save_all=True, append_images=frames[1:])
    convert(tif)
    assert len(server.calls) == 2


def test_exif_rotated_photo_arrives_upright(fake, tmp_path):
    server, _ = fake
    img = Image.new("RGB", (400, 200), "white")      # stored landscape...
    exif = Image.Exif()
    exif[0x0112] = 6                                  # ...shown rotated 90° (portrait)
    photo = tmp_path / "phone.jpg"
    img.save(photo, exif=exif)
    convert(photo)
    w, h = server.calls[0]["size"]
    assert h > w, "the EXIF orientation must be applied before OCR"


def test_transparent_png_is_flattened_on_white(fake, tmp_path):
    server, _ = fake
    png = tmp_path / "clear.png"
    Image.new("RGBA", (300, 300), (0, 0, 0, 0)).save(png)
    convert(png)
    sent = server.calls[0]["image"].convert("RGB")
    assert sent.getpixel((10, 10)) == (255, 255, 255)


def test_page_too_big_for_the_context_is_rerendered_smaller(fake, tmp_path, monkeypatch):
    server, _ = fake
    monkeypatch.setattr("app.core.engines.glm_ocr_server.DEFAULT_CTX_SIZE", 3000)
    pdf = write_pdf(tmp_path / "big.pdf", [prose_page()])
    convert(pdf)
    assert max(server.calls[0]["size"]) == 1200, "75% of 1600 fits the smaller context"


def test_repetition_guard_retries_then_cuts_and_warns(fake, tmp_path):
    server, _ = fake
    loop = "Header\n" + "same line\n" * 20
    server.replies = [loop, loop]
    png = tmp_path / "p.png"
    Image.new("RGB", (500, 700), "white").save(png)
    md, opts = convert(png)
    assert [c["repeat_penalty"] for c in server.calls] == [None, 1.1]
    assert md.count("same line") == 1
    assert any("repeating" in w for w in opts.engine_warnings)


def test_length_limit_triggers_the_retry(fake, tmp_path):
    server, _ = fake
    server.finish = "length"
    png = tmp_path / "p.png"
    Image.new("RGB", (500, 700), "white").save(png)
    _, opts = convert(png)
    assert len(server.calls) == 2 and opts.engine_warnings


def test_progress_is_reported_after_each_page(fake, tmp_path):
    server, _ = fake
    registry = JobRegistry()
    job = registry.start("job-progress-1")
    seen = []
    server.before_call = lambda n: seen.append((registry.get("job-progress-1")["page"], registry.get("job-progress-1")["pages"]))
    pdf = write_pdf(tmp_path / "doc.pdf", [prose_page(), prose_page(), prose_page()])
    convert(pdf, ConversionOptions(engine=EngineKind.GLM_OCR, job=job))
    assert seen == [(0, 3), (1, 3), (2, 3)]
    final = registry.get("job-progress-1")
    assert final["page"] == 3 and final["pages"] == 3


def test_cancel_stops_between_pages(fake, tmp_path):
    server, _ = fake
    registry = JobRegistry()
    job = registry.start("job-cancel-01")
    server.before_call = lambda n: registry.cancel("job-cancel-01") if n == 0 else None
    pdf = write_pdf(tmp_path / "doc.pdf", [prose_page(), prose_page(), prose_page()])
    with pytest.raises(ConversionCancelledError):
        convert(pdf, ConversionOptions(engine=EngineKind.GLM_OCR, job=job))
    assert len(server.calls) == 1, "cancel takes effect before the next page"


def test_server_errors_reach_the_user_verbatim(fake, tmp_path):
    server, _ = fake

    def fail(*a, **k):
        raise GlmOcrServerError("The GLM-OCR server did not become ready within 90 seconds.")

    server.ocr = fail
    png = tmp_path / "p.png"
    Image.new("RGB", (100, 100), "white").save(png)
    with pytest.raises(UserFacingConversionError, match="90 seconds"):
        convert(png)


def test_not_installed_or_not_selftested_is_refused(monkeypatch, tmp_path):
    png = tmp_path / "p.png"
    Image.new("RGB", (100, 100), "white").save(png)
    for manager, needle in ((FakeManager(installed=False), "not installed"), (FakeManager(usable=False), "self-test")):
        with patch("app.core.glm_ocr_manager.GlmOcrManager.get_instance", return_value=manager),                 pytest.raises(UserFacingConversionError, match=needle):
            convert(png)


def test_estimate_counts_pages(monkeypatch, tmp_path):
    class M(FakeManager):
        def estimate_seconds(self, pages):
            return pages * 20.0

        def seconds_per_page(self):
            return 20.0

        def active_accel(self):
            return "cpu"

    monkeypatch.setattr("app.core.glm_ocr_manager.GlmOcrManager.get_instance", lambda: M())
    pdf = write_pdf(tmp_path / "doc.pdf", [prose_page()] * 20)
    est = ge.estimate_for_file(str(pdf))
    assert est["pages"] == 20 and est["seconds_est"] == 400.0 and est["long_job"] is True


def test_cold_first_page_is_not_counted_as_page_speed(fake, tmp_path):
    server, manager = fake
    server.running = False

    def warm_up(n):
        server.running = True

    server.before_call = warm_up
    pdf = write_pdf(tmp_path / "doc.pdf", [prose_page(), prose_page(), prose_page()])
    convert(pdf)
    assert manager.measured and manager.measured[0][1] == 2, "only the two warm pages count"
