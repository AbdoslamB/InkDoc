"""pywebview's page bridge without eval (app/desktop/webview_bridge.py).

The page's policy forbids eval, and WebKit (the Linux and macOS windows) enforces
that against pywebview's own script. These tests pin what the patch replaces in
pywebview 6.2.1, so an upgrade that changes it fails here rather than leaving the
Linux and macOS windows without the page's functions.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

webview = pytest.importorskip("webview")

from app.desktop import webview_bridge as wb  # noqa: E402
from app.server.server import page_csp  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def _api_js() -> str:
    return (Path(webview.__file__).parent / "js" / "api.js").read_text(encoding="utf-8")


def test_the_requirement_pins_the_patched_version():
    pins = re.findall(r"^pywebview==(\S+)$", (REPO / "requirements.txt").read_text(encoding="utf-8"), re.M)
    assert pins == [wb.PATCHED_VERSION]


def test_the_installed_pywebview_is_the_patched_one():
    assert wb.installed_version() == wb.PATCHED_VERSION


def test_api_js_has_the_one_new_function_the_patch_replaces():
    js = _api_js()
    assert js.count(wb.NEW_FUNCTION) == 1
    assert js.count("new Function") == 1


def test_the_patched_script_has_no_eval_left():
    patched = wb.patch_api_js(_api_js())
    assert "new Function" not in patched
    assert wb.CLOSURE in patched
    assert not re.search(r"\beval\s*\(", patched)
    # The closure calls what the original function body called, by the same names.
    for name in ("window.pywebview._checkValue(", "window.pywebview._jsApiCallback("):
        assert name in _api_js() and name in wb.CLOSURE


def test_a_changed_api_js_is_refused():
    with pytest.raises(ValueError):
        wb.patch_api_js(_api_js().replace(wb.NEW_FUNCTION, "      nestedObject[functionName] = f;"))


def test_call_results_still_return_through_evaluate_js_with_that_prefix():
    from webview import util

    source = inspect.getsource(util.js_bridge_call)
    assert "window.evaluate_js(" in source
    assert "f'" + wb.RETURN_PREFIX + "{func_name}\"]" in source
    # The bridge injection still finds load_js_files in util's globals.
    assert "load_js_files(window, platform)" in inspect.getsource(util.inject_pywebview)


@pytest.fixture
def patched(monkeypatch):
    from webview import util
    from webview.window import Window

    monkeypatch.setattr(util, "load_js_files", util.load_js_files)
    monkeypatch.setattr(Window, "evaluate_js", Window.evaluate_js)
    assert wb.apply() is True
    return util, Window


def test_apply_replaces_the_page_script(patched):
    util, _ = patched

    class FakeWindow:
        uid = "master"
        js_api_endpoint = None
        text_select = False
        zoomable = False
        draggable = False
        easy_drag = False
        frameless = False
        state = {}

    js_code, _finish = util.load_js_files(FakeWindow(), "gtk")
    assert "new Function" not in js_code and wb.CLOSURE in js_code


def test_apply_sends_call_results_without_eval(patched):
    _, Window = patched
    calls = []

    class Stub:
        def run_js(self, script):
            calls.append(script)
            return None

    script = wb.RETURN_PREFIX + 'save_markdown"]["123"]({value: \'"ok"\'})'
    Window.evaluate_js(Stub(), script)
    assert calls == [script]


def test_apply_is_idempotent(patched):
    util, Window = patched
    load, evaluate = util.load_js_files, Window.evaluate_js
    assert wb.apply() is True
    assert (util.load_js_files, Window.evaluate_js) == (load, evaluate)
    # Applied twice (the desktop window and a test, say), the script is still patched once.
    test_apply_replaces_the_page_script(patched)


def test_another_version_is_left_alone(monkeypatch):
    from webview import util
    from webview.window import Window

    monkeypatch.setattr(util, "load_js_files", util.load_js_files)
    monkeypatch.setattr(Window, "evaluate_js", Window.evaluate_js)
    monkeypatch.setattr(wb, "installed_version", lambda: "9.9.9")
    load, evaluate = util.load_js_files, Window.evaluate_js
    assert wb.apply() is False
    assert (util.load_js_files, Window.evaluate_js) == (load, evaluate)


def test_the_page_policy_never_allows_eval():
    policy = page_csp("abc")
    assert "script-src 'self' 'nonce-abc';" in policy
    assert "unsafe-eval" not in policy


def test_the_release_smoke_test_runs_the_bridge_check():
    smoke = (REPO / "scripts" / "smoke_test_artifact.py").read_text(encoding="utf-8")
    assert '"--selftest-bridge"' in smoke
    from app.desktop import runner

    assert "--selftest-bridge" in inspect.getsource(runner.main)
