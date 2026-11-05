"""Thorough's layout steps: scans with an old OCR layer, pages read again, diagrams, charts drawn
in the PDF, broken lines, checkboxes, pictures and formulas, and the PDF's own formatting.

Pure pieces are tested with hand-made pages (the table finder's segments and
page_layout); the finder's export with a drawn PDF. No conversion engine is needed.
"""
from __future__ import annotations

from app.core.auto_engine import (
    AutoDecision,
    ocr_layer_pages,
    plan_page_split,
    thorough_ocr_layer_route,
)
from app.core.figure_text import (
    Area,
    formula_areas,
    join_broken_lines,
    mark_checkboxes,
    picture_areas,
    recover_dropped_paragraphs,
    recover_figures,
    write_formulas,
    write_picture_text,
)
from app.core.markdown_tables import find_table_blocks
from app.core.pdf_probe import PageProbe, PdfProbe
from app.core.quality_check import insert_page_text, page_of_line, page_spans
from app.core.queue_model import EngineKind
from app.core.scanned_tables import target_pages
from app.core.table_finder import _chunks, color_key, find_tables, font_style
from app.core.text_polish import restore_formatting, unescape_angle_brackets
from tests._pdf_fixtures import prose_page, write_pdf

G, D = EngineKind.GLM_OCR, EngineKind.DOCLING


def _seg(text, x0, top, x1=None, bottom=None):
    return {"text": text, "x0": x0, "x1": x1 if x1 is not None else x0 + 6 * len(text),
            "top": top, "bottom": bottom if bottom is not None else top + 7}


def _box(x0, top, x1, bottom, fill="", stroke=True, rgb=""):
    return {"x0": x0, "top": top, "x1": x1, "bottom": bottom, "fill": fill, "rgb": rgb or fill, "stroke": stroke}


def _probe(kinds: str) -> PdfProbe:
    """'t' a text page, 'o' a scan with an OCR layer, 's' a scan without text."""
    pages = []
    for i, k in enumerate(kinds):
        text = "" if k == "s" else "words " * 40
        pages.append(PageProbe(index=i, text=text, char_count=len(text.replace(" ", "")),
                               image_coverage=1.0 if k in "os" else 0.0, image_only=k == "s",
                               scan_likely=k == "s", ocr_layer=k == "o"))
    return PdfProbe(page_count=len(pages), pages=pages, producer="", truncated=False, elapsed_ms=1)


# ─── Scans with an old OCR layer ─────────────────────────────────────────────
def test_ocr_layer_pages_go_to_glm_ocr_in_a_page_split():
    probe = _probe("ttooot")
    assert ocr_layer_pages(probe) == [3, 4, 5]
    assert plan_page_split(probe, [G, D]) is None, "without the step, an OCR layer is text"
    split = plan_page_split(probe, [G, D], reread_ocr_layer=True)
    assert [(s.engine, s.pages) for s in split] == [(D, [1, 2]), (G, [3, 4, 5]), (D, [6])]


def test_a_pdf_of_ocr_layer_scans_goes_to_glm_ocr_whole():
    decision = AutoDecision(EngineKind.MARKITDOWN, "Digital PDF with a full text layer")
    assert thorough_ocr_layer_route(decision, _probe("oooot"), glm_ok=True) == 4
    assert decision.engine == G and "earlier OCR layer" in decision.reason
    mostly_text = AutoDecision(EngineKind.MARKITDOWN, "Digital PDF with a full text layer")
    assert thorough_ocr_layer_route(mostly_text, _probe("ottt"), glm_ok=True) == 0
    assert mostly_text.engine == EngineKind.MARKITDOWN
    no_glm = AutoDecision(EngineKind.MARKITDOWN, "Digital PDF with a full text layer")
    assert thorough_ocr_layer_route(no_glm, _probe("oooo"), glm_ok=False) == 0


def test_tables_on_reread_ocr_layer_pages_are_read_again_too():
    assert target_pages(_probe("tos"), [2, 3]) == [2, 3], "an earlier OCR's text is no text layer"


# ─── Where each page is in the Markdown ──────────────────────────────────────
TEXTS = {
    1: "Quarterly market outlook for investors\nEquities rose sharply this week",
    2: "Bone metabolism\nFigure 2.1 Calcium homeostasis",
    3: "Cervical spine trauma\nCan the spine be cleared clinically today",
}
MD = ("# Quarterly market outlook for investors\n\nEquities rose sharply this week.\n\n"
      "## Bone metabolism\n\n<!-- image -->\n\n## Cervical spine trauma\n\n<!-- image -->\n")


def test_pages_are_placed_by_their_own_lines():
    spans = page_spans(MD, TEXTS)
    lines = MD.split("\n")
    assert lines[spans[1][0]].startswith("# Quarterly") and lines[spans[2][0]] == "## Bone metabolism"
    assert lines[spans[3][0]] == "## Cervical spine trauma", "two long words place a page"
    placeholder = lines.index("<!-- image -->")
    assert page_of_line(spans, placeholder) == (2, 3)


def test_another_engines_page_goes_after_its_own_and_repeats_nothing():
    reading = "Bone metabolism\n\nSun\nSkin\nLiver\n\n7"
    out = insert_page_text(MD, 2, reading, "markitdown", TEXTS)
    lines = out.split("\n")
    at = lines.index("<!-- inkdoc:page-text page=2 source=markitdown -->")
    assert lines.index("## Bone metabolism") < at < lines.index("## Cervical spine trauma")
    assert out.count("Bone metabolism") == 1, "the heading the engine kept is not repeated"
    assert "Sun\nSkin\nLiver" in out and "\n7\n" not in out, "a lone page number is left out"


# ─── Paragraphs: a text layer without spaces ─────────────────────────────────
def test_a_text_layer_without_spaces_is_not_taken_for_a_missing_paragraph():
    page = {"page": 5, "width": 595, "height": 842, "segments": [
        _seg("Comparison of methods", 60, 100),
        _seg("DSPy(Khattabetal.,2024)Prompts Offline", 60, 120),
        _seg("GEPA(Agrawaletal.,2026)Prompts Offline", 60, 130),
        _seg("ADAS(Huetal.,2025)AgentDesign Offline", 60, 140),
        _seg("AFlow(Zhangetal.,2025)AgentWorkflow Offline", 60, 150),
    ]}
    md = ("Comparison of methods\n\n| Method | Object | Stage |\n| --- | --- | --- |\n"
          "| DSPy (Khattab et al., 2024) | Prompts | Offline |\n| GEPA (Agrawal et al., 2026) | Prompts | Offline |\n"
          "| ADAS (Hu et al., 2025) | Agent Design | Offline |\n| AFlow (Zhang et al., 2025) | Agent Workflow | Offline |\n")
    assert recover_dropped_paragraphs(md, [page]) == (md, 0)


# ─── Diagrams drawn in boxes ─────────────────────────────────────────────────
DIAGRAM_PAGE = {"page": 3, "width": 595, "height": 842, "segments": [
    _seg("Figure 2 | The harness components", 60, 80),
    _seg("Context construction", 70, 110), _seg("C1 System prompt", 80, 130), _seg("every call", 80, 140),
    _seg("C2 Guardrails", 80, 160),
    _seg("Middleware", 320, 110), _seg("C8 Plugin", 330, 130), _seg("plugin/<name>.ts", 330, 140),
    _seg("After the figure the paper goes on with its text.", 60, 300),
], "boxes": [
    _box(65, 105, 260, 190), _box(75, 125, 250, 150), _box(75, 155, 250, 175),
    _box(315, 105, 520, 190), _box(325, 125, 510, 150),
]}


def test_a_diagram_comes_out_as_a_list_of_its_boxes():
    md = "Figure 2 | The harness components\n\n<!-- image -->\n\nAfter the figure the paper goes on with its text.\n"
    out, filled, _charts = recover_figures(md, [DIAGRAM_PAGE])
    assert filled == 1
    assert ("- Context construction\n  - C1 System prompt every call\n  - C2 Guardrails\n"
            "- Middleware\n  - C8 Plugin plugin/&lt;name>.ts") in out, "panels in reading order, '<n>' not a tag"


# ─── Charts drawn in the PDF ─────────────────────────────────────────────────
def _bar_chart(stated: str) -> dict:
    """A vertical bar chart: 0-30 on the left, three years, bars of 10, 20 and 30 (1 pt = 0.25)."""
    segs = [_seg("Returns by year", 60, 70), _seg(stated, 120, 104)]
    for value, y in ((30, 100), (20, 140), (10, 180), (0, 220)):
        segs.append(_seg(str(value), 40, y - 3.5, 52, y + 3.5))
    labels = []
    for i, year in enumerate(("2020", "2021", "2022")):
        cx = 80 + 40 * i
        labels.append(_seg(year, cx - 12, 226, cx + 12, 233))
    boxes = [_box(80 + 40 * i - 8, 220 - h, 80 + 40 * i + 8, 220, fill="blue", stroke=False)
             for i, h in enumerate((40, 80, 120))]
    return {"page": 1, "width": 612, "height": 792, "segments": segs + labels, "boxes": boxes,
            "paths": [{"pts": [(55, 100), (55, 220)], "color": "black", "rgb": "#000000"}]}


def test_a_bar_chart_is_measured_against_its_axis_labels():
    md = "Returns by year\n\n<!-- image -->\n\nSource: the fund, 2023.\n"
    page = _bar_chart("Average = 20")
    page["segments"].append(_seg("Source: the fund, 2023.", 60, 260))
    out, _filled, charts = recover_figures(md, [page], charts=True)
    assert charts == 1 and "checked=axis-labels,stated-average" in out
    rows = [b for b in find_table_blocks(out.split("\n")) if b.rows[1][0] == "2020"][0].rows
    assert rows[1:] == [["2020", "10.0"], ["2021", "20.0"], ["2022", "30.0"]]


def test_a_chart_whose_stated_average_disagrees_is_not_measured():
    md = "Returns by year\n\n<!-- image -->\n\nSource: the fund, 2023.\n"
    page = _bar_chart("Average = 25")
    page["segments"].append(_seg("Source: the fund, 2023.", 60, 260))
    out, _filled, charts = recover_figures(md, [page], charts=True)
    assert charts == 0 and "inkdoc:chart" not in out


def test_a_line_chart_is_read_at_each_tick_even_when_its_labels_run_together():
    segs = [_seg("Volatility", 60, 60)]
    for value, y in ((30, 100), (20, 150), (10, 200)):
        segs.append(_seg(str(value), 40, y - 3.5, 52, y + 3.5))
    segs.append(_seg("-20-10", 55, 206, 115, 213))   # glued by the text layer: ticks at 70 and 100
    segs.append(_seg("0", 125, 206, 135, 213))
    segs.append(_seg("10", 150, 206, 170, 213))
    segs.append(_seg("Days around the event", 80, 222))
    segs.append(_seg("Index level", 60, 85))
    line = {"pts": [(70, 150), (100, 140), (130, 100), (160, 200)], "color": "blue", "rgb": "#2040c0"}
    page = {"page": 2, "width": 612, "height": 792, "segments": segs, "boxes": [_box(55, 95, 170, 205)],
            "paths": [line]}
    md = "Volatility\n\n<!-- image -->\n\nThe text goes on after the chart.\n"
    page["segments"].append(_seg("The text goes on after the chart.", 60, 260))
    out, _filled, charts = recover_figures(md, [page], charts=True)
    assert charts == 1
    rows = [b for b in find_table_blocks(out.split("\n")) if b.rows[0][0] == "Days around the event"][0].rows
    assert rows[1:] == [["-20", "20.0"], ["-10", "22.0"], ["0", "30.0"], ["10", "10.0"]]


# ─── Broken lines and checkboxes ─────────────────────────────────────────────
FORM_PAGE = {"page": 16, "width": 612, "height": 792, "segments": [
    _seg("Surgical Safety Checklist for every theatre", 60, 60),
    _seg("Has the patient confirmed his/her identity,", 60, 100, 260, 106),
    _seg("site, procedure, and consent?", 60, 107, 200, 113),
    _seg("Yes", 72, 120, 90, 126),
    _seg("Is the site marked?", 60, 135, 160, 141),
    _seg("Yes", 72, 150, 90, 156), _seg("Not applicable", 72, 160, 140, 166),
], "boxes": [
    _box(62, 120, 67, 125, fill="black", stroke=False), _box(62.5, 120.5, 66.5, 124.5, fill="white", stroke=False),
    _box(62, 150, 67, 155, fill="black", stroke=False), _box(62.5, 150.5, 66.5, 154.5, fill="white", stroke=False),
    _box(62, 160, 67, 165, fill="black", stroke=False), _box(62.5, 160.5, 66.5, 164.5, fill="white", stroke=False),
]}
FORM_MD = ("## Surgical Safety Checklist for every theatre\n\nHas the patient confirmed his/her identity,\n\n"
           "site, procedure, and consent?\n\nYes\n\nIs the site marked?\n\nYes\n\nNot applicable\n")


def test_lines_an_engine_broke_off_their_sentence_are_joined():
    out, joined = join_broken_lines(FORM_MD, [FORM_PAGE])
    assert joined == 1
    assert "Has the patient confirmed his/her identity, site, procedure, and consent?\n\nYes" in out
    assert "Is the site marked?\n\nYes" in out, "a question ends its sentence"


def test_checkboxes_drawn_in_the_pdf_are_marked():
    out, marked = mark_checkboxes(FORM_MD, [FORM_PAGE])
    assert marked == 3 and out.count("☐ Yes") == 2 and "☐ Not applicable" in out
    assert "☐ Is the site marked?" not in out


# ─── Pictures and formulas read by GLM-OCR ───────────────────────────────────
def test_a_picture_is_read_only_when_its_page_has_as_many_images_as_placeholders():
    page = {"page": 4, "width": 612, "height": 792, "segments": [
        _seg("How to make tea in four steps", 60, 60), _seg("Fill the kettle with fresh water", 60, 400)],
        "images": [{"x0": 100, "top": 100, "x1": 400, "bottom": 350}]}
    md = "How to make tea in four steps\n\n<!-- image -->\n\nFill the kettle with fresh water\n"
    areas = picture_areas(md, [page])
    assert [(a.page, a.bbox) for a in areas] == [(4, [100.0, 100.0, 400.0, 350.0])]
    areas[0].reading = "Boil the water then pour it"
    out, n = write_picture_text(md, areas)
    assert n == 1 and "<!-- inkdoc:figure-text page=4 source=glm-ocr -->" in out
    two = md.replace("<!-- image -->", "<!-- image -->\n\n<!-- image -->")
    assert picture_areas(two, [page]) == [], "two placeholders, one image: which is which is not known"


def test_a_formula_is_written_only_when_it_holds_the_pdfs_own_characters():
    page = {"page": 2, "width": 612, "height": 792, "segments": [
        _seg("The energy of a body at rest is given by", 60, 60),
        _seg("E = mc2", 200, 100, 260, 110), _seg("where c is the speed of light in vacuum", 60, 140)]}
    md = "The energy of a body at rest is given by\n\n<!-- formula-not-decoded -->\n\nwhere c is the speed of light in vacuum\n"
    layout = {"pages": [{"page": 2, "regions": [{"label": "formula", "bbox": [195, 95, 265, 115], "score": 0.9}]}]}
    areas = formula_areas(md, [page], layout)
    assert len(areas) == 1
    areas[0].reading = "$$E = m c^{2}$$"
    out, n = write_formulas(md, areas, [page])
    assert n == 1 and "<!-- inkdoc:formula page=2 source=glm-ocr checked=text-layer -->\n$$\nE = m c^{2}\n$$" in out
    wrong = [Area(areas[0].line, 2, areas[0].bbox, "\\frac{a}{b}")]
    assert write_formulas(md, wrong, [page]) == (md, 0), "a reading the PDF's text contradicts is not used"


# ─── The PDF's own characters and fonts ──────────────────────────────────────
def test_angle_brackets_come_back_where_they_cannot_open_a_tag_or_a_quote():
    md = "Risk of &gt;500ml blood loss and &lt;15 GCS\n\n&gt; a quote stays\n\n- &gt;5 stairs\n\n`a &lt;b&gt;` &lt;span&gt;"
    out, n = unescape_angle_brackets(md)
    assert out.split("\n")[0] == "Risk of >500ml blood loss and <15 GCS"
    assert "&gt; a quote stays" in out and "- &gt;5 stairs" in out
    assert "`a &lt;b&gt;`" in out and "&lt;span>" in out
    assert n == 3


def test_bold_italic_and_raised_text_come_back_from_the_fonts():
    pages = [{"page": 1, "segments": [
        _seg("Treasury yield breached 5%, 9 and the Fed", 60, 100),
        _seg("Harness Configuration. Let M be a model", 60, 120),
        _seg("WeanalyzeHarness2alongsix lines", 60, 140),
        _seg("Harness 2 refines the harness", 60, 160),
    ], "styles": [
        [{"t": "Treasury yield breached 5%,", "s": ""}, {"t": "9", "s": "sup"}, {"t": "and the Fed", "s": ""}],
        [{"t": "Harness Configuration.", "s": "b"}, {"t": "Let M be a model", "s": ""}],
        [{"t": "WeanalyzeHarness", "s": ""}, {"t": "2", "s": "sup"}, {"t": "alongsix lines", "s": ""}],
        [{"t": "Harness", "s": ""}, {"t": "2", "s": "sup"}, {"t": "refines the harness", "s": ""}],
    ]}]
    md = ("Treasury yield breached 5%, 9 and the Fed.\n\nHarness Configuration. Let M be a model.\n\n"
          "We analyze Harness 2 along six lines.\n\n- Harness 2 refines the harness.\n\n## 2. Harness 2\n")
    out, n = restore_formatting(md, pages)
    assert "breached 5%,<sup>9</sup> and" in out
    assert "**Harness Configuration.** Let M" in out
    assert "We analyze Harness<sup>2</sup> along" in out, "the engine's spacing is kept"
    assert "- Harness<sup>2</sup> refines" in out
    assert out.endswith("## 2. Harness 2\n"), "headings are left as they are"
    assert n == 4


def test_a_phrase_set_plain_somewhere_is_not_marked():
    pages = [{"page": 1, "segments": [_seg("Past performance is no guarantee", 60, 100),
                                      _seg("Past performance is no guarantee", 60, 200)],
              "styles": [[{"t": "Past performance is no guarantee", "s": "b"}]]}]
    md = "Past performance is no guarantee.\n"
    assert restore_formatting(md, pages) == (md, 0)


# ─── The finder's export ─────────────────────────────────────────────────────
def test_the_finder_exports_styles_boxes_and_colours(tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf", [prose_page(60)])
    found = find_tables(str(pdf), [1], deep=True)
    page = found["pages"][0]
    assert {"segments", "boxes", "styles", "images", "paths", "vtext"} <= set(page)
    assert font_style("ABCDEF+Arial-BoldItalicMT") == "bi" and font_style("MinionPro-It") == "i"
    assert font_style("Helvetica") == ""
    assert color_key((1.0, 0.0, 0.0)) == "#ff0000" and color_key(0.5) == "#808080"
    assert _chunks([]) == [], "an empty line is no cells (the rating grids' heading may be empty)"


def test_a_table_row_glm_ocr_broke_inside_a_cell_is_one_row_again():
    from app.core.engines.glm_ocr_engine import join_table_rows

    text = ("| Unit | Chapter |\n| :--- | :--- |\n| **UNIT 1**  \n*Page 1* | **Chapter 1**  \n*Birthdays* |\n"
            "| plain | row |\n\nA paragraph | with a pipe\nstays as it is.")
    out = join_table_rows(text)
    assert "| **UNIT 1**<br>*Page 1* | **Chapter 1**<br>*Birthdays* |" in out
    assert find_table_blocks(out.split("\n"))[0].rows[1] == ["**UNIT 1**<br>*Page 1*", "**Chapter 1**<br>*Birthdays*"]
    assert out.endswith("A paragraph | with a pipe\nstays as it is.")


# ─── Tables in an image file (Thorough, read by GLM-OCR) ─────────────────────
def test_an_image_has_its_table_read_again_and_nothing_else_runs(tmp_path, monkeypatch):
    import asyncio
    from pathlib import Path

    from PIL import Image

    import app.core.auto_engine as auto_engine
    import app.core.engines.glm_ocr_engine as glm
    import app.core.engines.markitdown_worker_client as worker
    from app.core.converter import ConversionOptions
    from app.core.queue_model import QueueItem, SourceKind

    image = tmp_path / "scan.jpg"
    Image.new("RGB", (600, 400), "white").save(image)
    page_reading = ("No Name Price\n\n75\nДюбели\n0,66\n\n76\nГрунтовака\n14292,53\n\n77\nКсилол\n11374,17")
    table = [["No", "Name", "Price"], ["75", "Дюбелі", "0.66"], ["76", "Грунтовка", "14292,53"],
             ["77", "Ксилол", "11374,17"]]

    def fake_convert(item, options, engine, fetched):
        options.engine_used = engine
        return page_reading

    monkeypatch.setattr(auto_engine, "_convert_once", fake_convert)
    monkeypatch.setattr(auto_engine, "ocr_engines_usable", lambda: [G])
    monkeypatch.setattr(auto_engine, "layout_model_usable", lambda: True)
    monkeypatch.setattr(auto_engine, "layout_model_location", lambda: (Path(tmp_path), {"model.onnx": "ab" * 32}))
    monkeypatch.setattr(auto_engine, "glm_estimate_seconds", lambda *a: 10.0)
    monkeypatch.setattr(worker, "find_layout_in_worker", lambda source, pages, folder, digest, job=None: {
        "pages": [{"page": 1, "skipped": "", "tables": [{"bbox": [10, 10, 590, 390], "score": 0.9}]}]})
    monkeypatch.setattr(glm, "read_table_area", lambda path, page, bbox, detail="normal": glm.TableReading(table))
    item = QueueItem(source=str(image), kind=SourceKind.FILE, display_name="scan.jpg", engine=EngineKind.AUTO)
    options = ConversionOptions(engine=EngineKind.AUTO, auto_quality="thorough")
    markdown = asyncio.run(auto_engine.run_auto(item, options))
    assert options.engine_used == G
    assert find_table_blocks(markdown.split("\n"))[0].rows[1] == ["75", "Дюбелі", "0,66"]
    passes = {p["name"]: p for p in options.table_report.passes}
    assert passes["scanned_tables"]["ran"] and passes["scanned_tables"]["changed"] == 1
    assert passes["figure_text"]["skipped"] == "not a PDF"
    fast = ConversionOptions(engine=EngineKind.AUTO, auto_quality="fast")
    assert asyncio.run(auto_engine.run_auto(item, fast)) == page_reading and fast.table_report is None


def test_an_image_is_read_by_its_pixels(tmp_path):
    from PIL import Image

    from app.core.engines.glm_ocr_engine import render_region
    from app.core.engines.layout_model import _render, image_frame

    path = tmp_path / "a.png"
    Image.new("RGBA", (300, 200), (0, 0, 0, 0)).save(path)
    frame = image_frame(str(path), 1)
    assert frame.mode == "RGB" and frame.getpixel((5, 5)) == (255, 255, 255), "transparency on white"
    assert image_frame(str(path), 2) is None
    img, points, why = _render(str(path), 1)
    assert img.size == (300, 200) and points == 1.0 and why == ""
    crop = render_region(str(path), 1, [0, 0, 100, 50], 400)
    assert crop.size[0] > 100, "a small area is read larger"


def test_a_table_too_dense_for_normal_detail_is_offered_and_read_in_high_detail(tmp_path, monkeypatch):
    import asyncio
    from pathlib import Path

    from PIL import Image

    import app.core.auto_engine as auto_engine
    import app.core.engines.glm_ocr_engine as glm
    import app.core.engines.markitdown_worker_client as worker
    from app.core.converter import ConversionOptions
    from app.core.queue_model import QueueItem, SourceKind

    image = tmp_path / "dense.png"
    Image.new("RGB", (600, 800), "white").save(image)
    page_reading = "Directory\n\n" + "\n".join(f"Paper {n} {n} 000" for n in range(1, 7)) + "\n\nEnd"
    table = [["Town", "Name", "Copies"]] + [[f"{n} 000", f"Paper {n}", f"{n} 300"] for n in range(1, 7)]
    asked: list[str] = []

    def read(path, page, bbox, detail="normal"):
        asked.append(detail)
        return glm.TableReading(table, seconds=50.0) if detail == "high" else glm.TableReading(None, True, 40.0)

    monkeypatch.setattr(auto_engine, "_convert_once", lambda item, options, engine, fetched: (
        setattr(options, "engine_used", engine) or page_reading))
    monkeypatch.setattr(auto_engine, "ocr_engines_usable", lambda: [G])
    monkeypatch.setattr(auto_engine, "layout_model_usable", lambda: True)
    monkeypatch.setattr(auto_engine, "layout_model_location", lambda: (Path(tmp_path), {"model.onnx": "ab" * 32}))
    monkeypatch.setattr(auto_engine, "glm_estimate_seconds", lambda *a: 10.0)
    monkeypatch.setattr(worker, "find_layout_in_worker", lambda source, pages, folder, digest, job=None: {
        "pages": [{"page": 1, "skipped": "", "tables": [{"bbox": [10, 10, 590, 790], "score": 0.9}]}]})
    monkeypatch.setattr(glm, "read_table_area", read)
    item = QueueItem(source=str(image), kind=SourceKind.FILE, display_name="dense.png", engine=EngineKind.AUTO)

    def run(detail, can=(True, "")):
        monkeypatch.setattr(auto_engine, "high_detail_available", lambda: can)
        options = ConversionOptions(engine=EngineKind.AUTO, auto_quality="thorough", table_detail=detail)
        return asyncio.run(auto_engine.run_auto(item, options)), options.table_report

    markdown, report = run("normal")
    assert markdown == page_reading and asked == ["normal"]
    assert report.high_detail == {"pages": [1], "seconds": 100}, "offered, with the time it may take"
    assert report.to_dict()["high_detail"] == {"pages": [1], "seconds": 100}

    _markdown, report = run("normal", can=(False, "it needs GLM-OCR running on a graphics card"))
    assert report.high_detail is None
    assert any("high detail isn't available: it needs GLM-OCR running on a graphics card" in d for d in report.details)

    asked.clear()
    markdown, report = run("high")
    assert asked == ["high"] and report.high_detail is None
    assert "checked=layout,high-detail" in markdown and find_table_blocks(markdown.split("\n"))[0].rows == table

    asked.clear()
    _markdown, report = run("high", can=(False, "it needs 1.5 GB of free memory"))
    assert asked == ["normal"], "asked for, but no longer possible: normal detail"
    assert any("High detail wasn't used (it needs 1.5 GB of free memory)" in d for d in report.details)


def test_a_cancel_stops_a_high_detail_reading_at_once(monkeypatch):
    import threading
    import time

    import app.core.auto_engine as auto_engine
    import app.core.engines.glm_ocr_server as gs

    aborted = threading.Event()

    class Server:
        def abort_request(self):
            aborted.set()

    class Job:
        def __init__(self, after):
            self.after = after

        def cancelled(self):
            return time.monotonic() > self.after

    monkeypatch.setattr(gs.GlmOcrServer, "get_instance", classmethod(lambda cls: Server()))
    monkeypatch.setattr(auto_engine, "CANCEL_POLL_S", 0.05)

    def slow_read(path, page, bbox):
        assert aborted.wait(5), "the reading was never stopped"
        return "stopped"

    read = auto_engine._cancellable(slow_read, Job(time.monotonic() + 0.2))
    started = time.monotonic()
    assert read("scan.png", 1, [0, 0, 1, 1]) == "stopped" and time.monotonic() - started < 3

    aborted.clear()
    quick = auto_engine._cancellable(lambda *a: "read", Job(time.monotonic() + 60))
    assert quick("scan.png", 1, [0, 0, 1, 1]) == "read" and not aborted.is_set(), "no cancel: nothing stopped"


def test_a_cancel_before_the_request_begins_is_repeated(monkeypatch):
    import threading
    import time

    import app.core.auto_engine as auto_engine
    import app.core.engines.glm_ocr_server as gs

    begun = threading.Event()
    aborted = threading.Event()

    class Server:
        def abort_request(self):
            if begun.is_set():               # like the real server: nothing to stop before it begins
                aborted.set()

    class Job:
        def cancelled(self):
            return True

    monkeypatch.setattr(gs.GlmOcrServer, "get_instance", classmethod(lambda cls: Server()))
    monkeypatch.setattr(auto_engine, "CANCEL_POLL_S", 0.05)

    def late_read(path, page, bbox):
        time.sleep(0.3)                      # the cancel came first
        begun.set()
        assert aborted.wait(5), "the reading was never stopped"
        return "stopped"

    read = auto_engine._cancellable(late_read, Job())
    assert read("scan.png", 1, [0, 0, 1, 1]) == "stopped"
