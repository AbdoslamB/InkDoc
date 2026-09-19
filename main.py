"""Entry point for InkDoc Desktop Application."""
from __future__ import annotations

import os
import sys

# Ensure repository root is on sys.path
_repo_root = os.path.dirname(os.path.abspath(__file__))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from app.desktop.runner import main

if __name__ == "__main__":
    sys.exit(main())

