"""Explain what InkDoc does with one PDF, and where in the code to look when it goes wrong.

Development-only. Runs InkDoc's own code on the file, as a conversion would,
and puts what is now spread over the probe, the response metadata and the
Markdown into one report:

  pages      what the probe saw on each page (text, image-only, scanned and
             why, drawn lines and boxes)
  steps      which "Auto quality" steps the level runs here, and why the
             others don't (app/core/thorough_passes.py)
  auto       what Auto chose and why, what each step did, the missing-text
             check, the inkdoc: marks in its Markdown, and the tables still
             wrong in what it keeps (and why the table stage left them)
  engines    each installed engine on its own: how much of the PDF's text it
             wrote (per page), and every table the PDF's lines and words
             show, with what the table stage would do with that engine's
             Markdown and why
  findings   each problem or doubt, with the step and modules that decide it;
             the same one on several pages is listed once. Problems are
             about what Auto keeps (or, without Auto, every engine); what
             the other engines do is a note, except an engine that fails.

    python scripts/pdf_diagnose.py FILE.pdf [--level thorough]
        [--engines markitdown,docling,glm_ocr] [--no-auto]
        [--passes-off polish,figure_text] [--out scratch/pdf-diagnose]

Nothing here re-implements a heuristic or copies a threshold: every verdict
comes from the module that makes it, so the report says what the app does.
Nothing is saved to ~/Downloads. The report (report.json), each engine's
Markdown and Auto's go to --out/<file name> (default scratch/pdf-diagnose,
which is gitignored), because documents may be private. GLM-OCR reads whole
pages slowly, so it is left out by default when its estimate for the file is
over Auto's own time limit; name it in --engines to include it.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.table_check import (  # noqa: E402
    LOOSE,
    MERGED,
    MISMATCH,
    MISSING,
    PICTURE,
    SHAPES,
    SPLIT,
)
from app.core.table_stage import LEVELS  # noqa: E402
from app.core.thorough_passes import (  # noqa: E402
    BY_NAME,
    LEVEL_FAST,
    LEVEL_THOROUGH,
    PASSES,
    PassPlan,
)

ENGINES = ("markitdown", "docling", "glm_ocr")
# Another engine missing this many percentage points less of the text than the
# one Auto kept is worth a look (a report threshold only; Auto's are its own).
BETTER_BY_PCT = 5

# Where the decisions that are not a step's are made.
PROBE = ("app.core.pdf_probe",)
ROUTING = ("app.core.auto_engine",)
QUALITY = ("app.core.quality_check",)
FINDER = ("app.core.table_finder",)
LOCATE = ("app.core.table_check",)

_MARK_RE = re.compile(r"<!--\s*inkdoc:([a-z][a-z-]*)")

# convert(engine, pdf, options) -> Markdown; options is a ConversionOptions.
Converter = Callable[[str, Path, Any], str]


# ─── Facts (each from the module that owns it) ───────────────────────────────
def page_facts(probe: Any) -> list[dict[str, Any]]:
    """What pdf_probe saw on each probed page."""
    rows = []
    for p in probe.pages:
        if p.scan_likely:
            kind = "scan"
        elif p.image_only:
            kind = "picture"
        elif p.ocr_layer:
            kind = "ocr-layer"
        else:
            kind = "text"
        rows.append({
            "page": p.index + 1, "kind": kind, "chars": p.char_count,
            "image_coverage": round(p.image_coverage, 2), "scan_evidence": list(p.scan_evidence),
            "rule_lines": p.rule_lines, "vector_boxes": p.vector_boxes, "table_candidate": p.table_candidate,
        })
    return rows


def step_facts(plan: PassPlan) -> list[dict[str, Any]]:
    # plan.relevant() holds only this level's steps, so one that is off and not
    # switched off is waiting for an engine or add-on to be installed.
    return [
        {"name": p.name, "title": p.title, "on": plan.on(p.name), "reason": plan.reason(p.name),
         "needs_install": not plan.on(p.name) and p.name not in plan.disabled, "modules": list(p.modules)}
        for p in plan.relevant()
    ]


def _owner(kind: str) -> str:
    """The step whose finder results are tables of `kind`."""
    for p in PASSES:
        if kind in p.table_kinds:
            return p.name
    return "table_check"


def band_facts(bands: list[Any]) -> list[dict[str, Any]]:
    """Every table band the finder found, as the table check classified and (maybe) repaired it."""
    rows = []
    for b in bands:
        tables = [
            {"kind": t.get("kind", "lines"), "step": _owner(t.get("kind", "lines")),
             "source": t.get("source", "pdf_lines"), "verified": bool(t.get("verified")),
             "reason": t.get("reason", ""), "checked": t.get("checked", ""),
             "shape": [len(t.get("rows") or []), max((len(r) for r in t.get("rows") or []), default=0)]}
            for t in b.tables
        ]
        rows.append({
            "page": b.page, "located": b.located, "how": b.how, "issue": b.issue or "ok",
            "message": b.message, "repaired": b.repaired, "not_repaired": b.not_repaired, "tables": tables,
        })
    return rows


def quality_facts(report: Any) -> dict[str, Any] | None:
    if report is None:
        return None
    data = report.to_dict()
    data["pages"] = [{"page": n, "coverage": c, "words": w} for n, c, w in report.page_coverages]
    return data


def marks(markdown: str) -> dict[str, int]:
    """How many of each inkdoc: comment the Markdown carries (what InkDoc wrote into it)."""
    return dict(Counter(_MARK_RE.findall(markdown)))


# ─── Findings (pure; tests/test_pdf_diagnose.py) ─────────────────────────────
def _finding(
    severity: str, where: str, what: str, *, step: str = "", modules: Collection[str] = (),
    pages: Collection[int] = (),
) -> dict:
    """One finding; a step's own modules come first, then `modules`."""
    own = BY_NAME[step].modules if step in BY_NAME else ()
    return {"severity": severity, "where": where, "what": what, "step": step,
            "modules": list(dict.fromkeys((*own, *modules))), "pages": sorted(set(pages)), "count": 1}


def _merge(found: list[dict]) -> list[dict]:
    """One finding per kind of trouble: the same thing on several pages is listed once, with its pages."""
    merged: dict[tuple, dict] = {}
    for f in found:
        key = (f["severity"], f["where"], f["what"], f["step"], tuple(f["modules"]))
        if key in merged:
            m = merged[key]
            m["pages"] = sorted(set(m["pages"]) | set(f["pages"]))
            m["count"] += f["count"]
        else:
            merged[key] = dict(f)
    return list(merged.values())


# The table check's issues (app/core/table_check.py), in a few words each.
ISSUES = {
    MERGED: "side-by-side tables merged", SPLIT: "a table split into pieces",
    LOOSE: "a table written as loose lines", MISSING: "a table missing", PICTURE: "a table left as a picture",
    SHAPES: "the shapes in a table's cells left out", MISMATCH: "a table's rows or columns differ",
}

# The modules that run each engine, for an engine that fails.
ENGINE_MODULES = {
    "markitdown": ("app.core.engines.markitdown_worker_client",),
    "docling": ("app.core.engines.docling_engine", "app.core.engines.docling_worker_client"),
    "glm_ocr": ("app.core.engines.glm_ocr_engine",),
}


def _table_findings(where: str, tables: dict[str, Any], steps_on: set[str], severity: str) -> list[dict]:
    """What the table stage did, or would not do, with one Markdown."""
    out = []
    if tables.get("error"):
        out.append(_finding(severity, where, f"the table stage failed: {tables['error']}", step="table_check"))
    for page in tables.get("skipped_pages", []):
        out.append(_finding("note", where, f"the table finder skipped the page ({page['skipped']})",
                            modules=FINDER, pages=[page["page"]]))
    if tables.get("truncated"):
        out.append(_finding("note", where, "the table finder reached its time limit; later pages were not read",
                            modules=FINDER))
    for band in tables.get("bands", []):
        if band["issue"] == "ok":
            continue
        page = [band["page"]]
        step = min((t["step"] for t in band["tables"]), default="table_check")
        issue = ISSUES.get(band["issue"], band["issue"]) if band["located"] else "a table not found in the result"
        why = band["not_repaired"]
        if band["repaired"]:
            sources = ", ".join(sorted({t["source"] for t in band["tables"]}))
            out.append(_finding("note", where, f"{issue}: replaced with the PDF's table ({sources})",
                                step=step, pages=page))
        elif step not in steps_on:
            out.append(_finding("note", where, f"{issue}, not replaced: the {step} step is off here",
                                step=step, pages=page))
        elif not band["located"]:
            # The PDF's table is known, but not where the engine put it.
            out.append(_finding(severity, where, f"{issue}, not replaced: {why}", step=step, modules=LOCATE,
                                pages=page))
        elif not all(t["verified"] for t in band["tables"]):
            # The finder's reading couldn't be vouched for (or wasn't confirmed).
            out.append(_finding(severity, where, f"{issue}, not replaced: {why or 'not verified'}",
                                step=step, modules=FINDER, pages=page))
        else:
            out.append(_finding(severity, where, f"{issue}, not replaced: {why or 'no reason recorded'}",
                                step=step, modules=LOCATE, pages=page))
    return out


def _engine_error(name: str, data: dict[str, Any]) -> dict:
    if name == "docling" and data.get("timed_out"):
        # Auto reads a long PDF in parts (auto_engine.docling_chunks); Docling on its own doesn't.
        return _finding("problem", name, f"conversion failed: {data['error']} Auto reads a long PDF with Docling "
                        "in parts; Docling chosen on its own reads the whole file at once",
                        modules=ENGINE_MODULES[name] + ROUTING)
    return _finding("problem", name, f"conversion failed: {data['error']}", modules=ENGINE_MODULES.get(name, ()))


def _suggestion_findings(auto_quality: dict, engines: dict[str, Any]) -> list[dict]:
    """A missing-text warning that suggests an engine which, measured, misses more of the whole file."""
    suggested = auto_quality.get("suggestion")
    ours = auto_quality.get("missing_pct")
    other = (engines.get(suggested) or {}).get("quality") or {}
    theirs = other.get("missing_pct")
    if ours is None or theirs is None or theirs <= ours:
        return []
    own = {p["page"]: p["coverage"] for p in auto_quality.get("pages", [])}
    low = set(auto_quality.get("low_pages", []))
    better = [p["page"] for p in other.get("pages", []) if p["page"] in low and p["coverage"] > own.get(p["page"], 1.0)]
    what = (f"the warning suggests {suggested}, which misses {theirs}% of the whole file (Auto's output "
            f"misses {ours}%)" + (f"; it does read more of pages {better}" if better else ""))
    return [_finding("problem", "auto", what, modules=QUALITY)]


def findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """What deserves a look, each with the step and modules that decide it; problems first."""
    out: list[dict] = []
    if report.get("probe_error"):
        return [_finding("problem", "file", report["probe_error"], modules=PROBE)]
    if report.get("probe_truncated"):
        out.append(_finding("note", "file", "the probe stopped early: later pages were not classified",
                            modules=PROBE))
    for page in report.get("pages", []):
        if page["kind"] == "picture":
            out.append(_finding("note", "probe",
                                "image-only with no sign of a scan, so treated as a picture and not read by "
                                "OCR; if it holds text, the scan classifier missed it",
                                modules=PROBE, pages=[page["page"]]))

    steps = {s["name"]: s for s in report.get("steps", [])}
    steps_on = {name for name, s in steps.items() if s["on"]}
    for s in steps.values():
        if s.get("needs_install"):
            out.append(_finding("note", "setup", f"{s['title']} did not run: it {s['reason']}", step=s["name"]))

    auto = report.get("auto") or {}
    if auto.get("error"):
        out.append(_finding("problem", "auto", f"Auto failed: {auto['error']}", modules=ROUTING))
    chosen = (auto.get("decision") or {}).get("chosen")
    auto_quality = auto.get("quality") or {}
    engines = report.get("engines", {})
    if auto_quality.get("warning"):
        out.append(_finding("problem", "auto", f"missing-text check: {auto_quality.get('message')}",
                            modules=QUALITY + ROUTING))
        out += _suggestion_findings(auto_quality, engines)
    for entry in (auto.get("tables") or {}).get("passes", []):
        # Most reasons are the plan's ("needs GLM-OCR", "the page split chose the
        # engines"); a step that broke says so ("it failed", "could not be read").
        why = entry["skipped"]
        if not entry["ran"] and ("fail" in why or "could not" in why):
            out.append(_finding("problem", "auto", f"{entry['name']} did not run: {why}", step=entry["name"]))
    # Auto's output is what the user gets: the tables still wrong in it are problems.
    out += _table_findings("auto", auto.get("tables_left") or {}, steps_on, "problem")

    # Without Auto, every engine's troubles are problems; with it, they are notes,
    # except an engine that fails (someone may choose it on its own).
    severity = "note" if chosen else "problem"
    chosen_missing = auto_quality.get("missing_pct")
    for name, data in engines.items():
        if data.get("error"):
            out.append(_engine_error(name, data))
            continue
        q = data.get("quality") or {}
        if q.get("garbled"):
            out.append(_finding(severity, name, "the output is full of (cid:N) or replacement characters",
                                modules=QUALITY + ROUTING))
        if q.get("scan_pages"):
            out.append(_finding(severity, name, "left scanned pages unread", modules=ROUTING,
                                pages=q["scan_pages"]))
        if q.get("low_pages"):
            out.append(_finding(severity, name, "pages missing much of their text", modules=QUALITY,
                                pages=q["low_pages"]))
        if (chosen and name != chosen and chosen_missing is not None and q.get("missing_pct") is not None
                and chosen_missing - q["missing_pct"] >= BETTER_BY_PCT):
            out.append(_finding("problem", "auto", f"Auto kept {chosen} ({chosen_missing}% of the text missing); "
                                f"{name} misses {q['missing_pct']}%", modules=ROUTING))
        out += _table_findings(name, data.get("tables") or {}, steps_on, severity)
    out = _merge(out)
    out.sort(key=lambda f: f["severity"] != "problem")
    return out


# ─── Running ─────────────────────────────────────────────────────────────────
def installed() -> set[str]:
    """What the steps can use here: the OCR engines on disk, and the layout model."""
    from app.core import auto_engine

    found = {e.value for e in auto_engine.ocr_engines_usable()}
    if auto_engine.layout_model_usable():
        found.add(auto_engine.LAYOUT_MODEL)
    return found


def default_engines(available: set[str], pages: int) -> tuple[list[str], list[str]]:
    """(engines to run, notes): MarkItDown always; Docling and GLM-OCR if installed,
    GLM-OCR only when its estimate for the file is within Auto's time limit."""
    from app.core.auto_engine import GLM_OCR_TIME_LIMIT_S, glm_estimate_for_pages

    run, notes = ["markitdown"], []
    if "docling" in available:
        run.append("docling")
    if "glm_ocr" in available:
        estimate = glm_estimate_for_pages(pages)
        if estimate is not None and estimate > GLM_OCR_TIME_LIMIT_S:
            notes.append(f"glm_ocr left out: about {estimate / 60:.0f} min for {pages} pages "
                         "(name it in --engines to include it)")
        else:
            run.append("glm_ocr")
    return run, notes


def _convert(engine: str, pdf: Path, options: Any) -> str:
    from app.core.converter import convert_item
    from app.core.queue_model import EngineKind, QueueItem, SourceKind

    kind = EngineKind(engine)
    item = QueueItem(source=str(pdf), kind=SourceKind.FILE, display_name=pdf.name, engine=kind)
    return convert_item(item, options)


def _options(engine: str, level: str, passes_off: Collection[str]) -> Any:
    from app.core.converter import ConversionOptions
    from app.core.queue_model import EngineKind

    # An engine on its own doesn't fall back: its failure is what we want to see,
    # not MarkItDown's result under its name. Auto keeps its own fallbacks.
    return ConversionOptions(engine=EngineKind(engine), auto_quality=level, passes_off=tuple(passes_off),
                             allow_engine_fallback=engine == "auto")


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def run_auto(
    pdf: Path, level: str, passes_off: Collection[str], out: Path, convert: Converter,
) -> tuple[dict[str, Any], str | None]:
    """(what Auto did, the Markdown it keeps; None when it failed)."""
    options = _options("auto", level, passes_off)
    started = time.perf_counter()
    try:
        markdown = convert("auto", pdf, options)
    except Exception as exc:
        return {"error": _error(exc), "seconds": round(time.perf_counter() - started, 1)}, None
    (out / f"auto-{level}.md").write_text(markdown, encoding="utf-8")
    decision = options.auto_decision
    return {
        "seconds": round(time.perf_counter() - started, 1),
        "decision": decision.to_dict(options.engine_used) if decision is not None else None,
        "fallback": options.fallback_reason or None,
        "warnings": list(options.engine_warnings),
        "quality": quality_facts(options.quality_report),
        "tables": options.table_report.to_dict() if options.table_report is not None else None,
        "marks": marks(markdown),
    }, markdown


def remembered(fn: Callable[..., Any], key: Callable[..., Any]) -> Callable[..., Any]:
    """`fn`, run once per `key(*args, **kwargs)`: Auto's output and each engine's ask the table
    finder and GLM-OCR the same questions. Callers get copies (the stage marks what it finds)."""
    seen: dict[Any, Any] = {}

    def call(*args: Any, **kwargs: Any) -> Any:
        k = key(*args, **kwargs)
        if k not in seen:
            seen[k] = fn(*args, **kwargs)
        return copy.deepcopy(seen[k])

    return call


def _finder_key(path: str, pages: list[int], _job: Any, hints: Any = None, detectors: Any = None) -> tuple:
    return tuple(pages), json.dumps(hints, sort_keys=True), tuple(detectors or ())


def _read_table_region(path: str, page: int, bbox: list[float]) -> Any:
    from app.core.engines.glm_ocr_engine import read_table_region

    return read_table_region(path, page, bbox)


def inspect_engine(
    pdf: Path, markdown: str, probe: Any, plan: PassPlan, *, find: Any, read_table: Any,
) -> dict[str, Any]:
    """The table stage on one Markdown, as Auto runs it at this level (nothing is saved); never raises."""
    from app.core.table_stage import (
        confirm_tables,
        finish_tables,
        inspect_tables,
        pending_confirmations,
    )

    if not plan.finder_needed():
        return {"skipped": "no step at this level uses the table finder"}
    try:
        insp = inspect_tables(str(pdf), markdown, probe, detectors=plan.detectors(), find=find)
        if insp.early is not None:
            return {"skipped": insp.early.skipped_reason or "nothing to check", "bands": []}
        if plan.on("borderless_tables") and pending_confirmations(insp):
            confirm_tables(insp, read_table)
        _fixed, summary = finish_tables(insp, repair_allowed=True, kinds=plan.table_kinds())
    except Exception as exc:
        return {"error": _error(exc), "bands": []}
    found = insp.found
    return {
        "pages_checked": sorted(p["page"] for p in found.get("pages", [])),
        "skipped_pages": [{"page": p["page"], "skipped": p["skipped"]}
                          for p in found.get("pages", []) if p.get("skipped")],
        "truncated": bool(found.get("truncated")),
        "bands": band_facts(insp.bands),
        "details": list(summary.details),
    }


def run_engine(
    engine: str, pdf: Path, probe: Any, plan: PassPlan, out: Path, convert: Converter, *, find: Any, read_table: Any,
) -> dict[str, Any]:
    from app.core.auto_engine import ocr_status
    from app.core.quality_check import assess

    options = _options(engine, LEVEL_FAST, ())
    started = time.perf_counter()
    try:
        markdown = convert(engine, pdf, options)
    except Exception as exc:
        # A Docling worker timeout (docling_worker_client) is told apart from other failures.
        return {"error": _error(exc), "timed_out": isinstance(exc, TimeoutError),
                "seconds": round(time.perf_counter() - started, 1)}
    seconds = round(time.perf_counter() - started, 1)
    (out / f"{engine}.md").write_text(markdown, encoding="utf-8")
    ocr_engine, installable = ocr_status()
    report = assess(str(pdf), markdown, engine, probe=probe, ocr_engine=ocr_engine, ocr_installable=installable)
    tables = inspect_engine(pdf, markdown, probe, plan, find=find, read_table=read_table)
    return {"seconds": seconds, "chars": len(markdown), "quality": quality_facts(report), "tables": tables}


def diagnose(
    pdf: Path, *, level: str = LEVEL_THOROUGH, engines: list[str] | None = None, auto: bool = True,
    passes_off: Collection[str] = (), out: Path, convert: Converter = _convert,
    available: set[str] | None = None, find: Any = None, read_table: Any = None,
) -> dict[str, Any]:
    from app.core.pdf_probe import probe_pdf
    from app.core.table_stage import _find_in_worker

    find = remembered(find or _find_in_worker, _finder_key)
    read_table = remembered(read_table or _read_table_region, lambda _path, page, bbox: (page, tuple(bbox)))
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"file": pdf.name, "level": level, "notes": []}
    probe = probe_pdf(str(pdf))
    if probe is None:
        report["probe_error"] = "pdfium could not read the PDF"
        report["findings"] = findings(report)
        return report
    report.update(page_count=probe.page_count, producer=probe.producer, probe_truncated=probe.truncated,
                  pages=page_facts(probe))
    available = installed() if available is None else set(available)
    report["installed"] = sorted(available)
    plan = PassPlan(level, available=available, disabled=passes_off)
    report["steps"] = step_facts(plan)
    if engines is None:
        engines, notes = default_engines(available, probe.page_count)
        report["notes"] += notes
    if auto:
        print(f"  auto ({level}) ...", flush=True)
        report["auto"], markdown = run_auto(pdf, level, passes_off, out, convert)
        if markdown is not None:
            # The engine's own Markdown exists only inside Auto (in parts, or split by
            # page), so the tables still wrong are judged on what Auto keeps.
            report["auto"]["tables_left"] = inspect_engine(pdf, markdown, probe, plan, find=find,
                                                           read_table=read_table)
    report["engines"] = {}
    for engine in engines:
        print(f"  {engine} ...", flush=True)
        report["engines"][engine] = run_engine(engine, pdf, probe, plan, out, convert,
                                               find=find, read_table=read_table)
    report["findings"] = findings(report)
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


# ─── The text report ─────────────────────────────────────────────────────────
def _module_paths(modules: list[str]) -> str:
    return ", ".join(m.replace(".", "/") + ".py" for m in modules)


def _tables_text(t: dict[str, Any]) -> str:
    bands = t.get("bands", [])
    counts = Counter("replaced" if b["repaired"] else ("ok" if b["issue"] == "ok" else "left") for b in bands)
    return t.get("skipped") or t.get("error") or (
        f"{len(bands)} places: {counts['ok']} ok, {counts['replaced']} replaced, {counts['left']} left")


def _where(f: dict[str, Any]) -> str:
    pages = f.get("pages") or []
    text = f["where"]
    if pages:
        # Plain ASCII: a Windows console may not print anything else.
        shown = ", ".join(map(str, pages[:12])) + (", ..." if len(pages) > 12 else "")
        text += f", page{'s' if len(pages) != 1 else ''} {shown}"
    if f.get("count", 1) > 1:
        text += f" ({f['count']} times)"
    return text


def render(report: dict[str, Any]) -> str:
    lines = [f"{report['file']}: {report.get('page_count', '?')} pages, level {report['level']}"]
    if report.get("producer"):
        lines.append(f"  producer: {report['producer']}")
    if report.get("pages"):
        lines += ["", "Pages", f"  {'page':>4}  {'kind':9} {'chars':>6} {'images':>6} {'lines/boxes':>11}  evidence"]
        for p in report["pages"]:
            drawn = f"{p['rule_lines']}/{p['vector_boxes']}" + (" *" if p["table_candidate"] else "")
            lines.append(f"  {p['page']:>4}  {p['kind']:9} {p['chars']:>6} {p['image_coverage']:>6} "
                         f"{drawn:>11}  {', '.join(p['scan_evidence'])}")
        lines.append("  (* drawn enough to hold a table: the table stage looks at the page)")
    if report.get("steps"):
        lines += ["", f"Steps at {report['level'].capitalize()} (installed: {', '.join(report['installed']) or 'none'})"]
        for s in report["steps"]:
            lines.append(f"  {s['name']:20} {'on' if s['on'] else 'off: ' + s['reason']}")
    auto = report.get("auto")
    if auto:
        lines += ["", f"Auto ({auto['seconds']} s)"]
        if auto.get("error"):
            lines.append(f"  failed: {auto['error']}")
        else:
            d = auto.get("decision") or {}
            lines.append(f"  chose {d.get('chosen')} (routed to {d.get('routed_to')}): {d.get('reason')}")
            if d.get("escalation"):
                lines.append(f"  escalation: {d['escalation']}")
            if d.get("segments"):
                lines.append("  pages: " + "; ".join(f"{s['engine']} {s['pages']}" for s in d["segments"]))
            for entry in (auto.get("tables") or {}).get("passes", []):
                state = f"ran, changed {entry['changed']}" if entry["ran"] else f"skipped: {entry['skipped']}"
                lines.append(f"  {entry['name']:20} {state}")
            q = auto.get("quality") or {}
            if q:
                lines.append(f"  missing text: {q.get('missing_pct')}%" + (f"  ({q['message']})" if q.get("warning") else ""))
            if auto.get("marks"):
                lines.append("  inkdoc marks: " + ", ".join(f"{k} {v}" for k, v in sorted(auto["marks"].items())))
            if auto.get("tables_left"):
                lines.append(f"  tables in its output: {_tables_text(auto['tables_left'])}")
    if report.get("engines"):
        lines += ["", "Engines (each on its own; tables: what the table stage would do with its Markdown)"]
        for name, data in report["engines"].items():
            if data.get("error"):
                lines.append(f"  {name:10} failed: {data['error']}")
                continue
            q = data.get("quality") or {}
            low = f", low pages {q['low_pages']}" if q.get("low_pages") else ""
            lines.append(f"  {name:10} {data['seconds']:>6} s  missing {q.get('missing_pct')}%{low}  "
                         f"tables: {_tables_text(data.get('tables') or {})}")
    found = report.get("findings", [])
    lines += ["", "Where to look" if found else "Nothing to look at."]
    for f in found:
        lines.append(f"  [{f['severity']}] {_where(f)}: {f['what']}")
        pointer = " -> ".join(x for x in (f["step"] and f"step {f['step']}", _module_paths(f["modules"])) if x)
        if pointer:
            lines.append(f"      {pointer}")
    for note in report.get("notes", []):
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pdf", type=Path, help="the PDF to explain")
    parser.add_argument("--level", choices=LEVELS, default=LEVEL_THOROUGH, help="the Auto quality level (default thorough)")
    parser.add_argument("--engines", help=f"engines to run on their own, of {','.join(ENGINES)} (default: those installed)")
    parser.add_argument("--no-auto", action="store_true", help="leave out the Auto conversion")
    parser.add_argument("--passes-off", default="", help="steps to switch off, as the thorough_passes_off setting does")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "scratch" / "pdf-diagnose")
    args = parser.parse_args(argv)

    from app.core.pdf_probe import is_pdf

    if not args.pdf.is_file() or not is_pdf(args.pdf):
        parser.error(f"{args.pdf} is not a PDF")
    def names(value: str, allowed: Collection[str], what: str) -> list[str]:
        found = [n.strip() for n in value.split(",") if n.strip()]
        unknown = [n for n in found if n not in allowed]
        if unknown:
            parser.error(f"unknown {what}: {', '.join(unknown)}")
        return found

    engines = names(args.engines, ENGINES, "engine(s)") if args.engines else None
    passes_off = names(args.passes_off, BY_NAME, "step(s)")
    out = args.out / args.pdf.stem
    if hasattr(sys.stdout, "reconfigure"):
        # Messages quote the document; a console that can't show a character gets "?", not a crash.
        sys.stdout.reconfigure(errors="replace")
    try:
        report = diagnose(args.pdf, level=args.level, engines=engines, auto=not args.no_auto,
                          passes_off=passes_off, out=out)
    finally:
        try:
            from app.core.engines.markitdown_worker_client import MarkItDownWorker

            MarkItDownWorker.get_instance().shutdown()
        except Exception:
            pass
    print()
    print(render(report))
    print(f"\nReport and Markdown: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
