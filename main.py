"""Entry point for InkDoc Desktop Application."""
from __future__ import annotations

import io
import os
import sys

# Configure thread pool limits before any native scientific libraries (NumPy, PyTorch, Docling) load.
# This prevents OpenBLAS "Memory allocation still failed after 10 retries" crashes on Windows.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

# In Windows GUI mode (e.g. PyInstaller --windowed / pythonw), standard streams
# (stdout, stderr, stdin) are None. Provide safe fallbacks so stdio writes or
# .isatty() checks in logging or third-party libraries do not crash the application.
if sys.stdout is None:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    except Exception:
        sys.stdout = io.StringIO()

if sys.stderr is None:
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    except Exception:
        sys.stderr = io.StringIO()

if sys.stdin is None:
    try:
        sys.stdin = open(os.devnull, encoding="utf-8", errors="replace")  # noqa: SIM115
    except Exception:
        sys.stdin = io.StringIO()

# Ensure repository root is on sys.path
_repo_root = os.path.dirname(os.path.abspath(__file__))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# The MarkItDown worker (app/core/engines/markitdown_worker.py) is this same
# executable, started by InkDoc itself. Checked before the desktop app is
# imported, so the worker never loads the GUI or the web server.
if __name__ == "__main__" and sys.argv[1:2] == ["--markitdown-worker"]:
    from app.core.engines.markitdown_worker import main as _markitdown_worker

    sys.exit(_markitdown_worker(sys.argv[2:]))

# The Linux release shows its window with the host's GTK and WebKit2GTK; point GTK and
# GObject introspection at them before anything imports gi (app/core/linux_host.py).
if getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
    from app.core.linux_host import use_host_gtk

    use_host_gtk(os.environ, getattr(sys, "_MEIPASS", ""))

from app.desktop.runner import main

if __name__ == "__main__":
    sys.exit(main())

