"""The layout model runner (app/core/engines/layout_model.py).

Box handling is pure and tested with hand-made candidates; detection runs on
real PDF pages with a stand-in model. One test uses the real model when it is
on this computer (the layout_model add-on, or the Docling pack's copy) and is
skipped otherwise, as in CI.
"""
from __future__ import annotations

import pytest

import app.core.engines.layout_model as lm
from tests._pdf_fixtures import write_pdf
from tests._table_fixtures import FIXTURES


def _c(box, score):
    return {"box": [float(v) for v in box], "score": score}


# ─── One box per table ───────────────────────────────────────────────────────
def test_a_box_holding_two_tables_side_by_side_is_dropped():
    left, right = _c((100, 200, 500, 400), 0.7), _c((640, 200, 1000, 400), 0.6)
    union = _c((95, 195, 1005, 405), 0.9)
    assert lm.clean_up([union, left, right]) == [left, right], "two tables stay two"


def test_duplicates_and_fragments_give_way_to_the_strongest_box():
    table = _c((100, 200, 500, 400), 0.8)
    duplicate = _c((104, 204, 505, 398), 0.5)
    fragment = _c((110, 300, 490, 340), 0.4)
    assert lm.clean_up([duplicate, fragment, table]) == [table]
    # A stronger fragment does not replace the table around it.
    assert lm.clean_up([_c((110, 300, 490, 340), 0.9), table]) == [_c((110, 300, 490, 340), 0.9)]


def test_reading_order_is_rows_then_left_to_right():
    right = _c((640, 199, 1000, 400), 0.9)          # a hair higher than its neighbour
    left = _c((100, 200, 500, 400), 0.9)
    below = _c((100, 600, 1000, 800), 0.9)
    assert lm.reading_order([below, right, left]) == [left, right, below]


def test_box_geometry():
    a, b = [0, 0, 10, 10], [5, 0, 15, 10]
    assert lm.area(a) == 100 and lm.inside(a, b) == 0.5 and lm.iou(a, b) == pytest.approx(1 / 3)
    assert lm.side_by_side([0, 0, 10, 10], [12, 2, 20, 9]) and not lm.side_by_side([0, 0, 10, 10], [0, 20, 10, 30])


# ─── Detection with a stand-in model ─────────────────────────────────────────
class _Session:
    """Answers like the model: one table candidate and one text region, in render pixels."""

    def __init__(self):
        self.sizes = []

    def run(self, _outputs, feed):
        import numpy as np

        assert feed["images"].shape == (1, 3, lm.INPUT_SIDE, lm.INPUT_SIDE) and feed["images"].dtype == np.uint8
        self.sizes.append(tuple(feed["orig_target_sizes"][0]))
        labels = np.array([[lm.LABELS.index("table"), lm.LABELS.index("text"), lm.LABELS.index("table")]])
        boxes = np.array([[[150.0, 300.0, 750.0, 600.0], [0, 0, 100, 100], [150.0, 300.0, 760.0, 600.0]]])
        scores = np.array([[0.9, 0.95, 0.2]])
        return labels, boxes, scores


def test_boxes_come_back_in_pdf_points(tmp_path, monkeypatch):
    session = _Session()
    monkeypatch.setattr(lm, "_session", lambda model_dir, sha256: session)
    pages, _ = FIXTURES["table_in_prose"]()
    pdf = str(write_pdf(tmp_path / "doc.pdf", pages))
    result = lm.detect_tables(pdf, [1, 5], str(tmp_path), "ab" * 32)
    k = 72 / lm.RENDER_DPI
    assert result["error"] == "" and not result["truncated"]
    assert result["pages"][0] == {"page": 1, "skipped": "", "tables": [
        {"bbox": [round(v * k, 2) for v in (150.0, 300.0, 750.0, 600.0)], "score": 0.9}]}
    assert result["pages"][1] == {"page": 5, "skipped": "missing", "tables": []}
    (width, height), = session.sizes
    assert abs(width - 612 / k) <= 1 and abs(height - 792 / k) <= 1, "the page at RENDER_DPI (pdfium rounds up)"


def test_rotated_pages_are_skipped(tmp_path, monkeypatch):
    import pypdfium2 as pdfium

    monkeypatch.setattr(lm, "_session", lambda model_dir, sha256: _Session())
    pages, _ = FIXTURES["table_in_prose"]()
    pdf = tmp_path / "doc.pdf"
    write_pdf(pdf, pages)
    doc = pdfium.PdfDocument(str(pdf))
    doc[0].set_rotation(90)
    doc.save(str(tmp_path / "turned.pdf"))
    doc.close()
    result = lm.detect_tables(str(tmp_path / "turned.pdf"), [1], str(tmp_path), "ab" * 32)
    assert result["pages"] == [{"page": 1, "skipped": "rotated", "tables": []}]


def test_a_huge_page_is_rendered_no_bigger_than_the_cap(tmp_path, monkeypatch):
    import pypdfium2 as pdfium

    session = _Session()
    monkeypatch.setattr(lm, "_session", lambda model_dir, sha256: session)
    doc = pdfium.PdfDocument.new()
    doc.new_page(14400, 14400)           # 200 inches square: 30000 px at RENDER_DPI
    doc.save(str(tmp_path / "huge.pdf"))
    doc.close()
    result = lm.detect_tables(str(tmp_path / "huge.pdf"), [1], str(tmp_path), "ab" * 32)
    (width, height), = session.sizes
    assert max(width, height) <= lm.MAX_RENDER_SIDE + 1
    points = 14400 / lm.MAX_RENDER_SIDE   # the boxes still come back in the page's points
    assert result["pages"][0]["tables"][0]["bbox"] == pytest.approx([150 * points, 300 * points, 750 * points, 600 * points], rel=1e-3)


def test_the_model_is_loaded_once_from_the_bytes_that_were_checked(tmp_path, monkeypatch):
    import hashlib

    import onnxruntime as ort

    data = b"a stand-in model"
    (tmp_path / lm.MODEL_FILE).write_bytes(data)
    loads = []

    class _Session:
        def __init__(self, model, options, providers):
            loads.append(model)

    monkeypatch.setattr(ort, "InferenceSession", _Session)
    monkeypatch.setattr(lm, "_session_cache", {})
    digest = hashlib.sha256(data).hexdigest()
    first = lm._session(tmp_path, digest)
    assert lm._session(tmp_path, digest) is first and loads == [data], "the checked bytes, once"
    (tmp_path / lm.MODEL_FILE).write_bytes(b"swapped afterwards")
    assert lm._session(tmp_path, digest) is first, "the loaded model is the checked one"


def test_a_model_that_is_not_the_pinned_one_is_never_loaded(tmp_path):
    (tmp_path / lm.MODEL_FILE).write_bytes(b"not the model")
    result = lm.detect_tables("unused.pdf", [1], str(tmp_path), "0" * 64)
    assert result["pages"] == [] and "does not match its pinned SHA-256" in result["error"]
    missing = lm.detect_tables("unused.pdf", [1], str(tmp_path / "nowhere"), "0" * 64)
    assert missing["error"]


# ─── The real model, where it is installed ───────────────────────────────────
def _real_model():
    from app.core.addon_manager import AddonManager

    try:
        return AddonManager().model_location(lm.ADDON)
    except Exception:
        return None


@pytest.mark.skipif(_real_model() is None, reason="the layout model is not on this computer")
def test_the_real_model_finds_side_by_side_tables(tmp_path):
    from app.core.table_finder import find_tables

    folder, digests = _real_model()
    pages, _ = FIXTURES["side_by_side_ruled"]()
    pdf = str(write_pdf(tmp_path / "pair.pdf", pages))
    found = lm.detect_tables(pdf, [1], str(folder), digests[lm.MODEL_FILE])
    boxes = [t["bbox"] for t in found["pages"][0]["tables"]]
    truth = [t["bbox"] for b in find_tables(pdf, [1])["pages"][0]["bands"] for t in b["tables"]]
    assert len(boxes) == 2, boxes
    assert all(lm.iou(found_box, true_box) >= 0.8 for found_box, true_box in zip(boxes, truth, strict=True))
