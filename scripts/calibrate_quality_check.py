"""Phase 0: calibrate the missing-text check and Auto's routes on a real corpus.

Development-only. Converts every document in a local folder with each engine,
runs the probe and the missing-text check, and reports what the thresholds in
app/core/quality_check.py and app/core/auto_engine.py would do. Nothing is
saved to ~/Downloads; per-document results and the format bake-off outputs go
to --out (default: scratch/calibration, which is gitignored).

The corpus stays local, because documents may be private. Only the measured
numbers belong in the repository, as a comment next to each constant.

    python scripts/calibrate_quality_check.py CORPUS_DIR [--labels labels.json]
        [--engines markitdown,docling] [--out scratch/calibration]

What to put in CORPUS_DIR (plan_for_new_features.md, 6.2):
  PDFs   digital papers with tables, fully scanned PDFs (fax/JBIG2 and colour),
         mixed digital + scanned, reports with cover photos, slide decks, forms,
         an Arabic PDF, a CJK PDF, a 300+ page PDF, an encrypted PDF, and a PDF
         whose fonts come out as "(cid:N)".
  Other  EPUB, IPYNB, RSS, Atom, YAML and XML samples for the format bake-off
         (each is converted with MarkItDown and Markit for side-by-side review).

labels.json (optional) says what each file is, so the acceptance criteria can be
measured rather than eyeballed:

    {
      "paper.pdf":       {"expect": "clean"},
      "lost_table.pdf":  {"expect": {"docling": "bad", "markitdown": "clean"}},
      "scan.pdf":        {"scanned": true, "expect": {"markitdown": "bad"}},
      "annual.pdf":      {"cover_photo": true, "expect": "clean"}
    }

"expect" is "clean" (the conversion is complete: a warning would be false) or
"bad" (text really is missing: a warning is a detection), for every engine or
per engine. "scanned" files must route to OCR; "cover_photo" files must not.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core import auto_engine, quality_check  # noqa: E402
from app.core.converter import ConversionOptions, convert_item  # noqa: E402
from app.core.pdf_probe import is_pdf, probe_pdf  # noqa: E402
from app.core.queue_model import EngineKind, QueueItem, SourceKind  # noqa: E402

BAKEOFF_EXTENSIONS = {".epub", ".ipynb", ".rss", ".atom", ".yaml", ".yml", ".xml"}
DOC_WARN_SWEEP = [0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.96]
PAGE_WARN_SWEEP = [0.50, 0.60, 0.70, 0.75, 0.80, 0.85]

# Acceptance criteria (plan 6.3).
MAX_FALSE_WARNING_RATE = 0.02
MIN_DETECTION_RATE = 0.90
GO_NO_GO_DETECTION = 0.85


def convert(path: Path, engine: EngineKind) -> tuple[str | None, float, str]:
    """(markdown or None, seconds, error). Never saves anything."""
    item = QueueItem(source=str(path), kind=SourceKind.FILE, display_name=path.name, engine=engine)
    options = ConversionOptions(engine=engine, allow_engine_fallback=False)
    start = time.perf_counter()
    try:
        markdown = convert_item(item, options)
        return markdown, time.perf_counter() - start, ""
    except Exception as exc:
        return None, time.perf_counter() - start, f"{type(exc).__name__}: {exc}"[:300]


def expectation(labels: dict, name: str, engine: str) -> str | None:
    expect = labels.get(name, {}).get("expect")
    if isinstance(expect, dict):
        return expect.get(engine)
    return expect


def would_warn(record: dict, doc_warn: float, page_warn: float) -> bool:
    """Re-derive the verdict for other thresholds from the recorded coverages."""
    if record["scan_pages"] or record["garbled"]:
        return True
    if record["coverage"] is None:
        return False
    if record["coverage"] < doc_warn:
        return True
    return any(
        words >= quality_check.PAGE_WARN_MIN_WORDS and cov < page_warn
        for _page, cov, words in record["page_coverages"]
    )


def rate(hits: int, total: int) -> str:
    return f"{hits}/{total} ({hits / total:.0%})" if total else "n/a"


def calibrate_pdfs(pdfs: list[Path], engines: list[EngineKind], labels: dict, out: Path) -> list[dict]:
    records: list[dict] = []
    ocr_engines = auto_engine.ocr_engines_usable()
    for path in pdfs:
        t0 = time.perf_counter()
        probe = probe_pdf(path)
        probe_ms = (time.perf_counter() - t0) * 1000
        decision = auto_engine.decide(str(path), ocr_engines=[EngineKind.DOCLING], probe=probe, pdf=True)
        print(f"\n{path.name}: {probe.page_count if probe else '?'} pages, probe {probe_ms:.0f} ms, "
              f"scan={probe.scan_pages if probe else None} image={probe.image_pages if probe else None}, "
              f"Auto -> {decision.engine.value} ({decision.reason})")
        for engine in engines:
            markdown, seconds, error = convert(path, engine)
            record = {
                "file": path.name, "engine": engine.value, "pages": probe.page_count if probe else None,
                "probe_ms": round(probe_ms, 1), "convert_s": round(seconds, 2), "error": error,
                "auto_route": decision.engine.value, "auto_reason": decision.reason,
                "expect": expectation(labels, path.name, engine.value),
                "label": labels.get(path.name, {}),
            }
            if markdown is not None:
                t1 = time.perf_counter()
                report = quality_check.assess(
                    str(path), markdown, engine, probe=probe,
                    ocr_engine=ocr_engines[0] if ocr_engines else EngineKind.DOCLING,
                )
                record["check_ms"] = round((time.perf_counter() - t1) * 1000, 1)
                if report is not None:
                    record.update({
                        "coverage": report.coverage, "missing_pct": report.missing_pct,
                        "low_pages": report.low_pages, "scan_pages": report.scan_pages,
                        "image_pages": report.image_pages, "garbled": report.garbled,
                        "warning": report.warning, "message": report.message,
                        "skipped_reason": report.skipped_reason,
                        "page_coverages": report.page_coverages,
                    })
                print(f"  {engine.value:<10} {seconds:6.1f}s  coverage={record.get('coverage')}  "
                      f"warn={record.get('warning')}  {record.get('message', '')}")
            else:
                print(f"  {engine.value:<10} FAILED {error}")
            records.append(record)
    (out / "pdf_results.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    return records


def summarise(records: list[dict]) -> None:
    checked = [r for r in records if "warning" in r]
    print("\n=== Coverage distribution (documents) ===")
    for engine in sorted({r["engine"] for r in checked}):
        values = [r["coverage"] for r in checked if r["engine"] == engine and r["coverage"] is not None]
        if values:
            q = statistics.quantiles(values, n=10) if len(values) >= 2 else values
            print(f"  {engine:<10} n={len(values)} min={min(values):.3f} p10={q[0]:.3f} "
                  f"median={statistics.median(values):.3f}")
        pages = [c for r in checked if r["engine"] == engine for _p, c, w in r["page_coverages"]
                 if w >= quality_check.PAGE_WARN_MIN_WORDS]
        if pages:
            print(f"  {'':<10} pages n={len(pages)} min={min(pages):.3f} median={statistics.median(pages):.3f}")

    print("\n=== Overhead (probe + check) ===")
    over = [(r["pages"] or 0, r["probe_ms"] + r.get("check_ms", 0)) for r in checked]
    if over:
        print(f"  worst {max(o for _p, o in over):.0f} ms; "
              f"<=25 pages median {statistics.median([o for p, o in over if p <= 25] or [0]):.0f} ms "
              f"(bar: <=150 ms for 20 pages, <=3000 ms worst case)")

    labelled = [r for r in checked if r["expect"] in ("clean", "bad")]
    if labelled:
        print("\n=== Threshold sweep (false warnings on clean / detection on bad) ===")
        print(f"  {'DOC_WARN':>8} {'PAGE_WARN':>9}  {'false warnings':>18}  {'detection':>18}")
        for doc_warn in DOC_WARN_SWEEP:
            for page_warn in PAGE_WARN_SWEEP:
                clean = [r for r in labelled if r["expect"] == "clean"]
                bad = [r for r in labelled if r["expect"] == "bad"]
                fw = sum(would_warn(r, doc_warn, page_warn) for r in clean)
                det = sum(would_warn(r, doc_warn, page_warn) for r in bad)
                mark = " <- current" if (doc_warn, page_warn) == (quality_check.DOC_WARN, quality_check.PAGE_WARN) else ""
                print(f"  {doc_warn:>8.2f} {page_warn:>9.2f}  {rate(fw, len(clean)):>18}  {rate(det, len(bad)):>18}{mark}")

        clean = [r for r in labelled if r["expect"] == "clean"]
        bad = [r for r in labelled if r["expect"] == "bad"]
        fw_rate = sum(r["warning"] for r in clean) / len(clean) if clean else 0.0
        det_rate = sum(r["warning"] for r in bad) / len(bad) if bad else 1.0
        print("\n=== Acceptance at the current thresholds ===")
        print(f"  false warnings {fw_rate:.1%} (bar <= {MAX_FALSE_WARNING_RATE:.0%}); "
              f"detection {det_rate:.1%} (bar >= {MIN_DETECTION_RATE:.0%})")
        if fw_rate <= MAX_FALSE_WARNING_RATE and det_rate >= MIN_DETECTION_RATE:
            print("  GO: ship the check as designed.")
        elif fw_rate <= MAX_FALSE_WARNING_RATE and det_rate >= GO_NO_GO_DETECTION:
            print("  GO with care: detection is under 90% but over the 85% floor.")
        else:
            print("  NO-GO for coverage warnings: ship scanned-page and garbled detection only, "
                  "with coverage as an info-level detail (plan 6.3).")

    scanned = [r for r in records if r["label"].get("scanned")]
    covers = [r for r in records if r["label"].get("cover_photo")]
    if scanned or covers:
        print("\n=== Scan classifier (Auto routing) ===")
        files = {r["file"]: r for r in scanned}
        ok = sum(r["auto_route"] == "docling" for r in files.values())
        print(f"  fully scanned -> OCR: {rate(ok, len(files))} (bar 100%)")
        files = {r["file"]: r for r in covers}
        wrong = sum(r["auto_route"] == "docling" for r in files.values())
        print(f"  cover photo -> OCR:   {rate(wrong, len(files))} (bar 0)")

    digital = [r for r in checked if r["engine"] == "markitdown" and r["auto_route"] == "markitdown"]
    if digital:
        escalate = sum(bool(r["warning"]) for r in digital)
        print(f"\n  Auto escalation on digital PDFs: {rate(escalate, len(digital))} (bar <= 10%)")


def bakeoff(files: list[Path], out: Path) -> None:
    if not files:
        return
    target = out / "bakeoff"
    target.mkdir(parents=True, exist_ok=True)
    print("\n=== Format bake-off (MarkItDown vs Markit) ===")
    print(f"  outputs written to {target} for side-by-side review")
    for path in files:
        row = []
        for engine in (EngineKind.MARKITDOWN, EngineKind.MARKIT):
            markdown, seconds, error = convert(path, engine)
            if markdown is None:
                row.append(f"{engine.value}: FAILED {error[:80]}")
                continue
            (target / f"{path.name}.{engine.value}.md").write_text(markdown, encoding="utf-8")
            lines = markdown.splitlines()
            row.append(
                f"{engine.value}: {len(markdown)} chars, {sum(ln.startswith('#') for ln in lines)} headings, "
                f"{sum(ln.lstrip().startswith(('- ', '* ', '1.')) for ln in lines)} list items, "
                f"{markdown.count('```') // 2} code blocks, {seconds:.1f}s"
            )
        print(f"  {path.name} (Auto routes to {auto_engine.AUTO_ROUTES.get(path.suffix.lower(), 'markitdown')})")
        for line in row:
            print(f"    {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("corpus", type=Path, help="Folder of local test documents (searched recursively)")
    parser.add_argument("--labels", type=Path, help="labels.json describing the expected outcome per file")
    parser.add_argument("--engines", default="markitdown,docling",
                        help="Comma-separated engines to convert PDFs with (default: markitdown,docling)")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "scratch" / "calibration")
    args = parser.parse_args()

    if not args.corpus.is_dir():
        parser.error(f"{args.corpus} is not a folder")
    labels = json.loads(args.labels.read_text(encoding="utf-8")) if args.labels else {}
    engines = [EngineKind(name.strip()) for name in args.engines.split(",") if name.strip()]
    if EngineKind.DOCLING in engines and not auto_engine.docling_usable():
        print("Docling is not usable here (not installed, or no models on disk); skipping it.")
        engines.remove(EngineKind.DOCLING)
    args.out.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in args.corpus.rglob("*") if p.is_file())
    pdfs = [p for p in files if is_pdf(p)]
    others = [p for p in files if p.suffix.lower() in BAKEOFF_EXTENSIONS]
    print(f"{len(pdfs)} PDFs, {len(others)} bake-off files; engines: {', '.join(e.value for e in engines)}")
    print(f"Thresholds: DOC_WARN={quality_check.DOC_WARN} PAGE_WARN={quality_check.PAGE_WARN} "
          f"REF_PAGE_MIN_WORDS={quality_check.REF_PAGE_MIN_WORDS}")

    records = calibrate_pdfs(pdfs, engines, labels, args.out)
    summarise(records)
    bakeoff(others, args.out)
    print(f"\nPer-document results: {args.out / 'pdf_results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
