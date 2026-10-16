"""Copy some pages of a PDF into a new PDF (Auto's page split, app/core/auto_engine.py).

Docling's worker converts whole files, so the text pages of a mixed PDF reach
it as a smaller PDF of just those pages. pypdfium2 copies the pages as they
are (content, fonts, images); the document outline, links and form fields
are not carried over, which page-by-page conversion does not use.

pdfium is not thread-safe: the shared lock (pdf_probe.pdfium_lock) is held
for the copy, and both documents are closed before returning, so Windows can
delete the files afterwards.
"""
from __future__ import annotations

import os

from app.core.pdf_probe import pdfium_lock


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
