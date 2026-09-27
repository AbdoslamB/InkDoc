"""Put Docling's detected code language back into the exported Markdown.

Docling's code enrichment model identifies the programming language of every
code block it transcribes and records it on the `CodeItem`, but the Markdown
serializer drops it: `MarkdownTextSerializer` emits a bare ```` ``` ```` fence and
never reads `item.code_language`. Only the doclang and doctags serializers keep
it. So the language is detected and then thrown away before anything downstream
-- this app, GitHub, any other reader -- can see it.

This module puts it back, rewriting the opening fence of each code block to
carry a language identifier.

Why rewrite the exported string rather than subclass the serializer
-------------------------------------------------------------------
Injecting a custom text serializer means constructing `MarkdownDocSerializer`
ourselves, which means reproducing the twenty-odd `MarkdownParams` arguments
that `export_to_markdown()` fills in. Those are a moving target, and this code
has to run against two different docling_core installs at once: the app's own
environment and the engine pack's bundled interpreter, which are not pinned to
the same version (measured: 2.97.0 and 2.97.2). Reproducing those defaults would
silently drift apart from whatever `export_to_markdown()` does next release.

Rewriting the output instead keeps `export_to_markdown()` as the single source
of truth for every other formatting decision. The document itself supplies the
languages, so nothing here guesses: the mapping is driven by `CodeItem`s walked
in document order, matched against a moving cursor so that repeated identical
blocks still line up with their own languages.

Choosing the identifier
-----------------------
The fence carries the *accurate* name for the language, not whichever name a
particular highlighter happens to use. A reader that does not recognise
``cython`` or ``cuda`` renders a plain block, which is the correct outcome and
costs nothing; a fence mislabelled ``python`` to suit one highlighter would be
wrong in every other tool and in the saved file. The front end keeps a small
alias table for the handful of languages where a close relative's grammar gives
a good result.
"""
from __future__ import annotations

from typing import Any

# CodeLanguageLabel value -> Markdown fence identifier.
#
# Keys are the enum *values* as docling_core spells them, which are display
# names rather than slugs ("C#", "ObjectiveC", "FORTRAN"), so this cannot be
# derived by lowercasing. Labels deliberately absent produce a bare fence:
# `unknown` because there is nothing to say, and `DocLang` because it is
# Docling's own internal markup and no reader has a grammar for it.
CODE_LANGUAGE_SLUGS: dict[str, str] = {
    "Ada": "ada",
    "Awk": "awk",
    "Bash": "bash",
    "bc": "bc",
    "C": "c",
    "C#": "csharp",
    "C++": "cpp",
    "CMake": "cmake",
    "COBOL": "cobol",
    "CSS": "css",
    "Ceylon": "ceylon",
    "Clojure": "clojure",
    "Crystal": "crystal",
    "Cuda": "cuda",
    "Cython": "cython",
    "D": "d",
    "Dart": "dart",
    "dc": "dc",
    "Dockerfile": "dockerfile",
    "Elixir": "elixir",
    "Erlang": "erlang",
    "FORTRAN": "fortran",
    "Forth": "forth",
    "Go": "go",
    "HTML": "html",
    "Haskell": "haskell",
    "Haxe": "haxe",
    "Java": "java",
    "JavaScript": "javascript",
    "JSON": "json",
    "Julia": "julia",
    "Kotlin": "kotlin",
    "Latex": "latex",
    "Lisp": "lisp",
    "Lua": "lua",
    "Matlab": "matlab",
    "MoonScript": "moonscript",
    "Nim": "nim",
    "OCaml": "ocaml",
    "ObjectiveC": "objectivec",
    "Octave": "octave",
    "PHP": "php",
    "Pascal": "pascal",
    "Perl": "perl",
    "Prolog": "prolog",
    "Python": "python",
    "Racket": "racket",
    "Ruby": "ruby",
    "Rust": "rust",
    "SML": "sml",
    "SQL": "sql",
    "Scala": "scala",
    "Scheme": "scheme",
    "Swift": "swift",
    "Tikz": "tikz",
    "TypeScript": "typescript",
    "VisualBasic": "vbnet",
    "XML": "xml",
    "YAML": "yaml",
}


def slug_for(label: Any) -> str:
    """Map a CodeLanguageLabel (or its value) onto a fence identifier.

    Returns "" when the language is unknown or has no meaningful identifier,
    which leaves the fence bare.
    """
    if label is None:
        return ""
    value = getattr(label, "value", label)
    if not isinstance(value, str):
        return ""
    return CODE_LANGUAGE_SLUGS.get(value, "")


def apply_code_languages(doc: Any, markdown: str) -> str:
    """Rewrite bare code fences in `markdown` to carry the detected language.

    `doc` is the DoclingDocument the Markdown was exported from. Anything
    unexpected -- a document shape this does not recognise, a block whose text
    cannot be located -- is skipped rather than raised: a missing language is a
    cosmetic loss, and it must never cost the conversion itself.
    """
    if not markdown or doc is None:
        return markdown

    try:
        from docling_core.types.doc import CodeItem
    except Exception:
        return markdown

    try:
        items = [item for item, _level in doc.iterate_items(with_groups=False)]
    except Exception:
        try:
            items = list(getattr(doc, "texts", []) or [])
        except Exception:
            return markdown

    cursor = 0
    for item in items:
        if not isinstance(item, CodeItem):
            continue
        slug = slug_for(getattr(item, "code_language", None))
        text = getattr(item, "text", None)
        if not slug or not text:
            continue

        # The serializer writes a code block verbatim between bare fences, with
        # HTML escaping and underscore escaping both switched off, so the block
        # appears in the output exactly as it does on the item.
        block = "```\n" + text + "\n```"
        found = markdown.find(block, cursor)
        if found == -1:
            # An inline code item (single backticks) or text the serializer
            # altered. Leave it alone and keep scanning from the same place.
            continue

        markdown = markdown[:found] + "```" + slug + markdown[found + 3:]
        # Advance past this block, allowing for the identifier just inserted.
        cursor = found + len(block) + len(slug)

    return markdown
