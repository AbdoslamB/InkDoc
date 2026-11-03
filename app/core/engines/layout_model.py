"""The layout model: where the tables are on a page image (docling-layout-heron, ONNX).

Runs in the MarkItDown worker (action "layout", app/core/engines/markitdown_worker.py),
so onnxruntime, numpy and Pillow load there and never in InkDoc's own process;
like the table finder, it runs in InkDoc's process only when the worker can't
start. The model is the "layout_model" add-on (app/core/addon_manager.py):
InkDoc's own install, or the identical copy in the Docling engine pack. Its file
is hashed against the pinned digest before it is first loaded.

The model takes a page resized to 640 x 640 and returns 300 candidate regions
(label, score, box). It removes no overlaps itself, so detect_tables keeps one
box per table (clean_up). On scan-like test pages it found every ruled table
with a near-exact box and kept side-by-side tables apart, in about half a
second a page on four CPU threads.

Never raises for a bad page: that page is reported as skipped.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ADDON = "layout_model"      # its name in app/core/addons.json
MODEL_FILE = "model.onnx"
# The model's region labels, in output order (its config.json id2label).
LABELS = (
    "caption", "footnote", "formula", "list_item", "page_footer", "page_header", "picture",
    "section_header", "table", "text", "title", "document_index", "code", "checkbox_selected",
    "checkbox_unselected", "form", "key_value_region",
)
INPUT_SIDE = 640
RENDER_DPI = 150            # found 10 of 13 test tables; 100 DPI found 8
MAX_RENDER_SIDE = 2000      # pixels: a page's size is the document's to choose, memory is not
MIN_SCORE = 0.3             # 0.5 misses real tables (a rating grid scored 0.36)
OVERLAP_IOU = 0.5           # boxes overlapping this much are the same table
INSIDE_SHARE = 0.8          # a box this much inside another is part of it
MAX_THREADS = 4             # leave the other cores to the conversion
DEFAULT_TIME_BUDGET_S = 60.0
MAX_PAGES = 60

Box = list[float]  # (x0, top, x1, bottom)

_session_lock = threading.Lock()
_session_cache: dict[str, Any] = {}  # {"key": (path, sha256), "session": ...}: one model per process


# ─── Boxes (pure) ────────────────────────────────────────────────────────────
def area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def inside(a: Box, b: Box) -> float:
    """The share of box a that lies inside box b."""
    x = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    y = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return x * y / area(a) if area(a) else 0.0


def iou(a: Box, b: Box) -> float:
    x = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    y = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    union = area(a) + area(b) - x * y
    return x * y / union if union else 0.0


def side_by_side(a: Box, b: Box) -> bool:
    """Apart horizontally, level with each other."""
    x_overlap = min(a[2], b[2]) - max(a[0], b[0])
    y_overlap = min(a[3], b[3]) - max(a[1], b[1])
    return x_overlap <= 0.1 * min(a[2] - a[0], b[2] - b[0]) and y_overlap > 0.3 * min(a[3] - a[1], b[3] - b[1])


def clean_up(candidates: list[dict]) -> list[dict]:
    """One box per table from the model's overlapping candidates ({"box", "score"}).

    1. A box that holds two candidates side by side is their union: dropped,
       so two tables next to each other stay two tables.
    2. Strongest first, a box that overlaps or sits inside one already kept is
       dropped (or holds one: then it is the same table again).
    """
    def parts(c: dict) -> list[dict]:
        return [
            o for o in candidates
            if o is not c and inside(o["box"], c["box"]) >= INSIDE_SHARE and area(o["box"]) < INSIDE_SHARE * area(c["box"])
        ]

    whole = [c for c in candidates if not any(side_by_side(a["box"], b["box"]) for a in parts(c) for b in parts(c))]
    kept: list[dict] = []
    for c in sorted(whole, key=lambda c: -c["score"]):
        if any(
            iou(c["box"], k["box"]) >= OVERLAP_IOU
            or inside(c["box"], k["box"]) >= INSIDE_SHARE
            or inside(k["box"], c["box"]) >= INSIDE_SHARE
            for k in kept
        ):
            continue
        kept.append(c)
    return reading_order(kept)


def reading_order(items: list[dict]) -> list[dict]:
    """Top to bottom, and left to right within a row of boxes level with each other."""
    rows: list[list[dict]] = []
    for item in sorted(items, key=lambda c: c["box"][1]):
        row = rows[-1] if rows else None
        if row and item["box"][1] < min(r["box"][3] for r in row):
            row.append(item)
        else:
            rows.append([item])
    return [item for row in rows for item in sorted(row, key=lambda c: c["box"][0])]


# ─── The model ───────────────────────────────────────────────────────────────
def _session(model_dir: Path, sha256: str) -> Any:
    """The model, loaded once per process from the very bytes that matched `sha256`
    (read once: the file can't change between the check and the load)."""
    model = model_dir / MODEL_FILE
    key = (str(model), sha256.lower())
    with _session_lock:
        if _session_cache.get("key") == key:
            return _session_cache["session"]
        data = model.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256.lower():
            raise ValueError("the layout model file does not match its pinned SHA-256")
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(MAX_THREADS, os.cpu_count() or 1))
        session = ort.InferenceSession(data, options, providers=["CPUExecutionProvider"])
        _session_cache.update(key=key, session=session)
        return session


# Besides tables, the regions the steps after the tables read (formulas, pictures).
REGION_LABELS = ("formula", "picture")


def _candidates(session: Any, image: Any, labels_wanted: tuple[str, ...] = ("table",)) -> list[dict]:
    """The model's candidates of the given labels on one page image, boxes in its pixels."""
    import numpy as np
    from PIL import Image

    width, height = image.size
    pixels = np.asarray(image.resize((INPUT_SIDE, INPUT_SIDE), Image.BILINEAR), dtype=np.uint8)
    labels, boxes, scores = session.run(None, {
        "images": pixels.transpose(2, 0, 1)[None],
        "orig_target_sizes": np.array([[width, height]], dtype=np.int64),
    })
    found = []
    for label, box, score in zip(labels[0], boxes[0], scores[0], strict=True):
        if 0 <= int(label) < len(LABELS) and LABELS[int(label)] in labels_wanted and float(score) >= MIN_SCORE:
            x0, top, x1, bottom = (float(v) for v in box)
            found.append({
                "box": [max(0.0, x0), max(0.0, top), min(float(width), x1), min(float(height), bottom)],
                "score": float(score),
                "label": LABELS[int(label)],
            })
    return found


def _render(path: str, number: int) -> tuple[Any, float, str]:
    """(RGB image or None, points per pixel, why skipped). pdfium lock held.

    RENDER_DPI, but never more than MAX_RENDER_SIDE pixels on the long side:
    a document can declare any page size, and the model sees 640 x 640 anyway.
    """
    import pypdfium2 as pdfium

    from app.core.pdf_probe import pdfium_lock

    scale = RENDER_DPI / 72
    with pdfium_lock():
        doc = pdfium.PdfDocument(path)
        try:
            if number > len(doc):
                return None, 1 / scale, "missing"
            page = doc[number - 1]
            try:
                if page.get_rotation() % 360:
                    return None, 1 / scale, "rotated"
                scale = min(scale, MAX_RENDER_SIDE / max(1.0, *page.get_size()))
                bitmap = page.render(scale=scale)
                try:
                    # convert() makes a copy that outlives the bitmap it is closed with.
                    return bitmap.to_pil().convert("RGB"), 1 / scale, ""
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            doc.close()


def detect_tables(
    path: str,
    pages: list[int],
    model_dir: str,
    sha256: str,
    *,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
) -> dict:
    """The tables on the given 1-based pages of the PDF at `path`. Never raises.

    {"pages": [{"page", "skipped", "tables": [{"bbox", "score"}],
     "regions": [{"label", "bbox", "score"}]}], "truncated", "elapsed_ms", "error"}:
    `regions` are the formulas and pictures (REGION_LABELS), from the same run. A bbox is (x0, top, x1, bottom) in points from the
    page's top-left corner, as the table finder's, in reading order. Rotated
    pages are skipped. Stops after MAX_PAGES pages or `time_budget_s` seconds
    and says so with truncated=True.
    """
    started = time.perf_counter()
    result: dict[str, Any] = {"pages": [], "truncated": False, "elapsed_ms": 0, "error": ""}
    try:
        session = _session(Path(model_dir), sha256)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return result
    for count, number in enumerate(sorted({int(p) for p in pages if int(p) >= 1})):
        if count >= MAX_PAGES or time.perf_counter() - started > time_budget_s:
            result["truncated"] = True
            break
        try:
            image, points, skipped = _render(path, number)
            if image is None:
                result["pages"].append({"page": number, "skipped": skipped, "tables": []})
                continue
            found = _candidates(session, image, ("table", *REGION_LABELS))
            tables = clean_up([c for c in found if c["label"] == "table"])
            # Overlapping candidates of one label are one region, as for tables.
            regions = reading_order([r for label in REGION_LABELS for r in clean_up([c for c in found if c["label"] == label])])
            result["pages"].append({
                "page": number,
                "skipped": "",
                "tables": [
                    {"bbox": [round(v * points, 2) for v in t["box"]], "score": round(t["score"], 3)}
                    for t in tables
                ],
                "regions": [
                    {"label": r["label"], "bbox": [round(v * points, 2) for v in r["box"]], "score": round(r["score"], 3)}
                    for r in regions
                ],
            })
        except Exception as exc:
            logger.warning("Layout model skipped page %d: %s", number, exc)
            result["pages"].append({"page": number, "skipped": "error", "tables": []})
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return result
