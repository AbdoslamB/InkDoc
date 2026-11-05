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

When no line the engine wrote sits below the figure on its page (the figure
fills the rest of the page, or the engine put the caption above it), the
figure's page is found from the lines around the placeholder
(app/core/quality_check.page_spans) and its text is the page's text the
engine left out. Text inside drawn boxes (a flowchart, a diagram's panels)
is written as a nested list, box by box, in reading order.

Each addition is preceded by "<!-- inkdoc:figure-text page=N source=pdf-text -->".
Exact by construction (it is the PDF's own text), and only ever added: no
line of the engine's Markdown is changed. The one removal is a line next to
a placeholder made only of characters the PDF's text has nowhere ("口",
what an engine reads a chart's legend marker as).

Charts drawn in the PDF (the chart_data step) are measured, not read: each
bar's end and each line's points, mapped to values through the axis labels'
positions, as "<!-- inkdoc:chart page=N source=pdf-shapes checked=axis-labels -->"
and a table. A chart whose text states an average the measured values don't
give is not written.

Also here, the steps that fix how an engine laid out the PDF's lines:
join_broken_lines (one PDF line per paragraph) and mark_checkboxes.

Pure: `pages` are the positioned text segments from the table finder
(app/core/table_finder.page_segments and page_layout), coordinates in points
from the page's top-left corner.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.markdown_tables import line_tokens, render_pipe_table
from app.core.quality_check import normalize_markdown, page_of_line, page_spans

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


def _rotated(page: dict) -> list[_Seg]:
    """Labels written sideways (axis titles, a diagram's vertical labels)."""
    return [
        _Seg(s["text"], s["x0"], s["x1"], s["top"], s["bottom"], line_tokens(s["text"]))
        for s in page.get("vtext") or []
    ]


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _letters(text: str) -> str:
    return re.sub(r"[\W_]+", "", text)


class _Presence:
    """Whether a piece of the PDF's text is somewhere in the Markdown.

    By its words, or, for a text layer that lost its spaces
    ("DSPy(Khattabetal.,2024)"), by its characters with all spacing ignored.
    """

    def __init__(self, markdown: str) -> None:
        stream = [t for ln in markdown.split("\n") for t in line_tokens(ln)]
        self.joined = " " + " ".join(stream) + " "
        self.squashed = _letters(normalize_markdown(markdown))

    def has(self, seg: _Seg) -> bool:
        if len(seg.tokens) < 3:
            if all(f" {t} " in self.joined for t in seg.tokens):
                return True
        elif f" {' '.join(seg.tokens)} " in self.joined:
            return True
        key = _letters(normalize_markdown(seg.text))
        return len(key) >= 8 and key in self.squashed

    def label(self, seg: _Seg) -> bool:
        """A short label ("30%", "Yes"): part of a figure even when the same words are elsewhere."""
        return len(seg.tokens) < 3


def _page_map(markdown: str, pages: list[dict]) -> dict[int, tuple[int, int]]:
    texts = {p["page"]: "\n".join(s["text"] for s in p.get("segments") or []) for p in pages}
    return page_spans(markdown, texts)


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


def _running_keys(pages: list[dict]) -> dict[str, int]:
    """How many pages each line (numbers aside) is on: page headers and footers repeat."""
    running: dict[str, int] = {}
    for p in pages:
        for key in {_key(s["text"]) for s in p.get("segments") or []}:
            running[key] = running.get(key, 0) + 1
    return running


def _body(seg: _Seg, height: float, running: dict[str, int]) -> bool:
    """Not a page header or footer: inside the page's margins and, if it has words, not on many pages.

    Numbers alone ("30", "1.2%") repeat across pages as chart labels; page numbers are in the margins.
    """
    repeated = any(ch.isalpha() for ch in seg.text) and running.get(_key(seg.text), 0) >= RUNNING_MIN_PAGES
    return not repeated and MARGIN_SHARE * height <= seg.top and seg.bottom <= (1 - MARGIN_SHARE) * height


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
    running = _running_keys(pages)
    present = _Presence(markdown).has

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


# ─── Text in drawn boxes (diagrams, flowcharts) ──────────────────────────────
BOX_MIN_W = 20.0               # narrower boxes are markers and bars, not panels
BOX_MIN_H = 10.0
BOX_FRAME_SHARE = 0.9          # a box around this share of a figure's text is its frame
BOXED_MIN_BOXES = 2
BOXED_MIN_SHARE = 0.5          # this share of the figure's text must sit in boxes
_LIST_MARKER = re.compile(r"^([-*+#>]|\d+[.)])")
_TAG_START = re.compile(r"<(?=[A-Za-z/!?])")


@dataclass(eq=False)
class _Box:
    x0: float
    top: float
    x1: float
    bottom: float
    items: list[_Seg] = field(default_factory=list)
    children: list[_Box] = field(default_factory=list)

    @property
    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.bottom - self.top)

    def holds(self, x: float, y: float) -> bool:
        return self.x0 - 1 <= x <= self.x1 + 1 and self.top - 1 <= y <= self.bottom + 1

    def inside(self, other: _Box) -> bool:
        return (
            other is not self and other.area > self.area
            and other.x0 - 1 <= self.x0 and self.x1 <= other.x1 + 1
            and other.top - 1 <= self.top and self.bottom <= other.bottom + 1
        )


def _bounds(node: _Box | _Seg) -> tuple[float, float, float, float]:
    return node.x0, node.top, node.x1, node.bottom


def _xy_order(nodes: list) -> list:
    """Reading order of boxes and loose text: bands top to bottom, columns left to right (an XY cut)."""
    if len(nodes) <= 1:
        return list(nodes)
    for lo, hi in ((1, 3), (0, 2)):  # first cut across (bands), then down (columns)
        spans = sorted(nodes, key=lambda n: _bounds(n)[lo])
        groups: list[list] = [[spans[0]]]
        reach = _bounds(spans[0])[hi]
        for node in spans[1:]:
            b = _bounds(node)
            if b[lo] >= reach - 0.5:
                groups.append([node])
            else:
                groups[-1].append(node)
            reach = max(reach, b[hi])
        if len(groups) > 1:
            return [n for g in groups for n in _xy_order(g)]
    return sorted(nodes, key=lambda n: (_bounds(n)[1], _bounds(n)[0]))


def _join_rows(items: list[_Seg]) -> str:
    """A box's lines as one entry: wrapped lines joined, separate ones with " · "."""
    out = ""
    for row in _rows(items):
        text = " ".join(s.text for s in row).strip()
        if not out:
            out = text
        elif out.endswith("-") and text[:1].islower():
            out += text
        elif text[:1].islower() or out.endswith((",", "(", "/")):
            out += " " + text
        else:
            out += " · " + text
    return out


def _safe(text: str) -> str:
    """The PDF's text as Markdown text: "<n>" is not a tag."""
    return _TAG_START.sub("&lt;", text)


def _entry(text: str) -> str:
    """List-item text that stays text ("-Age >65y" is not a nested list, "<n>" not a tag)."""
    text = _safe(text)
    return "\\" + text if _LIST_MARKER.match(text) else text


def _boxed_lines(items: list[_Seg], boxes: list[dict]) -> list[str] | None:
    """The figure's text as a nested list, one entry per drawn box, or None if it isn't drawn in boxes."""
    if not items or not boxes:
        return None
    candidates = [
        _Box(float(b["x0"]), float(b["top"]), float(b["x1"]), float(b["bottom"]))
        for b in boxes
        if float(b["x1"]) - float(b["x0"]) >= BOX_MIN_W and float(b["bottom"]) - float(b["top"]) >= BOX_MIN_H
    ]
    homes: dict[int, _Box] = {}
    for seg in items:
        holding = [b for b in candidates if b.holds(seg.cx, seg.cy)]
        if holding:
            homes[id(seg)] = min(holding, key=lambda b: b.area)
    used = list({id(h): h for h in homes.values()}.values())
    # A frame around (nearly) all of the figure's text adds a level and says nothing.
    frames = [
        b for b in candidates
        if sum(1 for s in items if b.holds(s.cx, s.cy)) >= BOX_FRAME_SHARE * len(items)
    ]
    frame_ids = {id(f) for f in frames}
    for key, home in list(homes.items()):
        if id(home) in frame_ids:
            del homes[key]
    used = [b for b in used if id(b) not in frame_ids]
    if len(used) < BOXED_MIN_BOXES or len(homes) < BOXED_MIN_SHARE * len(items):
        return None
    # Keep the boxes that hold text, and the ones around them (a panel of boxes).
    kept = list(used)
    kept_ids = {id(b) for b in kept}
    for b in candidates:
        if id(b) not in kept_ids and id(b) not in frame_ids and any(u.inside(b) for u in used):
            kept.append(b)
            kept_ids.add(id(b))
    for b in kept:
        b.items, b.children = [], []
    for seg in items:
        home = homes.get(id(seg))
        if home is not None:
            home.items.append(seg)
    roots: list[_Box] = []
    for b in sorted(kept, key=lambda b: b.area):
        parents = [o for o in kept if b.inside(o)]
        if parents:
            min(parents, key=lambda o: o.area).children.append(b)
        else:
            roots.append(b)
    loose = [s for s in items if id(s) not in homes]

    def render(nodes: list, depth: int) -> list[str]:
        out: list[str] = []
        pad = "  " * depth
        for node in _xy_order(nodes):
            if isinstance(node, _Seg):
                out.append(f"{pad}- {_entry(node.text)}")
                continue
            if not node.children:
                if node.items:
                    out.append(f"{pad}- {_entry(_join_rows(node.items))}")
                continue
            # A panel: its heading is the text above every box inside it.
            first = min(c.top for c in node.children)
            title = [s for s in node.items if s.bottom <= first + 1]
            rest = [s for s in node.items if s.bottom > first + 1]
            if title:
                out.append(f"{pad}- {_entry(_join_rows(title))}")
                out += render([*node.children, *rest], depth + 1)
            else:
                out += render([*node.children, *rest], depth)
        return out

    lines = render([*roots, *loose], 0)
    return lines or None


# ─── Charts drawn in the PDF (the chart_data step) ───────────────────────────
AXIS_MIN_TICKS = 3
AXIS_ALIGN_PT = 3.0            # tick labels of one axis line up within this
AXIS_MAX_RESIDUAL = 0.03       # share of the axis range a tick may sit off its straight line
BASELINE_PT = 1.2              # a bar end this close to the baseline starts there
BARS_MIN = 3                   # bars of one colour on one baseline make a series
SERIES_MIN_POINTS = 3
LEGEND_REACH_PT = 24.0
STATED_TOLERANCE = 0.05        # a stated average agrees within this share of one tick step
FLAT_PT = 0.5                  # a line this flat is a reference line (an average, a target), not data
_STATED = re.compile(r"(?:average|avg\.?|mean)[^=]{0,60}=\s*([-−+]?\d[\d.,]*\s*%?)", re.I)
_NUM = re.compile(r"^\(?([-−+]?)[$€£]?(\d[\d,]*(?:\.\d+)?)(%?)\)?$")


def _value(text: str) -> tuple[float, str, int] | None:
    """(number, unit, decimals) of an axis label ("-20%" -> (-20.0, "%", 0)), or None."""
    m = _NUM.match(text.strip().replace(" ", ""))
    if m is None:
        return None
    sign, digits, unit = m.groups()
    number = float(digits.replace(",", ""))
    decimals = len(digits.split(".", 1)[1]) if "." in digits else 0
    return (-number if sign in ("-", "−") else number), unit, decimals


@dataclass
class _Axis:
    slope: float
    offset: float
    unit: str
    decimals: int
    low: float                 # smallest and largest tick values
    high: float
    step: float
    at: float                  # where the labels sit: the x of a value axis, the y of an axis under a chart
    ticks: list[tuple[float, float]] = field(default_factory=list)  # (position, value)

    def value(self, position: float) -> float:
        return self.slope * position + self.offset

    def position(self, value: float) -> float:
        return (value - self.offset) / self.slope


def _fit(points: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """(slope, offset, worst miss) of value = slope * position + offset."""
    n = len(points)
    mx = sum(p for p, _ in points) / n
    my = sum(v for _, v in points) / n
    sxx = sum((p - mx) ** 2 for p, _ in points)
    if sxx <= 0:
        return None
    slope = sum((p - mx) * (v - my) for p, v in points) / sxx
    offset = my - slope * mx
    return slope, offset, max(abs(slope * p + offset - v) for p, v in points)


_GLUED = re.compile(r"[-−]?\d+(?:\.\d+)?%?")


def _unglued(items: list[_Seg]) -> list[_Seg]:
    """Tick labels a text layer ran together ("-50-45-40") as one piece each, placed by character."""
    out: list[_Seg] = []
    for s in items:
        text = s.text.replace(" ", "")
        parts = list(_GLUED.finditer(text))
        if len(parts) < 2 or "".join(m.group(0) for m in parts) != text:
            out.append(s)
            continue
        width = (s.x1 - s.x0) / max(1, len(text))
        for m in parts:
            x0 = s.x0 + m.start() * width
            out.append(_Seg(m.group(0), x0, x0 + len(m.group(0)) * width, s.top, s.bottom, [m.group(0)]))
    return out


def _axes(items: list[_Seg], vertical: bool) -> list[_Axis]:
    """Numeric tick labels that line up: a column of them (vertical) or a row, each a straight scale."""
    numbers = [(s, _value(s.text)) for s in (items if vertical else _unglued(items))]
    numbers = [(s, v) for s, v in numbers if v is not None]
    found: list[_Axis] = []
    used: set[int] = set()
    for s, _v in sorted(numbers, key=lambda t: t[0].x1 if vertical else t[0].cy):
        if id(s) in used:
            continue
        line = [
            (o, ov) for o, ov in numbers
            if id(o) not in used and abs((o.x1 - s.x1) if vertical else (o.cy - s.cy)) <= AXIS_ALIGN_PT
        ]
        if len(line) < AXIS_MIN_TICKS:
            continue
        points = sorted(((o.cy if vertical else o.cx), ov[0]) for o, ov in line)
        values = [v for _, v in points]
        if len(set(values)) != len(values):
            continue
        rising = all(b > a for a, b in zip(values, values[1:], strict=False))
        falling = all(b < a for a, b in zip(values, values[1:], strict=False))
        if not (rising or falling):
            continue
        fit = _fit(points)
        if fit is None:
            continue
        slope, offset, miss = fit
        span = max(values) - min(values)
        if span <= 0 or miss > AXIS_MAX_RESIDUAL * span:
            continue
        used.update(id(o) for o, _ in line)
        steps = sorted(abs(b - a) for a, b in zip(values, values[1:], strict=False))
        at = (sum(o.x1 for o, _ in line) if vertical else sum(o.cy for o, _ in line)) / len(line)
        found.append(_Axis(
            slope, offset, line[0][1][1], max(ov[2] for _, ov in line), min(values), max(values),
            steps[len(steps) // 2], at, points,
        ))
    return found


def _fmt(value: float, axis: _Axis) -> str:
    text = f"{value:.{min(3, axis.decimals + 1)}f}"
    if text.startswith("-") and float(text) == 0:
        text = text[1:]
    return text + axis.unit


def _fmt_tick(value: float, axis: _Axis) -> str:
    return f"{value:.{axis.decimals}f}{axis.unit}"


def _fill_key(b: dict) -> str:
    return b.get("rgb") or b.get("fill") or ""


def _line_key(p: dict) -> str:
    return p.get("rgb") or p.get("color") or ""


def _legend(color: str, boxes: list[dict], paths: list[dict], items: list[_Seg]) -> tuple[str, _Seg | None]:
    """The legend label next to a small swatch or line sample of `color`: (text, its segment)."""
    samples: list[tuple[float, float, float]] = []  # (right edge, centre y, height)
    for b in boxes:
        w, h = float(b["x1"]) - float(b["x0"]), float(b["bottom"]) - float(b["top"])
        if _fill_key(b) == color and w <= 14 and h <= 14:
            samples.append((float(b["x1"]), (float(b["top"]) + float(b["bottom"])) / 2, h))
    for p in paths:
        pts = p["pts"]
        if _line_key(p) == color and len(pts) <= 3:
            xs, ys = [x for x, _ in pts], [y for _, y in pts]
            if max(xs) - min(xs) <= 40 and max(ys) - min(ys) <= 1.5:
                samples.append((max(xs), sum(ys) / len(ys), 4.0))
    for x1, cy, h in samples:
        right = [s for s in items if 0 <= s.x0 - x1 <= LEGEND_REACH_PT and abs(s.cy - cy) <= max(4.0, h)]
        if right:
            label = min(right, key=lambda s: s.x0)
            return label.text, label
    return "", None


def _stated_check(texts: list[str], means: list[float], axis: _Axis) -> str | None:
    """"stated-average" when the figure states averages and each matches a series, "" when it
    states none, None when one matches nothing (the measured values disagree with the figure)."""
    stated = []
    for text in texts:
        for m in _STATED.finditer(text):
            parsed = _value(m.group(1))
            if parsed is not None:
                stated.append(parsed[0])
    if not stated:
        return ""
    tolerance = STATED_TOLERANCE * axis.step
    if all(any(abs(mean - s) <= tolerance for mean in means) for s in stated):
        return "stated-average"
    return None


def _in(region: tuple[float, float, float, float], x: float, y: float) -> bool:
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def _chart_tables(items: list[_Seg], layout: dict, region: tuple[float, float, float, float], page: int) -> list[str]:
    """Tables of the values of the charts drawn in `region` (bars and lines), or []."""
    inside = [s for s in _segments(layout) if _in(region, s.cx, s.cy)]
    rotated = [s for s in _rotated(layout) if _in(region, s.cx, s.cy)]
    boxes = [
        b for b in layout.get("boxes") or []
        if _in(region, (float(b["x0"]) + float(b["x1"])) / 2, (float(b["top"]) + float(b["bottom"])) / 2)
    ]
    paths = [p for p in layout.get("paths") or [] if all(_in(region, x, y) for x, y in (p["pts"][0], p["pts"][-1]))]
    value_axes = sorted(_axes(inside, vertical=True), key=lambda a: a.at)
    out: list[str] = []
    for i, axis in enumerate(value_axes):
        # The chart's own area: right of its value axis, up to the next one.
        right = value_axes[i + 1].at - 25 if i + 1 < len(value_axes) else region[2]
        ends = sorted((axis.position(axis.low), axis.position(axis.high)))
        reach = abs(axis.position(axis.step) - axis.position(0.0)) / 2
        table = _one_chart(inside, rotated, boxes, paths, axis, (axis.at, ends[0] - reach, right, ends[1] + reach))
        if table is None:
            continue
        header, rows, checked, title = table
        out += ["", f"<!-- inkdoc:chart page={page} source=pdf-shapes checked={checked} -->"]
        out.append(f"*{title}: values measured from the chart's drawing*" if title else "*Values measured from the chart's drawing*")
        out += ["", *render_pipe_table([header, *rows])]
    return out


def _bar_values(boxes: list[dict], axis: _Axis, plot: tuple[float, float, float, float]) -> list[tuple[str, float, float]]:
    """(colour, centre x, value) of each bar standing on the chart's baseline."""
    px0, ptop, px1, pbottom = plot
    bars = [
        b for b in boxes
        if b.get("fill") and b["fill"] != "white"
        and px0 < float(b["x0"]) and float(b["x1"]) <= px1 + 1
        and ptop - 1 <= float(b["top"]) <= pbottom
        and float(b["x1"]) - float(b["x0"]) <= (px1 - px0) / 3
    ]
    floor = max(axis.position(axis.low), axis.position(axis.high))

    def bottom_of(b: dict) -> float:
        """A bar drawn past the chart's floor (cut to the chart's area) ends at the floor."""
        return min(float(b["bottom"]), pbottom) if float(b["bottom"]) > pbottom + 1 else float(b["bottom"])

    if axis.low <= 0 <= axis.high:
        zero = axis.position(0.0)
    elif bars:
        # No zero on the scale: the bars stand on the chart's floor, below its lowest label.
        bottoms = [round(bottom_of(b)) for b in bars if bottom_of(b) >= floor - BASELINE_PT]
        if not bottoms:
            return []
        zero = float(max(set(bottoms), key=bottoms.count))
    else:
        return []
    out = []
    for b in bars:
        top, bottom = float(b["top"]), bottom_of(b)
        if bottom - top < 0.3:
            continue
        if abs(bottom - zero) <= BASELINE_PT:
            end = top
        elif abs(top - zero) <= BASELINE_PT:
            end = bottom
        else:
            continue
        out.append((_fill_key(b), (float(b["x0"]) + float(b["x1"])) / 2, axis.value(end)))
    counts: dict[str, int] = {}
    for color, _x, _v in out:
        counts[color] = counts.get(color, 0) + 1
    return [bar for bar in out if counts[bar[0]] >= BARS_MIN]


def _line_values(path: dict, axis: _Axis, columns: list[tuple[str, float]]) -> dict[str, float]:
    """A line's value at each column, read off its drawn points."""
    pts = sorted(path["pts"])
    values: dict[str, float] = {}
    for label, x in columns:
        if not pts[0][0] - 0.5 <= x <= pts[-1][0] + 0.5:
            continue
        for (xa, ya), (xb, yb) in zip(pts, pts[1:], strict=False):
            if xa - 0.5 <= x <= xb + 0.5:
                y = ya if xb == xa else ya + (yb - ya) * (x - xa) / (xb - xa)
                values[label] = axis.value(y)
                break
    return values


def _one_chart(
    items: list[_Seg], rotated: list[_Seg], boxes: list[dict], paths: list[dict],
    axis: _Axis, plot: tuple[float, float, float, float],
) -> tuple[list[str], list[list[str]], str, str] | None:
    """(header, rows, what checked it, title) for one chart, or None."""
    px0, ptop, px1, pbottom = plot
    bars = _bar_values(boxes, axis, plot)
    lines = [
        p for p in paths
        if len(p["pts"]) >= SERIES_MIN_POINTS
        and max(y for _, y in p["pts"]) - min(y for _, y in p["pts"]) > FLAT_PT
        and all(px0 - 2 <= x <= px1 + 2 and ptop - 2 <= y <= pbottom + 2 for x, y in p["pts"])
    ]
    if not bars and not lines:
        return None
    # Along the chart: a numeric axis under it, else the category labels under the bars.
    floor = max(axis.position(axis.low), axis.position(axis.high))  # the lowest tick, not the widened edge
    below = [s for s in items if s.top >= floor - 2 and px0 - 10 <= s.cx <= px1 + 10]
    x_axes = _axes(below, vertical=False)
    x_axis = min(x_axes, key=lambda a: a.at) if x_axes else None
    if x_axis is not None:
        columns = [(_fmt_tick(v, x_axis), p) for p, v in sorted(x_axis.ticks)]
    else:
        labels = [s for s in rotated if s.top >= floor - 2 and px0 - 10 <= s.cx <= px1 + 10]
        labels += [s for s in below if _value(s.text) is None]
        columns = [(s.text, s.cx) for s in sorted(labels, key=lambda s: s.cx)]
    if len(columns) < 2:
        return None
    spacing = min(b - a for (_, a), (_, b) in zip(columns, columns[1:], strict=False)) or 1.0
    series: dict[tuple[str, str, int], dict[str, float]] = {}
    for color, cx, value in bars:
        label, x = min(columns, key=lambda c: abs(c[1] - cx))
        if abs(x - cx) <= spacing / 2:
            series.setdefault(("bars", color, 0), {})[label] = value
    for index, path in enumerate(lines):
        values = _line_values(path, axis, columns)
        if len(values) >= 2:
            series[("line", _line_key(path), index)] = values
    series = {k: v for k, v in series.items() if len(v) >= 2}
    if not series:
        return None
    checked = _stated_check([s.text for s in items], [sum(v.values()) / len(v) for v in series.values()], axis)
    if checked is None:
        return None
    legends = [_legend(color, boxes, paths, items) for _kind, color, _ in series]
    if len(series) == 1:
        # One series: its legend only counts outside the chart (inside, it is a callout).
        text, seg = legends[0]
        names = [text if seg is not None and not _in(plot, seg.cx, seg.cy) else "Value"]
    else:
        names = [text or f"series {i + 1}" for i, (text, _seg) in enumerate(legends)]
    legend_ids = {id(seg) for _text, seg in legends if seg is not None}
    x_title = ""
    if x_axis is not None:
        under = [s for s in items if s.top > x_axis.at + 2 and px0 <= s.cx <= px1 and _value(s.text) is None]
        if under:
            x_title = min(under, key=lambda s: s.top).text
    header = [x_title or ("X" if x_axis is not None else "Category"), *names]
    rows = [[label, *(_fmt(v[label], axis) if label in v else "" for v in series.values())] for label, _ in columns]
    rows = [r for r in rows if any(r[1:])]
    # The title: the nearest text above the chart that is not a legend label.
    above = [
        s for s in items
        if s.bottom <= ptop + 2 and px0 - 30 <= s.cx <= px1 and _value(s.text) is None and id(s) not in legend_ids
    ]
    title = max(above, key=lambda s: s.bottom).text.rstrip(" .:;,") if above else ""
    return header, rows, "axis-labels" + (f",{checked}" if checked else ""), _safe(title)


# ─── Entry point ─────────────────────────────────────────────────────────────
JUNK_MAX_CHARS = 2


def _placeholder_groups(lines: list[str]) -> list[list[int]]:
    """Runs of placeholders (blank lines between them allowed)."""
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
    return groups


def _junk_lines(lines: list[str], pages: list[dict]) -> set[int]:
    """Lines of a character or two that the PDF's text never has ("口": a chart's legend marker)."""
    known = set("".join(s["text"] for p in pages for s in p.get("segments") or []))
    return {
        i for i, line in enumerate(lines)
        if line.strip() and len(line.strip()) <= JUNK_MAX_CHARS and not any(ch in known for ch in line.strip())
    }


GROW_REACH_PT = 6.0            # shapes this close to a figure's area are part of it
GROW_MAX_ROUNDS = 12
LABEL_REACH_PT = 25.0          # a figure's labels sit this far outside its drawing (tick labels, legends)
PROSE_MIN_TOKENS = 8           # a line this long is prose, not a figure's label
CLUSTER_PT = 30.0              # a label the engine wrote counts when one it left out is this close
LABEL_GAP_PT = 40.0            # a category label this close to its bar, on its row, belongs to the chart
RULE_PT = 2.5                  # a shape this thin is a rule...
LONG_RULE_SHARE = 0.4          # ...and one this long a share of the page is part of the layout, not a figure


def _drawing(page: dict) -> list[tuple[float, float, float, float]]:
    """Bounds of everything drawn on the page: boxes, paths and images."""
    out = [(float(b["x0"]), float(b["top"]), float(b["x1"]), float(b["bottom"])) for b in page.get("boxes") or []]
    out += [(float(i["x0"]), float(i["top"]), float(i["x1"]), float(i["bottom"])) for i in page.get("images") or []]
    for path in page.get("paths") or []:
        xs = [x for x, _ in path["pts"]]
        ys = [y for _, y in path["pts"]]
        out.append((min(xs), min(ys), max(xs), max(ys)))
    width, height = float(page.get("width") or 612), float(page.get("height") or 792)
    kept = []
    for x0, top, x1, bottom in out:
        if x0 < -1 or top < -1 or x1 > width + 1 or bottom > height + 1:
            continue  # drawn past the page and cut to a chart's area: where it ends is not known
        w, h = x1 - x0, bottom - top
        if w <= 0 and h <= 0:
            continue
        # A page-wide frame, or a long rule (under a heading, between columns), would join
        # everything on the page into one figure.
        if (w >= 0.9 * width and h >= 0.9 * height) or (min(w, h) <= RULE_PT and max(w / width, h / height) >= LONG_RULE_SHARE):
            continue
        kept.append((x0, top, x1, bottom))
    return kept


def _near(shape: tuple[float, float, float, float], box: tuple[float, float, float, float], reach: float) -> bool:
    a, b, c, d = shape
    return a <= box[2] + reach and c >= box[0] - reach and b <= box[3] + reach and d >= box[1] - reach


def _grown(seed: list[_Seg], page: dict) -> tuple[float, float, float, float]:
    """The drawing the figure's text touches (a chart's axes and bars, a diagram's boxes), with room
    for its labels; the text's own bounds when it touches nothing drawn. The drawing grows shape to
    shape, so a paragraph wrapped around the figure stays outside."""
    shapes = _drawing(page)
    start = [sh for sh in shapes if any(_near(sh, _bounds(t), GROW_REACH_PT) for t in seed)]
    if not start:
        return (
            min(t.x0 for t in seed) - X_MARGIN, min(t.top for t in seed) - X_MARGIN,
            max(t.x1 for t in seed) + X_MARGIN, max(t.bottom for t in seed) + X_MARGIN,
        )
    drawn = (min(sh[0] for sh in start), min(sh[1] for sh in start), max(sh[2] for sh in start), max(sh[3] for sh in start))
    for _ in range(GROW_MAX_ROUNDS):
        joined = [sh for sh in shapes if _near(sh, drawn, GROW_REACH_PT)]
        grown = (
            min(drawn[0], *(sh[0] for sh in joined)), min(drawn[1], *(sh[1] for sh in joined)),
            max(drawn[2], *(sh[2] for sh in joined)), max(drawn[3], *(sh[3] for sh in joined)),
        )
        if grown == drawn:
            break
        drawn = grown
    return (
        drawn[0] - LABEL_REACH_PT, drawn[1] - LABEL_REACH_PT,
        drawn[2] + LABEL_REACH_PT, drawn[3] + LABEL_REACH_PT,
    )


def _figure_items(
    seed: list[_Seg], page: dict, segs: list[_Seg], presence: _Presence, running: dict[str, int], taken: set[int],
) -> list[_Seg]:
    """The text of the figure `seed` is part of: everything left out (and every label) in its drawing."""
    height = float(page.get("height") or 792)
    own = [s for s in seed if presence.label(s) or not presence.has(s)] or seed
    region = _grown(own, page)
    shapes = [sh for sh in _drawing(page) if _near(sh, region, 0.0)]

    def labels_a_shape(item: _Seg) -> bool:
        """A category label beside its bar or box, on the same row (not a paragraph beside a frame)."""
        h = max(1.0, item.bottom - item.top)
        return any(
            sh[3] - sh[1] <= 3 * h and sh[1] <= item.bottom and sh[3] >= item.top
            and min(abs(sh[0] - item.x1), abs(item.x0 - sh[2])) <= LABEL_GAP_PT
            for sh in shapes
        )

    pool = [s for s in segs + _rotated(page) if id(s) not in taken and _body(s, height, running)]
    seed_ids = {id(s) for s in own}
    return [
        s for s in pool
        if (presence.label(s) or not presence.has(s))
        and (_in(region, s.cx, s.cy) or (id(s) in seed_ids and labels_a_shape(s)))
    ]


def _page_region(
    page: dict, segs: list[_Seg], presence: _Presence, running: dict[str, int], taken: set[int],
) -> list[_Seg]:
    """A page's figure text the engine left out: the missing lines, and what sits among them."""
    height = float(page.get("height") or 792)
    pool = [s for s in segs + _rotated(page) if id(s) not in taken and _body(s, height, running)]
    # Labels, not prose: a missing paragraph is the dropped_paragraphs step's.
    core = [s for s in pool if 2 <= len(s.tokens) < PROSE_MIN_TOKENS and not presence.has(s)]
    if len(core) < MIN_SEGMENTS:
        return []
    items = _figure_items(core, page, segs, presence, running, taken)
    missing = [s for s in items if not presence.has(s)]

    def among_missing(seg: _Seg) -> bool:
        return any(
            m is not seg and abs(m.cy - seg.cy) <= CLUSTER_PT and (m.x0 - CLUSTER_PT <= seg.cx <= m.x1 + CLUSTER_PT)
            or (abs(m.cx - seg.cx) <= CLUSTER_PT and abs(m.cy - seg.cy) <= 2 * CLUSTER_PT)
            for m in missing
        )

    # Numbers are a chart's own labels (ticks, values), wherever else they appear.
    return [s for s in items if not presence.has(s) or _value(s.text) is not None or among_missing(s)]


_CAPTION = re.compile(r"^(?:Figure|Fig\.?|Exhibit|Chart|Diagram)\s*\d+(?:\.\d+)*[A-Za-z]?\b", re.I)
CAPTION_SLACK_PT = 40.0        # a label may start this far left of its caption


def _by_caption(items: list[_Seg]) -> list[tuple[str, list[_Seg]]] | None:
    """Labels grouped under the caption above them ("Figure 1.2 Cross-section of lamellar bone"),
    when the area holds two or more captioned figures; None otherwise."""
    marks = [s for s in items if _CAPTION.match(s.text)]
    if len(marks) < 2:
        return None
    captions: list[tuple[_Seg, str, set[int]]] = []
    for mark in marks:
        # The caption's title: the piece right after "Figure 1.2" on its row.
        title = [
            o for o in items
            if o is not mark and abs(o.cy - mark.cy) <= ROW_TOLERANCE and 0 <= o.x0 - mark.x1 <= 15 and not _CAPTION.match(o.text)
        ]
        title = sorted(title, key=lambda o: o.x0)[:1]
        text = " ".join([mark.text, *(o.text for o in title)])
        captions.append((mark, text, {id(mark), *(id(o) for o in title)}))
    used = set().union(*(ids for _, _, ids in captions))
    groups: dict[int, list[_Seg]] = {i: [] for i in range(len(captions))}
    loose: list[_Seg] = []
    for seg in items:
        if id(seg) in used:
            continue
        above = [
            (i, c) for i, (c, _t, _ids) in enumerate(captions)
            if c.top <= seg.cy and c.x0 <= seg.cx + CAPTION_SLACK_PT
        ]
        if not above:
            loose.append(seg)
            continue
        nearest_top = max(c.top for _, c in above)
        same_row = [(i, c) for i, c in above if abs(c.top - nearest_top) <= ROW_TOLERANCE]
        i, _c = max(same_row, key=lambda t: t[1].x0)
        groups[i].append(seg)
    out = [(text, groups[i]) for i, (_c, text, _ids) in enumerate(captions)]
    if loose:
        out.insert(0, ("", loose))
    return out


def _figure_block(part: list[_Seg], page: dict, number: int, charts: bool, text: bool = True) -> tuple[list[str], int]:
    """The lines added after one placeholder (its text, its charts' values), and how many charts were measured."""
    block = _figure_text_block(part, page, number) if text else []
    if not charts:
        return block, 0
    region = (
        min(s.x0 for s in part) - X_MARGIN, min(s.top for s in part) - X_MARGIN,
        max(s.x1 for s in part) + X_MARGIN, max(s.bottom for s in part) + X_MARGIN,
    )
    tables = _chart_tables(part, page, region, number)
    if tables and not block:
        tables = tables[1:]  # no figure text above them: no blank line to open with
    return block + tables, sum(1 for ln in tables if ln.startswith("<!-- inkdoc:chart"))


def _figure_text_block(part: list[_Seg], page: dict, number: int) -> list[str]:
    """A figure's own text: a nested list of its boxes, its labels under each caption, or one line."""
    boxed = _boxed_lines(part, page.get("boxes") or [])
    if boxed is not None:
        return [f"<!-- inkdoc:figure-text page={number} source=pdf-text -->", "*Text in the figure:*", "", *boxed]
    captioned = _by_caption(part)
    if captioned is not None:
        block = [f"<!-- inkdoc:figure-text page={number} source=pdf-text -->", "*Text in the figures:*", ""]
        for caption, labels in captioned:
            text = " · ".join(" ".join(s.text for s in r) for r in _rows(labels))
            if caption and text:
                block.append(f"- {_entry(caption)}: {_safe(text)}")
            elif caption:
                block.append(f"- {_entry(caption)}")
            elif text:
                block.append(f"- {_entry(text)}")
        return block
    return figure_lines(part, number)


def recover_figures(
    markdown: str, pages: list[dict], *, charts: bool = False, text: bool = True,
) -> tuple[str, int, int]:
    """(Markdown with each figure's text after its placeholder, figures filled, charts measured).

    `text` and `charts` are the figure_text and chart_data steps: either can be off.
    """
    pages = [p for p in pages if p.get("segments")]
    if not pages or "<!--" not in markdown:
        return markdown, 0, 0
    pages.sort(key=lambda p: p["page"])
    by_number = {p["page"]: p for p in pages}
    segs = {p["page"]: _segments(p) for p in pages}
    lines = markdown.split("\n")
    junk = _junk_lines(lines, pages)
    if junk:
        lines = [ln for i, ln in enumerate(lines) if i not in junk]
        markdown = "\n".join(lines)

    def is_text(i: int) -> bool:
        return bool(lines[i].strip()) and not _COMMENT.match(lines[i])

    spans: dict[int, tuple[int, int]] | None = None
    presence = _Presence(markdown)
    running = _running_keys(pages)
    taken: dict[int, set[int]] = {}
    insertions: list[tuple[int, list[str]]] = []
    measured = 0
    page_cursor = 0
    text_so_far = " ".join(lines)
    for group in _placeholder_groups(lines):
        b = next((k for k in range(group[0] - 1, -1, -1) if is_text(k)), None)
        a = next((k for k in range(group[-1] + 1, len(lines)) if is_text(k)), None)
        found = None
        if b is not None and a is not None:
            found = _find_region(pages, segs, line_tokens(lines[b]), line_tokens(lines[a]), page_cursor)
        parts: list[list[_Seg]] = []
        number = 0
        if found is not None:
            number, before, after = found
            x0 = min(before.x0, after.x0) - X_MARGIN
            x1 = max(before.x1, after.x1) + X_MARGIN
            inside = [
                s for s in segs[number]
                if s is not before and s is not after and x0 <= s.cx <= x1 and before.bottom <= s.cy <= after.top
            ]
            # Text the engine did write (a caption it kept) is not repeated.
            inside = [s for s in inside if presence.label(s) or (s.text not in text_so_far and not presence.has(s))]
            if len(inside) >= MIN_SEGMENTS:
                page_cursor = number
                grown = _figure_items(
                    inside, by_number[number], segs[number], presence, running, taken.setdefault(number, set())
                )
                parts = _split_by_x(grown if len(grown) >= MIN_SEGMENTS else inside, len(group))
        if not parts:
            # No line of the engine's below the figure on its page: place it by its page.
            if spans is None:
                spans = _page_map(markdown, pages)
            before_page, after_page = page_of_line(spans, group[0])
            order = [before_page]
            # The next page only for a figure at its top: nothing the engine wrote in between.
            if after_page is not None and after_page != before_page:
                start = spans[after_page][0]
                if not any(is_text(k) for k in range(group[-1] + 1, start)):
                    order.append(after_page)
            for candidate in dict.fromkeys(p for p in order if p in by_number):
                region = _page_region(
                    by_number[candidate], segs[candidate], presence, running, taken.setdefault(candidate, set())
                )
                if len(region) >= MIN_SEGMENTS:
                    number = candidate
                    parts = _split_by_x(region, len(group))
                    break
        for index, part in zip(group, parts, strict=False):
            if len(part) >= MIN_SEGMENTS:
                taken.setdefault(number, set()).update(id(s) for s in part)
                block, n = _figure_block(part, by_number[number], number, charts, text)
                measured += n
                if block:
                    insertions.append((index, block))
    for index, block in sorted(insertions, key=lambda t: t[0], reverse=True):
        lines[index + 1:index + 1] = ["", *block]
    return "\n".join(lines), len(insertions), measured


def recover_figure_text(markdown: str, pages: list[dict]) -> tuple[str, int]:
    """(Markdown with each figure's text added after its placeholder, figures filled)."""
    result, filled, _measured = recover_figures(markdown, pages)
    return result, filled


# ─── Lines an engine wrote one per paragraph (the broken_lines step) ─────────
# A form or a narrow column often comes out of an engine one PDF line per
# paragraph ("Has the patient confirmed his/her identity," / "site, procedure,
# and consent?"). Two such paragraphs are joined only when each is exactly one
# line of the PDF, the second sits right under the first in the same column,
# and the first plainly goes on: it ends without a sentence's end, and the
# second starts in lower case or the first ends with a comma or a hyphen.
JOIN_GAP_SHARE = 0.9           # the second line starts within this share of a line's height below the first
JOIN_X_PT = 3.0                # and this close to the first line's left edge
_ENDS_SENTENCE = re.compile(r"[.!?:;]$")
_PLAIN_LINE = re.compile(r"^\s*[^\s#|<>`*+\-\d]")


def _line_index(pages: list[dict]) -> dict[str, list[tuple[int, _Seg]]]:
    """Each PDF line's letters -> where it is (page, segment)."""
    index: dict[str, list[tuple[int, _Seg]]] = {}
    for p in pages:
        for seg in _segments(p):
            key = _letters(normalize_markdown(seg.text))
            if key:
                index.setdefault(key, []).append((p["page"], seg))
    return index


def _continues(first: _Seg, second: _Seg) -> bool:
    h = max(1.0, first.bottom - first.top)
    return abs(second.x0 - first.x0) <= JOIN_X_PT and 0 <= second.top - first.bottom <= JOIN_GAP_SHARE * h


def join_broken_lines(markdown: str, pages: list[dict]) -> tuple[str, int]:
    """(Markdown with the PDF's wrapped lines joined back into their sentences, joins made)."""
    pages = [p for p in pages if p.get("segments")]
    if not pages:
        return markdown, 0
    index = _line_index(pages)
    lines = markdown.split("\n")
    joined = 0
    i = 0
    while i + 2 < len(lines):
        a, gap, b = lines[i], lines[i + 1], lines[i + 2]
        if gap.strip() or not _PLAIN_LINE.match(a) or not _PLAIN_LINE.match(b):
            i += 1
            continue
        first, second = a.strip(), b.strip()
        goes_on = not _ENDS_SENTENCE.search(first) and (
            second[:1].islower() or first.endswith((",", "-", "–", "/", "(", "&"))
        )
        places_a = index.get(_letters(normalize_markdown(first)), [])
        places_b = index.get(_letters(normalize_markdown(second)), [])
        pair = goes_on and len(places_a) == 1 and any(
            pa == pb and _continues(sa, sb) for pa, sa in places_a for pb, sb in places_b
        )
        if not pair:
            i += 1
            continue
        page_a, seg_a = places_a[0]
        seg_b = next(sb for pb, sb in places_b if pb == page_a and _continues(seg_a, sb))
        glue = "" if first.endswith("-") and second[:1].islower() else " "
        lines[i] = first + glue + second
        del lines[i + 1:i + 3]
        joined += 1
        # Stay on this line: the PDF's next line may continue it too, from the line just joined.
        index[_letters(normalize_markdown(lines[i]))] = [(page_a, seg_b)]
    return "\n".join(lines), joined


# ─── Checkboxes (the checkboxes step) ────────────────────────────────────────
# A drawn checkbox is a small square, stroked or white over a dark square of the
# same size (a frame drawn by fill), with its text just to the right. A line
# the engine wrote is marked "☐ " only when every place the PDF has that text
# on the page is a checkbox's, so a "Yes" that is not a checkbox stops it.
CHECKBOX_MIN_PT = 4.0
CHECKBOX_MAX_PT = 12.0
CHECKBOX_TEXT_GAP_PT = 8.0
CHECKBOX = "☐"


def _squares(page: dict) -> list[tuple[float, float, float, float]]:
    """The checkboxes drawn on a page (x0, top, x1, bottom)."""
    squares = []
    boxes = page.get("boxes") or []
    for b in boxes:
        w, h = float(b["x1"]) - float(b["x0"]), float(b["bottom"]) - float(b["top"])
        if not (CHECKBOX_MIN_PT <= w <= CHECKBOX_MAX_PT and abs(w - h) <= 1.5):
            continue
        cx, cy = (float(b["x0"]) + float(b["x1"])) / 2, (float(b["top"]) + float(b["bottom"])) / 2
        empty = b.get("fill") in ("", "white")
        framed = b.get("stroke") or any(
            o is not b and o.get("fill") not in ("", "white")
            and abs((float(o["x0"]) + float(o["x1"])) / 2 - cx) <= 1.0
            and abs((float(o["top"]) + float(o["bottom"])) / 2 - cy) <= 1.0
            and 0 <= (float(o["x1"]) - float(o["x0"])) - w <= 2.0
            for o in boxes
        )
        if empty and framed:
            squares.append((float(b["x0"]), float(b["top"]), float(b["x1"]), float(b["bottom"])))
    return squares


def mark_checkboxes(markdown: str, pages: list[dict]) -> tuple[str, int]:
    """(Markdown with the PDF's checkbox items marked "☐ ", items marked)."""
    pages = [p for p in pages if p.get("segments")]
    if not pages:
        return markdown, 0
    spans = _page_map(markdown, pages)
    lines = markdown.split("\n")
    marked = 0
    for page in pages:
        if page["page"] not in spans:
            continue
        squares = _squares(page)
        if not squares:
            continue
        segs = _segments(page)
        boxed: dict[str, int] = {}
        for seg in segs:
            h = max(3.0, seg.bottom - seg.top)
            if any(0 <= seg.x0 - x1 <= CHECKBOX_TEXT_GAP_PT and abs(seg.cy - (top + bottom) / 2) <= h for _x0, top, x1, bottom in squares):
                key = _letters(normalize_markdown(seg.text))
                boxed[key] = boxed.get(key, 0) + 1
        if not boxed:
            continue
        everywhere: dict[str, int] = {}
        for seg in segs:
            key = _letters(normalize_markdown(seg.text))
            if key in boxed:
                everywhere[key] = everywhere.get(key, 0) + 1
        sure = {k for k, n in boxed.items() if k and everywhere.get(k) == n}
        first, last = spans[page["page"]]
        for i in range(first, last + 1):
            text = lines[i].strip()
            if not text or text.startswith((CHECKBOX, "#", "|", "<!--", "```")):
                continue
            body = text[2:] if text.startswith(("- ", "* ")) else text
            if body.startswith(CHECKBOX):
                continue
            key = _letters(normalize_markdown(body))
            if any(key == k or key.startswith(k) and len(k) >= 6 for k in sure):
                lines[i] = lines[i].replace(body, f"{CHECKBOX} {body}", 1)
                marked += 1
    return "\n".join(lines), marked


# ─── Pictures and formulas GLM-OCR reads (the picture_text and formulas steps) ─
# Two places an engine leaves without text: a picture ("<!-- image -->") whose
# text is inside the image, and a formula Docling did not decode
# ("<!-- formula-not-decoded -->"). Each is matched to its area on its page
# (the picture's image in the PDF, the formula's region from the layout
# model), GLM-OCR reads that area alone, and the reading is written marked
# with where it came from. A page is matched only when it has as many areas
# as placeholders, so a reading never lands on the wrong one.
_FORMULA_PLACEHOLDER = re.compile(r"^\s*<!--\s*formula-not-decoded\s*-->\s*$", re.I)
PICTURE_MIN_SHARE = 0.01       # an image smaller than this share of the page is an icon
PICTURE_MAX_SHARE = 0.9        # one larger is the page itself (a scan)
PICTURE_MIN_WORDS = 3
REGIONS_MAX = 12               # read per conversion
FORMULA_AGREE_SHARE = 0.6      # of the PDF's own characters in a formula's area that its reading must hold


@dataclass
class Area:
    """One place GLM-OCR reads: the placeholder's line, its page and the area on it."""

    line: int
    page: int
    bbox: list[float]
    reading: str | None = None


def _page_for(spans: dict[int, tuple[int, int]], lines: list[str], i: int) -> int | None:
    """The page a placeholder at line `i` belongs to (the next page only for one at its top)."""
    before, after = page_of_line(spans, i)
    if before is not None and (after is None or after == before):
        return before
    if after is not None and not any(
        lines[k].strip() and not _COMMENT.match(lines[k]) and not _PLACEHOLDER.match(lines[k])
        for k in range(i + 1, spans[after][0])
    ):
        return after
    return before


def _filled(lines: list[str], i: int) -> bool:
    """Whether a placeholder already has a figure's text after it."""
    following = next((ln for ln in lines[i + 1:] if ln.strip()), "")
    return following.lstrip().startswith("<!-- inkdoc:figure-text")


def picture_areas(markdown: str, pages: list[dict]) -> list[Area]:
    """The pictures whose text GLM-OCR may read: placeholders with nothing after them, matched to images."""
    pages = [p for p in pages if p.get("segments")]
    if not pages or "<!-- image -->" not in markdown:
        return []
    lines = markdown.split("\n")
    spans = _page_map(markdown, pages)
    by_page: dict[int, list[int]] = {}
    for i, line in enumerate(lines):
        if _PLACEHOLDER.match(line):
            page = _page_for(spans, lines, i)
            if page is not None:
                by_page.setdefault(page, []).append(i)
    areas: list[Area] = []
    for p in pages:
        found = by_page.get(p["page"], [])
        area = float(p.get("width") or 612) * float(p.get("height") or 792)
        images = [
            im for im in p.get("images") or []
            if PICTURE_MIN_SHARE * area
            <= (float(im["x1"]) - float(im["x0"])) * (float(im["bottom"]) - float(im["top"]))
            <= PICTURE_MAX_SHARE * area
        ]
        if not found or len(images) != len(found):
            continue
        images.sort(key=lambda im: (round(float(im["top"]) / 20), float(im["x0"])))
        for i, im in zip(found, images, strict=True):
            if not _filled(lines, i):
                areas.append(Area(i, p["page"], [float(im[k]) for k in ("x0", "top", "x1", "bottom")]))
    return areas[:REGIONS_MAX]


def write_picture_text(markdown: str, areas: list[Area]) -> tuple[str, int]:
    """(Markdown with GLM-OCR's reading of each picture after its placeholder, pictures filled)."""
    lines = markdown.split("\n")
    presence = _Presence(markdown)
    inserts: list[tuple[int, list[str]]] = []
    for area in areas:
        text = " ".join((area.reading or "").split())
        words = [w for w in re.findall(r"[^\W\d_]{2,}", text)]
        if len(words) < PICTURE_MIN_WORDS:
            continue
        reading = _Seg(text, 0, 0, 0, 0, line_tokens(text))
        if presence.has(reading):
            continue  # the engine wrote it already (a picture of text the page repeats)
        inserts.append((area.line, [
            "", f"<!-- inkdoc:figure-text page={area.page} source=glm-ocr -->", f"*Text in the picture:* {_safe(text)}",
        ]))
    for line, block in sorted(inserts, reverse=True):
        lines[line + 1:line + 1] = block
    return "\n".join(lines), len(inserts)


def formula_pages(markdown: str, pages: list[dict]) -> dict[int, list[int]]:
    """{page: lines of its undecoded formulas}, for the pages the Markdown can place."""
    pages = [p for p in pages if p.get("segments")]
    if not pages or "formula-not-decoded" not in markdown:
        return {}
    lines = markdown.split("\n")
    spans = _page_map(markdown, pages)
    out: dict[int, list[int]] = {}
    for i, line in enumerate(lines):
        if _FORMULA_PLACEHOLDER.match(line):
            page = _page_for(spans, lines, i)
            if page is not None:
                out.setdefault(page, []).append(i)
    return out


def formula_areas(markdown: str, pages: list[dict], layout: dict) -> list[Area]:
    """Each undecoded formula matched to the layout model's formula on its page, in reading order."""
    by_page = formula_pages(markdown, pages)
    regions = {
        int(p["page"]): [r for r in p.get("regions") or [] if r.get("label") == "formula"]
        for p in layout.get("pages") or []
    }
    areas: list[Area] = []
    for page, found in sorted(by_page.items()):
        boxes = regions.get(page, [])
        if len(boxes) != len(found):
            continue
        areas += [Area(i, page, [float(v) for v in r["bbox"]]) for i, r in zip(found, boxes, strict=True)]
    return areas[:REGIONS_MAX]


_LATEX_WRAP = re.compile(r"^\s*(?:\$\$|\\\[|\$)\s*|\s*(?:\$\$|\\\]|\$)\s*$")
_LATEX_COMMAND = re.compile(r"\\[A-Za-z]+")


def _agrees(latex: str, page: dict, bbox: list[float]) -> bool | None:
    """Whether a formula's reading holds the PDF's own characters in its area; None when there are none."""
    x0, top, x1, bottom = bbox
    inside = "".join(s.text for s in _segments(page) if x0 - 2 <= s.cx <= x1 + 2 and top - 2 <= s.cy <= bottom + 2)
    own = [ch for ch in inside if ch.isalnum()]
    if not own:
        return None
    read = list(_LATEX_COMMAND.sub(" ", latex))
    pool: dict[str, int] = {}
    for ch in read:
        if ch.isalnum():
            pool[ch] = pool.get(ch, 0) + 1
    held = 0
    for ch in own:
        if pool.get(ch, 0):
            pool[ch] -= 1
            held += 1
    return held / len(own) >= FORMULA_AGREE_SHARE


def write_formulas(markdown: str, areas: list[Area], pages: list[dict]) -> tuple[str, int]:
    """(Markdown with each undecoded formula replaced by GLM-OCR's LaTeX, formulas written)."""
    by_number = {p["page"]: p for p in pages}
    lines = markdown.split("\n")
    written = 0
    for area in sorted(areas, key=lambda a: a.line, reverse=True):
        latex = _LATEX_WRAP.sub("", (area.reading or "").strip()).strip()
        if not latex:
            continue
        agrees = _agrees(latex, by_number[area.page], area.bbox) if area.page in by_number else None
        if agrees is False:
            continue
        checked = "text-layer" if agrees else "none"
        lines[area.line:area.line + 1] = [
            f"<!-- inkdoc:formula page={area.page} source=glm-ocr checked={checked} -->", "$$", latex, "$$",
        ]
        written += 1
    return "\n".join(lines), written
