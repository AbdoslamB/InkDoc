"""The table stage: find a PDF's tables, check an engine's Markdown, fix what is safe.

Runs after a PDF conversion when the "Auto quality" setting is Balanced or
Thorough (Fast, the default, skips it entirely):

1. pages to look at come from the probe (pages with drawn lines or boxes,
   app/core/pdf_probe.py), plus the pages of any wide table the engine wrote
   (four columns or more: possibly two borderless tables side by side), so a
   document without tables costs nothing more;
2. the table finder reads those pages' tables from the PDF itself, in the
   MarkItDown worker (app/core/table_finder.py), and splits the engine's wide
   tables where the PDF shows a gutter;
3. the engine's Markdown is compared with them (app/core/table_check.py);
4. in Auto, tables the engine got wrong are replaced with the PDF's own,
   only when verified against the text layer; explicit engines are only
   checked, never changed.

Thorough also looks for tables without lines on every text page (from the
words' positions). Such a table is written only where the engine wrote no
table at all, and only when a second, independent reading of the same area
(GLM-OCR, "Table Recognition:") agrees with its shape: inspect_tables ->
confirm_tables (in GLM-OCR's lane) -> finish_tables. run_table_stage is the
one-call version without second readings.

InkDoc thereby edits an engine's output. Every table it writes says where it
came from, in an HTML comment in the Markdown and in the response's "tables"
metadata. Never raises: any failure leaves the Markdown as it was.
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

from app.core.jobs import NULL_JOB, ConversionCancelledError
from app.core.markdown_tables import find_table_blocks, line_tokens
from app.core.table_check import (
    LOOSE,
    MISSING,
    PICTURE,
    BandResult,
    TableSummary,
    check,
    repair,
    summarize,
)
from app.core.thorough_passes import (
    ALL_DETECTORS,
    LEVEL_BALANCED,
    LEVEL_FAST,
    LEVEL_THOROUGH,
    PassPlan,
)

logger = logging.getLogger(__name__)

# The levels and the steps each runs live in app/core/thorough_passes.py.
LEVELS = (LEVEL_FAST, LEVEL_BALANCED, LEVEL_THOROUGH)
DEFAULT_LEVEL = LEVEL_FAST

SKIP_UNREADABLE = "unreadable"
SKIP_ERROR = "error"
SKIP_SWITCHED_OFF = "switched_off"  # every step that uses the table finder is off

# An engine's table this wide may be two borderless tables side by side.
HINT_MIN_COLUMNS = 4
# Share of a table's words a page's text layer must hold for it to be "on" that page.
HINT_PAGE_COVERAGE = 0.9
MAX_HINTS = 40

# find(path, pages, job, hints=..., detectors=...) -> the finder's JSON (app/core/table_finder.py)
Finder = Callable[..., dict]


def normalize_level(value: Any) -> str:
    return value if value in LEVELS else DEFAULT_LEVEL


def checks_tables(level: str, disabled: Collection[str] = ()) -> bool:
    """Whether the table check runs at this level (it does from Balanced up, unless switched off)."""
    return PassPlan(normalize_level(level), disabled=disabled).on("table_check")


def _find_in_worker(
    path: str, pages: list[int], job: Any, hints: dict | None = None, detectors: list[str] | None = None
) -> dict:
    from app.core.engines.markitdown_worker_client import find_tables_in_worker

    return find_tables_in_worker(path, pages, job, hints=hints, detectors=detectors)


def wide_table_hints(markdown: str, probe: Any) -> dict[int, list[list[list[str]]]]:
    """{page: [engine table rows, ...]} for the engine's tables of four columns or more.

    Each is placed on the page whose text layer holds (nearly) all its words,
    so the finder knows where to look for a gutter. Tables placed nowhere are
    left out. Pure apart from the probe's page text.
    """
    blocks = [b for b in find_table_blocks(markdown.split("\n")) if b.n_cols >= HINT_MIN_COLUMNS]
    if not blocks:
        return {}
    pages: list[Counter[str]] | None = None
    hints: dict[int, list[list[list[str]]]] = {}
    for block in blocks[:MAX_HINTS]:
        words = Counter(t for row in block.rows for cell in row for t in line_tokens(cell))
        total = sum(words.values())
        if not total:
            continue
        if pages is None:
            from app.core.quality_check import normalize_text, tokenize

            pages = [Counter(tokenize(normalize_text(p.text))) for p in probe.pages]
        best, best_cover = 0, 0.0
        for index, page_words in enumerate(pages):
            cover = sum(min(c, page_words[t]) for t, c in words.items()) / total
            if cover > best_cover:
                best, best_cover = index, cover
        if best_cover >= HINT_PAGE_COVERAGE:
            hints.setdefault(probe.pages[best].index + 1, []).append(block.rows)
    return hints


@dataclass
class TableInspection:
    """What inspect_tables found, carried to confirm_tables and finish_tables."""

    path: str
    markdown: str
    found: dict[str, Any] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    bands: list[BandResult] = field(default_factory=list)
    early: TableSummary | None = None   # set when there is nothing (more) to do


def _text_pages(probe: Any) -> list[int]:
    return [p.index + 1 for p in probe.pages if not p.image_only and p.char_count >= 25]


def inspect_tables(
    path: str,
    markdown: str,
    probe: Any,
    *,
    deep: bool = False,
    detectors: Collection[str] | None = None,
    job: Any = None,
    find: Finder | None = None,
) -> TableInspection:
    """Find the PDF's tables and compare them with `markdown`; changes nothing. Blocking.

    `detectors` (app/core/thorough_passes.py) are what the finder also looks
    for, on every text page: rating grids, tables without lines (those need
    confirm_tables before they can be written), positioned text. `deep` means
    all of them.
    A cancelled job raises ConversionCancelledError; any other failure ends
    the inspection with `early` set and the Markdown untouched.
    """
    job = job or NULL_JOB
    insp = TableInspection(path=path, markdown=markdown)
    if probe is None:
        insp.early = TableSummary(checked=False, skipped_reason=SKIP_UNREADABLE)
        return insp
    try:
        hints = wide_table_hints(markdown, probe)
    except Exception as exc:  # a hint is an extra, never a reason to stop
        logger.warning("Table hints skipped: %s", exc)
        hints = {}
    wanted = [d for d in ALL_DETECTORS if deep or d in (detectors or ())]
    pages = set(probe.table_pages) | set(hints)
    if wanted:  # these look at every text page, not only those with drawn tables
        pages |= set(_text_pages(probe))
    if not pages:
        insp.early = TableSummary(checked=True)
        return insp
    job.raise_if_cancelled()
    job.set_phase("checking", "Checking tables…")
    try:
        found = (find or _find_in_worker)(path, sorted(pages), job, hints=hints, detectors=wanted)
    except ConversionCancelledError:
        raise
    except Exception as exc:
        logger.warning("Table check skipped: %s", exc)
        insp.early = TableSummary(checked=False, skipped_reason=SKIP_ERROR)
        return insp
    if found.get("error") and not found.get("pages"):
        logger.warning("Table check skipped: %s", found["error"])
        insp.early = TableSummary(checked=False, skipped_reason=SKIP_ERROR)
        return insp
    job.raise_if_cancelled()
    try:
        insp.lines, insp.bands = check(markdown, found)
    except Exception as exc:  # a bug here must never cost the conversion
        logger.exception("Table check failed: %s", exc)
        insp.early = TableSummary(checked=False, skipped_reason=SKIP_ERROR)
        return insp
    insp.found = found
    return insp


# A table without lines, found from word positions, is written only when a
# second, independent reading of the same area agrees with it, and only where
# the engine wrote no table at all (loose lines, a picture, nothing).
CONFIRMABLE = (LOOSE, PICTURE, MISSING)
MAX_CONFIRMATIONS = 6
CONFIRM_TIME_LIMIT_S = 300.0
AGREE_SHARE = 0.9


def pending_confirmations(insp: TableInspection) -> list[tuple[BandResult, dict]]:
    """(band, table) pairs that could be written if a second reading agrees."""
    if insp.early is not None:
        return []
    return [
        (band, table)
        for band in insp.bands
        if band.located and band.issue in CONFIRMABLE
        for table in band.tables
        if table.get("confirm") and not table.get("verified")
    ]


def grids_agree(ours: list[list[str]], theirs: list[list[str]] | None) -> bool:
    """Same rows and columns, and at least AGREE_SHARE of the cells read alike."""
    from app.core.markdown_tables import cell_key

    if not ours or not theirs or len(ours) != len(theirs):
        return False
    width = max(len(r) for r in ours)
    if max(len(r) for r in theirs) != width:
        return False
    total = same = 0
    for a, b in zip(ours, theirs, strict=True):
        a = list(a) + [""] * (width - len(a))
        b = list(b) + [""] * (width - len(b))
        for x, y in zip(a, b, strict=True):
            total += 1
            same += cell_key(x) == cell_key(y)
    return total > 0 and same >= AGREE_SHARE * total


Reader = Callable[[str, int, list[float]], "list[list[str]] | None"]


def confirm_tables(
    insp: TableInspection,
    read: Reader,
    *,
    job: Any = None,
    limit: int = MAX_CONFIRMATIONS,
    time_limit_s: float = CONFIRM_TIME_LIMIT_S,
) -> int:
    """Read each pending table again with `read` (GLM-OCR); mark the ones that agree. Blocking.

    The table keeps its own text (exact, from the PDF): the second reading
    only has to agree on its shape and most of its cells. Returns how many
    were confirmed.
    """
    import time

    job = job or NULL_JOB
    pending = pending_confirmations(insp)[:limit]
    started = time.monotonic()
    confirmed = 0
    for i, (band, table) in enumerate(pending):
        job.raise_if_cancelled()
        if time.monotonic() - started > time_limit_s:
            table["reason"] = "there was no time left for a second reading"
            continue
        job.set_phase(
            "checking", f"Reading table {i + 1} of {len(pending)} with GLM-OCR…", engine="glm_ocr"
        )
        try:
            rows = read(insp.path, band.page, table["bbox"])
        except ConversionCancelledError:
            raise
        except Exception as exc:
            logger.warning("Second reading of a table failed: %s", exc)
            table["reason"] = "the second reading failed"
            continue
        if grids_agree(table["rows"], rows):
            table["verified"] = True
            table["reason"] = ""
            table["checked"] = "text-layer,glm-ocr"
            confirmed += 1
        else:
            table["reason"] = "GLM-OCR read it differently"
    return confirmed


def finish_tables(
    insp: TableInspection, *, repair_allowed: bool, kinds: Collection[str] | None = None
) -> tuple[str, TableSummary]:
    """(Markdown to keep, what happened to its tables): repair what may be repaired. Blocking.

    `kinds` are the finder results whose step is on (app/core/thorough_passes.py);
    None allows every kind.
    """
    if insp.early is not None:
        return insp.markdown, insp.early
    found = insp.found
    try:
        result = repair(insp.lines, insp.bands, kinds=kinds) if repair_allowed else insp.markdown
        summary = summarize(insp.bands, [p["page"] for p in found.get("pages", [])], repair_allowed=repair_allowed)
    except Exception as exc:  # a bug here must never cost the conversion
        logger.exception("Table check failed: %s", exc)
        return insp.markdown, TableSummary(checked=False, skipped_reason=SKIP_ERROR)
    if found.get("truncated"):
        summary.details.append("Some pages' tables were not checked: the check reached its time limit.")
    skipped = [p["page"] for p in found.get("pages", []) if p.get("skipped") == "rotated"]
    if skipped:
        summary.details.append(
            f"Rotated page{'s' if len(skipped) != 1 else ''} {', '.join(map(str, skipped[:10]))} "
            "not checked for tables."
        )
    return (result if summary.repaired else insp.markdown), summary


def run_table_stage(
    path: str,
    markdown: str,
    probe: Any,
    *,
    repair_allowed: bool,
    job: Any = None,
    find: Finder | None = None,
    deep: bool = False,
) -> tuple[str, TableSummary]:
    """inspect_tables and finish_tables in one call (no second readings). Blocking.

    `probe` is the pdf_probe result for `path` (None: the PDF could not be read).
    A cancelled job raises ConversionCancelledError; every other failure is
    reported in the summary and leaves the Markdown unchanged.
    """
    insp = inspect_tables(path, markdown, probe, deep=deep, job=job, find=find)
    return finish_tables(insp, repair_allowed=repair_allowed)
