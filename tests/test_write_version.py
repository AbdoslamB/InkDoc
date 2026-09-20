"""Tests for scripts/write_version.py and unknown version handling in UpdateManager."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.write_version import (
    resolve_version_string,
    write_version_file,
    verify_written_version,
)
from app.core.update_manager import UpdateManager, UpdateState


class TestWriteVersion(unittest.TestCase):
    """Test semantic version parsing, writer, and verification."""

    def test_parse_valid_semver(self):
        """v1.2.3 strips the leading 'v' to produce 1.2.3."""
        self.assertEqual(resolve_version_string("v1.2.3"), "1.2.3")

    def test_parse_valid_prerelease(self):
        """v1.2.3-rc1 strips the leading 'v' to produce 1.2.3-rc1."""
        self.assertEqual(resolve_version_string("v1.2.3-rc1"), "1.2.3-rc1")

    def test_parse_invalid_main(self):
        """Non-semver tag 'main' must fail when not in dispatch mode."""
        with self.assertRaises(ValueError):
            resolve_version_string("main")

    def test_parse_empty(self):
        """Empty version tag must fail."""
        with self.assertRaises(ValueError):
            resolve_version_string("")
        with self.assertRaises(ValueError):
            resolve_version_string("   ")
        with self.assertRaises(ValueError):
            resolve_version_string(None)

    def test_dispatch_mode(self):
        """Dispatch mode resolves to 0.0.0+dev.<short-sha>."""
        res = resolve_version_string("main", is_dispatch=True, sha="9fa1265")
        self.assertEqual(res, "0.0.0+dev.9fa1265")

    def test_write_and_verify_in_temp_file(self):
        """Writing and importing in a temporary file correctly exports get_version()."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "_version.py"

            # 1. Test v1.2.3
            write_version_file("1.2.3", target)
            verify_written_version("1.2.3", target)

            # Inspect imported module directly
            spec = importlib.util.spec_from_file_location("test_ver_1", str(target))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertEqual(mod.get_version(), "1.2.3")
            self.assertEqual(mod.__version__, "1.2.3")

            # 2. Test v1.2.3-rc1
            write_version_file("1.2.3-rc1", target)
            verify_written_version("1.2.3-rc1", target)
            spec = importlib.util.spec_from_file_location("test_ver_2", str(target))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertEqual(mod.get_version(), "1.2.3-rc1")

    def test_cli_execution_in_temp_repo(self):
        """Run scripts/write_version.py via CLI in a temp dir."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "_version.py"
            script_path = str(REPO_ROOT / "scripts" / "write_version.py")

            # v1.2.3
            res = subprocess.run(
                [sys.executable, script_path, "v1.2.3", "--target", str(target)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("1.2.3", target.read_text())

            # v1.2.3-rc1
            res = subprocess.run(
                [sys.executable, script_path, "v1.2.3-rc1", "--target", str(target)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("1.2.3-rc1", target.read_text())

            # main (must fail)
            res = subprocess.run(
                [sys.executable, script_path, "main", "--target", str(target)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)

            # empty (must fail)
            res = subprocess.run(
                [sys.executable, script_path, "", "--target", str(target)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)

            # manual dispatch with sha
            res = subprocess.run(
                [sys.executable, script_path, "--dispatch", "--sha", "abcdef1", "--target", str(target)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("0.0.0+dev.abcdef1", target.read_text())


class TestUpdateManagerUnknownVersionGuards(unittest.TestCase):
    """Verify UpdateManager refuses checks and downloads when version is unknown."""

    def test_check_refused_for_unknown_version(self):
        mgr = UpdateManager()
        with patch("app.core.update_manager.get_version", return_value="0.0.0+unknown"):
            status = mgr.check_for_updates(force=True)
            self.assertEqual(status["state"], UpdateState.ERROR.value)
            self.assertIn("application version is unknown", status["error"])

    def test_download_refused_for_unknown_version(self):
        mgr = UpdateManager()
        with patch("app.core.update_manager.get_version", return_value="0.0.0+unknown"):
            with self.assertRaises(ValueError) as ctx:
                mgr.download_update()
            self.assertIn("application version is unknown", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
