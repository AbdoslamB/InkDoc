"""Undo an engine's simplifications of the PDF's own characters (Auto, Thorough).

Docling writes "&" as "&amp;", em dashes as hyphens ("Strategy-The" for
"Strategy—The") and curly quotes as straight single quotes ("'Handprint'" for
"“Handprint”"). The PDF's text layer has the real characters, so each such
word is put back exactly as the PDF has it, but only when the PDF has
exactly one spelling of that word: nothing is ever guessed.

Pure functions; app/core/auto_engine.py calls them on Docling's output.
"""
from __future__ import annotations

import re
from collections import defaultdict

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
