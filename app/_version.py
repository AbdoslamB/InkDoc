"""Canonical version definition for InkDoc.

Single source of truth for application version across desktop runner,
embedded FastAPI server, and updater.
"""
from __future__ import annotations

# CI updates this value before packaging. Default fallback for development checkouts.
__version__ = "1.0.1"


def get_version() -> str:
    """Return the current InkDoc version string."""
    return globals().get("__version__", "0.0.0+unknown")
