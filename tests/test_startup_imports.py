"""The desktop window must not wait on markitdown.

app/desktop/runner.py imports app.server.server before it can open a window, and
the window only opens once that server answers /health. Importing markitdown
loads magika and onnxruntime -- about 2s warm, more from a cold frozen bundle --
so a module-level markitdown import anywhere on that path puts those seconds
between double-clicking the app and seeing it. converter.py imports it lazily
and prewarm_markitdown() loads it in the background once the UI is up.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HEAVY_MODULES = ("markitdown", "magika", "onnxruntime")


def _modules_loaded_after(statement: str) -> set[str]:
    # A fresh interpreter: in-process, whatever an earlier test imported would
    # already sit in sys.modules and hide a regression.
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        f"{statement}\n"
        f"print(','.join(m for m in {HEAVY_MODULES!r} if m in sys.modules))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    last_line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return {m for m in last_line.split(",") if m}


def test_server_import_does_not_load_markitdown():
    assert _modules_loaded_after("import app.server.server") == set()


def test_prewarm_loads_markitdown():
    loaded = _modules_loaded_after(
        "from app.core.converter import prewarm_markitdown; prewarm_markitdown()"
    )
    assert "markitdown" in loaded


def test_every_pyinstaller_build_disables_upx():
    # UPX-compressed DLLs are unpacked in memory at every launch and are a
    # common antivirus false-positive trigger; both slow startup.
    release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    invocations = [line for line in release.splitlines() if "pyinstaller --noconfirm" in line]
    assert len(invocations) == 2, invocations
    for line in invocations:
        assert "--noupx" in line, line

    local = (REPO_ROOT / "scripts" / "build_local_exe.ps1").read_text(encoding="utf-8")
    assert '"--noupx"' in local
