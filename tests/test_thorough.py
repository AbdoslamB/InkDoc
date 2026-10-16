"""Thorough for digital PDFs: layout-aware engine, polish, figure text, dropped paragraphs, rating grids.

Pure pieces are tested with hand-made input; the finder's rating grids with a
drawn PDF. No conversion engine is needed.
"""
from __future__ import annotations

from app.core.auto_engine import AutoDecision, _polish, docling_chunks, thorough_digital_route
from app.core.converter import ConversionOptions
from app.core.figure_text import recover_dropped_paragraphs, recover_figure_text
from app.core.markdown_tables import find_table_blocks, render_pipe_table
from app.core.pdf_probe import probe_pdf
from app.core.queue_model import EngineKind
from app.core.table_check import check, repair
from app.core.table_finder import find_tables
from app.core.text_polish import restore_typography, spelling_map, unescape_ampersands
from tests._pdf_fixtures import prose_page, write_pdf
from tests._table_fixtures import rating_grid


# ─── Routing ─────────────────────────────────────────────────────────────────
def test_thorough_moves_digital_pdfs_to_a_layout_aware_engine():
    d = AutoDecision(EngineKind.MARKITDOWN, "Digital PDF with a full text layer")
    thorough_digital_route(d, docling_ok=True, glm_ok=True)
    assert d.engine == EngineKind.DOCLING and "Thorough" in d.reason
    d = AutoDecision(EngineKind.MARKITDOWN, "Digital PDF with a full text layer")
    thorough_digital_route(d, docling_ok=False, glm_ok=True)
    assert d.engine == EngineKind.GLM_OCR
    d = AutoDecision(EngineKind.MARKITDOWN, "Digital PDF with a full text layer")
    thorough_digital_route(d, docling_ok=False, glm_ok=False)
    assert d.engine == EngineKind.MARKITDOWN
    # Not a digital PDF (unreadable, an Office file): left alone.
    d = AutoDecision(EngineKind.MARKITDOWN, "PDF structure unreadable; trying MarkItDown")
    thorough_digital_route(d, docling_ok=True, glm_ok=True)
    assert d.engine == EngineKind.MARKITDOWN


def test_long_pdfs_go_to_docling_in_parts(tmp_path):
    short = probe_pdf(write_pdf(tmp_path / "short.pdf", [prose_page(30, i) for i in range(5)]))
    assert docling_chunks(short) is None
    long = probe_pdf(write_pdf(tmp_path / "long.pdf", [prose_page(30, i) for i in range(30)]))
    parts = docling_chunks(long)
    assert [p.pages[0] for p in parts] == [1, 13, 25] and parts[-1].pages[-1] == 30
    assert all(p.engine == EngineKind.DOCLING for p in parts)


# ─── Polish ──────────────────────────────────────────────────────────────────
PDF_TEXT = ["Macro Strategy—The Yen and Iran: Bessent’s War", "Consider its “Handprint”: Behind",
            "Fenner & Smith", "returns of −1.6% and — more"]


def test_ampersands_come_back_but_entities_and_code_stay():
    text, n = unescape_ampersands("Fenner &amp; Smith\n&amp;lt;tag&amp;gt;\n```\na &amp; b\n```")
    assert n == 1
    assert text == "Fenner & Smith\n&amp;lt;tag&amp;gt;\n```\na &amp; b\n```"


def test_dashes_and_quotes_come_back_from_the_pdf():
    md = "Macro Strategy-The Yen and Iran: Bessent's War\n\nConsider its 'Handprint': Behind"
    text, n = restore_typography(md, PDF_TEXT)
    assert text == "Macro Strategy—The Yen and Iran: Bessent’s War\n\nConsider its “Handprint”: Behind"
    assert n == 3


def test_polish_never_guesses():
    # A list marker stays a list marker; a minus sign is not put back into numbers.
    text, _ = restore_typography("- item\n- -1.6%", PDF_TEXT)
    assert text == "- item\n- -1.6%"
    # Two spellings in the PDF: nothing is chosen.
    assert "it's" not in spelling_map(["it’s here", "it's there"])
    # MarkItDown and Markit copy the text layer: nothing to polish.
    options = ConversionOptions()
    assert _polish("a &amp; b", type("P", (), {"pages": []})(), EngineKind.MARKITDOWN, options) == "a &amp; b"


# ─── Figure text ─────────────────────────────────────────────────────────────
def _seg(text, x0, top, x1=None, bottom=None):
    return {"text": text, "x0": x0, "x1": x1 if x1 is not None else x0 + 6 * len(text),
            "top": top, "bottom": bottom if bottom is not None else top + 7}


BAR_CHART_PAGE = {"page": 7, "width": 612, "height": 792, "segments": [
    _seg("S&P Sector Returns", 22, 524, 111),
    _seg("Healthcare", 64, 549, 93), _seg("1.9%", 193, 550, 207),
    _seg("Energy", 74, 559, 93), _seg("-1.2%", 130, 559, 147),
    _seg("Utilities", 72, 569, 93), _seg("-3.0%", 105, 569, 121),
    _seg("-4% -3% 0% 3%", 104, 585, 213),
    _seg("Sources: Bloomberg, Factset. Total Returns", 22, 610, 204),
]}


def test_a_bar_chart_becomes_a_table_of_its_labels_and_values():
    md = "## S&P Sector Returns\n\n<!-- image -->\n\nSources: Bloomberg, Factset. Total Returns from the period\n"
    out, n = recover_figure_text(md, [BAR_CHART_PAGE])
    assert n == 1
    assert "<!-- inkdoc:figure-text page=7 source=pdf-text -->" in out
    rows = find_table_blocks(out.split("\n"))[0].rows
    assert rows == [["Label", "Value"], ["Healthcare", "1.9%"], ["Energy", "-1.2%"], ["Utilities", "-3.0%"]]
    assert out.index("<!-- image -->") < out.index("| Label")  # added after, nothing removed
    assert out.count("Sources: Bloomberg") == 1


def test_columns_of_a_vertical_chart_pair_with_their_values():
    page = {"page": 5, "width": 612, "height": 792, "segments": [
        _seg("Exhibit 3: Emissions", 411, 400, 520),
        _seg("1000", 420, 440, 432), _seg("500", 420, 470, 432), _seg("0", 424, 500, 432),
        _seg("180", 450, 495, 460), _seg("300", 490, 485, 500), _seg("500", 530, 470, 540),
        _seg("2024", 448, 520, 462), _seg("Base Case", 480, 520, 510), _seg("Liftoff", 525, 520, 545),
        _seg("2035", 485, 529, 500), _seg("2035", 528, 529, 543),
        _seg("Source: International Energy Agency", 411, 560, 577),
    ]}
    md = "## Exhibit 3: Emissions\n\n<!-- image -->\n\nSource: International Energy Agency, 2025.\n"
    out, n = recover_figure_text(md, [page])
    assert n == 1
    assert find_table_blocks(out.split("\n"))[0].rows == [
        ["Category", "Value"], ["2024", "180"], ["Base Case 2035", "300"], ["Liftoff 2035", "500"],
    ]


def test_figure_text_needs_both_anchors_and_never_repeats_a_kept_caption():
    md = "<!-- image -->\n\nSources: Bloomberg, Factset. Total Returns from the period\n"
    assert recover_figure_text(md, [BAR_CHART_PAGE]) == (md, 0)  # nothing before it to anchor on
    assert recover_figure_text("no placeholder here", [BAR_CHART_PAGE]) == ("no placeholder here", 0)


# ─── Dropped paragraphs ──────────────────────────────────────────────────────
NOTE_PAGE = {"page": 7, "width": 612, "height": 792, "segments": [
    _seg("Private Credit", 239, 539, 278), _seg("Real Assets", 239, 549, 272), _seg("Cash", 239, 561, 253),
    _seg("* Many products that pursue Alternative Investment strategies are", 236, 578, 566),
    _seg("available only to qualified investors. CIO asset class views are", 236, 587, 566),
    _seg("relative to the CIO Strategic Asset Allocation of a portfolio.", 236, 596, 566),
    _seg("7 of 8 September 21, 2026", 22, 767, 213),  # a page footer: never put back
]}


def test_a_dropped_paragraph_goes_back_after_the_block_above_it():
    md = "| Asset | View |\n|---|---|\n| Private Credit | |\n| Real Assets | |\n| Cash | |\n\n## Next section\n"
    out, n = recover_dropped_paragraphs(md, [NOTE_PAGE])
    assert n == 1
    lines = out.split("\n")
    note = next(i for i, ln in enumerate(lines) if ln.startswith("\\* Many products"))
    assert lines[note - 1] == "<!-- inkdoc:dropped-text page=7 source=pdf-text -->"
    assert lines.index("| Cash | |") < note < lines.index("## Next section")
    assert "September 21" not in out
    assert len(find_table_blocks(lines)[0].rows) == 4, "the table is not broken"


def test_short_missing_pieces_are_never_put_back():
    page = {"page": 1, "width": 612, "height": 792, "segments": [
        _seg("Cash", 239, 561, 253), _seg("dividend-", 418, 530, 460), _seg("focused strategies.", 418, 542, 520),
    ]}
    md = "Cash\n\nOur dividend-focused strategies.\n"
    assert recover_dropped_paragraphs(md, [page]) == (md, 0)


# ─── Rating grids drawn with markers ─────────────────────────────────────────
def test_a_rating_grid_is_read_from_its_markers_and_rebuilt(tmp_path):
    pages, truth = rating_grid()
    pdf = str(write_pdf(tmp_path / "rating.pdf", pages))
    found = find_tables(pdf, [1], deep=True)
    tables = [t for b in found["pages"][0]["bands"] for t in b["tables"]]
    assert [t["rows"] for t in tables if t["source"] == "pdf_shapes"] == [truth[0]["rows"]]
    # Only Thorough reads markers.
    assert not [t for p in find_tables(pdf, [1])["pages"] for b in p["bands"] for t in b["tables"]]
    # An engine's empty grid (cells it couldn't see), drawn markers as pictures around it.
    engine = "\n".join([
        "## Asset Class Weightings", "", "<!-- image -->", "",
        "| Asset Class | Underweight | CIO View Neutral | Overweight |", "|---|---|---|---|",
        "| Equities | | | |", "| Bonds | | | |", "| Cash | | | |", "", "<!-- image -->", "", "After the grid.",
    ])
    lines, bands = check(engine, found)
    out = repair(lines, bands)
    assert bands[0].repaired and bands[0].source == "pdf_shapes"
    assert find_table_blocks(out.split("\n"))[0].rows == truth[0]["rows"]
    assert "<!-- image -->" not in out and out.rstrip().endswith("After the grid.")
    assert "source=pdf-shapes" in out


def test_render_of_a_rating_table_is_plain_markdown():
    lines = render_pipe_table([["Sector", "CIO View", "Scale"], ["Energy", "Neutral", "○ ○ ● ○ ○ (yellow)"]])
    assert lines[2] == "| Energy | Neutral  | ○ ○ ● ○ ○ (yellow) |"
