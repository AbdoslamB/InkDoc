"""Display settings (layout width, preview text size): stored in settings.json
and put on the page before first paint.

The desktop app's web view clears browser storage on every launch, so each
choice lives in settings.json. The page gets it from the server, inside the
nonce'd script that already sets the title bar, so it never renders at one
width and then jumps to another.
"""
from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

import app.server.server as server_module
from app.core.engine_manager import DEFAULT_SETTINGS, LAYOUT_WIDTHS, EngineManager
from app.server.server import app

client = TestClient(app)
TOKEN = {"X-InkDoc-Token": server_module.SESSION_TOKEN}


def _page_width(html: str) -> str | None:
    match = re.search(r"document\.documentElement\.dataset\.layoutWidth = '([a-z]+)';", html)
    return match.group(1) if match else None


def test_default_is_standard():
    assert DEFAULT_SETTINGS["layout_width"] == "standard"
    assert LAYOUT_WIDTHS == ("standard", "wide", "full")
    assert client.get("/settings").json()["layout_width"] == "standard"
    assert _page_width(client.get("/InkDoc").text) == "standard"


def test_each_width_is_saved_and_served():
    mgr = EngineManager.get_instance()
    for width in LAYOUT_WIDTHS:
        resp = client.post("/settings", json={"layout_width": width}, headers=TOKEN)
        assert resp.status_code == 200
        assert client.get("/settings").json()["layout_width"] == width
        assert _page_width(client.get("/InkDoc").text) == width
        # Persisted, not only cached: survives a restart of the app.
        stored = json.loads(mgr.get_settings_file_path().read_text(encoding="utf-8"))
        assert stored["layout_width"] == width


def test_unknown_width_is_refused():
    for bad in ["huge", "", "WIDE", 1920, "wide'; alert(1); '"]:
        resp = client.post("/settings", json={"layout_width": bad}, headers=TOKEN)
        assert resp.status_code == 422, bad
    assert EngineManager.get_instance().get_settings()["layout_width"] == "standard"


def test_needs_the_session_token():
    resp = client.post("/settings", json={"layout_width": "full"})
    assert resp.status_code == 403
    assert EngineManager.get_instance().get_settings()["layout_width"] == "standard"


def test_other_settings_leave_the_width_alone():
    client.post("/settings", json={"layout_width": "wide"}, headers=TOKEN)
    client.post("/settings", json={"quality_check_enabled": False}, headers=TOKEN)
    assert EngineManager.get_instance().get_settings()["layout_width"] == "wide"


def test_hand_edited_value_falls_back_to_standard():
    """Only a known value is ever written into the page's script."""
    mgr = EngineManager.get_instance()
    for stored in ["huge", "wide'; alert(1); '", None, 1920, ["wide"]]:
        mgr.update_settings({"layout_width": stored})
        html = client.get("/InkDoc").text
        assert _page_width(html) == "standard", stored
        assert "alert(1)" not in html


def test_settings_file_without_the_key_means_standard():
    """settings.json written by an older InkDoc has no layout_width."""
    mgr = EngineManager.get_instance()
    path = mgr.get_settings_file_path()
    path.write_text(json.dumps({"quality_check_enabled": True}), encoding="utf-8")
    mgr._settings_cache = None
    assert mgr.get_settings()["layout_width"] == "standard"
    assert _page_width(client.get("/InkDoc").text) == "standard"


def test_width_is_set_inside_the_nonce_script():
    """Under the page's CSP only the nonce'd script may run, and it runs before first paint."""
    client.post("/settings", json={"layout_width": "full"}, headers=TOKEN)
    resp = client.get("/InkDoc")
    nonce = re.search(r"'nonce-([a-f0-9]+)'", resp.headers["content-security-policy"]).group(1)
    script = re.search(
        rf'<script id="inkdoc-session-token" nonce="{nonce}">(.*?)</script>', resp.text, re.S
    )
    assert script is not None
    assert "document.documentElement.dataset.layoutWidth = 'full';" in script.group(1)
    head_end = resp.text.index("</head>")
    assert resp.text.index("layoutWidth = 'full'") < head_end, "must run before the body is painted"


# ─── Preview text size ────────────────────────────────────────────────────────
# Same path as the layout width: settings.json, then onto <html> before first paint.

def _page_text_size(html: str) -> str | None:
    match = re.search(r"document\.documentElement\.dataset\.textSize = '(\d+)';", html)
    return match.group(1) if match else None


def test_text_size_default_is_100():
    from app.core.engine_manager import PREVIEW_TEXT_SIZES

    assert DEFAULT_SETTINGS["preview_text_size"] == 100
    assert PREVIEW_TEXT_SIZES == (85, 90, 100, 110, 120, 135, 150)
    assert client.get("/settings").json()["preview_text_size"] == 100
    assert _page_text_size(client.get("/InkDoc").text) == "100"


def test_each_text_size_is_saved_and_served():
    from app.core.engine_manager import PREVIEW_TEXT_SIZES

    mgr = EngineManager.get_instance()
    for size in PREVIEW_TEXT_SIZES:
        resp = client.post("/settings", json={"preview_text_size": size}, headers=TOKEN)
        assert resp.status_code == 200
        assert client.get("/settings").json()["preview_text_size"] == size
        assert _page_text_size(client.get("/InkDoc").text) == str(size)
        stored = json.loads(mgr.get_settings_file_path().read_text(encoding="utf-8"))
        assert stored["preview_text_size"] == size


def test_text_size_off_the_steps_is_refused():
    for bad in [105, 0, 1, 200, -100, "110", True, 110.5, "x'; alert(1); '"]:
        resp = client.post("/settings", json={"preview_text_size": bad}, headers=TOKEN)
        assert resp.status_code == 422, bad
    assert EngineManager.get_instance().get_settings()["preview_text_size"] == 100


def test_text_size_needs_the_session_token():
    assert client.post("/settings", json={"preview_text_size": 150}).status_code == 403
    assert EngineManager.get_instance().get_settings()["preview_text_size"] == 100


def test_hand_edited_text_size_falls_back_to_100():
    mgr = EngineManager.get_instance()
    for stored in [105, "120", True, None, 1.5, "x'; alert(1); '", [110]]:
        mgr.update_settings({"preview_text_size": stored})
        html = client.get("/InkDoc").text
        assert _page_text_size(html) == "100", stored
        assert "alert(1)" not in html


def test_width_and_text_size_are_independent():
    client.post("/settings", json={"layout_width": "full", "preview_text_size": 135}, headers=TOKEN)
    client.post("/settings", json={"preview_text_size": 90}, headers=TOKEN)
    html = client.get("/InkDoc").text
    assert _page_width(html) == "full"
    assert _page_text_size(html) == "90"


# ─── Math formulas (built-in add-on) ─────────────────────────────────────────
# render_math: on unless explicitly switched off; set on <html> before first
# paint so a preview can never be parsed with the wrong setting.

def _page_render_math(html: str) -> str | None:
    match = re.search(r"document\.documentElement\.dataset\.renderMath = '(on|off)';", html)
    return match.group(1) if match else None


def test_render_math_is_on_by_default():
    assert DEFAULT_SETTINGS["render_math"] is True
    assert client.get("/settings").json()["render_math"] is True
    assert _page_render_math(client.get("/InkDoc").text) == "on"


def test_render_math_off_and_on_again():
    mgr = EngineManager.get_instance()
    for value, page in [(False, "off"), (True, "on")]:
        resp = client.post("/settings", json={"render_math": value}, headers=TOKEN)
        assert resp.status_code == 200
        assert client.get("/settings").json()["render_math"] is value
        assert _page_render_math(client.get("/InkDoc").text) == page
        stored = json.loads(mgr.get_settings_file_path().read_text(encoding="utf-8"))
        assert stored["render_math"] is value


def test_render_math_refuses_non_booleans():
    for bad in ["maybe", 2, [True], {"on": True}, "off'; alert(1); '"]:
        resp = client.post("/settings", json={"render_math": bad}, headers=TOKEN)
        assert resp.status_code == 422, bad
    assert EngineManager.get_instance().get_settings()["render_math"] is True


def test_render_math_needs_the_session_token():
    assert client.post("/settings", json={"render_math": False}).status_code == 403
    assert EngineManager.get_instance().get_settings()["render_math"] is True


def test_hand_edited_render_math_is_on_unless_false():
    """Only an explicit false turns it off; nothing stored is ever written into the page."""
    mgr = EngineManager.get_instance()
    for stored, page in [(None, "on"), ("false", "on"), (0, "on"), ("off'; alert(1); '", "on"), (False, "off")]:
        mgr.update_settings({"render_math": stored})
        html = client.get("/InkDoc").text
        assert _page_render_math(html) == page, stored
        assert "alert(1)" not in html


def test_page_policy_unchanged_by_math():
    """KaTeX comes from /static under the existing policy: no eval, no other origins."""
    csp = client.get("/InkDoc").headers["content-security-policy"]
    assert "script-src 'self' 'nonce-" in csp and "unsafe-eval" not in csp
    assert "font-src 'self' https://fonts.gstatic.com data:;" in csp
    assert "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;" in csp


def test_katex_is_served_from_static():
    for path in ["/static/vendor/katex/katex.min.js", "/static/vendor/katex/katex.min.css",
                 "/static/vendor/katex/fonts/KaTeX_Main-Regular.woff2"]:
        assert client.get(path).status_code == 200, path
