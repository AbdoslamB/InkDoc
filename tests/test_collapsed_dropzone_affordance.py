"""The collapsed drop zone must still look like a drop zone.

Collapsing replaced the expanded panel -- 2px dashed border, large upload icon --
with a compact bar that had a solid border and no icon at all. Nothing on screen
said the bar still accepted a drop, so the affordance was lost exactly when the
control got small enough to need it most.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

INDEX_HTML = (REPO_ROOT / "app" / "ui" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "app" / "ui" / "style.css").read_text(encoding="utf-8")


def css_block(selector: str) -> str:
    """Return the declaration body of the first rule matching selector exactly."""
    pattern = re.compile(
        r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{(.*?)\}", re.DOTALL | re.MULTILINE
    )
    match = pattern.search(STYLE_CSS)
    assert match, f"no CSS rule found for {selector}"
    return match.group(1)


class TestCollapsedBarKeepsTheDropIcon(unittest.TestCase):
    def test_compact_bar_contains_a_drop_icon(self) -> None:
        self.assertIn(
            'class="compact-drop-icon"', INDEX_HTML,
            "the collapsed bar shows no drop icon, so nothing signals it is droppable",
        )

    def test_the_icon_sits_inside_the_compact_bar(self) -> None:
        bar = INDEX_HTML.split('class="dropzone-compact-bar"', 1)[1]
        bar = bar.split("<!-- Accessible live status", 1)[0]
        self.assertIn("compact-drop-icon", bar)

    def test_icon_is_scaled_down_not_full_size(self) -> None:
        """The expanded panel's icon is 40px; the bar is only 44px tall."""
        block = INDEX_HTML.split('class="compact-drop-icon"', 1)[1][:400]
        size = re.search(r'width="(\d+)"', block)
        self.assertIsNotNone(size, "the compact drop icon has no explicit width")
        self.assertLessEqual(
            int(size.group(1)), 20,
            "the icon must be scaled down to fit a 44px bar",
        )

    def test_icon_is_decorative_for_screen_readers(self) -> None:
        """The adjacent label already says 'Drop files or URL to convert'."""
        icon_tag = re.search(r'<span class="compact-drop-icon"[^>]*>', INDEX_HTML)
        self.assertIsNotNone(icon_tag, "the compact drop icon is missing")
        self.assertIn('aria-hidden="true"', icon_tag.group(0))

    def test_icon_responds_to_drag_over(self) -> None:
        self.assertIn(".dropzone-compact-bar.drag-over .compact-drop-icon", STYLE_CSS)

    def test_icon_animation_respects_reduced_motion(self) -> None:
        reduced = STYLE_CSS.split("prefers-reduced-motion: reduce", 1)
        self.assertEqual(len(reduced), 2, "no reduced-motion block found")
        self.assertIn("compact-drop-icon", reduced[1][:800])


class TestCollapsedBarKeepsTheDashedBorder(unittest.TestCase):
    def test_compact_bar_border_is_dashed(self) -> None:
        border = re.search(r"border:\s*([^;]+);", css_block(".dropzone-compact-bar"))
        self.assertIsNotNone(border, ".dropzone-compact-bar has no border declaration")
        self.assertIn(
            "dashed", border.group(1),
            "a solid border reads as a static bar; dashed says it accepts a drop",
        )

    def test_it_matches_the_expanded_panel(self) -> None:
        """Collapsing changes the size of the drop target, not what it is."""
        expanded = re.search(r"border:\s*([^;]+);", css_block(".dropzone-card"))
        compact = re.search(r"border:\s*([^;]+);", css_block(".dropzone-compact-bar"))
        self.assertIn("dashed", expanded.group(1))
        self.assertIn("dashed", compact.group(1))

    def test_bar_height_is_unchanged_by_the_thicker_border(self) -> None:
        """A 2px border must not push the 44px bar out of alignment."""
        block = css_block(".dropzone-compact-bar")
        self.assertIn("box-sizing: border-box", block)
        self.assertIn("height: 44px", block)

    def test_drag_over_still_highlights_the_border(self) -> None:
        self.assertIn("border-color: var(--mark)", css_block(".dropzone-compact-bar.drag-over"))


if __name__ == "__main__":
    unittest.main()
