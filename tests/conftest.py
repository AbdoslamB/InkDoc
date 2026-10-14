"""Shared test setup.

Every test gets its own empty settings.json. Without this, a test that saves or
changes a setting would read and write the developer's real one: with a save
folder chosen in the desktop app, auto-save tests would write into that folder.
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
