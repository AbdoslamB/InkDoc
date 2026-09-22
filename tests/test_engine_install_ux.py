"""UI guarantees for the engine settings panel.

Two problems this pins:

1. Installing Docling spends minutes re-hashing ~31,000 files with the progress bar
   pinned at 100%. Measured locally: 30s downloading, 321s verifying. With nothing
   on screen explaining that, the install reads as frozen.

2. Removing Docling used window.confirm(), which renders OS chrome titled
   "localhost:13118". That looks like a browser security prompt rather than part of
   the application, and it blocks the renderer thread while open.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

APP_JS = (REPO_ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "app" / "ui" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "app" / "ui" / "style.css").read_text(encoding="utf-8")


def _uncommented_lines(source: str) -> list[str]:
    return [
        line for line in source.splitlines()
        if not line.strip().startswith(("//", "*", "/*"))
    ]


class TestNoBlockingBrowserDialogs(unittest.TestCase):
    """confirm/alert/prompt must not appear anywhere in the UI."""

    def test_app_js_uses_no_native_dialogs(self) -> None:
        pattern = re.compile(r"(?<![\w.])(confirm|alert|prompt)\s*\(")
        offenders = [
            line.strip() for line in _uncommented_lines(APP_JS)
            if pattern.search(line) and "showConfirm(" not in line
        ]
        self.assertEqual(
            offenders, [],
            "window.confirm/alert/prompt render OS chrome titled with the loopback "
            "address and block the renderer; use showConfirm() instead",
        )

    def test_confirm_dialog_markup_exists_and_starts_hidden(self) -> None:
        for element_id in ("confirmBackdrop", "confirmTitle", "confirmBody",
                           "confirmAccept", "confirmCancel"):
            with self.subTest(element=element_id):
                self.assertIn(f'id="{element_id}"', INDEX_HTML)
        backdrop = re.search(r'id="confirmBackdrop"[^>]*>', INDEX_HTML)
        self.assertIsNotNone(backdrop)
        self.assertIn("hidden", backdrop.group(0), "the dialog must not show on load")

    def test_dialog_is_announced_to_assistive_technology(self) -> None:
        self.assertIn('role="alertdialog"', INDEX_HTML)
        self.assertIn('aria-modal="true"', INDEX_HTML)

    def test_dialog_defaults_focus_to_cancel(self) -> None:
        """A stray Enter must not delete the user's installed engine."""
        self.assertIn("cancelBtn.focus();", APP_JS)

    def test_escape_and_backdrop_dismiss_the_dialog(self) -> None:
        self.assertIn('e.key === "Escape"', APP_JS)
        self.assertIn("onBackdrop", APP_JS)

    def test_dialog_layers_above_the_settings_popover(self) -> None:
        """Remove Docling is triggered from inside the popover (z-index 200)."""
        block = re.search(r"\.confirm-backdrop\s*\{(.*?)\}", STYLE_CSS, re.DOTALL)
        self.assertIsNotNone(block, ".confirm-backdrop styling is missing")
        z = re.search(r"z-index:\s*(\d+)", block.group(1))
        self.assertIsNotNone(z, ".confirm-backdrop needs an explicit z-index")
        self.assertGreater(int(z.group(1)), 200)


class TestSlowInstallPhaseIsExplained(unittest.TestCase):
    def test_note_markup_exists_and_starts_hidden(self) -> None:
        for element_id in ("doclingProgressNote", "doclingProgressNoteText"):
            with self.subTest(element=element_id):
                self.assertIn(f'id="{element_id}"', INDEX_HTML)
        note = re.search(r'id="doclingProgressNote"[^>]*>', INDEX_HTML)
        self.assertIn("hidden", note.group(0), "the note must not show immediately")

    def test_note_appears_only_after_thirty_seconds(self) -> None:
        self.assertIn("SLOW_PHASE_AFTER_MS = 30000", APP_JS)

    def test_note_covers_the_phases_that_actually_take_minutes(self) -> None:
        """extracting and verifying are the slow ones; downloading shows real bytes."""
        block = re.search(r"SLOW_PHASE_NOTES\s*=\s*\{(.*?)\n  \};", APP_JS, re.DOTALL)
        self.assertIsNotNone(block, "SLOW_PHASE_NOTES is missing")
        body = block.group(1)
        self.assertIn("extracting:", body)
        self.assertIn("verifying:", body)

    def test_elapsed_is_measured_per_phase(self) -> None:
        """The API's elapsed_seconds counts from the download start, so a long
        download would trip the note the instant verification began."""
        self.assertIn("installPhaseStartedAt", APP_JS)
        self.assertIn("if (status !== installPhase)", APP_JS)

    def test_note_is_cleared_when_the_install_ends(self) -> None:
        self.assertGreaterEqual(
            APP_JS.count("resetInstallPhaseNote();"), 4,
            "the note must be cleared on idle, complete, error and cancelled",
        )


class TestProgressPayloadFieldsMatchTheApi(unittest.TestCase):
    """The UI read fields the API never sends, so failures said 'Unknown error'."""

    def test_error_text_uses_the_field_the_api_returns(self) -> None:
        from app.core.engine_manager import EngineManager

        keys = set(EngineManager(manifest=None).get_progress("docling"))
        self.assertIn("error_message", keys)
        self.assertNotIn("error", keys)
        self.assertIn("prog.error_message", APP_JS)
        self.assertNotIn("prog.error ", APP_JS)

    def test_ui_does_not_read_a_message_field_that_is_never_sent(self) -> None:
        from app.core.engine_manager import EngineManager

        keys = set(EngineManager(manifest=None).get_progress("docling"))
        self.assertNotIn("message", keys)
        self.assertNotIn("prog.message", APP_JS)


if __name__ == "__main__":
    unittest.main()
