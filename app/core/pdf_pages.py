"""Copy some pages of a PDF into a new PDF (Auto's page split, app/core/auto_engine.py),
and draw a page as an image (the preview's "Original" view, app/server/server.py).

Docling's worker converts whole files, so the text pages of a mixed PDF reach
it as a smaller PDF of just those pages. pypdfium2 copies the pages as they
are (content, fonts, images); the document outline, links and form fields
are not carried over, which page-by-page conversion does not use.

pdfium is not thread-safe: the shared lock (pdf_probe.pdfium_lock) is held
for the copy, and both documents are closed before returning, so Windows can
delete the files afterwards.
"""
from __future__ import annotations

import io
import os

from app.core.pdf_probe import pdfium_lock

# The largest page image drawn for the preview: about 12 megapixels, so a very
# tall page (a receipt, a poster) asked for at full width stays bounded.
PREVIEW_MAX_PIXELS = 12_000_000
PREVIEW_MIN_WIDTH = 200
PREVIEW_MAX_WIDTH = 2400


def write_page_subset(source: str | os.PathLike[str], pages: list[int], dest: str | os.PathLike[str]) -> None:
    """Write the 1-based `pages` of `source`, in that order, to the new file `dest`.

    Raises ValueError for a page that is not in the document and the error of
    pypdfium2 for an unreadable PDF; `dest` is not left half written.
    """
    import pypdfium2 as pdfium

    if not pages:
        raise ValueError("no pages to copy")
    with pdfium_lock():
        src = pdfium.PdfDocument(str(source))
        try:
            count = len(src)
            bad = [p for p in pages if not 1 <= p <= count]
            if bad:
                raise ValueError(f"page {bad[0]} is not in this {count}-page PDF")
            out = pdfium.PdfDocument.new()
            try:
                out.import_pages(src, [p - 1 for p in pages])
                try:
                    out.save(str(dest))
                except Exception:
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                    raise
            finally:
                out.close()
        finally:
            src.close()


def page_sizes(source: str | os.PathLike[str]) -> list[tuple[float, float]]:
    """Width and height in points of every page, as displayed (the page's /Rotate applied).

    Cheap: pdfium reads the sizes without loading the pages. Raises the error of
    pypdfium2 for an unreadable or password-protected PDF.
    """
    import pypdfium2 as pdfium

    with pdfium_lock():
        doc = pdfium.PdfDocument(str(source))
        try:
            return [tuple(doc.get_page_size(i)) for i in range(len(doc))]
        finally:
            doc.close()


def render_page_png(source: str | os.PathLike[str], page: int, width: int) -> bytes:
    """The 1-based `page` of `source` as a PNG about `width` pixels wide.

    `width` is clamped to PREVIEW_MIN_WIDTH..PREVIEW_MAX_WIDTH and the image to
    PREVIEW_MAX_PIXELS. The lock is held for this one page only, and the
    document is closed before returning, so the file can be deleted afterwards.
    Raises ValueError for a page that is not in the document.
    """
    import pypdfium2 as pdfium

    width = max(PREVIEW_MIN_WIDTH, min(PREVIEW_MAX_WIDTH, int(width)))
    with pdfium_lock():
        doc = pdfium.PdfDocument(str(source))
        try:
            count = len(doc)
            if not 1 <= page <= count:
                raise ValueError(f"page {page} is not in this {count}-page PDF")
            pg = doc[page - 1]
            try:
                w, h = pg.get_size()
                scale = width / max(w, 1.0)
                scale = min(scale, (PREVIEW_MAX_PIXELS / max(w * h, 1.0)) ** 0.5)
                bitmap = pg.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                finally:
                    bitmap.close()
            finally:
                pg.close()
        finally:
            doc.close()
    # Encoded outside the lock: the next page can be drawn meanwhile.
    out = io.BytesIO()
    image.convert("RGB").save(out, format="PNG", compress_level=1)
    return out.getvalue()


def page_runs(pages: list[int]) -> str:
    """"1-12, 15, 20-22" for a sorted list of page numbers."""
    runs: list[str] = []
    start = prev = None
    for p in sorted(pages):
        if start is None:
            start = prev = p
        elif p == prev + 1:  # type: ignore[operator]
            prev = p
        else:
            runs.append(f"{start}-{prev}" if start != prev else f"{start}")
            start = prev = p
    if start is not None:
        runs.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ", ".join(runs)
