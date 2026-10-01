"""Tests for app/core/quality_check.py, the missing-text check.

Most cases build PdfProbe objects directly, so the comparison logic is tested
without pdfium. A few run end to end through a hand-built PDF.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.core.quality_check as qc  # noqa: E402
from app.core.pdf_probe import PageProbe, PdfProbe  # noqa: E402
from app.core.quality_check import (  # noqa: E402
    assess,
    format_pages,
    normalize_markdown,
    normalize_text,
)
from app.core.queue_model import EngineKind  # noqa: E402
from tests._pdf_fixtures import image_page, prose_page, words, write_pdf  # noqa: E402

MD = EngineKind.MARKITDOWN
DOCLING = EngineKind.DOCLING


def page(index: int, text: str = "", *, image_only: bool = False, scan: bool = False,
         ocr_layer: bool = False) -> PageProbe:
    return PageProbe(
        index=index,
        text=text,
        char_count=sum(1 for c in text if not c.isspace()),
        image_coverage=1.0 if (image_only or ocr_layer) else 0.0,
        image_only=image_only,
        scan_likely=scan,
        ocr_layer=ocr_layer,
    )


def probe_of(pages: list[PageProbe], page_count: int | None = None, truncated: bool = False) -> PdfProbe:
    return PdfProbe(page_count=page_count or len(pages), pages=pages, producer="",
                    truncated=truncated, elapsed_ms=1)


def prose(n: int, offset: int) -> str:
    text = words(n, offset).split()
    return "\n".join(" ".join(text[i:i + 10]) for i in range(0, len(text), 10))


TABLE = "\n".join(
    f"Region {r} revenue {1000 + r * 37} costs {700 + r * 11} margin {300 + r * 26} units {50 + r}"
    for r in range(12)
)


def check(pages, markdown, engine=MD, **kw):
    kw.setdefault("ocr_engine", DOCLING)
    return assess("doc.pdf", markdown, engine, probe=probe_of(pages), **kw)


# ─── Coverage ────────────────────────────────────────────────────────────────

def test_full_output_has_full_coverage():
    pages = [page(i, prose(80, i * 3)) for i in range(4)]
    report = check(pages, "\n\n".join(p.text for p in pages))
    assert report.checked and report.coverage > 0.99
    assert not report.warning and report.message == "" and report.suggestion is None
    assert report.pages_checked == 4 and report.page_count == 4


def test_missing_table_flags_its_page():
    pages = [page(0, prose(80, 1)), page(1, prose(40, 5) + "\n" + TABLE), page(2, prose(80, 9))]
    output = "\n\n".join([pages[0].text, prose(40, 5), pages[2].text])  # table rows dropped
    report = check(pages, output, engine=DOCLING)
    assert report.warning
    assert report.low_pages == [2]
    assert "page 2" in report.message
    assert report.message.startswith(f"~{report.missing_pct}% of the source text may be missing")
    assert "Docling may have dropped a table or region. Try MarkItDown." in report.message
    assert report.suggestion == "markitdown"


def test_repeated_number_elsewhere_does_not_hide_a_lost_table():
    """One '1037' in the output covers one '1037' in the PDF, not every copy of it."""
    pages = [page(0, prose(60, 2) + "\n" + TABLE), page(1, TABLE)]
    output = pages[0].text  # the second copy of the table is gone
    report = check(pages, output)
    assert report.missing_pct == 40  # 120 of 300 reference words
    # Which copy was lost is ambiguous, so the shortfall is shared by both pages.
    assert report.low_pages == [1, 2]


def test_shortfall_lands_on_the_page_that_lost_text():
    """Common words are shared by every page; losing one page must not blame the last."""
    def own_words(i: int) -> str:
        # Letters, not digits: the running-header filter treats digits as page numbers.
        prefix = "abcde"[i]
        return " ".join(f"{prefix}x{w}" for w in words(50, 3).split())

    pages = [page(i, prose(40, 0) + "\n" + own_words(i)) for i in range(5)]
    output = "\n".join(p.text for i, p in enumerate(pages) if i != 2)
    report = check(pages, output)
    assert report.low_pages == [3]


def test_markitdown_low_coverage_suggests_ocr_engine():
    pages = [page(0, prose(100, 1)), page(1, prose(100, 4))]
    report = check(pages, pages[0].text)
    assert report.warning and report.suggestion == "docling"
    assert report.message.endswith("Try Docling.")


def test_small_shortfall_does_not_warn():
    pages = [page(i, prose(100, i)) for i in range(3)]
    output = "\n".join(p.text for p in pages)
    output = output.replace("revenue", "", 3)  # three words of 300 missing
    report = check(pages, output)
    assert not report.warning and report.coverage > 0.95


# ─── Normalisation ───────────────────────────────────────────────────────────

def test_hyphenation_ligatures_and_escapes():
    ref = page(0, prose(40, 0) + "\nthe classiﬁcation of hyphen￾ated words and\nsnake_case and co-\noperation")
    out = (
        prose(40, 0).replace("\n", " ")
        + " the classification of hyphen-\nated words and snake\\_case and co-operation"
    )
    report = check([ref], out)
    assert report.coverage == 1.0, report


def test_markdown_noise_is_ignored():
    text = prose(60, 3)
    out = (
        "# Title\n<!-- image -->\n![figure](data:image/png;base64,QUJDREVGRw==)\n"
        + text.replace("revenue", "**revenue**").replace("costs", "[costs](https://example.com/x)")
        + "\n| a | b |\n|---|---|\n&amp; <br> `code`"
    )
    report = check([page(0, text)], out)
    assert report.coverage == 1.0


def test_running_headers_and_footers_are_excluded():
    pages = [
        page(i, f"ACME Corp Annual Report 2024\n{prose(60, i * 5)}\nPage {i + 1} of 6")
        for i in range(6)
    ]
    output = "\n\n".join(prose(60, i * 5) for i in range(6))  # Docling dropped them
    report = check(pages, output, engine=DOCLING)
    assert report.coverage == 1.0 and not report.warning


def test_normalize_helpers():
    assert normalize_text("Ｆｕｌｌ­width ﬁ") == "fullwidth fi"
    assert "image" not in normalize_markdown("<!-- image -->")
    assert normalize_markdown("a\\_b").strip() == "a b"


# ─── Pages that are not compared ─────────────────────────────────────────────

def test_ocr_layer_and_cjk_pages_are_skipped():
    cjk = "本報告書は当社の年次決算と事業概況を説明するものであり" * 4
    pages = [page(0, prose(80, 1)), page(1, prose(80, 2), ocr_layer=True), page(2, cjk)]
    report = check(pages, pages[0].text)
    assert report.pages_checked == 1 and not report.warning
    assert any("OCR text layer" in d for d in report.details)
    assert any("CJK" in d for d in report.details)


def test_reversed_rtl_tokens_match():
    arabic = " ".join(["مرحبا", "بالعالم", "هذا", "تقرير", "سنوي", "عن", "الإيرادات"] * 6)
    visual = " ".join(word[::-1] for word in arabic.split())  # pdfium visual order
    report = check([page(0, visual)], arabic)
    assert report.coverage == 1.0


def test_too_little_text_and_no_text_layer():
    report = check([page(0, "Short title page")], "Short title page")
    assert report.coverage is None and report.skipped_reason == "too_little_text"
    report = check([page(0, image_only=True)], "")
    assert report.skipped_reason == "no_text_layer"


# ─── Scanned and image pages ─────────────────────────────────────────────────

def test_scanned_pages_warn_for_markitdown():
    pages = [page(0, prose(80, 1)), page(1, image_only=True, scan=True), page(2, prose(80, 2))]
    report = check(pages, pages[0].text + "\n" + pages[2].text)
    assert report.warning and report.scan_pages == [2]
    assert report.message == (
        "1 of 3 pages is a scanned image with no text. MarkItDown can't read it. Try Docling (OCR)."
    )
    assert report.suggestion == "docling"


def test_scanned_pages_with_docling_not_installed():
    pages = [page(0, prose(80, 1))] + [page(i, image_only=True, scan=True) for i in range(1, 4)]
    report = check(pages, pages[0].text, ocr_engine=None, ocr_installable=True)
    assert report.message == (
        "3 of 4 pages are scanned images with no text. MarkItDown can't read them. "
        "Install Docling in Settings to read scanned pages."
    )
    assert report.suggestion == "install_docling"


def test_no_suggestion_where_no_ocr_engine_exists():
    pages = [page(0, prose(80, 1)), page(1, image_only=True, scan=True)]
    report = check(pages, pages[0].text, ocr_engine=None, ocr_installable=False)
    assert report.warning and report.suggestion is None
    assert "Install" not in report.message and "Try" not in report.message


def test_whole_document_scanned_with_empty_output():
    pages = [page(i, image_only=True, scan=True) for i in range(3)]
    report = check(pages, "")
    assert report.message == "This PDF is scanned, so it has no text to extract. Try Docling (OCR)."
    assert report.suggestion == "docling"
    report = check(pages, "  \n", ocr_engine=None, ocr_installable=True)
    assert report.message == "This PDF is scanned, so it has no text to extract. Install Docling in Settings to read it with OCR."


def test_docling_reads_scans_itself():
    pages = [page(0, prose(80, 1)), page(1, image_only=True, scan=True)]
    report = check(pages, pages[0].text + " ocr text", engine=DOCLING)
    assert not report.warning and report.scan_pages == []
    assert any("read with Docling's OCR" in d for d in report.details)


def test_cover_photo_only_adds_a_detail():
    pages = [page(0, image_only=True)] + [page(i, prose(80, i)) for i in range(1, 5)]
    report = check(pages, "\n".join(p.text for p in pages[1:]))
    assert not report.warning and report.image_pages == [1]
    assert "Page 1 is a full-page image; any text inside it wasn't extracted." in report.details


def test_markit_is_treated_like_markitdown():
    pages = [page(0, prose(80, 1)), page(1, image_only=True, scan=True)]
    report = check(pages, pages[0].text, engine=EngineKind.MARKIT)
    assert report.warning and "Markit can't read it" in report.message


# ─── Garbled output ──────────────────────────────────────────────────────────

def test_cid_output_is_garbled():
    text = prose(80, 2)
    garbled = " ".join("(cid:12)(cid:7)" if i % 3 == 0 else w for i, w in enumerate(text.split()))
    report = check([page(0, text)], garbled)
    assert report.garbled and report.warning
    assert report.message.startswith("Part of this PDF came out as unreadable characters")
    assert report.suggestion == "docling"


def test_replacement_character_runs_are_garbled():
    report = check([page(0, prose(80, 2))], prose(80, 2) + " ����")
    assert report.garbled


# ─── Plumbing ────────────────────────────────────────────────────────────────

def test_format_pages():
    assert format_pages([4]) == "page 4"
    assert format_pages([4, 7]) == "pages 4, 7"
    assert format_pages([1, 2, 3, 4, 5]) == "pages 1, 2, 3, 4, 5"
    assert format_pages(list(range(4, 13))) == "pages 4, 5, 6 and 6 more"


def test_non_pdf_returns_none(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("# hello")
    assert assess(str(md), "# hello", MD) is None


def test_internal_error_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(qc, "_assess", boom)
    report = assess("x.pdf", "text", MD, probe=probe_of([page(0, prose(50, 0))]))
    assert report.checked is False and report.skipped_reason == "error"


def test_unreadable_pdf(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 garbage")
    report = assess(str(bad), "", MD)
    assert report.checked is False and report.skipped_reason == "unreadable"


def test_timeout(monkeypatch):
    pages = [page(i, prose(80, i)) for i in range(3)]
    report = check(pages, pages[0].text, time_budget_s=-1)
    assert report.skipped_reason == "timeout" and report.coverage is None
    assert not report.warning


def test_truncated_probe_is_noted():
    pages = [page(i, prose(60, i)) for i in range(2)]
    report = assess("d.pdf", "\n".join(p.text for p in pages), MD,
                    probe=probe_of(pages, page_count=900, truncated=True))
    assert "Only the first 2 of 900 pages were checked." in report.details


def test_to_dict_shape():
    report = check([page(0, prose(60, 0))], prose(60, 0))
    data = report.to_dict()
    for key in ("checked", "coverage", "missing_pct", "low_pages", "scan_pages", "image_pages",
                "garbled", "page_count", "pages_checked", "warning", "message", "suggestion",
                "skipped_reason", "details"):
        assert key in data


# ─── End to end through pdfium ───────────────────────────────────────────────

def test_end_to_end_with_real_pdf(tmp_path):
    pdf = write_pdf(tmp_path / "r.pdf", [
        image_page("dct"),
        prose_page(90, 1, extra_lines=["a line that ends in a hyph-", "enated word here"]),
        prose_page(90, 8),
        prose_page(90, 15),
        prose_page(90, 22),
        image_page("ccitt"),
    ])
    body = [words(90, 1), "a line that ends in a hyph-", "enated word here", words(90, 8),
            words(90, 15), words(90, 22)]
    report = assess(str(pdf), "\n".join(body), MD, ocr_engine=DOCLING)
    assert report.coverage == 1.0, report
    assert report.image_pages == [1]
    assert report.scan_pages == [6] and report.warning
    assert report.message.startswith("1 of 6 pages is a scanned image")

    missing_page = assess(str(pdf), "\n".join(body[:3] + body[4:]), MD, ocr_engine=DOCLING)
    assert missing_page.low_pages == [3]
