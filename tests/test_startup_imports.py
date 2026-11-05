"""InkDoc's own process never loads markitdown.

app/desktop/runner.py imports app.server.server before it can open a window, and
the window only opens once that server answers /health. Importing markitdown
loads magika, onnxruntime, pandas and ~20 other libraries: about 2 s, and about
95 MB that Python never returns. Since 1.3 none of it is loaded in InkDoc's own
process at all, not at startup and not to convert: MarkItDown conversions run
in a worker process that starts on demand and stops when idle
(app/core/engines/markitdown_worker_client.py).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HEAVY_MODULES = ("markitdown", "magika", "onnxruntime", "pandas")


def _modules_loaded_after(statement: str) -> tuple[set[str], str]:
    # A fresh interpreter: in-process, whatever an earlier test imported would
    # already sit in sys.modules and hide a regression.
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        f"{statement}\n"
        f"print('LOADED:' + ','.join(m for m in {HEAVY_MODULES!r} if m in sys.modules))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=180,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    loaded = next(line for line in reversed(lines) if line.startswith("LOADED:"))[len("LOADED:"):]
    other = "\n".join(line for line in lines if not line.startswith("LOADED:"))
    return {m for m in loaded.split(",") if m}, other


def test_server_import_does_not_load_markitdown():
    assert _modules_loaded_after("import app.server.server")[0] == set()


def test_runner_import_does_not_load_markitdown():
    assert _modules_loaded_after("import app.desktop.runner")[0] == set()


def test_markitdown_version_does_not_load_markitdown():
    loaded, out = _modules_loaded_after(
        "from app.core.converter import get_markitdown_version; print(get_markitdown_version())"
    )
    assert loaded == set()
    assert out.strip(), "a version is still reported"


def test_prewarm_starts_the_worker_without_loading_markitdown():
    loaded, out = _modules_loaded_after(
        "import time\n"
        "from app.core.converter import prewarm_markitdown\n"
        "from app.core.engines.markitdown_worker_client import MarkItDownWorker\n"
        "prewarm_markitdown()\n"
        "w = MarkItDownWorker.get_instance()\n"
        "print('pid', w.pid())\n"
        "w.shutdown()"
    )
    assert loaded == set()
    assert "pid None" not in out, "the worker process was started"


def test_converting_never_loads_markitdown_here(tmp_path):
    """A real txt, xlsx and PDF conversion: the text is right, and nothing heavy is loaded here."""
    from openpyxl import Workbook

    from tests._pdf_fixtures import prose_page, write_pdf

    txt = tmp_path / "notes.txt"
    txt.write_text("# Notes\n\nPlain words here.\n", encoding="utf-8")
    xlsx = tmp_path / "sheet.xlsx"
    wb = Workbook()
    wb.active.append(["name", "value"])
    wb.active.append(["alpha", 42])
    wb.save(xlsx)
    pdf = write_pdf(tmp_path / "doc.pdf", [prose_page(40, 0)])

    loaded, out = _modules_loaded_after(
        "from app.core.converter import convert_item, ConversionOptions\n"
        "from app.core.queue_model import QueueItem, SourceKind\n"
        "from app.core.engines.markitdown_worker_client import MarkItDownWorker\n"
        f"for path in {[str(txt), str(xlsx), str(pdf)]!r}:\n"
        "    item = QueueItem(source=path, kind=SourceKind.FILE, display_name=path)\n"
        "    print(repr(convert_item(item, ConversionOptions())[:60]))\n"
        "print('worker', MarkItDownWorker.get_instance().pid() is not None)\n"
        "MarkItDownWorker.get_instance().shutdown()"
    )
    assert loaded == set(), f"loaded in InkDoc's own process: {loaded}"
    assert "Plain words here" in out
    assert "alpha" in out and "42" in out
    assert "worker True" in out, "converted by the worker, not in-process"


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
