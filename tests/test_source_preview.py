"""The preview's Original view: a PDF kept by the server and drawn page by page.

POST /preview/pdf keeps an upload and gives its page sizes, GET .../page/{n}
draws one page as a PNG, DELETE forgets it. All three need the session token,
and the page's policy is not loosened for any of it (no frames, no objects).
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pdfium = pytest.importorskip("pypdfium2")

from fastapi.testclient import TestClient  # noqa: E402

import app.server.server as server_module  # noqa: E402
from app.core import pdf_pages  # noqa: E402

client = TestClient(server_module.app)
TOKEN = {"X-InkDoc-Token": server_module.SESSION_TOKEN}


def _pdf(*sizes: tuple[float, float]) -> bytes:
    doc = pdfium.PdfDocument.new()
    for w, h in sizes:
        doc.new_page(w, h)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _open(data: bytes, name: str = "a.pdf"):
    return client.post("/preview/pdf", files={"file": (name, data, "application/pdf")}, headers=TOKEN)


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


@pytest.fixture(autouse=True)
def _no_previews_left():
    yield
    server_module._remove_preview_files(server_module._take_stale_previews(0.0, keep=0))


def test_open_gives_page_sizes_and_draws_pages():
    resp = _open(_pdf((612, 792), (792, 612)))
    assert resp.status_code == 200
    data = resp.json()
    assert data["pages"] == [[612.0, 792.0], [792.0, 612.0]]

    page = client.get(f"/preview/pdf/{data['id']}/page/2?width=800", headers=TOKEN)
    assert page.status_code == 200
    assert page.headers["content-type"] == "image/png"
    assert page.headers["cache-control"] == "no-store"
    w, h = _png_size(page.content)
    assert w == 800 and abs(h - 800 * 612 / 792) <= 1


def test_width_is_clamped():
    pid = _open(_pdf((612, 792))).json()["id"]
    small = client.get(f"/preview/pdf/{pid}/page/1?width=10", headers=TOKEN)
    big = client.get(f"/preview/pdf/{pid}/page/1?width=9000", headers=TOKEN)
    assert _png_size(small.content)[0] == pdf_pages.PREVIEW_MIN_WIDTH
    assert _png_size(big.content)[0] == pdf_pages.PREVIEW_MAX_WIDTH


def test_a_very_tall_page_stays_within_the_pixel_cap(tmp_path):
    path = tmp_path / "tall.pdf"
    path.write_bytes(_pdf((200, 14000)))
    w, h = _png_size(pdf_pages.render_page_png(path, 1, 2400))
    assert w * h <= pdf_pages.PREVIEW_MAX_PIXELS * 1.01
    with pytest.raises(ValueError):
        pdf_pages.render_page_png(path, 2, 800)


def test_every_endpoint_needs_the_token():
    data = _pdf((612, 792))
    assert client.post("/preview/pdf", files={"file": ("a.pdf", data, "application/pdf")}).status_code == 403
    pid = _open(data).json()["id"]
    assert client.get(f"/preview/pdf/{pid}/page/1").status_code == 403
    assert client.delete(f"/preview/pdf/{pid}").status_code == 403
    assert pid in server_module._source_previews


def test_unknown_preview_and_page_are_404():
    pid = _open(_pdf((612, 792))).json()["id"]
    assert client.get(f"/preview/pdf/{pid}/page/2", headers=TOKEN).status_code == 404
    assert client.get(f"/preview/pdf/{pid}/page/0", headers=TOKEN).status_code == 404
    assert client.get("/preview/pdf/nope/page/1", headers=TOKEN).status_code == 404


def test_refusals_leave_no_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(server_module.tempfile, "tempdir", str(tmp_path))
    not_pdf = _open(b"just text", "a.pdf")
    assert not_pdf.status_code == 415
    broken = _open(b"%PDF-1.4 not really", "b.pdf")
    assert broken.status_code == 422
    assert "can't be shown" in broken.json()["detail"]
    assert list(tmp_path.iterdir()) == []


def test_a_cancelled_open_leaves_no_copy(monkeypatch, tmp_path):
    # The page gives up while the pages are counted (CancelledError is no Exception).
    import asyncio
    import time

    monkeypatch.setattr(server_module.tempfile, "tempdir", str(tmp_path))

    def cancelled(path):
        raise asyncio.CancelledError()

    monkeypatch.setattr(pdf_pages, "page_sizes", cancelled)
    with pytest.raises((asyncio.CancelledError, RuntimeError)):  # the test client: "No response returned"
        _open(_pdf((612, 792)))
    deadline = time.monotonic() + 5
    while list(tmp_path.iterdir()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert list(tmp_path.iterdir()) == [], "the copy outlived its cancelled request"


def test_close_deletes_the_copy():
    pid = _open(_pdf((612, 792))).json()["id"]
    path = server_module._source_previews[pid].path
    assert os.path.exists(path)
    assert client.delete(f"/preview/pdf/{pid}", headers=TOKEN).json() == {"status": "closed"}
    assert not os.path.exists(path)
    assert client.delete(f"/preview/pdf/{pid}", headers=TOKEN).json() == {"status": "not_open"}
    assert client.get(f"/preview/pdf/{pid}/page/1", headers=TOKEN).status_code == 404


def test_only_the_most_recently_used_are_kept(monkeypatch):
    monkeypatch.setattr(server_module, "SOURCE_PREVIEW_MAX", 2)
    data = _pdf((612, 792))
    first = _open(data).json()["id"]
    first_path = server_module._source_previews[first].path
    second = _open(data).json()["id"]
    # The first is the older; looking at it makes the second the least recently used.
    server_module._source_previews[first].used -= 200
    server_module._source_previews[second].used -= 100
    assert client.get(f"/preview/pdf/{first}/page/1?width=200", headers=TOKEN).status_code == 200
    second_path = server_module._source_previews[second].path
    third = _open(data).json()["id"]
    assert set(server_module._source_previews) == {first, third}
    assert os.path.exists(first_path) and not os.path.exists(second_path)


def test_idle_previews_are_removed():
    pid = _open(_pdf((612, 792))).json()["id"]
    path = server_module._source_previews[pid].path
    later = server_module._source_previews[pid].used + server_module.SOURCE_PREVIEW_IDLE_S + 1
    server_module._remove_preview_files(server_module._take_stale_previews(later))
    assert pid not in server_module._source_previews
    assert not os.path.exists(path)


def test_the_page_policy_allows_no_frames_or_objects():
    policy = server_module.page_csp("n")
    assert "frame-src" not in policy and "object-src" not in policy and "child-src" not in policy
    assert "default-src 'self';" in policy
