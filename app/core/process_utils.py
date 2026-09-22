"""Helpers for spawning child processes without disturbing the desktop UI."""
from __future__ import annotations

import subprocess
import sys
from typing import Any

# Documented value of CREATE_NO_WINDOW, used as a fallback in the unlikely event the
# constant is missing from this interpreter's subprocess module.
_CREATE_NO_WINDOW = 0x08000000


def hidden_process_kwargs() -> dict[str, Any]:
    """Popen/run keyword arguments that keep a console child invisible on Windows.

    InkDoc's packaged executable is built with PyInstaller --windowed, so it owns no
    console. When such a process launches a console application -- the engine pack's
    python.exe, or the markit/npx/wsl CLIs -- Windows allocates a brand new console
    and shows it. The user sees a Command Prompt appear, and because that window owns
    the child, closing it sends CTRL_CLOSE_EVENT and kills the conversion mid-run.

    CREATE_NO_WINDOW runs the child with no console at all, which is what a background
    worker wants: nothing to show, and nothing for the user to close. Standard handles
    are already redirected to pipes by every caller, so suppressing the console costs
    no output. STARTF_USESHOWWINDOW/SW_HIDE is set alongside it for hosts that honour
    startupinfo but ignore creationflags.

    Returns an empty mapping off Windows, where console allocation does not happen.
    """
    if sys.platform != "win32":
        return {}

    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    startupinfo.wShowWindow = subprocess.SW_HIDE  # type: ignore[attr-defined]

    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW),
        "startupinfo": startupinfo,
    }
