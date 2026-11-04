"""Undo an engine's simplifications of the PDF's own characters and fonts (Auto, Thorough).

Docling writes "&" as "&amp;", "<" and ">" as "&lt;" and "&gt;", em dashes
as hyphens ("Strategy-The" for "Strategy—The") and curly quotes as straight
single quotes ("'Handprint'" for "“Handprint”"). The PDF's text layer has the
real characters, so each such word is put back exactly as the PDF has it, but
only when the PDF has exactly one spelling of that word: nothing is ever
guessed.

Engines also drop what the PDF's fonts say: bold and italic words, and the
small raised text of footnote numbers and exponents ("Harness²", "5%⁹").
restore_formatting puts them back from the font of each word (the table
finder's `styles`, app/core/table_finder.page_layout): a phrase is marked only
when the PDF sets it that way every time it appears, and only where the
engine wrote it as plain text, outside headings, tables, code and comments.

Pure functions; app/core/auto_engine.py calls them on Docling's output.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

# Characters an engine may have simplified, and what they were simplified to.
_FOLD = str.maketrans({
    "—": "-", "–": "-", "‒": "-", "−": "-", "‐": "-", "‑": "-",
    "“": "'", "”": "'", "„": "'", "‟": "'", '"': "'",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
})
_SIMPLIFIED = re.compile(r"[-'\"]")
_ENTITY_AMP = re.compile(r"&amp;(?!#?\w+;)")
_FENCE = re.compile(r"^\s*(```|~~~)")


def fold(word: str) -> str:
    """A word with its dashes and quotes reduced to "-" and "'"."""
    return word.translate(_FOLD)


def unescape_ampersands(markdown: str) -> tuple[str, int]:
    """'&amp;' back to '&' outside code blocks (not '&amp;lt;', which stays literal)."""
    out: list[str] = []
    count = 0
    in_code = False
    for line in markdown.split("\n"):
        if _FENCE.match(line):
            in_code = not in_code
        elif not in_code and "&amp;" in line:
            line, n = _ENTITY_AMP.subn("&", line)
            count += n
        out.append(line)
    return "\n".join(out), count


# "&lt;" is put back only where "<" cannot open a tag (before a digit, a
# space, "=" or the end); "&gt;" never where it would start a quote (the
# start of a line or of a list item).
_ENTITY = re.compile(r"&(lt|gt);")
_INLINE_CODE = re.compile(r"`+[^`]*`+")
_QUOTE_START = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])\s+)?$")


def unescape_angle_brackets(markdown: str) -> tuple[str, int]:
    """'&lt;' and '&gt;' back to '<' and '>' outside code, where they can't become a tag or a quote."""
    out: list[str] = []
    count = 0
    in_code = False
    for line in markdown.split("\n"):
        if _FENCE.match(line):
            in_code = not in_code
        elif not in_code and "&" in line and not line.lstrip().startswith("<!--"):
            code = [m.span() for m in _INLINE_CODE.finditer(line)]

            def swap(match: re.Match[str], line: str = line, code: list = code) -> str:
                nonlocal count
                at = match.start()
                if any(a <= at < b for a, b in code):
                    return match.group(0)
                if match.group(1) == "lt":
                    following = line[match.end():match.end() + 1]
                    if following and not (following.isdigit() or following.isspace() or following == "="):
                        return match.group(0)
                    count += 1
                    return "<"
                if _QUOTE_START.match(line[:at]):
                    return match.group(0)
                count += 1
                return ">"

            line = _ENTITY.sub(swap, line)
        out.append(line)
    return "\n".join(out), count


_ALNUM = re.compile(r"[^\W_]")


def spelling_map(page_texts: list[str]) -> dict[str, str]:
    """{folded word: the PDF's one spelling of it}, for words with a dash or quote.

    Only words with a letter or digit (a lone "—" would turn Markdown list
    markers into dashes), and never a minus sign: "−1.6" stays "-1.6", which
    is what anything reading the numbers expects.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    for text in page_texts:
        for word in text.split():
            folded = fold(word)
            # Every spelling of a word that has a dash or quote counts, plain
            # ones too: a PDF with both "it’s" and "it's" decides nothing.
            if _SIMPLIFIED.search(folded) and _ALNUM.search(word) and "−" not in word:
                seen[folded].add(word)
    return {
        key: next(iter(spellings))
        for key, spellings in seen.items()
        if len(spellings) == 1 and next(iter(spellings)) != key
    }


_WORD = re.compile(r"\S+")


def restore_typography(markdown: str, page_texts: list[str]) -> tuple[str, int]:
    """Put back the PDF's dashes and quotes in words an engine simplified. (text, words changed)."""
    spellings = spelling_map(page_texts)
    if not spellings:
        return markdown, 0
    count = 0
    out: list[str] = []
    in_code = False

    def swap(match: re.Match[str]) -> str:
        nonlocal count
        word = match.group(0)
        if not _SIMPLIFIED.search(word):
            return word
        original = spellings.get(fold(word))
        if original is None or original == word:
            return word
        count += 1
        return original

    for line in markdown.split("\n"):
        if _FENCE.match(line):
            in_code = not in_code
        elif not in_code and not line.lstrip().startswith("<!--"):
            line = _WORD.sub(swap, line)
        out.append(line)
    return "\n".join(out), count


# ─── Bold, italics and raised text (the formatting step) ─────────────────────
STYLE_MIN_CHARS = 4            # shorter bold or italic runs ("a", "2.") are not worth a mark
SUP_MAX_CHARS = 4              # a raised run longer than this is not a footnote mark or exponent
_SUP_TEXT = re.compile(r"^[\w†‡§*¶,′″+\-−]+$")
_MARKS = {"b": "**", "i": "*", "bi": "***"}
_SKIP_LINE = re.compile(r"^\s*(#|\||<!--|```|~~~|>)")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _loose_pattern(text: str) -> str:
    """A regex for `text` that allows any spacing between its characters (text layers lose spaces)."""
    return r"\s*".join(re.escape(ch) for ch in _squash(text))


def _editable(lines: list[str]) -> list[bool]:
    """Lines the formatting step may change: plain paragraphs and list items, not code."""
    ok: list[bool] = []
    in_code = False
    for line in lines:
        if _FENCE.match(line):
            in_code = not in_code
            ok.append(False)
            continue
        ok.append(not in_code and bool(line.strip()) and not _SKIP_LINE.match(line))
    return ok


def _layer_counts(layer: str, phrase: str) -> int:
    """How often the PDF's text has `phrase`, spacing aside. `layer`: the pages' text, squashed."""
    key = _squash(phrase)
    return layer.count(key) if key else 0


def _superscripts(styles: list[list[dict]]) -> Counter[tuple[str, str]]:
    """(word before, raised text) for every raised run that follows a word on its line."""
    found: Counter[tuple[str, str]] = Counter()
    for runs in styles:
        for before, run in zip(runs, runs[1:], strict=False):
            text = run["t"].strip()
            if run["s"] != "sup" or before["s"] == "sup" or not text:
                continue
            if len(text) > SUP_MAX_CHARS or not _SUP_TEXT.match(text):
                continue
            words = before["t"].split()
            if words:
                found[(words[-1], text)] += 1
    return found


def _leading_superscripts(styles: list[list[dict]]) -> Counter[tuple[str, str]]:
    """(raised text, words after it) for footnotes that start with their raised number."""
    found: Counter[tuple[str, str]] = Counter()
    for runs in styles:
        if len(runs) >= 2 and runs[0]["s"] == "sup" and runs[1]["s"] != "sup":
            mark = runs[0]["t"].strip()
            after = " ".join(runs[1]["t"].split()[:3])
            if mark and len(mark) <= SUP_MAX_CHARS and _SUP_TEXT.match(mark) and len(after) >= 6:
                found[(mark, after)] += 1
    return found


def _styled_phrases(styles: list[list[dict]]) -> Counter[tuple[str, str]]:
    """(text, "b"|"i"|"bi") for each bold or italic run."""
    found: Counter[tuple[str, str]] = Counter()
    for runs in styles:
        for run in runs:
            text = run["t"].strip()
            if run["s"] in _MARKS and len(_squash(text)) >= STYLE_MIN_CHARS:
                found[(text, run["s"])] += 1
    return found


def _boundary_ok(line: str, start: int, end: int) -> bool:
    """Emphasis marks at start..end would open and close (CommonMark flanking), and not inside a word."""
    before = line[start - 1] if start > 0 else " "
    after = line[end] if end < len(line) else " "
    if before.isalnum() or after.isalnum() or before == "*" or after == "*":
        return False
    return not (line[start].isspace() or line[end - 1].isspace())


def restore_formatting(markdown: str, pages: list[dict]) -> tuple[str, int]:
    """Mark the PDF's bold, italic and raised text where the engine wrote it plain. (text, marks added)."""
    styles = [runs for p in pages for runs in p.get("styles") or []]
    if not styles:
        return markdown, 0
    # The pages' text and each line with all spacing removed: a phrase can only be in a line
    # whose squashed text holds its squashed characters, a plain substring test (fast), so
    # the patterns run on those lines only.
    layer = "\x00".join(_squash(" ".join(seg["text"] for seg in p.get("segments") or [])) for p in pages)
    lines = markdown.split("\n")
    editable = _editable(lines)
    squashed = [_squash(line) for line in lines]
    count = 0

    def candidates(key: str) -> list[int]:
        return [i for i, line in enumerate(squashed) if editable[i] and key in line]

    def each_line(pattern: re.Pattern[str], make: Any, key: str) -> int:
        nonlocal count
        done = 0
        for i in candidates(key):
            new, n = pattern.subn(make, lines[i])
            if n:
                lines[i] = new
                squashed[i] = _squash(new)
                done += n
                count += n
        return done

    # Raised text after a word: "Harness 2" -> "Harness<sup>2</sup>", only when
    # the PDF raises it every time that word is followed by it. A text layer
    # that lost its spaces glues words together ("WeanalyzeHarness"), so a raised
    # run counts for every word it ends with; the engine's own text is kept.
    raised_after = _superscripts(styles)
    by_mark: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (w, s), n in raised_after.items():
        by_mark[s].append((_squash(w), n))
    for word, mark in sorted(raised_after, key=lambda k: len(k[0])):
        key = _squash(word)
        raised = sum(n for w, n in by_mark[mark] if w.endswith(key))
        if _layer_counts(layer, word + mark) != raised:
            continue
        pattern = re.compile(rf"(?<![\w<]){_loose_pattern(word)}\s?{re.escape(mark)}(?![\w<])")
        each_line(pattern, lambda m, s=mark: f"{m.group(0)[:-len(s)].rstrip()}<sup>{s}</sup>", key + _squash(mark))
    # A footnote that starts with its raised number: "9 Bloomberg." -> "<sup>9</sup> Bloomberg."
    for (mark, after), raised in _leading_superscripts(styles).items():
        if raised != 1:
            continue
        pattern = re.compile(rf"^(\s*){re.escape(mark)}\s?(?={_loose_pattern(after)})")
        each_line(pattern, lambda m, s=mark: f"{m.group(1)}<sup>{s}</sup> ", _squash(mark + after))
    # Bold and italic phrases, each found once in the result and set that way every time in the PDF.
    for (phrase, style), styled in _styled_phrases(styles).items():
        if _layer_counts(layer, phrase) != styled:
            continue
        pattern = re.compile(_loose_pattern(phrase))
        hits = [(i, m) for i in candidates(_squash(phrase)) for m in pattern.finditer(lines[i])]
        if len(hits) != 1:
            continue
        i, m = hits[0]
        line = lines[i]
        if not _boundary_ok(line, m.start(), m.end()):
            continue
        mark = _MARKS[style]
        lines[i] = f"{line[:m.start()]}{mark}{m.group(0)}{mark}{line[m.end():]}"
        squashed[i] = _squash(lines[i])
        count += 1
    return "\n".join(lines), count
