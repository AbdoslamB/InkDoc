"""The steps of "Auto quality": what runs at Balanced and Thorough, in order, and what each needs.

One registry for every step InkDoc adds on top of an engine's conversion.
Auto (app/core/auto_engine.py) builds a PassPlan for each conversion and asks
it which steps are on; each step's work lives in its own module. A step that
is off, or whose engine is not installed, costs nothing and is reported as
skipped in the response's `tables.passes`.

Stages, in the order a conversion meets them:

- route:        before converting, which engine reads which pages
- polish:       right after converting
- tables:       the table check (its finder runs in the MarkItDown worker)
- finder:       extra things the finder looks for on every text page
- scanned:      tables on pages without a text layer, read again by GLM-OCR
- after_tables: additions once the tables are settled

Steps can be switched off one by one (the `thorough_passes_off` setting,
Settings -> General -> Auto quality -> Steps); a new step arrives switched on.

Adding a step: a PassInfo below (its place in PASSES is its order), its work
in its own module (named in PassInfo.modules), and a call where its stage
runs. A step is core code, so its modules import only what the core-or-add-on
rule allows (AGENTS.md, checked by tests/test_step_imports.py); what needs a
download is an add-on the step lists in needs_all / needs_any. Nothing here
imports anything heavy.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

LEVEL_FAST = "fast"
LEVEL_BALANCED = "balanced"
LEVEL_THOROUGH = "thorough"
_LEVEL_RANK = {LEVEL_FAST: 0, LEVEL_BALANCED: 1, LEVEL_THOROUGH: 2}

STAGE_ROUTE = "route"
STAGE_POLISH = "polish"
STAGE_TABLES = "tables"
STAGE_FINDER = "finder"
STAGE_SCANNED = "scanned"
STAGE_AFTER_TABLES = "after_tables"

# What a step can need: engines, and add-ons (app/core/addons.json).
ENGINE_NAMES = {"docling": "Docling", "glm_ocr": "GLM-OCR", "layout_model": "the layout model"}

# What the table finder looks for besides ruled tables (app/core/table_finder.py).
DETECT_RATING_GRIDS = "rating_grids"
DETECT_BORDERLESS = "borderless_tables"
DETECT_SEGMENTS = "segments"
ALL_DETECTORS = (DETECT_RATING_GRIDS, DETECT_BORDERLESS, DETECT_SEGMENTS)

# Which finder results each step owns: a table is only written when its step is on.
TABLE_KIND_LINES = "lines"            # ruled tables (pdfplumber's line strategy)
TABLE_KIND_HINT_SPLIT = "hint_split"  # an engine's wide table split at the PDF's gutter
TABLE_KIND_RATING = "rating_grids"    # rating grids drawn with markers
TABLE_KIND_BORDERLESS = "borderless_tables"  # from word positions, confirmed by GLM-OCR


@dataclass(frozen=True)
class PassInfo:
    name: str
    title: str
    description: str
    level: str                          # the lowest "Auto quality" level it runs at
    stage: str
    needs_all: tuple[str, ...] = ()     # engines that must all be installed
    needs_any: tuple[str, ...] = ()     # engines of which one is enough
    cost: str = "low"                   # what it adds to a conversion: low, medium, high
    detector: str = ""                  # what it asks the table finder to look for
    table_kinds: tuple[str, ...] = ()   # finder results it may write
    needs_segments: bool = False        # uses the finder's positioned text
    modules: tuple[str, ...] = ()       # where its work lives (tests/test_step_imports.py checks them)


PASSES: tuple[PassInfo, ...] = (
    PassInfo(
        "digital_layout", "Layout-aware engine for digital PDFs",
        "Reads a digital PDF with Docling (or GLM-OCR without Docling) instead of MarkItDown, which "
        "interleaves columns, sidebars and side-by-side tables.",
        LEVEL_THOROUGH, STAGE_ROUTE, needs_any=("docling", "glm_ocr"), cost="high",
        modules=("app.core.auto_engine",),
    ),
    PassInfo(
        "page_split", "Page split for mixed PDFs",
        "Reads the text pages of a PDF with Docling and only its scanned pages with GLM-OCR.",
        LEVEL_THOROUGH, STAGE_ROUTE, needs_all=("docling", "glm_ocr"), cost="medium",
        modules=("app.core.auto_engine", "app.core.pdf_pages"),
    ),
    PassInfo(
        "polish", "The PDF's own characters",
        "Puts back the '&', dashes and quotes an engine simplified, where the PDF spells the word one way.",
        LEVEL_THOROUGH, STAGE_POLISH, modules=("app.core.text_polish",),
    ),
    PassInfo(
        "table_check", "Table check",
        "Compares the result's tables with the PDF's own and, in Auto, rebuilds the ones an engine got "
        "wrong: merged side-by-side tables, merged cells, tables shown as loose text or as a picture.",
        LEVEL_BALANCED, STAGE_TABLES, table_kinds=(TABLE_KIND_LINES, TABLE_KIND_HINT_SPLIT),
        modules=("app.core.table_stage", "app.core.table_finder", "app.core.table_check",
                 "app.core.markdown_tables"),
    ),
    PassInfo(
        "rating_grids", "Rating grids",
        "Reads grids drawn with coloured markers (Underweight ... Overweight, arrows) and writes them "
        "as a table.",
        LEVEL_THOROUGH, STAGE_FINDER, detector=DETECT_RATING_GRIDS, table_kinds=(TABLE_KIND_RATING,),
        modules=("app.core.table_finder",),
    ),
    PassInfo(
        "borderless_tables", "Tables without lines",
        "Finds borderless tables from the positions of the PDF's words; writes one only when GLM-OCR's "
        "reading of the same area agrees.",
        LEVEL_THOROUGH, STAGE_FINDER, needs_all=("glm_ocr",), detector=DETECT_BORDERLESS,
        table_kinds=(TABLE_KIND_BORDERLESS,), cost="medium",
        modules=("app.core.table_finder", "app.core.table_stage"),
    ),
    PassInfo(
        "scanned_tables", "Tables on scanned pages",
        "Finds each table on a page without a text layer with the layout model and has GLM-OCR read it "
        "on its own; writes that reading when it agrees with GLM-OCR's reading of the whole page.",
        LEVEL_THOROUGH, STAGE_SCANNED, needs_all=("glm_ocr", "layout_model"), cost="high",
        modules=("app.core.scanned_tables",),
    ),
    PassInfo(
        "figure_text", "Chart text",
        "Adds the labels and values inside charts an engine left out; a labelled bar chart becomes a table.",
        LEVEL_THOROUGH, STAGE_AFTER_TABLES, needs_segments=True,
        modules=("app.core.figure_text", "app.core.table_finder"),
    ),
    PassInfo(
        "dropped_paragraphs", "Dropped paragraphs",
        "Puts back whole paragraphs an engine left out.",
        LEVEL_THOROUGH, STAGE_AFTER_TABLES, needs_segments=True,
        modules=("app.core.figure_text", "app.core.table_finder"),
    ),
)
BY_NAME: dict[str, PassInfo] = {p.name: p for p in PASSES}
PASS_NAMES: tuple[str, ...] = tuple(p.name for p in PASSES)


def level_rank(level: Any) -> int:
    return _LEVEL_RANK.get(level, 0)


def known_names(names: Any) -> tuple[str, ...]:
    """The step names among `names`, once each, in run order; anything else is dropped."""
    if not isinstance(names, (list, tuple, set, frozenset)):
        return ()
    return tuple(n for n in PASS_NAMES if n in names)


def _needs_text(names: Iterable[str], joiner: str) -> str:
    return f" {joiner} ".join(ENGINE_NAMES.get(n, n) for n in names)


class PassPlan:
    """Which steps are on for one conversion, and why the others are not."""

    def __init__(self, level: Any, available: Collection[str] = (), disabled: Collection[str] = ()) -> None:
        self.level = level if level in _LEVEL_RANK else LEVEL_FAST
        self.available = frozenset(available)
        self.disabled = frozenset(disabled)

    def relevant(self) -> list[PassInfo]:
        """The steps this level includes, on or not (what the response reports)."""
        return [p for p in PASSES if level_rank(self.level) >= level_rank(p.level)]

    def reason(self, name: str) -> str:
        """'' when the step is on; otherwise why it is not."""
        info = BY_NAME[name]
        if level_rank(self.level) < level_rank(info.level):
            return f"runs at {info.level.capitalize()} and above"
        if name in self.disabled:
            return "switched off"
        missing = [n for n in info.needs_all if n not in self.available]
        if missing:
            return f"needs {_needs_text(info.needs_all, 'and')}"
        if info.needs_any and not any(n in self.available for n in info.needs_any):
            return f"needs {_needs_text(info.needs_any, 'or')}"
        return ""

    def on(self, name: str) -> bool:
        return not self.reason(name)

    def detectors(self) -> list[str]:
        """What the table finder should look for besides ruled tables."""
        found = [p.detector for p in PASSES if p.detector and self.on(p.name)]
        if any(p.needs_segments and self.on(p.name) for p in PASSES):
            found.append(DETECT_SEGMENTS)
        return found

    def finder_needed(self) -> bool:
        """Whether the table finder runs at all (the table check, or a step that uses it)."""
        return self.on("table_check") or bool(self.detectors())

    def table_kinds(self) -> frozenset[str]:
        """Finder results that may be written (their step is on)."""
        return frozenset(k for p in PASSES if self.on(p.name) for k in p.table_kinds)


class PassLog:
    """What each step did in one conversion, for the response's `tables.passes`."""

    def __init__(self, plan: PassPlan) -> None:
        self._entries: dict[str, dict[str, Any]] = {}
        for info in plan.relevant():
            why = plan.reason(info.name)
            self._entries[info.name] = {"name": info.name, "ran": False, "changed": 0, "skipped": why}

    def ran(self, name: str, changed: int = 0) -> None:
        entry = self._entries.get(name)
        if entry is not None:
            entry.update(ran=True, skipped="")
            entry["changed"] += int(changed)

    def skipped(self, name: str, why: str) -> None:
        entry = self._entries.get(name)
        if entry is not None and not entry["ran"]:
            entry["skipped"] = why

    def to_list(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self._entries.values()]


def catalogue() -> list[dict[str, Any]]:
    """Every step, for the Settings page (GET /settings) and the API documentation."""
    return [
        {
            "name": p.name, "title": p.title, "description": p.description, "level": p.level,
            "needs_all": list(p.needs_all), "needs_any": list(p.needs_any), "cost": p.cost,
        }
        for p in PASSES
    ]
