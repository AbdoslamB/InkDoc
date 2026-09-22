"""Child processes must not open a Command Prompt window on Windows.

InkDoc's packaged executable is built with PyInstaller --windowed and owns no
console. When it launched a console application -- the engine pack's python.exe,
or the markit/npx/wsl CLIs -- Windows allocated a new console and showed it. Users
saw a Command Prompt appear on every Docling conversion, and because that window
owned the worker, closing it sent CTRL_CLOSE_EVENT and killed the conversion.

The runtime test below spawns a real child that reports whether it has a console,
so it verifies the effect rather than the presence of a flag.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.process_utils import hidden_process_kwargs  # noqa: E402

# Prints "<console window handle> <processes sharing that console>".
#
# The handle is what matters: it is 0 when the child has no visible console window,
# and non-zero when Windows gave it a real one. Measured behaviour of the three ways
# a child can be spawned:
#
#   inherited (no flags)   hwnd=0        attached=4   shares the parent's console
#   CREATE_NO_WINDOW       hwnd=0        attached=1   own console, no window
#   CREATE_NEW_CONSOLE     hwnd=3017896  attached=1   real, visible window
#
# The packaged app owns no console, so an unsuppressed console child behaves like the
# third case: a Command Prompt appears, and closing it kills the child.
CONSOLE_PROBE = (
    "import ctypes;"
    "k=ctypes.windll.kernel32;"
    "buf=(ctypes.c_uint*4)();"
    "print(k.GetConsoleWindow(), k.GetConsoleProcessList(buf, 4))"
)


def _probe(**kwargs: object) -> tuple[int, int]:
    """Return (console window handle, processes attached) for a spawned child."""
    result = subprocess.run(
        [sys.executable, "-c", CONSOLE_PROBE],
        capture_output=True, text=True, timeout=60, **kwargs,  # type: ignore[arg-type]
    )
    if result.returncode != 0:
        raise AssertionError(f"console probe failed: {result.stderr.strip()[:300]}")
    hwnd, attached = result.stdout.split()
    return int(hwnd), int(attached)


class TestHiddenProcessKwargs(unittest.TestCase):
    def test_windows_requests_no_console(self) -> None:
        kwargs = hidden_process_kwargs()
        if sys.platform != "win32":
            self.assertEqual(kwargs, {}, "no console suppression is needed off Windows")
            return
        self.assertEqual(kwargs["creationflags"], 0x08000000, "CREATE_NO_WINDOW")
        self.assertEqual(kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE)

    @unittest.skipUnless(sys.platform == "win32", "console windows are a Windows concern")
    def test_child_gets_no_console_window(self) -> None:
        """The effect, not the flag: the spawned child has no visible console."""
        hwnd, attached = _probe(**hidden_process_kwargs())
        self.assertEqual(
            hwnd, 0,
            "child owns a console window: a Command Prompt appears on every "
            "conversion, and closing it kills the worker",
        )
        self.assertEqual(
            attached, 1,
            "child shares a console with other processes, so closing that console "
            "would signal the worker too",
        )

    @unittest.skipUnless(sys.platform == "win32", "console windows are a Windows concern")
    def test_probe_can_actually_see_a_console_window(self) -> None:
        """Without this, test_child_gets_no_console_window could pass vacuously.

        GetConsoleWindow() returns 0 under a ConPTY terminal even though a console
        exists, so simply comparing against an unsuppressed child proves nothing here.
        CREATE_NEW_CONSOLE forces a genuinely visible window, which is the thing the
        packaged app was accidentally producing. It flashes briefly when run locally.
        """
        hwnd, _ = _probe(creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.assertNotEqual(
            hwnd, 0,
            "the probe cannot detect a console window even when one is forced, so it "
            "cannot prove the suppressed case is actually hidden",
        )


class TestEveryConsoleSpawnIsHidden(unittest.TestCase):
    """No call site may spawn a console child without the suppression kwargs.

    The Docling worker was fixed once; markit_engine ran markit, npx and wsl the
    same way and would have kept flashing a window on every Markit conversion.
    """

    CALL_SITES = [
        Path("app/core/engines/docling_worker_client.py"),
        Path("app/core/engines/markit_engine.py"),
    ]

    def test_all_known_console_spawns_pass_the_kwargs(self) -> None:
        for rel in self.CALL_SITES:
            with self.subTest(module=rel.as_posix()):
                src = (REPO_ROOT / rel).read_text(encoding="utf-8")
                spawns = src.count("subprocess.Popen(") + src.count("subprocess.run(")
                hidden = src.count("**hidden_process_kwargs()")
                self.assertGreater(spawns, 0, "expected at least one spawn here")
                self.assertEqual(
                    hidden, spawns,
                    f"{rel.as_posix()} has {spawns} process spawns but {hidden} pass "
                    "hidden_process_kwargs(); each unguarded one shows a Command Prompt",
                )


if __name__ == "__main__":
    unittest.main()
