"""Phase 0 of the table plan: score how engines read tables, with and without the table stage.

Development-only. Converts each document with each engine, then runs the table
stage on the result (app/core/table_stage.py, repair on, as Auto does at the
Balanced level), and scores both against the expected tables. Nothing is
saved to ~/Downloads; per-document results go to --out (default:
scratch/table-bench, which is gitignored).

    python scripts/table_bench.py [CORPUS_DIR] [--engines markitdown,docling,glm_ocr]
        [--no-synthetic] [--thorough] [--scanned] [--out scratch/table-bench]

The synthetic pages of tests/_table_fixtures.py (side-by-side tables, merged
cells, shapes in cells, ...) are always included unless --no-synthetic. A
corpus stays local, because documents may be private: each PDF in CORPUS_DIR
needs a truth file next to it, NAME.tables.json, listing its tables in reading
order with InkDoc's Markdown rules applied (merged cells repeated, stacked
headers flattened):

    [{"page": 1, "rows": [["Region", "Q1"], ["North", "120"]]}, ...]

--thorough runs the Thorough stage (tables without lines too, written only
when GLM-OCR's reading of the same area agrees); the default is Balanced.

--scanned scores tables on scanned pages instead: every document becomes an
image-only copy (SCAN_DPI, grey), GLM-OCR reads it, and the stage is
"Tables on scanned pages" (app/core/scanned_tables.py), which needs the
layout model add-on or the Docling engine. Invented cells are judged against
the original's text layer.

Per engine, before and after the stage:
  exact     tables whose columns, body rows and headers all match
  cells     share of expected cells found at their row and column
  count     documents whose number of tables is right (merged and split tables)
  invented  cells in the output whose words are not in the PDF's text layer
  numbers   share of the text layer's numbers the output kept, and of the
            output's numbers the text layer has (scripts/numeric_recall.py;
            the original's text layer when --scanned)
plus the time of the conversion and of the stage. Only the measured numbers
belong in the repository, next to the thresholds they set.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.markdown_tables import cell_key, find_table_blocks, line_tokens  # noqa: E402
from app.core.table_check import SHAPE_LABEL_RE, same_table  # noqa: E402

DEFAULT_ENGINES = ("markitdown", "docling", "glm_ocr")
SCAN_DPI = 200
_SCALE_CELL = re.compile(r"^[○●◀▶▲▼ ]+(\([a-z]+\))?$")
_INKDOC_HEADINGS = {"scale", "label", "value", "category", "view", "item"}


# ─── Scoring (pure; tests/test_table_bench.py) ───────────────────────────────
def _cells(rows: list[list[str]]) -> list[list[str]]:
    width = max((len(r) for r in rows), default=0)
    return [[cell_key(c) for c in r] + [""] * (width - len(r)) for r in rows]


def cell_accuracy(expected: list[list[str]], found: list[list[str]]) -> tuple[int, int]:
    """(expected cells found at their position, expected cells) for one table.

    Body rows are aligned from the bottom, so a stacked header in `found`
    does not shift every row; the header row is compared as one set of words
    per column (see table_check.same_table).
    """
    exp, got = _cells(expected), _cells(found)
    total = sum(len(r) for r in exp)
    if not exp or not got:
        return 0, total
    hits = 0
    body = exp[1:]
    offset = len(got) - len(body)
    for i, row in enumerate(body):
        j = offset + i
        if 0 <= j < len(got):
            hits += sum(1 for c, cell in enumerate(row) if c < len(got[j]) and got[j][c] == cell)
    head_rows = got[:max(0, offset)] or got[:1]
    for c, cell in enumerate(exp[0]):
        have = {t for r in head_rows if c < len(r) for t in r[c].split()}
        if set(cell.split()) <= have:
            hits += 1
    return hits, total


def score(
    truth: list[dict], markdown: str, source_words: set[str] | None = None, numbers: Any = None,
) -> dict[str, Any]:
    """How well `markdown` reproduces the `truth` tables (same order, any page), and with
    `numbers` (numeric_recall.pdf_numbers) the PDF's numbers."""
    found = [b.rows for b in find_table_blocks(markdown.split("\n"))]
    exact = 0
    hits = total = 0
    used: set[int] = set()
    for table in truth:
        rows = table["rows"]
        best, best_hits, best_total = None, -1, sum(len(r) for r in rows)
        for i, cand in enumerate(found):
            if i in used:
                continue
            h, t = cell_accuracy(rows, cand)
            if h > best_hits:
                best, best_hits, best_total = i, h, t
        if best is not None:
            used.add(best)
            if same_table(rows, found[best]):
                exact += 1
            hits += best_hits
        total += best_total
    invented = 0
    if source_words is not None:
        for rows in found:
            for row in rows:
                for cell in row:
                    # InkDoc's own words describe drawing, not text: shape labels
                    # ("[green shape]"), rating scales ("○ ● ○ (green)") and the
                    # headings of the tables it writes ("Scale", "Label", "Value").
                    if _SCALE_CELL.match(cell.strip()) or cell.strip().lower() in _INKDOC_HEADINGS:
                        continue
                    words = line_tokens(SHAPE_LABEL_RE.sub(" ", cell))
                    if words and any(w not in source_words for w in words):
                        invented += 1
    result = {
        "expected": len(truth),
        "found": len(found),
        "exact": exact,
        "cells_hit": hits,
        "cells_total": total,
        "count_right": len(found) == len(truth),
        "invented_cells": invented,
    }
    if numbers is not None:
        from scripts.numeric_recall import compare, markdown_numbers

        md_numbers = markdown_numbers(markdown)
        r = compare(numbers, md_numbers)
        result["numbers"] = {"total": r["numbers"], "found": r["found"], "output": len(md_numbers),
                             "extra": r["extra"], "changed": r["missing_by_kind"].get("changed", 0)}
    return result


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over documents for one engine and stage."""
    if not rows:
        return {}
    expected = sum(r["expected"] for r in rows)
    cells_total = sum(r["cells_total"] for r in rows)
    nums = [r["numbers"] for r in rows if "numbers" in r]
    n_total = sum(n["total"] for n in nums)
    n_output = sum(n["output"] for n in nums)
    numbers = {
        "numbers_recall_pct": round(100 * sum(n["found"] for n in nums) / n_total, 1) if n_total else None,
        "numbers_precision_pct": round(100 * (1 - sum(n["extra"] for n in nums) / n_output), 1) if n_output else None,
        "numbers_changed": sum(n["changed"] for n in nums),
    } if nums else {}
    return {
        "documents": len(rows),
        "tables": expected,
        "exact": sum(r["exact"] for r in rows),
        "exact_pct": round(100 * sum(r["exact"] for r in rows) / expected, 1) if expected else None,
        "cells_pct": round(100 * sum(r["cells_hit"] for r in rows) / cells_total, 1) if cells_total else None,
        "count_right_pct": round(100 * sum(1 for r in rows if r["count_right"]) / len(rows), 1),
        "invented_cells": sum(r["invented_cells"] for r in rows),
        **numbers,
    }


# ─── Running ─────────────────────────────────────────────────────────────────
def _documents(corpus: Path | None, synthetic: bool, out: Path) -> list[tuple[str, Path, list[dict]]]:
    docs: list[tuple[str, Path, list[dict]]] = []
    if synthetic:
        from tests._pdf_fixtures import write_pdf
        from tests._table_fixtures import FIXTURES

        folder = out / "synthetic"
        folder.mkdir(parents=True, exist_ok=True)
        for name, build in FIXTURES.items():
            pages, truth = build()
            docs.append((f"synthetic/{name}", write_pdf(folder / f"{name}.pdf", pages), truth))
    if corpus is not None:
        for pdf in sorted(corpus.glob("*.pdf")):
            truth_file = pdf.with_suffix(".tables.json")
            if not truth_file.is_file():
                print(f"  skipping {pdf.name}: no {truth_file.name}")
                continue
            docs.append((pdf.name, pdf, json.loads(truth_file.read_text(encoding="utf-8"))))
    return docs


def scan_copy(pdf: Path, out: Path) -> Path:
    """An image-only copy of `pdf`, as a scanner would make it: each page a grey image at SCAN_DPI."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    try:
        images = [doc[i].render(scale=SCAN_DPI / 72).to_pil().convert("L") for i in range(len(doc))]
    finally:
        doc.close()
    images[0].save(out, "PDF", resolution=SCAN_DPI, save_all=True, append_images=images[1:])
    return out


def _scanned_stage(pdf: Path, markdown: str, probe: Any) -> tuple[str, list[dict]]:
    """Run "Tables on scanned pages" on GLM-OCR's reading of every page."""
    from app.core.auto_engine import layout_model_location
    from app.core.engines.glm_ocr_engine import read_table_region
    from app.core.engines.layout_model import MODEL_FILE
    from app.core.engines.markitdown_worker_client import find_layout_in_worker
    from app.core.scanned_tables import (
        find_scanned_tables,
        read_scanned_tables,
        target_pages,
        write_scanned_tables,
    )

    location = layout_model_location()
    if location is None:
        raise SystemExit("--scanned needs the layout model: install it in Settings, or the Docling engine.")
    folder, digests = location

    def detect(path: str, pages: list[int], job: Any) -> dict:
        return find_layout_in_worker(path, pages, str(folder), digests[MODEL_FILE], job)

    found = find_scanned_tables(str(pdf), target_pages(probe, range(1, probe.page_count + 1)), detect)
    read_scanned_tables(found, read_table_region)
    fixed, written, _details = write_scanned_tables(markdown, found)
    return fixed, written


def _usable(engine: str) -> bool:
    from app.core import auto_engine

    if engine == "docling":
        return auto_engine.docling_usable()
    if engine == "glm_ocr":
        return auto_engine.glm_ocr_usable()
    return True


def run(
    corpus: Path | None, engines: list[str], synthetic: bool, out: Path, thorough: bool = False,
    scanned: bool = False,
) -> dict[str, Any]:
    from app.core.converter import ConversionOptions, convert_item
    from app.core.pdf_probe import probe_pdf
    from app.core.quality_check import normalize_text, tokenize
    from app.core.queue_model import EngineKind, QueueItem, SourceKind
    from app.core.table_finder import find_tables
    from app.core.table_stage import confirm_tables, finish_tables, inspect_tables
    from scripts.numeric_recall import pdf_numbers

    out.mkdir(parents=True, exist_ok=True)
    docs = _documents(corpus, synthetic, out)
    if scanned:
        engines = ["glm_ocr"]
        folder = out / "scanned"
        folder.mkdir(parents=True, exist_ok=True)
        docs = [(name, scan_copy(pdf, folder / pdf.name), truth, pdf) for name, pdf, truth in docs]
    else:
        docs = [(name, pdf, truth, pdf) for name, pdf, truth in docs]
    results: dict[str, Any] = {"documents": len(docs), "engines": {}}
    for engine in engines:
        if not _usable(engine):
            print(f"{engine}: not installed here, skipped")
            continue
        per_doc = []
        for name, pdf, truth, original in docs:
            kind = EngineKind(engine)
            started = time.perf_counter()
            try:
                md = convert_item(
                    QueueItem(source=str(pdf), kind=SourceKind.FILE, display_name=pdf.name, engine=kind),
                    ConversionOptions(engine=kind),
                )
            except Exception as exc:
                print(f"  {engine} {name}: conversion failed: {exc}")
                continue
            convert_s = time.perf_counter() - started
            probe = probe_pdf(str(pdf))
            source = probe_pdf(str(original)) if scanned else probe
            source_words = set()
            if source is not None:
                for page in source.pages:
                    source_words.update(tokenize(normalize_text(page.text)))
            numbers = pdf_numbers(original)
            started = time.perf_counter()
            if scanned:
                fixed, repaired = _scanned_stage(pdf, md, probe)
                issues: list[dict] = []
            else:
                inspection = inspect_tables(
                    str(pdf), md, probe, deep=thorough,
                    find=lambda p, pages, _job, **kwargs: find_tables(p, pages, **kwargs),
                )
                if thorough and _usable("glm_ocr"):
                    from app.core.engines.glm_ocr_engine import read_table_region

                    confirm_tables(inspection, read_table_region)
                fixed, summary = finish_tables(inspection, repair_allowed=True)
                repaired, issues = summary.repaired, summary.issues
            stage_s = time.perf_counter() - started
            before = score(truth, md, source_words, numbers)
            after = score(truth, fixed, source_words, numbers)
            per_doc.append({
                "document": name, "before": before, "after": after,
                "convert_s": round(convert_s, 2), "stage_s": round(stage_s, 3),
                "repaired": repaired, "issues": issues,
            })
            print(f"  {engine:10} {name:40} exact {before['exact']}/{before['expected']} -> "
                  f"{after['exact']}/{after['expected']}  ({convert_s:.1f} s + {stage_s:.2f} s)")
        results["engines"][engine] = {
            "before": summarize([d["before"] for d in per_doc]),
            "after": summarize([d["after"] for d in per_doc]),
            "convert_s_mean": round(sum(d["convert_s"] for d in per_doc) / len(per_doc), 2) if per_doc else None,
            "stage_s_mean": round(sum(d["stage_s"] for d in per_doc) / len(per_doc), 3) if per_doc else None,
            "documents": per_doc,
        }
    (out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("corpus", nargs="?", type=Path, help="folder of PDFs with NAME.tables.json truth files")
    parser.add_argument("--engines", default=",".join(DEFAULT_ENGINES))
    parser.add_argument("--no-synthetic", action="store_true", help="leave out tests/_table_fixtures.py")
    parser.add_argument("--thorough", action="store_true",
                        help="the Thorough stage: also tables without lines, confirmed by GLM-OCR if installed")
    parser.add_argument("--scanned", action="store_true",
                        help="image-only copies read by GLM-OCR, then 'Tables on scanned pages' (layout model)")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "scratch" / "table-bench")
    args = parser.parse_args(argv)
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    results = run(args.corpus, engines, not args.no_synthetic, args.out, thorough=args.thorough,
                  scanned=args.scanned)
    print()
    print(f"{'engine':10} {'stage':7} {'exact':>12} {'cells':>7} {'count ok':>9} {'invented':>9} {'numbers':>8}")
    for engine, data in results["engines"].items():
        for stage in ("before", "after"):
            s = data[stage]
            if not s:
                continue
            print(f"{engine:10} {stage:7} {s['exact']:>4}/{s['tables']:<4} ({s['exact_pct']}%) "
                  f"{s['cells_pct']:>6}% {s['count_right_pct']:>8}% {s['invented_cells']:>9} "
                  f"{s.get('numbers_recall_pct')!s:>7}%")
    try:
        from app.core.engines.markitdown_worker_client import MarkItDownWorker

        MarkItDownWorker.get_instance().shutdown()
    except Exception:
        pass
    print(f"\nPer-document results: {args.out / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
