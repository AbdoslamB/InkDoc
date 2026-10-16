"""Put back the text inside figures that an engine left out (Auto, Thorough).

Docling marks a chart or picture with "<!-- image -->" and keeps none of
what is written inside it. In a digital PDF that text is still in the PDF:
axis titles, category labels, the values printed on the bars. This module
finds where each figure sits on its page, between the PDF lines the engine
wrote just before and just after the placeholder, and adds the PDF's text
from that area right after the placeholder:

- as a small table when the figure is a labelled bar chart (a label and a
  value on each row, or categories under bars with their values above);
- otherwise as one line of the figure's text, in reading order.

Each addition is preceded by "<!-- inkdoc:figure-text page=N source=pdf-text -->".
Exact by construction (it is the PDF's own text), and only ever added: no
line of the engine's Markdown is removed or changed.

Pure: `pages` are the positioned text segments from the table finder
(app/core/table_finder.page_segments), coordinates in points from the
page's top-left corner.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.markdown_tables import line_tokens, render_pipe_table

_PLACEHOLDER = re.compile(r"^\s*<!--\s*image\s*-->\s*$", re.I)
_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")
_NUMBER = re.compile(r"^[(\-+−–]?[$€£]?\d[\d.,]*%?\)?[*†]*$")
ANCHOR_MIN_TOKENS = 2
X_MARGIN = 20.0
ROW_TOLERANCE = 3.0
MIN_SEGMENTS = 2
MIN_PAIRS = 3


def _is_number(text: str) -> bool:
    return bool(_NUMBER.match(text.strip()))


@dataclass
class _Seg:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    tokens: list[str]

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


def _segments(page: dict) -> list[_Seg]:
    return [
        _Seg(s["text"], s["x0"], s["x1"], s["top"], s["bottom"], line_tokens(s["text"]))
        for s in page.get("segments") or []
    ]


# ─── Locating a figure on its page ───────────────────────────────────────────
def _ends_with(line: list[str], seg: list[str]) -> bool:
    return bool(seg) and len(seg) <= len(line) and line[len(line) - len(seg):] == seg


def _starts_with(line: list[str], seg: list[str]) -> bool:
    return bool(seg) and len(seg) <= len(line) and line[:len(seg)] == seg


def _find_region(
    pages: list[dict], segs: dict[int, list[_Seg]], before: list[str], after: list[str], start_page: int
) -> tuple[int, _Seg, _Seg] | None:
    """(page, segment the line before ends with, segment the line after starts with)."""
    for page in pages:
        number = page["page"]
        if number < start_page:
            continue
        candidates = segs[number]
        befores = [s for s in candidates if len(s.tokens) >= ANCHOR_MIN_TOKENS and _ends_with(before, s.tokens)]
        afters = [s for s in candidates if len(s.tokens) >= ANCHOR_MIN_TOKENS and _starts_with(after, s.tokens)]
        # The longest matches first (a chart's own label can end like the
        # caption above it: "... Midterm Election Day"), then the closest pair.
        best: tuple[tuple[int, float], _Seg, _Seg] | None = None
        for b in befores:
            for a in afters:
                gap = a.top - b.bottom
                if gap <= 0:
                    continue
                key = (-(len(b.tokens) + len(a.tokens)), gap)
                if best is None or key < best[0]:
                    best = (key, b, a)
        if best is not None:
            return number, best[1], best[2]
    return None


def _split_by_x(items: list[_Seg], parts: int) -> list[list[_Seg]]:
    """The figure's segments in `parts` side-by-side groups (several figures in a row)."""
    if parts <= 1 or len(items) < parts:
        return [items]
    spans: list[list[float]] = []
    for s in sorted(items, key=lambda s: s.x0):
        if spans and s.x0 <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], s.x1)
        else:
            spans.append([s.x0, s.x1])
    if len(spans) < parts:
        return [items]
    gaps = sorted(range(len(spans) - 1), key=lambda i: spans[i + 1][0] - spans[i][1], reverse=True)[: parts - 1]
    cuts = sorted((spans[i][1] + spans[i + 1][0]) / 2 for i in gaps)
    groups: list[list[_Seg]] = [[] for _ in range(parts)]
    for s in items:
        groups[sum(1 for c in cuts if s.cx > c)].append(s)
    return groups


# ─── Writing a figure's text ─────────────────────────────────────────────────
def _rows(items: list[_Seg]) -> list[list[_Seg]]:
    rows: list[list[_Seg]] = []
    for s in sorted(items, key=lambda s: (s.cy, s.x0)):
        if rows and abs(s.cy - rows[-1][0].cy) <= ROW_TOLERANCE:
            rows[-1].append(s)
        else:
            rows.append([s])
    return [sorted(r, key=lambda s: s.x0) for r in rows]


def _label_value_rows(rows: list[list[_Seg]]) -> tuple[list[list[str]], list[list[_Seg]]] | None:
    """A horizontal bar chart: rows of 'label  value'. (table rows, rows left over)."""
    pairs, rest = [], []
    for row in rows:
        if len(row) == 2 and not _is_number(row[0].text) and _is_number(row[1].text):
            pairs.append([row[0].text, row[1].text])
        else:
            rest.append(row)
    if len(pairs) >= MIN_PAIRS and len(pairs) >= len(rows) / 2:
        return pairs, rest
    return None


def _category_values(rows: list[list[_Seg]]) -> tuple[list[list[str]], list[list[_Seg]]] | None:
    """A vertical bar chart: categories along the bottom, each with the value printed at its bar."""
    items = [s for r in rows for s in r]
    numbers = [s for s in items if _is_number(s.text)]
    # The value axis: numbers stacked in one column.
    axis = [s for s in numbers if sum(1 for o in numbers if abs(o.cx - s.cx) < 6 and o is not s) >= 2]
    axis_ids = {id(s) for s in axis}
    labelled = [r for r in rows if sum(1 for s in r if id(s) not in axis_ids) >= 3]
    if not labelled:
        return None
    # Category labels: the lowest row of three or more, plus a second line right under it.
    base = labelled[-1]
    below = [r for r in rows if r[0].cy > base[0].cy]
    categories = [[s.text, s.x0, s.x1, s.cx, s.cy] for s in base if id(s) not in axis_ids]
    if len(categories) < MIN_PAIRS:
        return None
    for row in below:
        for s in row:
            home = next((c for c in categories if c[1] - 2 <= s.cx <= c[2] + 2), None)
            if home is None:
                return None
            home[0] = f"{home[0]} {s.text}"
    base_ids = {id(s) for s in base} | {id(s) for r in below for s in r}
    values = [s for s in numbers if id(s) not in axis_ids and id(s) not in base_ids]
    if len(values) != len(categories):
        return None
    spacing = min(b[3] - a[3] for a, b in zip(categories, categories[1:], strict=False))
    table, used = [], set()
    for cat in categories:
        near = min(values, key=lambda v: abs(v.cx - cat[3]))
        if abs(near.cx - cat[3]) > spacing / 2 or id(near) in used:
            return None
        used.add(id(near))
        table.append([cat[0], near.text])
    rest = [
        [s for s in r if id(s) not in base_ids and id(s) not in used and id(s) not in axis_ids]
        for r in rows
    ]
    return table, [r for r in rest if r]


def figure_lines(items: list[_Seg], page: int) -> list[str]:
    """The Markdown added after one figure's placeholder."""
    rows = _rows(items)
    out = [f"<!-- inkdoc:figure-text page={page} source=pdf-text -->"]
    shaped = _label_value_rows(rows)
    header = ["Label", "Value"]
    if shaped is None:
        shaped = _category_values(rows)
        header = ["Category", "Value"]
    if shaped is not None:
        table, rest = shaped
        text = " · ".join(" ".join(s.text for s in r) for r in rest if r)
        if text:
            out += [f"*Text in the figure:* {text}", ""]
        out += render_pipe_table([header, *table])
        return out
    text = " · ".join(" ".join(s.text for s in r) for r in rows)
    out.append(f"*Text in the figure:* {text}")
    return out


# ─── Paragraphs an engine dropped ────────────────────────────────────────────
# A whole run of lines missing from the result (a disclaimer, a note under a
# table) is put back after the paragraph that holds the line just above it in
# the same column. Short pieces are never put back on their own: an engine
# may have joined a hyphenated word or read a footnote number into a word,
# which makes a single line look missing when it is not.
DROPPED_MIN_LINES = 3
DROPPED_MIN_TOKENS = 15
MARGIN_SHARE = 0.05            # page headers and footers live in the top and bottom 5%
RUNNING_MIN_PAGES = 3
ANCHOR_SEGMENTS = 3


def _key(text: str) -> str:
    return re.sub(r"\d", "#", text.strip().lower())


_BLOCK_MARKER = re.compile(r"^(\s*)([*+\-#>]|\d+[.)])(\s)")


def _join_lines(texts: list[str]) -> str:
    """One paragraph of PDF lines, safe to put in Markdown as plain text."""
    out = ""
    for t in texts:
        t = t.strip()
        if out.endswith("-") and t[:1].islower():
            out += t                      # "data-" + "center" -> "data-center"
        else:
            out = f"{out} {t}".strip()
    # A footnote's "* Many products ..." is not a bullet, nor "1. " a list.
    return _BLOCK_MARKER.sub(lambda m: f"{m.group(1)}\\{m.group(2)}{m.group(3)}", out, count=1)


def recover_dropped_paragraphs(markdown: str, pages: list[dict]) -> tuple[str, int]:
    """(Markdown with whole paragraphs the engine left out put back, paragraphs restored)."""
    pages = sorted((p for p in pages if p.get("segments")), key=lambda p: p["page"])
    if not pages:
        return markdown, 0
    lines = markdown.split("\n")
    stream: list[tuple[str, int]] = [(t, i) for i, ln in enumerate(lines) for t in line_tokens(ln)]
    joined = " " + " ".join(t for t, _ in stream) + " "
    running: dict[str, int] = {}
    for p in pages:
        for key in {_key(s["text"]) for s in p["segments"]}:
            running[key] = running.get(key, 0) + 1

    def present(seg: _Seg) -> bool:
        if len(seg.tokens) < 3:
            return all(f" {t} " in joined for t in seg.tokens)
        return f" {' '.join(seg.tokens)} " in joined

    insertions: list[tuple[int, list[str]]] = []
    for p in pages:
        height = float(p.get("height") or 792)
        segs = [
            s for s in _segments(p)
            if s.tokens and running.get(_key(s.text), 0) < RUNNING_MIN_PAGES
            and MARGIN_SHARE * height <= s.top and s.bottom <= (1 - MARGIN_SHARE) * height
        ]
        segs.sort(key=lambda s: (s.top, s.x0))
        blocks: list[list[_Seg]] = []
        for s in segs:
            if present(s):
                continue
            line_h = max(1.0, s.bottom - s.top)
            home = next(
                (b for b in blocks if abs(b[0].x0 - s.x0) <= 15 and 0 <= s.top - b[-1].bottom <= 1.8 * line_h),
                None,
            )
            if home is None:
                blocks.append([s])
            else:
                home.append(s)
        for block in blocks:
            if len(block) < DROPPED_MIN_LINES or sum(len(s.tokens) for s in block) < DROPPED_MIN_TOKENS:
                continue
            bx0, bx1 = min(s.x0 for s in block), max(s.x1 for s in block)
            above = [
                s for s in segs
                if s.bottom <= block[0].top and s.x0 < bx1 and s.x1 > bx0 and present(s)
            ]
            above.sort(key=lambda s: s.bottom)
            anchor = [t for s in above[-ANCHOR_SEGMENTS:] for t in s.tokens]
            if len(anchor) < 3:
                continue
            target = f" {' '.join(anchor)} "
            at = joined.find(target)
            if at < 0 or joined.find(target, at + 1) >= 0:
                continue  # not found, or not in one place only
            last_token = joined[:at + len(target)].count(" ") - 2
            end = stream[min(last_token, len(stream) - 1)][1]
            while end + 1 < len(lines) and lines[end + 1].strip():
                end += 1                  # after the paragraph or table that holds the anchor
            text = _join_lines([s.text for s in block])
            insertions.append((end, ["", f"<!-- inkdoc:dropped-text page={p['page']} source=pdf-text -->", text]))
    for end, block_lines in sorted(insertions, key=lambda t: t[0], reverse=True):
        lines[end + 1:end + 1] = block_lines
    return "\n".join(lines), len(insertions)


# ─── Entry point ─────────────────────────────────────────────────────────────
def recover_figure_text(markdown: str, pages: list[dict]) -> tuple[str, int]:
    """(Markdown with each figure's text added after its placeholder, figures filled)."""
    pages = [p for p in pages if p.get("segments")]
    if not pages or "<!--" not in markdown:
        return markdown, 0
    pages.sort(key=lambda p: p["page"])
    segs = {p["page"]: _segments(p) for p in pages}
    lines = markdown.split("\n")

    def is_text(i: int) -> bool:
        return bool(lines[i].strip()) and not _COMMENT.match(lines[i])

    # Runs of placeholders (blank lines between them allowed).
    groups: list[list[int]] = []
    i = 0
    while i < len(lines):
        if _PLACEHOLDER.match(lines[i]):
            group = [i]
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or _PLACEHOLDER.match(lines[j])):
                if _PLACEHOLDER.match(lines[j]):
                    group.append(j)
                j += 1
            groups.append(group)
            i = j
        else:
            i += 1

    insertions: list[tuple[int, list[str]]] = []
    page_cursor = 0
    text_so_far = " ".join(lines)
    for group in groups:
        b = next((k for k in range(group[0] - 1, -1, -1) if is_text(k)), None)
        a = next((k for k in range(group[-1] + 1, len(lines)) if is_text(k)), None)
        if b is None or a is None:
            continue
        found = _find_region(pages, segs, line_tokens(lines[b]), line_tokens(lines[a]), page_cursor)
        if found is None:
            continue
        number, before, after = found
        page_cursor = number
        x0 = min(before.x0, after.x0) - X_MARGIN
        x1 = max(before.x1, after.x1) + X_MARGIN
        inside = [
            s for s in segs[number]
            if s is not before and s is not after and x0 <= s.cx <= x1 and before.bottom <= s.cy <= after.top
        ]
        # Text the engine did write (a caption it kept) is not repeated.
        inside = [s for s in inside if len(s.tokens) < 3 or s.text not in text_so_far]
        if len(inside) < MIN_SEGMENTS:
            continue
        for index, part in zip(group, _split_by_x(inside, len(group)), strict=False):
            if len(part) >= MIN_SEGMENTS:
                insertions.append((index, figure_lines(part, number)))
    for index, block in sorted(insertions, key=lambda t: t[0], reverse=True):
        lines[index + 1:index + 1] = ["", *block]
    return "\n".join(lines), len(insertions)
