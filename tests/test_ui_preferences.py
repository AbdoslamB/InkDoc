"""Interface preferences (theme, engine choice, hidden engines, auto-save, Auto's
tip) and Restore defaults.

InkDoc 1.2 kept these in browser storage, which the desktop app's web view
clears on every launch, so they were forgotten at each restart. They now live in
settings.json like the layout width, and reach the page before first paint
inside the nonce'd script (window.__INKDOC_PREFS__ and data-* on <html>).
"""
from __future__ import annotations

import io
import json
import os
import re

import pytest
from fastapi.testclient import TestClient

import app.server.server as server_module
from app.core.engine_manager import (
    DEFAULT_SETTINGS,
    ENGINE_CHOICES,
    HIDEABLE_ENGINES,
    RESETTABLE_SETTINGS,
    THEMES,
    EngineManager,
    default_settings,
    sanitize_ui_preferences,
)
from app.server.server import SettingsPayload, app

client = TestClient(app)
TOKEN = {"X-InkDoc-Token": server_module.SESSION_TOKEN}


def _script(html: str) -> str:
    match = re.search(r'<script id="inkdoc-session-token" nonce="[a-f0-9]+">(.*?)</script>', html, re.S)
    assert match is not None
    return match.group(1)


def _page_theme(html: str) -> str | None:
    match = re.search(r"document\.documentElement\.dataset\.theme = '([a-z]+)';", html)
    return match.group(1) if match else None


def _page_hidden(html: str) -> str | None:
    match = re.search(r"document\.documentElement\.dataset\.hiddenEngines = '([a-z_ ]*)';", html)
    return match.group(1) if match else None


def _page_prefs(html: str) -> dict:
    match = re.search(r"window\.__INKDOC_PREFS__ = (\{.*?\});", html)
    assert match is not None
    return json.loads(match.group(1))


def _stored() -> dict:
    path = EngineManager.get_instance().get_settings_file_path()
    return json.loads(path.read_text(encoding="utf-8"))


# ─── Defaults ─────────────────────────────────────────────────────────────────

def test_defaults():
    assert THEMES == ("dark", "light")
    assert ENGINE_CHOICES == ("auto", "markitdown", "docling", "markit", "glm_ocr")
    assert ENGINE_CHOICES[1:] == HIDEABLE_ENGINES, "Auto can never be hidden"
    settings = client.get("/settings").json()
    assert settings["theme"] == "dark"
    assert settings["preferred_engine"] == "auto"
    assert settings["hidden_engines"] == []
    assert settings["auto_save"] is False
    assert settings["auto_hint_hidden"] is False
    assert settings["legacy_prefs_imported"] is False

    html = client.get("/InkDoc").text
    assert _page_theme(html) == "dark"
    assert _page_hidden(html) == ""
    assert _page_prefs(html) == {
        "preferred_engine": "auto",
        "auto_save": False,
        "auto_hint_hidden": False,
        "legacy_prefs_imported": False,
    }


def _schema_enum(field: str) -> set:
    """The allowed values of a field, from the public JSON schema."""
    props = SettingsPayload.model_json_schema()["properties"][field]
    for option in props.get("anyOf", [props]):
        if "enum" in option:
            return set(option["enum"])
        if "items" in option and "enum" in option["items"]:
            return set(option["items"]["enum"])
    raise AssertionError(f"no enum for {field}")


def test_payload_values_match_the_constants():
    assert _schema_enum("theme") == set(THEMES)
    assert _schema_enum("preferred_engine") == set(ENGINE_CHOICES)
    assert _schema_enum("hidden_engines") == set(HIDEABLE_ENGINES)


def test_returned_settings_are_copies():
    """Editing what get_settings() or update_settings() returned changes nothing stored."""
    mgr = EngineManager.get_instance()
    mgr.update_settings({"hidden_engines": ["markit"], "glm_ocr_measured_spp": {"q8": 1.0}})
    got = mgr.get_settings()
    got["hidden_engines"].append("docling")
    got["glm_ocr_measured_spp"]["q8"] = 99.0
    returned = mgr.update_settings({"theme": "light"})
    returned["hidden_engines"].clear()
    fresh = mgr.get_settings()
    assert fresh["hidden_engines"] == ["markit"]
    assert fresh["glm_ocr_measured_spp"] == {"q8": 1.0}


def test_default_copies_are_never_shared():
    first = default_settings()
    first["hidden_engines"].append("docling")
    first["glm_ocr_measured_spp"]["q8"] = 1.0
    assert DEFAULT_SETTINGS["hidden_engines"] == []
    assert DEFAULT_SETTINGS["glm_ocr_measured_spp"] == {}
    assert default_settings()["hidden_engines"] == []


# ─── Saving ───────────────────────────────────────────────────────────────────

def test_each_preference_is_saved_and_served():
    for patch in [
        {"theme": "light"},
        {"preferred_engine": "markit"},
        {"hidden_engines": ["docling", "glm_ocr"]},
        {"auto_save": True},
        {"auto_hint_hidden": True},
        {"legacy_prefs_imported": True},
    ]:
        resp = client.post("/settings", json=patch, headers=TOKEN)
        assert resp.status_code == 200, (patch, resp.text)
        key, value = next(iter(patch.items()))
        assert client.get("/settings").json()[key] == value
        assert _stored()[key] == value, "persisted, not only cached"

    html = client.get("/InkDoc").text
    assert _page_theme(html) == "light"
    assert _page_hidden(html) == "docling glm_ocr"
    assert _page_prefs(html) == {
        "preferred_engine": "markit",
        "auto_save": True,
        "auto_hint_hidden": True,
        "legacy_prefs_imported": True,
    }


def test_theme_back_to_dark():
    client.post("/settings", json={"theme": "light"}, headers=TOKEN)
    client.post("/settings", json={"theme": "dark"}, headers=TOKEN)
    assert _page_theme(client.get("/InkDoc").text) == "dark"


def test_hidden_engines_are_deduplicated_in_a_fixed_order():
    resp = client.post("/settings", json={"hidden_engines": ["markit", "docling", "markit"]}, headers=TOKEN)
    assert resp.status_code == 200
    assert resp.json()["hidden_engines"] == ["docling", "markit"]
    assert client.post("/settings", json={"hidden_engines": []}, headers=TOKEN).json()["hidden_engines"] == []


@pytest.mark.parametrize("patch", [
    {"theme": "blue"},
    {"theme": "Light"},
    {"theme": "light'; alert(1); '"},
    {"theme": 1},
    {"preferred_engine": "pandoc"},
    {"preferred_engine": ""},
    {"hidden_engines": ["auto"]},
    {"hidden_engines": ["pandoc"]},
    {"hidden_engines": "docling"},
    {"hidden_engines": ["markitdown", "docling", "markit", "glm_ocr", "docling"]},
    {"auto_save": "maybe"},
    {"auto_save": [True]},
    {"auto_hint_hidden": 2},
    {"legacy_prefs_imported": {"x": 1}},
])
def test_unknown_values_are_refused(patch):
    resp = client.post("/settings", json=patch, headers=TOKEN)
    assert resp.status_code == 422, resp.text
    settings = EngineManager.get_instance().get_settings()
    for key in patch:
        assert settings[key] == DEFAULT_SETTINGS[key]


def test_needs_the_session_token():
    assert client.post("/settings", json={"theme": "light"}).status_code == 403
    assert client.post("/settings/reset").status_code == 403
    assert EngineManager.get_instance().get_settings()["theme"] == "dark"


# ─── What reaches the page ────────────────────────────────────────────────────

@pytest.mark.parametrize("stored", [
    {"theme": "light'; alert(1); '", "preferred_engine": "x'; alert(1); '",
     "hidden_engines": ["docling'; alert(1); '"], "auto_save": "true'; alert(1); '"},
    {"theme": None, "preferred_engine": None, "hidden_engines": None, "auto_save": None},
    {"theme": ["light"], "preferred_engine": 3, "hidden_engines": "docling markit", "auto_save": 1},
    {"theme": "LIGHT", "preferred_engine": "AUTO", "hidden_engines": {"docling": True}, "auto_save": "yes"},
    {"hidden_engines": ["</script><script>alert(1)</script>", "markit"]},
])
def test_hand_edited_values_never_reach_the_page(stored):
    """Only a known value is ever written into the page's script."""
    EngineManager.get_instance().update_settings(stored)
    html = client.get("/InkDoc").text
    assert "alert(1)" not in html
    assert _page_theme(html) == "dark"
    prefs = _page_prefs(html)
    assert prefs["preferred_engine"] == "auto"
    assert prefs["auto_save"] is False
    assert _page_hidden(html) in ("", "markit")


def test_script_json_cannot_close_or_break_the_script():
    from app.server.server import _script_json

    out = _script_json({"x": "</script><script>alert(1)</script>", "y": "a b c"})
    assert "<" not in out and " " not in out and " " not in out
    assert json.loads(out) == {"x": "</script><script>alert(1)</script>", "y": "a b c"}


def test_a_hidden_preferred_engine_is_served_as_auto():
    EngineManager.get_instance().update_settings({"preferred_engine": "docling", "hidden_engines": ["docling"]})
    assert _page_prefs(client.get("/InkDoc").text)["preferred_engine"] == "auto"


def test_sanitize_keeps_good_values():
    assert sanitize_ui_preferences({
        "theme": "light", "preferred_engine": "glm_ocr", "hidden_engines": ["markit", "markit", "x"],
        "auto_save": True, "auto_hint_hidden": True, "legacy_prefs_imported": True,
    }) == {
        "theme": "light", "preferred_engine": "glm_ocr", "hidden_engines": ["markit"],
        "auto_save": True, "auto_hint_hidden": True, "legacy_prefs_imported": True,
    }
    assert sanitize_ui_preferences({})["theme"] == "dark"


def test_preferences_are_set_inside_the_nonce_script_before_the_body():
    client.post("/settings", json={"theme": "light", "hidden_engines": ["markit"]}, headers=TOKEN)
    resp = client.get("/InkDoc")
    script = _script(resp.text)
    assert "dataset.theme = 'light';" in script
    assert "dataset.hiddenEngines = 'markit';" in script
    assert "window.__INKDOC_PREFS__ = {" in script
    assert resp.text.index("__INKDOC_PREFS__") < resp.text.index("</head>")
    assert "unsafe-inline" not in resp.headers["content-security-policy"].split("script-src")[1].split(";")[0]


def test_legacy_theme_is_read_for_first_paint_only_until_imported():
    html = client.get("/InkDoc").text
    assert "localStorage.getItem('inkdoc-theme')" in _script(html)
    client.post("/settings", json={"legacy_prefs_imported": True}, headers=TOKEN)
    html = client.get("/InkDoc").text
    assert "localStorage" not in _script(html)


def test_one_settings_read_per_page(monkeypatch):
    mgr = EngineManager.get_instance()
    calls = []
    real = mgr.get_settings
    monkeypatch.setattr(mgr, "get_settings", lambda: calls.append(1) or real())
    client.get("/InkDoc")
    assert len(calls) == 1


def test_page_still_served_when_settings_cannot_be_read(monkeypatch):
    mgr = EngineManager.get_instance()

    def broken():
        raise OSError("unreadable")

    monkeypatch.setattr(mgr, "get_settings", broken)
    resp = client.get("/InkDoc")
    assert resp.status_code == 200
    assert _page_theme(resp.text) == "dark"
    assert _page_prefs(resp.text)["preferred_engine"] == "auto"


# ─── Older and newer settings files ───────────────────────────────────────────

def test_settings_file_from_inkdoc_1_2():
    mgr = EngineManager.get_instance()
    path = mgr.get_settings_file_path()
    path.write_text(json.dumps({"layout_width": "wide", "save_dir": "", "render_math": False}), encoding="utf-8")
    mgr._settings_cache = None
    settings = client.get("/settings").json()
    assert settings["theme"] == "dark" and settings["hidden_engines"] == []
    assert settings["layout_width"] == "wide" and settings["render_math"] is False


def test_first_write_without_a_cache_still_reports_every_key():
    """update_settings() before any read (the cache is empty) used to drop the defaults."""
    mgr = EngineManager.get_instance()
    path = mgr.get_settings_file_path()
    path.write_text(json.dumps({"layout_width": "wide"}), encoding="utf-8")
    mgr._settings_cache = None
    result = mgr.update_settings({"theme": "light"})
    assert result["layout_width"] == "wide" and result["theme"] == "light"
    assert result["preferred_engine"] == "auto"
    assert mgr.get_settings()["hidden_engines"] == []


def test_unknown_keys_survive_a_write():
    """A newer InkDoc's keys are kept by this one (downgrade, then upgrade again)."""
    mgr = EngineManager.get_instance()
    path = mgr.get_settings_file_path()
    path.write_text(json.dumps({"from_the_future": {"a": 1}}), encoding="utf-8")
    mgr._settings_cache = None
    client.post("/settings", json={"theme": "light"}, headers=TOKEN)
    assert _stored()["from_the_future"] == {"a": 1}


# ─── A settings file that can't be written ───────────────────────────────────

def _fail_writes(monkeypatch):
    import app.core.engine_manager as em

    def refuse(*_args, **_kwargs):
        raise PermissionError(13, "Access is denied", "C:\\Users\\someone\\AppData\\Local\\InkDoc\\settings.json")

    monkeypatch.setattr(em, "SETTINGS_REPLACE_DELAY_S", 0)
    monkeypatch.setattr(em.os, "replace", refuse)


def test_a_briefly_locked_settings_file_is_retried(monkeypatch):
    """Windows: an antivirus scan or the indexer holds the file for a moment."""
    import app.core.engine_manager as em

    real_replace = em.os.replace
    attempts = []

    def locked_twice(src, dst):
        attempts.append(1)
        if len(attempts) <= 2:
            raise PermissionError(13, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(em, "SETTINGS_REPLACE_DELAY_S", 0)
    monkeypatch.setattr(em.os, "replace", locked_twice)
    resp = client.post("/settings", json={"theme": "light"}, headers=TOKEN)
    assert resp.status_code == 200
    assert len(attempts) == 3
    assert _stored()["theme"] == "light"


def test_a_file_that_stays_locked_is_given_up_on_quickly(monkeypatch):
    import app.core.engine_manager as em

    calls = []

    def refuse(*_args, **_kwargs):
        calls.append(1)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(em, "SETTINGS_REPLACE_DELAY_S", 0)
    monkeypatch.setattr(em.os, "replace", refuse)
    assert client.post("/settings", json={"theme": "light"}, headers=TOKEN).status_code == 503
    assert len(calls) == em.SETTINGS_REPLACE_ATTEMPTS


def test_readers_never_wait_for_the_disk(monkeypatch):
    """A slow write (an antivirus scan, the retry) holds up only other writers."""
    import threading

    import app.core.engine_manager as em

    mgr = EngineManager.get_instance()
    mgr.get_settings()
    entered = threading.Event()
    release = threading.Event()
    real_replace = em.os.replace

    def slow_replace(src, dst):
        entered.set()
        assert release.wait(10)
        real_replace(src, dst)

    monkeypatch.setattr(em.os, "replace", slow_replace)
    writer = threading.Thread(target=mgr.update_settings, args=({"theme": "light"},))
    writer.start()
    try:
        assert entered.wait(10)
        result = {}
        reader = threading.Thread(target=lambda: result.update(mgr.get_settings()))
        reader.start()
        reader.join(2)
        assert not reader.is_alive(), "get_settings() waited for the file to be written"
        assert result["theme"] == "light", "and already sees the change"
    finally:
        release.set()
        writer.join(10)
    assert _stored()["theme"] == "light"


def test_concurrent_writes_leave_the_newest_settings_on_disk():
    import threading

    mgr = EngineManager.get_instance()
    threads = [
        threading.Thread(target=mgr.update_settings, args=({"preview_text_size": size},))
        for size in (85, 90, 110, 120, 135, 150)
    ]
    threads += [threading.Thread(target=mgr.update_settings, args=({"theme": "light"},))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert _stored() == mgr.get_settings(), "the file matches the settings in memory"
    assert _stored()["theme"] == "light"


def test_retry_budget_stays_under_half_a_second():
    import app.core.engine_manager as em

    assert (em.SETTINGS_REPLACE_ATTEMPTS - 1) * em.SETTINGS_REPLACE_DELAY_S <= 0.5


def test_a_failed_write_is_reported_to_the_page(monkeypatch):
    _fail_writes(monkeypatch)
    resp = client.post("/settings", json={"theme": "light"}, headers=TOKEN)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "lost when InkDoc restarts" in detail
    assert "someone" not in detail and "settings.json" not in detail, "no path in the message"
    # Still applied for this session, as before.
    assert EngineManager.get_instance().get_settings()["theme"] == "light"

    resp = client.post("/settings/reset", headers=TOKEN)
    assert resp.status_code == 503


def test_other_callers_still_never_see_a_write_error(monkeypatch):
    _fail_writes(monkeypatch)
    result = EngineManager.get_instance().update_settings({"glm_ocr_use_gpu": True})
    assert result["glm_ocr_use_gpu"] is True


def test_no_temporary_file_is_left_after_a_failed_write(monkeypatch):
    _fail_writes(monkeypatch)
    client.post("/settings", json={"theme": "light"}, headers=TOKEN)
    folder = EngineManager.get_instance().get_settings_file_path().parent
    assert not [p for p in folder.iterdir() if p.name.startswith("settings_")]


# ─── Restore defaults ─────────────────────────────────────────────────────────

KEPT = {
    "save_dir": "",  # replaced by a real folder in the test
    "docling_code_enrichment": True,
    "docling_formula_enrichment": True,
    "glm_ocr_use_gpu": True,
    "glm_ocr_download_source": "mirror_only",
    "glm_ocr_catalogue_seen": 7,
    "glm_ocr_measured_spp": {"q8": 12.5},
    "legacy_prefs_imported": True,
    "from_the_future": "kept",
}

CHANGED = {
    "theme": "light",
    "preferred_engine": "markit",
    "hidden_engines": ["docling", "glm_ocr"],
    "auto_save": True,
    "auto_hint_hidden": True,
    "fallback_to_markitdown": True,
    "quality_check_enabled": False,
    "auto_quality": "thorough",
    "layout_width": "full",
    "preview_text_size": 150,
    "render_math": False,
    "check_for_updates_daily": True,
}


def test_reset_list_is_exactly_the_changed_preferences():
    assert set(RESETTABLE_SETTINGS) == set(CHANGED)
    assert not set(RESETTABLE_SETTINGS) & set(KEPT)


def test_reset_restores_preferences_and_keeps_everything_else(tmp_path):
    mgr = EngineManager.get_instance()
    # Set directly: the save folder and enrichment flags can't be set over HTTP here.
    mgr.update_settings({**KEPT, **CHANGED, "save_dir": str(tmp_path)})

    resp = client.post("/settings/reset", headers=TOKEN)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    for key in RESETTABLE_SETTINGS:
        assert body[key] == DEFAULT_SETTINGS[key], key
        assert _stored()[key] == DEFAULT_SETTINGS[key], key
    for key, value in {**KEPT, "save_dir": str(tmp_path)}.items():
        assert body[key] == value, key
        assert _stored()[key] == value, key

    # Same body as GET /settings, so the page applies it the same way.
    assert body == client.get("/settings").json()
    assert body["save_folder"]["chosen"] == str(tmp_path)

    html = client.get("/InkDoc").text
    assert _page_theme(html) == "dark" and _page_hidden(html) == ""


def test_reset_is_one_write_and_can_run_twice(monkeypatch):
    import app.core.engine_manager as em

    client.post("/settings", json=CHANGED, headers=TOKEN)
    writes = []
    real_replace = em.os.replace
    monkeypatch.setattr(em.os, "replace", lambda a, b: writes.append(b) or real_replace(a, b))
    first = client.post("/settings/reset", headers=TOKEN).json()
    assert len(writes) == 1
    second = client.post("/settings/reset", headers=TOKEN).json()
    assert first == second


def test_reset_is_on_every_route_prefix():
    for prefix in ("", "/InkDoc", "/MarkItDown"):
        assert client.post(f"{prefix}/settings/reset", headers=TOKEN).status_code == 200, prefix


# ─── Hiding never changes conversion ──────────────────────────────────────────

def test_auto_and_explicit_engines_ignore_hidden_engines(monkeypatch):
    from unittest.mock import patch

    from tests.test_server import _ocr, _scanned_pdf_bytes

    monkeypatch.setattr(server_module, "_conversion_semaphores", {})
    client.post("/settings", json={"hidden_engines": list(HIDEABLE_ENGINES)}, headers=TOKEN)

    resp = client.post(
        "/convert/file?engine=auto&response_format=json",
        files={"file": ("notes.txt", io.BytesIO(b"Plain words here"), "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["engine_used"] == "markitdown"

    resp = client.post(
        "/convert/file?engine=markitdown&response_format=json",
        files={"file": ("notes.txt", io.BytesIO(b"Plain words here"), "text/plain")},
    )
    assert resp.status_code == 200 and resp.json()["engine_used"] == "markitdown"

    def fake_convert_local(file_item, options, *, fetched=None):
        options.engine_used = file_item.engine
        return "docling text"

    with patch("app.core.converter.convert_local", side_effect=fake_convert_local), \
         _ocr([server_module.EngineKind.DOCLING]):
        resp = client.post(
            "/convert/file?engine=auto&response_format=json",
            files={"file": ("scan.pdf", io.BytesIO(_scanned_pdf_bytes()), "application/pdf")},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["engine_used"] == "docling", "Auto still routes to a hidden engine"


def test_auto_save_setting_does_not_save_by_itself(tmp_path):
    """The server never reads auto_save: only save_to_downloads on the request saves."""
    mgr = EngineManager.get_instance()
    mgr.update_settings({"auto_save": True, "save_dir": str(tmp_path)})
    resp = client.post(
        "/convert/file?response_format=json",
        files={"file": ("notes.txt", io.BytesIO(b"Plain words here"), "text/plain")},
    )
    assert resp.status_code == 200
    assert os.listdir(tmp_path) == []
