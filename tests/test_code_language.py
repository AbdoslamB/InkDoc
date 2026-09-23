"""Tests for putting Docling's detected code language back into the Markdown.

The interesting cases are the ones where a naive implementation quietly gets it
wrong: two identical code blocks in different languages, a block whose language
Docling could not determine, and the copy of the mapping that has to live inside
the engine pack worker.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.engines.code_language import (  # noqa: E402
    CODE_LANGUAGE_SLUGS,
    apply_code_languages,
    slug_for,
)

WORKER_PATH = REPO_ROOT / "app" / "core" / "engines" / "worker.py"


def _worker_slug_map() -> dict[str, str]:
    """Read the worker's copy of the mapping without importing the worker.

    Importing it would run its module-level setup, which redirects file
    descriptor 1 to stderr and would take the test runner's stdout with it.
    Parsing the source is side-effect free.
    """
    tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_CODE_LANGUAGE_SLUGS":
                    return ast.literal_eval(node.value)
    raise AssertionError("_CODE_LANGUAGE_SLUGS not found in worker.py")


def test_worker_copy_matches_canonical_map():
    """The worker cannot import the app package, so it carries its own copy."""
    assert _worker_slug_map() == CODE_LANGUAGE_SLUGS
    print("[OK] test_worker_copy_matches_canonical_map passed")


def test_slug_for_handles_labels_values_and_junk():
    assert slug_for("C#") == "csharp"
    assert slug_for("C++") == "cpp"
    assert slug_for("ObjectiveC") == "objectivec"
    assert slug_for("VisualBasic") == "vbnet"
    assert slug_for("HTML") == "html"

    class _Label:
        value = "Python"

    assert slug_for(_Label()) == "python"

    # Anything without a meaningful identifier leaves the fence bare.
    assert slug_for(None) == ""
    assert slug_for("unknown") == ""
    assert slug_for("DocLang") == ""
    assert slug_for("Klingon") == ""
    assert slug_for(42) == ""
    print("[OK] test_slug_for_handles_labels_values_and_junk passed")


def test_mapping_is_lowercase_and_fence_safe():
    """A fence identifier with a space or backtick would break the block."""
    for label, slug in CODE_LANGUAGE_SLUGS.items():
        assert slug, f"{label} maps to an empty slug; omit the key instead"
        assert slug == slug.lower(), f"{label} -> {slug} is not lowercase"
        assert not any(c.isspace() or c == "`" for c in slug), f"{label} -> {slug}"
    print("[OK] test_mapping_is_lowercase_and_fence_safe passed")


# ─── apply_code_languages ────────────────────────────────────────────────────
# These use a stand-in for DoclingDocument so the mapping logic can be tested
# without docling installed. apply_code_languages type-checks against the real
# CodeItem, so the tests below are skipped when docling_core is unavailable.

try:
    from docling_core.types.doc import CodeItem, DocItemLabel

    _HAVE_DOCLING = True
except Exception:  # pragma: no cover - depends on the environment
    _HAVE_DOCLING = False


def _code_item(text: str, language: str | None):
    item = CodeItem(self_ref="#/texts/0", label=DocItemLabel.CODE, orig=text, text=text)
    if language is not None:
        item.code_language = language
    return item


class _FakeDoc:
    def __init__(self, items):
        self._items = items

    def iterate_items(self, with_groups=False):
        return [(item, 0) for item in self._items]


def test_apply_code_languages_rewrites_fence():
    if not _HAVE_DOCLING:
        print("[SKIP] test_apply_code_languages_rewrites_fence (docling_core absent)")
        return
    doc = _FakeDoc([_code_item("print('hi')", "Python")])
    md = "Intro\n\n```\nprint('hi')\n```\n"
    assert apply_code_languages(doc, md) == "Intro\n\n```python\nprint('hi')\n```\n"
    print("[OK] test_apply_code_languages_rewrites_fence passed")


def test_identical_blocks_keep_their_own_languages():
    """The failure mode a naive str.replace would hit."""
    if not _HAVE_DOCLING:
        print("[SKIP] test_identical_blocks_keep_their_own_languages (docling_core absent)")
        return
    same = "x = 1"
    doc = _FakeDoc([_code_item(same, "Python"), _code_item(same, "Ruby")])
    md = f"```\n{same}\n```\n\n```\n{same}\n```\n"
    out = apply_code_languages(doc, md)
    assert out == f"```python\n{same}\n```\n\n```ruby\n{same}\n```\n", out
    print("[OK] test_identical_blocks_keep_their_own_languages passed")


def test_unknown_language_leaves_fence_bare():
    if not _HAVE_DOCLING:
        print("[SKIP] test_unknown_language_leaves_fence_bare (docling_core absent)")
        return
    doc = _FakeDoc([_code_item("???", "unknown"), _code_item("also", None)])
    md = "```\n???\n```\n\n```\nalso\n```\n"
    assert apply_code_languages(doc, md) == md
    print("[OK] test_unknown_language_leaves_fence_bare passed")


def test_unmatched_text_is_skipped_not_raised():
    """Inline code and anything the serializer reshaped must not break export."""
    if not _HAVE_DOCLING:
        print("[SKIP] test_unmatched_text_is_skipped_not_raised (docling_core absent)")
        return
    doc = _FakeDoc([_code_item("never appears", "Python")])
    md = "Some prose with `inline` code.\n"
    assert apply_code_languages(doc, md) == md
    print("[OK] test_unmatched_text_is_skipped_not_raised passed")


def test_degrades_on_bad_input():
    assert apply_code_languages(None, "text") == "text"
    assert apply_code_languages(object(), "") == ""
    # A document that raises from iterate_items must not take the export with it.

    class _Hostile:
        def iterate_items(self, with_groups=False):
            raise RuntimeError("boom")

    assert apply_code_languages(_Hostile(), "text") == "text"
    print("[OK] test_degrades_on_bad_input passed")



def _worker_apply():
    """Exec just the worker's language helpers, without its module-level setup.

    The pack ships worker.py as a standalone file and verifies its hash before
    every launch, so the copy running on a user's machine can never be patched
    in place -- it can only be replaced by a new signed pack. That makes this
    the only place the shipped implementation gets exercised, so it is worth
    running the real function rather than only diffing the constant.
    """
    tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
    wanted = {"_CODE_LANGUAGE_SLUGS", "_code_language_slug", "_apply_code_languages"}
    kept = [
        node
        for node in tree.body
        if (isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) in wanted for t in node.targets))
        or (isinstance(node, ast.FunctionDef) and node.name in wanted)
    ]
    assert len(kept) == 3, f"expected 3 definitions, found {len(kept)}"
    namespace: dict = {}
    exec(compile(ast.Module(body=kept, type_ignores=[]), "<worker>", "exec"), namespace)
    return namespace["_apply_code_languages"]


def test_worker_implementation_behaves_like_canonical():
    worker_apply = _worker_apply()
    assert worker_apply(None, "text") == "text"
    if not _HAVE_DOCLING:
        print("[SKIP] test_worker_implementation_behaves_like_canonical (docling_core absent)")
        return
    nl = chr(10)
    fence = "```"
    same = "x = 1"
    doc = _FakeDoc([_code_item(same, "Python"), _code_item(same, "Ruby")])
    block = fence + nl + same + nl + fence
    md = block + nl + nl + block + nl
    assert worker_apply(doc, md) == apply_code_languages(doc, md)
    doc2 = _FakeDoc([_code_item("print(1)", "C#")])
    md2 = fence + nl + "print(1)" + nl + fence + nl
    expected = fence + "csharp" + nl + "print(1)" + nl + fence + nl
    assert worker_apply(doc2, md2) == expected
    print("[OK] test_worker_implementation_behaves_like_canonical passed")



# ─── Enrichment settings plumbing ────────────────────────────────────────────


def test_enrichment_defaults_are_off():
    """Either flag makes Docling load a 640 MB model for every conversion."""
    from app.core.converter import ConversionOptions
    from app.core.engine_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["docling_code_enrichment"] is False
    assert DEFAULT_SETTINGS["docling_formula_enrichment"] is False
    opts = ConversionOptions()
    assert opts.docling_code_enrichment is False
    assert opts.docling_formula_enrichment is False
    print("[OK] test_enrichment_defaults_are_off passed")


def test_enrichment_options_only_read_for_docling():
    from app.core.queue_model import EngineKind
    from app.server.server import _docling_enrichment_options

    assert _docling_enrichment_options(EngineKind.MARKITDOWN) == {}
    assert _docling_enrichment_options(EngineKind.MARKIT) == {}
    docling = _docling_enrichment_options(EngineKind.DOCLING)
    assert set(docling) == {"docling_code_enrichment", "docling_formula_enrichment"}
    print("[OK] test_enrichment_options_only_read_for_docling passed")


def test_converter_cache_key_includes_enrichment():
    """Turning the flags off must build a converter without the model stage.

    If the cache key ignored them, a converter built with enrichment on would
    be reused after the user switched it off and keep the weights resident.
    """
    import ast

    for path, name in (
        (REPO_ROOT / "app" / "core" / "engines" / "docling_engine.py", "current_options"),
        (WORKER_PATH, "curr_opts"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets
            ) and isinstance(node.value, ast.Tuple):
                found = [getattr(e, "id", None) for e in node.value.elts]
        assert found is not None, f"{name} tuple not found in {path.name}"
        assert found == [
            "ocr",
            "table_structure",
            "code_enrichment",
            "formula_enrichment",
        ], f"{path.name}: {found}"
    print("[OK] test_converter_cache_key_includes_enrichment passed")


def test_worker_sets_pipeline_flags():
    src = WORKER_PATH.read_text(encoding="utf-8")
    assert "pipeline_options.do_code_enrichment = code_enrichment" in src
    assert "pipeline_options.do_formula_enrichment = formula_enrichment" in src
    # Unknown keys must default off so an older payload cannot switch them on.
    assert 'payload.get("code_enrichment", False)' in src
    assert 'payload.get("formula_enrichment", False)' in src
    print("[OK] test_worker_sets_pipeline_flags passed")

if __name__ == "__main__":
    test_worker_copy_matches_canonical_map()
    test_slug_for_handles_labels_values_and_junk()
    test_mapping_is_lowercase_and_fence_safe()
    test_apply_code_languages_rewrites_fence()
    test_identical_blocks_keep_their_own_languages()
    test_unknown_language_leaves_fence_bare()
    test_unmatched_text_is_skipped_not_raised()
    test_degrades_on_bad_input()
    test_worker_implementation_behaves_like_canonical()
    test_enrichment_defaults_are_off()
    test_enrichment_options_only_read_for_docling()
    test_converter_cache_key_includes_enrichment()
    test_worker_sets_pipeline_flags()
    print("\nALL CODE LANGUAGE TESTS PASSED!")
