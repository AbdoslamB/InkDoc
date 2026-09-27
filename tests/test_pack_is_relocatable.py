"""The Docling pack must carry its own interpreter.

docling-pack-v2 shipped a virtualenv. `python -m venv` never copies the standard
library, not even with --copies: the stdlib and, on Windows, python311.dll stay in
the base installation and are located at runtime through pyvenv.cfg's `home` key.
On the build runner that key pointed at the toolcache, so the pack worked there and
failed everywhere else -- the interpreter died during initialisation, before running
a line of worker.py, surfacing as "Docling worker exited prematurely".

Every check at the time passed, because every check ran on the machine that built
the pack. These tests pin the build's own guards so the shape of that mistake cannot
return unnoticed.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.build_pack import (  # noqa: E402
    PBS_PYTHON_SERIES,
    assert_pack_is_self_contained,
)


class TestBuildRefusesNonRelocatablePacks(unittest.TestCase):
    """assert_pack_is_self_contained is the guard that needs no runtime to fire."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="inkdoc_relocatable_"))

    def _build_posix_tree(self) -> None:
        lib = self.root / "python" / "lib" / f"python{PBS_PYTHON_SERIES}"
        (lib / "encodings").mkdir(parents=True)
        (lib / "os.py").write_text("", encoding="utf-8")
        (lib / "encodings" / "__init__.py").write_text("", encoding="utf-8")
        for i in range(120):
            (lib / f"mod{i}.py").write_text("", encoding="utf-8")
        bindir = self.root / "python" / "bin"
        bindir.mkdir(parents=True)
        (bindir / "python3").write_text("", encoding="utf-8")

    def test_accepts_a_self_contained_tree(self) -> None:
        self._build_posix_tree()
        assert_pack_is_self_contained(self.root, "linux-x86_64")

    def test_rejects_missing_stdlib(self) -> None:
        bindir = self.root / "python" / "bin"
        bindir.mkdir(parents=True)
        (bindir / "python3").write_text("", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            assert_pack_is_self_contained(self.root, "linux-x86_64")
        self.assertIn("not self-contained", str(ctx.exception))

    def test_rejects_pyvenv_cfg(self) -> None:
        """A pyvenv.cfg means the stdlib is resolved outside the pack."""
        self._build_posix_tree()
        (self.root / "python" / "pyvenv.cfg").write_text(
            "home = /opt/hostedtoolcache/Python/3.11.9/x64/bin\n", encoding="utf-8"
        )
        with self.assertRaises(RuntimeError) as ctx:
            assert_pack_is_self_contained(self.root, "linux-x86_64")
        self.assertIn("pyvenv.cfg", str(ctx.exception))

    def test_rejects_windows_tree_without_python_dll(self) -> None:
        win = self.root / "python"
        (win / "Lib" / "encodings").mkdir(parents=True)
        (win / "python.exe").write_text("", encoding="utf-8")
        (win / "Lib" / "os.py").write_text("", encoding="utf-8")
        (win / "Lib" / "encodings" / "__init__.py").write_text("", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            assert_pack_is_self_contained(self.root, "windows-x86_64")
        self.assertIn("python311.dll", str(ctx.exception))


# The matching assertions about the *shipped* app/core/manifest.json -- no pyvenv.cfg,
# stdlib bundled, interpreter outside the old env/ layout -- live in
# tests/test_engine_manifest_guard.py, next to the other guards on that file.


if __name__ == "__main__":
    unittest.main()
