# The Auto engine and "Auto quality"

How Auto picks an engine for a file, may convert again with an OCR engine, splits mixed PDFs by
page, and what the "Auto quality" steps add at Balanced and Thorough. Tables have their own
guide, [tables.md](tables.md). The files this guide covers are listed in AGENTS.md's routing
table.

## Rules

- Auto always resolves to one of the concrete engines; every conversion is still done by an
  engine's real API.
- At "Fast" (the default) no step runs. Explicit engines are only checked, never changed.
- Every step is declared once in `app/core/thorough_passes.py`, is asked of a `PassPlan` before
  it runs, and records what it did in a `PassLog` (`tables.passes`).
- Steps are core code and follow AGENTS.md's "Core or add-on" rule, enforced by
  `tests/test_step_imports.py`.
- Steps that touch the text only add lines (figure text, dropped paragraphs) or restore the
  PDF's own spelling (polish). They never invent text.
- `pdf_probe` and `quality_check` never fail a conversion: on any error they return nothing.
- Each engine's lane is taken in async code *before* a thread is used.

## How it works

### Auto routing

`auto_engine.py` is the Auto engine: pure routing (`decide`, `should_escalate`,
`pick_result`, `glm_time_gate`, `AUTO_ROUTES`), OCR availability ("only what is on disk";
GLM-OCR first, then Docling), and the async `run_auto`, which takes each engine's lane
*before* using a thread and may re-convert once with the OCR engine. GLM-OCR is used only for
files estimated to finish within `GLM_OCR_TIME_LIMIT_S` (5 min); otherwise Docling.

**Page split (Thorough).** A mixed PDF is split by page (`plan_page_split`, `_convert_split`):
Docling reads the text pages (as sub-PDFs), GLM-OCR only the pages needing OCR
(`ConversionOptions.page_subset`, original page numbers kept), Docling's runs first, joined in
page order; needs both engines, ≥2 text pages, ≤8 runs, and GLM-OCR's time limit on its own
pages; a failed run falls back GLM-OCR → Docling → MarkItDown and is reported;
`auto.segments` lists who read which pages.

**Digital PDFs (Thorough).** A digital PDF that would go to MarkItDown goes to Docling
(`thorough_digital_route`; GLM-OCR if Docling is missing), in 12-page sub-PDF parts for long
files (`docling_chunks`, Docling's worker has a 3-minute limit per conversion), and `_polish`
(text_polish) runs on the result Auto keeps, after any re-conversion, unless MarkItDown or
Markit made it.

**After the conversion.** The table stage (Balanced/Thorough) runs in MarkItDown's lane; at
Thorough the tables of the scanned pages GLM-OCR read are read again one by one
(`_scanned_tables`; `_convert_split(read_by=...)` records which engine read each page of a
split), and figure text and dropped paragraphs are added, before the single save. Every one of
these steps is asked of a `thorough_passes.PassPlan` built once per conversion (level, the OCR
engines installed, `passes_off`), and what it did goes into a `PassLog` (`tables.passes`).

### The step registry

`thorough_passes.py` is the registry of "Auto quality" steps (pure, standard library only):
one `PassInfo` per step in run order (`digital_layout`, `page_split`, `polish`, `table_check`,
`rating_grids`, `borderless_tables`, `scanned_tables`, `figure_text`, `dropped_paragraphs`)
with its lowest level, stage, the engines it needs, cost, finder detector and the table kinds
it may write. `PassPlan(level, available, disabled)` says which steps are on and why the others
are not (`ConversionOptions.passes_off`, from the `thorough_passes_off` setting: Settings →
General → Auto quality → Steps; `borderless_tables` needs GLM-OCR, which confirms what it
finds; `scanned_tables` needs GLM-OCR and the `layout_model` add-on); `PassLog` records what
each did, reported as `tables.passes`.

A new step: a `PassInfo` (with `modules`, the modules that do its work), its work in its own
module, and a call where its stage runs. Steps are core code; see AGENTS.md's "Core or add-on".
CONTRIBUTING.md, "Adding an Auto quality step", walks through it.

### PDF helpers

- `pdf_probe.py`: one cheap pdfium pass over a PDF: per-page text layer, image coverage, and
  the scan-likely classifier that tells scanned pages from cover photos. Also counts vector
  paths per page from their bounds only (`rule_lines`, `vector_boxes` → `table_pages`), the
  table stage's filter. Imports `pypdfium2` lazily, never raises (returns `None`), serialises
  pdfium with a lock.
- `pdf_pages.py`: `write_page_subset()`: a PDF of some pages (pypdfium2, pdfium lock held,
  both documents closed) for Auto's page split.
- `quality_check.py`: the missing-text check: compares a PDF conversion's Markdown with the
  PDF's own text layer and builds the user-facing warning and suggestion. Read-only; never
  fails a conversion.

### Steps that touch the text

- `text_polish.py`: pure, Thorough: `&amp;` → `&` (not `&amp;lt;`, not in code) and
  dashes/quotes an engine simplified, restored from the PDF's text layer only when the PDF has
  one spelling of the word (never a minus sign, never a bare `-`).
- `figure_text.py`: pure, Thorough: `recover_figure_text` adds the PDF's text inside each
  `<!-- image -->` area (located between the PDF lines the engine wrote before and after it;
  labelled bar charts as a Label/Value or Category/Value table); `recover_dropped_paragraphs`
  puts back runs of ≥3 missing lines (≥15 words; page headers/footers and the 5% margins
  excluded) after the block holding the line above them. Only adds lines, each after an
  `inkdoc:figure-text` / `inkdoc:dropped-text` comment.

### Diagnosing one PDF

`scripts/pdf_diagnose.py FILE.pdf [--level thorough]` (development only) runs InkDoc's own
code on one PDF and reports, in one place: what the probe saw on each page, which steps the
level runs and why the others don't, what Auto chose, what each step did and which tables
are still wrong in what it keeps, and each installed engine on its own, with how much of the
text it wrote and what the table stage would do with its Markdown (every table the PDF
shows, and why it was or wasn't replaced). Each finding names the step and modules that
decide it; problems are about what Auto keeps, the other engines give notes. It calls the
module that makes each decision and copies no threshold: a new step or skip reason needs no
change there, a new kind of verdict does. Its output goes to `scratch/pdf-diagnose/`
(gitignored), never to Downloads.

## Tests

- `tests/test_auto_engine.py`, `tests/test_auto_glm.py`: routing, escalation, GLM-OCR's time
  limit.
- `tests/test_page_split.py`: the page split for mixed PDFs.
- `tests/test_thorough.py`: digital PDFs at Thorough, polish, figure text, dropped paragraphs.
- `tests/test_thorough_passes.py`: the registry, `PassPlan`, `PassLog`.
- `tests/test_step_imports.py`: what a step module may import.
- `tests/test_pdf_probe.py`, `tests/test_quality_check.py`.
- `tests/test_pdf_diagnose.py`: the diagnostic's findings, and one run of the real table stage.
- `tests/auto_engine_ui.test.js`, `tests/auto_steps_ui.test.js`: the page's side.

## Lessons

### Prove a refactor of Auto leaves its output unchanged
- **When:** you restructure Auto's routing or its steps without meaning to change results.
- **Lesson:** the tests check behaviour case by case, so they can miss a change in output.
  Replay a set of documents at every level against recorded engine replies, before and after,
  and compare the Markdown byte for byte.
- **Evidence:** 5e09b19 (the step registry: 17 documents at 3 levels replayed byte for byte).

### Give timing assertions room on CI
- **When:** a test asserts that the PDF probe or the quality check finishes within a time
  budget.
- **Lesson:** CI runners are slower and noisier than a development machine. A tight budget
  makes the test flaky without catching anything real. Use a generous one.
- **Evidence:** 31af784 ("generous time budget for PDF probe and quality check in tests (CI
  flake)").
