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

from app.desktop.runner import main

if __name__ == "__main__":
    sys.exit(main())

