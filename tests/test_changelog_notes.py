"""Release notes come from CHANGELOG.md (scripts/changelog_notes.py).

The release page (release.yml) and the signed update manifest (sign_manifest.py)
both use the CHANGELOG's section for the version, which the app shows under
Settings -> Updates. GitHub's generated notes were used before; with few pull
requests they are only a "Full Changelog" link, so the app showed a URL.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.changelog_notes import changelog_section  # noqa: E402
from scripts.sign_manifest import changelog_at_tag, pick_release_notes  # noqa: E402

CHANGELOG = """# Changelog

## [Unreleased]

### Added
- Something new.

## [1.2.0] - 2026-10-14

### Changed
- **Settings** now opens as a wider panel.
  It has five sections.

### Fixed
- A fix.

## [1.1.0] - 2026-10-01

### Added
- Older.

[Unreleased]: https://github.com/AbdoslamB/InkDoc/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/AbdoslamB/InkDoc/compare/v1.1.0...v1.2.0
"""

GENERATED = "**Full Changelog**: https://github.com/AbdoslamB/InkDoc/compare/v1.1.1...v1.2.0"


class TestChangelogSection(unittest.TestCase):
    def test_a_section_without_its_heading(self) -> None:
        self.assertEqual(
            changelog_section(CHANGELOG, "1.2.0"),
            "### Changed\n- **Settings** now opens as a wider panel.\n  It has five sections.\n\n### Fixed\n- A fix.",
        )

    def test_tag_names_and_crlf(self) -> None:
        crlf = CHANGELOG.replace("\n", "\r\n")
        self.assertEqual(changelog_section(crlf, "v1.2.0"), changelog_section(CHANGELOG, "1.2.0"))

    def test_the_last_section_leaves_out_the_comparison_links(self) -> None:
        self.assertEqual(changelog_section(CHANGELOG, "1.1.0"), "### Added\n- Older.")

    def test_missing_or_empty(self) -> None:
        self.assertIsNone(changelog_section(CHANGELOG, "9.9.9"))
        self.assertIsNone(changelog_section(CHANGELOG, "1.2"), "1.2 is not 1.2.0")
        self.assertIsNone(changelog_section(CHANGELOG, ""))
        self.assertIsNone(changelog_section("## [2.0.0] - 2026-11-01\n\n## [1.0.0]\n- x", "2.0.0"))

    def test_every_released_version_in_the_real_changelog(self) -> None:
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for version in ("1.2.0", "1.1.1", "1.1.0", "1.0.0"):
            section = changelog_section(text, version)
            self.assertTrue(section and "\n- " in "\n" + section, version)
            self.assertNotIn("## [", section, version)
            self.assertNotIn("]: https://", section, version)

    def test_command_line(self) -> None:
        script = REPO_ROOT / "scripts" / "changelog_notes.py"
        ok = subprocess.run([sys.executable, str(script), "v1.2.0"], capture_output=True, cwd=REPO_ROOT)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(ok.stdout.decode("utf-8").replace("\r\n", "\n"), changelog_section(text, "1.2.0") + "\n")

        missing = subprocess.run([sys.executable, str(script), "v0.0.1"], capture_output=True, cwd=REPO_ROOT)
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(missing.stdout, b"", "nothing for release.yml to mistake for notes")


class TestSignedNotes(unittest.TestCase):
    def test_the_changelog_section_wins_over_the_release_body(self) -> None:
        notes, source = pick_release_notes("1.2.0", CHANGELOG, GENERATED)
        self.assertEqual(notes, changelog_section(CHANGELOG, "1.2.0"))
        self.assertEqual(source, "CHANGELOG.md")

    def test_the_release_body_when_the_changelog_has_no_section(self) -> None:
        self.assertEqual(pick_release_notes("9.9.9", CHANGELOG, GENERATED), (GENERATED, "release body"))
        self.assertEqual(pick_release_notes("1.2.0", None, GENERATED), (GENERATED, "release body"))
        self.assertEqual(pick_release_notes("9.9.9", CHANGELOG, ""), ("", "release body"))


class TestChangelogAtTag(unittest.TestCase):
    def test_the_changelog_committed_at_the_tag(self) -> None:
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=CHANGELOG.encode("utf-8"))
        with patch("scripts.sign_manifest.subprocess.run", return_value=done) as run:
            self.assertEqual(changelog_at_tag("v1.2.0"), CHANGELOG)
        self.assertEqual(run.call_args.args[0], ["git", "show", "v1.2.0:CHANGELOG.md"])

    def test_the_working_tree_when_git_or_the_tag_is_missing(self) -> None:
        working_tree = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for error in (FileNotFoundError("git"), subprocess.CalledProcessError(128, ["git"])):
            with patch("scripts.sign_manifest.subprocess.run", side_effect=error):
                self.assertEqual(changelog_at_tag("v0.0.0"), working_tree, type(error).__name__)


class TestReleaseWorkflow(unittest.TestCase):
    def test_release_page_notes_come_from_the_changelog(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn('python3 scripts/changelog_notes.py "$TAG" > "$NOTES_FILE"', workflow)
        self.assertIn('NOTES_ARGS=(--notes-file "$NOTES_FILE")', workflow)
        self.assertIn('"${NOTES_ARGS[@]}"', workflow)
        create = next(line for line in workflow.splitlines() if "gh release create" in line)
        self.assertNotIn("--generate-notes", create, "only as the fallback")


if __name__ == "__main__":
    unittest.main()
