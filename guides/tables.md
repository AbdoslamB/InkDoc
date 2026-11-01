# Tables

The table stage: how InkDoc finds a PDF's tables, compares them with what the engine wrote and,
in Auto only, repairs the ones the engine got wrong. It also covers tables on scanned pages,
which use the layout model add-on. The files this guide covers are listed in AGENTS.md's
routing table.

## Rules

- The table stage runs only at the "Auto quality" levels Balanced and Thorough. At Fast (the
  default) nothing runs.
- Auto repairs; explicit engines are only checked, never changed.
- A table is replaced only when verified against the PDF's text layer (or, for word-position
  tables, confirmed by GLM-OCR's second reading).
- Every table InkDoc writes says where it came from, in an HTML comment in the Markdown
  (`<!-- inkdoc:table page=N source=pdf-lines|pdf-words checked=text-layer[,glm-ocr] -->`) and
  in the response's `tables` metadata.
- The stage never raises: any failure leaves the Markdown unchanged.
- pdfplumber, onnxruntime, numpy and Pillow run only in the MarkItDown worker, never in
  InkDoc's own process.
- `table_check.repair(kinds=...)` writes only the kinds whose step is on.

## How it works

### The stage

`table_stage.py` is the table stage, run after a PDF conversion at the "Auto quality" levels
Balanced and Thorough (`auto_quality` setting/request field; Fast, the default, skips it):
pages come from the probe's `table_pages` plus the pages of the engine's own tables of 4+
columns (`wide_table_hints`); the finder runs in the MarkItDown worker; Auto repairs, explicit
engines are only checked. Never raises; any failure leaves the Markdown unchanged.

### Finding tables in the PDF

`table_finder.py` runs in the MarkItDown worker (pdfplumber is loaded there; InkDoc's own
process never imports it):

- **Ruled tables.** From pdfplumber's line strategy, as InkDoc's grid, "verified" when every
  text-layer word in the table lands in a cell.
- **Shapes in cells.** Shapes and images in cells are named by fill colour (`[green shape]`,
  `[3 blue shapes]`).
- **Bands.** Side-by-side tables are grouped into bands, with the text lines above and below
  as anchors.
- **Merged wide tables.** `split_merged_table()` splits an engine's borderless wide table at a
  gutter the PDF shows, keeping the engine's text.
- **Detectors.** `detectors` (from the step plan; `deep` means all of them) add the rest:
  - `segments` gives the page's positioned text (for figure_text);
  - `rating_grids` runs `find_marker_grids()`, which reads rating grids drawn with dots and
    coloured markers/triangles (`source=pdf_shapes`; replaces any lines-"table" it overlaps;
    charts' gridlines with under 30% text cells are not tables);
  - `borderless_tables` runs `find_word_tables()`, which finds borderless tables from word
    positions (≥3 aligned columns, ≥3 rows; prose, dot-leader contents and two-column forms
    rejected), always unverified with `confirm=True`.
- **Kinds.** Every table carries its `kind` (`lines`, `hint_split`, `rating_grids`,
  `borderless_tables`); `table_check.repair(kinds=...)` writes only the kinds whose step is on.
- Never raises.

### Comparing and repairing

`table_check.py` is pure: it locates each band in the Markdown (a run of lines made only of its
words, else between its anchors when its words are nowhere else), compares (same columns and
body rows; each column under its own header words), classifies (merged, split, loose, missing,
picture, shapes, mismatch) and repairs only verified bands whose located lines hold nothing but
the band's own words, never cutting a Markdown table in two.

`markdown_tables.py` is pure: finding pipe and HTML tables in Markdown, and InkDoc's rules for
writing them (merged cells repeated, stacked headers flattened, full-width top rows as
captions).

### Thorough's second reading

`table_stage.inspect_tables` → `confirm_tables` (GLM-OCR's lane;
`glm_ocr_engine.read_table_region`, prompt `Table Recognition:` on just that area) →
`finish_tables`. A word-position table is written only where the engine wrote no table
(loose/picture/missing) and only when `grids_agree` (same shape, ≥90% of cells alike); it
keeps the PDF's text, marked `checked=text-layer,glm-ocr`. Unconfirmed ones are a detail line,
never a warning. At most 6 per file, within 5 minutes.

### Tables on scanned pages

`scanned_tables.py` is Thorough's "Tables on scanned pages" (needs GLM-OCR and the
`layout_model` add-on). On the pages GLM-OCR read that have no text layer:
`find_scanned_tables` (the layout model, in MarkItDown's lane) → `read_scanned_tables`
(GLM-OCR's `read_table_region` on each area, in GLM-OCR's lane; at most 12, within 5 minutes)
→ `write_scanned_tables` (pure).

- **When a reading is used.** A reading takes the place of what GLM-OCR wrote for that table,
  a table or a paragraph of its words (GLM-OCR often writes a scanned table as plain lines),
  only when it is a full grid, at least 90% of its words are in that place and it uses no
  script the place doesn't (NFKC first, so µ, Ω and º are letters).
- **What it replaces.** In a paragraph only the lines made entirely of the readings' words are
  replaced, and only with no other line between them; a table GLM-OCR wrote is replaced only
  when the readings hold at least 90% of its words and differ from it.
- **Places.** A place is never shared by readings of different pages; ties go to the earliest.
- **Words, not cells.** Words rather than cells, because the structure is what may differ (a
  shifted stacked header comes back as "Revenue 2023"). Matched by words, since GLM-OCR's
  output has no page boundaries; headings, comments, code and paragraphs with dot leaders
  (contents lists) are never taken for a table, and nothing is added where GLM-OCR wrote other
  words.
- **Marking.** Marked `<!-- inkdoc:table page=N source=glm-ocr checked=layout,page-reading -->`,
  reported in `tables.repaired` with `issue: "scanned"`.

### The layout model

`engines/layout_model.py` runs the layout model (docling-layout-heron, ONNX) in the MarkItDown
worker (`layout` action), so onnxruntime, numpy and Pillow never load in InkDoc's own process.
It reads the model once, checks those bytes against the pinned digest and loads them (one
session per process, at most 4 threads), renders pages at 150 DPI but never over 2000 px on the
long side (a document chooses its page size), keeps `table` candidates scoring 0.3 or more, and
`clean_up` keeps one box per table (a box holding two side-by-side candidates is dropped).
Boxes come back in PDF points from the top-left, in reading order; rotated pages are skipped.
It runs an add-on's model, as `glm_ocr_engine.py` does, so it is not a step module. Installing
the model is in [engines.md](engines.md).

## Tests

- `tests/test_table_stage.py`, `tests/test_table_finder.py`, `tests/test_table_check.py`,
  `tests/test_markdown_tables.py`, `tests/test_tables_api.py`.
- `tests/test_scanned_tables.py`, `tests/test_layout_model.py` (the layout model tests run
  where the model is installed and skip in CI).
- `tests/test_table_bench.py` and `scripts/table_bench.py`: the table benchmark.
- `tests/tables_ui.test.js`: how the page shows table results.

## Lessons

### Bound the work a crafted page can cause
- **When:** you hand a whole page to an algorithm whose cost grows faster than the page.
- **Lesson:** pdfplumber's table search compares every vertical edge with every horizontal
  one. A crafted page could hold a conversion lane for hours. Pages with more than 10,000 edges
  or 60,000 characters are skipped as "too_complex", and the result names them. Any new
  per-page analysis needs a limit like this.
- **Evidence:** bf34ffc.

### A document chooses its page size, so cap renders in pixels
- **When:** you render a PDF page to an image.
- **Lesson:** a DPI alone is not a limit: a huge MediaBox at 150 DPI could take about 10 GB.
  Cap the long side in pixels as well (the layout model uses 2000 px).
- **Evidence:** cda004d.

### Match scanned tables by words, and replace only whole lines
- **When:** you change how a reading replaces what GLM-OCR wrote.
- **Lesson:** GLM-OCR often writes a scanned table as plain lines, and its output has no page
  boundaries. Match by words, not by cells, and replace only the lines made entirely of the
  reading's words. Up to 10% of a paragraph, such as a note or source line next to the table,
  was dropped before that rule.
- **Evidence:** cda004d; `tests/test_scanned_tables.py`.
