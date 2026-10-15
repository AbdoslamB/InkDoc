"""Shared test setup.

Every test gets its own empty settings.json. Without this, a test that saves or
changes a setting would read and write the developer's real one: with a save
folder chosen in the desktop app, auto-save tests would write into that folder.

The PDF probe (2 s) and the missing-text check (1 s) stop when their time
budget runs out. That keeps a huge document from stalling a conversion, but on
a CI runner that pauses for a moment it made results depend on timing: a probe
cut off before its first page sent a scanned PDF to MarkItDown instead of
Docling. Tests therefore get a generous default; tests of the cut-off itself
pass their own budget, which this does not touch.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory, monkeypatch):
    from app.core.engine_manager import EngineManager

    mgr = EngineManager.get_instance()
    settings_file = tmp_path_factory.mktemp("settings") / "settings.json"
    monkeypatch.setattr(mgr, "get_settings_file_path", lambda: settings_file)
    monkeypatch.setattr(mgr, "_settings_cache", None)


TEST_TIME_BUDGET_S = 60.0


@pytest.fixture(autouse=True)
def _no_time_budget_flakes(monkeypatch):
    # Patched on the function objects themselves (keyword-only defaults), so
    # every caller sees it, including modules that imported them by name.
    from app.core.pdf_probe import probe_pdf
    from app.core.quality_check import assess

    for fn in (probe_pdf, assess):
        defaults = dict(fn.__kwdefaults__)
        defaults["time_budget_s"] = TEST_TIME_BUDGET_S
        monkeypatch.setattr(fn, "__kwdefaults__", defaults)
