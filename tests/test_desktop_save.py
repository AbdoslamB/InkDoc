"""The desktop Download button saves through a native Save dialog (issue #6).

pywebview cancels <a download> in the desktop window, so WindowControls.save_markdown
asks the OS for a location and writes the file itself. These tests drive it with a
fake window: no GUI, and the real Downloads folder is never touched.
"""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

from app.desktop import runner
from app.desktop.runner import WindowControls

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_URL = "http://127.0.0.1:13118/InkDoc"
CONTENT = "# تقرير\n\nLine one 📄\nLine two\n"


class FakeWindow:
    def __init__(self, result, gui="webview.platforms.winforms", confirm=True, url=APP_URL):
        self.result = result
        self.url = url
        self.gui = types.ModuleType(gui)
        self.confirm = confirm
        self.dialog_calls = []
        self.confirm_calls = []

    def create_file_dialog(self, dialog_type, **kwargs):
        self.dialog_calls.append((dialog_type, kwargs))
        result = self.result
        return result(kwargs) if callable(result) else result

    def get_current_url(self):
        return self.url

    def create_confirmation_dialog(self, title, message):
        self.confirm_calls.append((title, message))
        return self.confirm


@pytest.fixture(autouse=True)
def _sandboxed_save_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_default_save_dir", lambda: tmp_path)


def _save(window, filename="report.pdf", content=CONTENT):
    controls = WindowControls()
    controls._attach(window, APP_URL)
    return controls.save_markdown(filename, content)


@pytest.mark.parametrize("shape", [lambda p: (p,), lambda p: p], ids=["tuple", "macos-str"])
def test_saves_exact_bytes(tmp_path, shape):
    target = tmp_path / "out.md"
    window = FakeWindow(shape(str(target)))

    assert _save(window) == {"status": "saved", "path": str(target)}
    # UTF-8, LF kept as is (no CRLF on Windows).
    assert target.read_bytes() == CONTENT.encode("utf-8")


def test_dialog_opens_in_default_dir_with_suggested_name(tmp_path):
    import webview

    window = FakeWindow(None)
    _save(window, filename="Quarterly report.pdf")

    [(dialog_type, kwargs)] = window.dialog_calls
    assert dialog_type == webview.FileDialog.SAVE
    assert kwargs["directory"] == str(tmp_path)
    assert kwargs["save_filename"] == "Quarterly report.md"
    assert kwargs["file_types"] == ("Markdown files (*.md)",)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("../../x.pdf", "x.md"),
        ("..\\..\\x.pdf", "x.md"),
        ("a:b*?.pdf", "a_b__.md"),
        ("", "untitled.md"),
        ("...", "untitled.md"),
    ],
)
def test_suggested_name_is_sanitised(filename, expected):
    window = FakeWindow(None)
    _save(window, filename=filename)
    assert window.dialog_calls[0][1]["save_filename"] == expected


@pytest.mark.parametrize("result", [None, (), ("",), ""], ids=["none", "empty-tuple", "empty-path", "empty-str"])
def test_cancel_writes_nothing(tmp_path, result):
    assert _save(FakeWindow(result)) == {"status": "cancelled"}
    assert list(tmp_path.iterdir()) == []


def test_missing_extension_gets_md(tmp_path):
    res = _save(FakeWindow((str(tmp_path / "notes"),)))
    assert res == {"status": "saved", "path": str(tmp_path / "notes.md")}
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == CONTENT


def test_trailing_dot_does_not_double(tmp_path):
    res = _save(FakeWindow((str(tmp_path / "notes."),)))
    assert res["path"] == str(tmp_path / "notes.md")


def test_lone_surrogate_never_empties_the_replaced_file(tmp_path):
    # Encoding happens before the file is opened; the old file is replaced
    # by readable text, as a browser download would write it.
    target = tmp_path / "out.md"
    target.write_text("old", encoding="utf-8")

    res = _save(FakeWindow((str(target),)), content="x\ud800y")

    assert res["status"] == "saved"
    assert target.read_bytes() == "x�y".encode()


def test_added_extension_asks_before_replacing(tmp_path):
    # Qt and macOS return "notes" without checking "notes.md"; the code adds the
    # extension, so it has to ask on every backend.
    (tmp_path / "notes.md").write_text("old", encoding="utf-8")
    window = FakeWindow((str(tmp_path / "notes"),), confirm=False)

    assert _save(window) == {"status": "cancelled"}
    assert len(window.confirm_calls) == 1
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "old"


def test_chosen_extension_is_kept(tmp_path):
    res = _save(FakeWindow((str(tmp_path / "notes.txt"),)))
    assert res["path"] == str(tmp_path / "notes.txt")


@pytest.mark.parametrize("confirm", [True, False])
def test_gtk_asks_before_replacing(tmp_path, confirm):
    target = tmp_path / "out.md"
    target.write_text("old", encoding="utf-8")
    window = FakeWindow((str(target),), gui="webview.platforms.gtk", confirm=confirm)

    res = _save(window)

    assert len(window.confirm_calls) == 1
    if confirm:
        assert res["status"] == "saved"
        assert target.read_bytes() == CONTENT.encode("utf-8")
    else:
        assert res == {"status": "cancelled"}
        assert target.read_text(encoding="utf-8") == "old"


def test_gtk_new_file_does_not_ask(tmp_path):
    window = FakeWindow((str(tmp_path / "new.md"),), gui="webview.platforms.gtk")
    assert _save(window)["status"] == "saved"
    assert window.confirm_calls == []


def test_other_backends_never_ask(tmp_path):
    # Their own dialogs already confirm a replace; asking again would prompt twice.
    target = tmp_path / "out.md"
    target.write_text("old", encoding="utf-8")
    window = FakeWindow((str(target),), gui="webview.platforms.winforms")

    assert _save(window)["status"] == "saved"
    assert window.confirm_calls == []


def test_write_failure_is_reported(tmp_path):
    res = _save(FakeWindow((str(tmp_path / "missing-dir" / "out.md"),)))
    assert res["status"] == "error"
    assert res["message"]


def test_dialog_failure_is_reported():
    def boom(_kwargs):
        raise RuntimeError("dialog failed")

    assert _save(FakeWindow(boom)) == {"status": "error", "message": "dialog failed"}


@pytest.mark.parametrize(("filename", "content"), [(None, CONTENT), ("a.pdf", None), (1, 2)])
def test_bad_arguments_are_refused(filename, content):
    window = FakeWindow(None)
    assert _save(window, filename=filename, content=content)["status"] == "error"
    assert window.dialog_calls == []


@pytest.mark.parametrize(
    "url",
    ["https://evil.example/InkDoc", "http://127.0.0.1:9999/InkDoc", "http://localhost:13118/InkDoc", None, ""],
)
def test_only_inkdoc_page_may_save(url):
    # A link in a converted document can load a foreign page in the window,
    # and pywebview hands that page the same bridge.
    window = FakeWindow(("unused.md",), url=url)
    res = _save(window)
    assert res["status"] == "error"
    assert window.dialog_calls == []


def test_unknown_app_origin_refuses():
    controls = WindowControls()
    window = FakeWindow(("unused.md",))
    controls._attach(window)
    assert controls.save_markdown("a.pdf", CONTENT)["status"] == "error"
    assert window.dialog_calls == []


def test_no_window_attached():
    assert WindowControls().save_markdown("a.pdf", CONTENT)["status"] == "error"


# ─── Save folder (choose_save_folder / reset_save_folder) ─────────────────────


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Real _default_save_dir() over an isolated settings.json and Downloads."""
    from app.core import converter
    from app.core.engine_manager import EngineManager

    monkeypatch.undo()  # drop the autouse _default_save_dir patch
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    mgr = EngineManager.get_instance()
    monkeypatch.setattr(mgr, "get_settings_file_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(mgr, "_settings_cache", None)
    monkeypatch.setattr(converter, "get_downloads_dir", lambda: downloads)
    return mgr, downloads


def _controls(window):
    controls = WindowControls()
    controls._attach(window, APP_URL)
    return controls


def test_default_save_dir_follows_the_setting(settings, tmp_path):
    mgr, downloads = settings
    assert runner._default_save_dir() == downloads
    folder = tmp_path / "Notes"
    folder.mkdir()
    mgr.update_settings({"save_dir": str(folder)})
    assert runner._default_save_dir() == folder
    # The Download button's Save dialog opens there.
    window = FakeWindow(None)
    _save(window)
    assert window.dialog_calls[0][1]["directory"] == str(folder)


def test_choose_save_folder_stores_the_resolved_path(settings, tmp_path):
    import webview

    mgr, downloads = settings
    folder = tmp_path / "Notes"
    folder.mkdir()
    window = FakeWindow((str(folder / "." / ".." / "Notes"),))

    res = _controls(window).choose_save_folder()

    [(dialog_type, kwargs)] = window.dialog_calls
    assert dialog_type == webview.FileDialog.FOLDER
    assert kwargs["directory"] == str(downloads)
    assert res["status"] == "saved" and "warning" not in res
    assert res["folder"] == {"path": str(folder.resolve()), "chosen": str(folder.resolve()),
                             "custom": True, "problem": ""}
    assert mgr.get_settings()["save_dir"] == str(folder.resolve())


def test_choose_save_folder_stores_a_links_target(settings, tmp_path):
    mgr, _ = settings
    target = tmp_path / "Real"
    target.mkdir()
    link = tmp_path / "Shortcut"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")

    res = _controls(FakeWindow((str(link),))).choose_save_folder()

    assert res["status"] == "saved"
    assert mgr.get_settings()["save_dir"] == str(target.resolve())
    assert res["folder"]["problem"] == ""


@pytest.mark.parametrize("result", [None, (), ("",)], ids=["none", "empty-tuple", "qt-cancel"])
def test_choose_save_folder_cancel_changes_nothing(settings, result):
    mgr, _ = settings
    assert _controls(FakeWindow(result)).choose_save_folder() == {"status": "cancelled"}
    assert mgr.get_settings()["save_dir"] == ""


def test_choose_save_folder_refuses_unusable(settings, tmp_path, monkeypatch):
    mgr, _ = settings
    folder = tmp_path / "Locked"
    folder.mkdir()
    monkeypatch.setattr(runner, "check_usable", lambda path: "InkDoc can't write to that folder.")

    res = _controls(FakeWindow((str(folder),))).choose_save_folder()

    assert res == {"status": "error", "message": "InkDoc can't write to that folder."}
    assert mgr.get_settings()["save_dir"] == ""


def test_choose_save_folder_warns_when_not_kept(settings, tmp_path, monkeypatch):
    folder = tmp_path / "Notes"
    folder.mkdir()
    monkeypatch.setattr(runner, "store_save_dir", lambda value: False)
    res = _controls(FakeWindow((str(folder),))).choose_save_folder()
    assert res["status"] == "saved" and "couldn't be saved for next time" in res["warning"]


@pytest.mark.parametrize("method", ["choose_save_folder", "reset_save_folder"])
def test_save_folder_methods_refuse_foreign_pages(settings, method):
    mgr, _ = settings
    mgr.update_settings({"save_dir": "/kept"})
    window = FakeWindow(("/elsewhere",), url="https://evil.example/")

    res = getattr(_controls(window), method)()

    assert res["status"] == "error"
    assert window.dialog_calls == []
    assert mgr.get_settings()["save_dir"] == "/kept"


def test_reset_save_folder(settings, tmp_path):
    mgr, downloads = settings
    mgr.update_settings({"save_dir": str(tmp_path)})
    res = _controls(FakeWindow(None)).reset_save_folder()
    assert res["status"] == "saved"
    assert res["folder"]["path"] == str(downloads) and res["folder"]["custom"] is False
    assert mgr.get_settings()["save_dir"] == ""


@pytest.mark.parametrize("method", ["choose_save_folder", "reset_save_folder"])
def test_save_folder_refused_when_attached_to_another_instance(settings, method):
    # A second window that joined a running InkDoc would store a folder the
    # serving process never reads.
    mgr, _ = settings
    window = FakeWindow(("/elsewhere",))
    controls = WindowControls()
    controls._attach(window, APP_URL, owns_server=False)

    res = getattr(controls, method)()

    assert res["status"] == "error" and "another window" in res["message"]
    assert window.dialog_calls == []
    assert mgr.get_settings()["save_dir"] == ""


def test_attach_is_not_exposed_to_the_page():
    # pywebview exposes public methods to JavaScript; _attach holds the trusted origin.
    assert not hasattr(WindowControls, "attach")


def test_save_folder_methods_without_window():
    assert WindowControls().choose_save_folder()["status"] == "error"
    assert WindowControls().reset_save_folder()["status"] == "error"


def test_runner_import_does_not_load_webview():
    # webview is imported only when a window opens or a dialog is shown.
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import app.desktop.runner\n"
        "print('webview' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "False"


# ─── What the page can call ──────────────────────────────────────────────────

PAGE_NAMES = {
    "open_external_link", "toggle_maximize_window", "set_titlebar_button_rect",
    "set_titlebar_colors", "minimize_window", "close_window", "save_markdown",
    "choose_save_folder", "reset_save_folder",
}


class _Recorder:
    """Stands in for WindowControls and records every method that is reached."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append(name)
            return True
        return record


def test_page_functions_are_exactly_what_app_js_uses():
    source = (REPO_ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")
    import re

    used = set(re.findall(r"api\.(\w+)\(", source))
    used |= set(re.findall(r'changeSaveFolder\("(\w+)"\)', source))
    exposed = {f.__name__ for f in runner._page_functions(_Recorder())}
    assert exposed == used == PAGE_NAMES


def test_window_uses_expose_not_js_api():
    source = (REPO_ROOT / "app" / "desktop" / "runner.py").read_text(encoding="utf-8")
    assert "js_api=" not in source
    assert "window.expose(*_page_functions(window_controls))" in source


def _dispatch(names):
    """Send each name through pywebview's real bridge dispatcher, as page script would."""
    import time

    from webview.util import js_bridge_call

    recorder = _Recorder()
    bridge = type("Bridge", (), {})()
    bridge._functions = {f.__name__: f for f in runner._page_functions(recorder)}
    bridge._js_api = None  # as in run_desktop(): nothing is passed as js_api
    bridge._callbacks = {}
    bridge.replies = []
    bridge.evaluate_js = bridge.replies.append
    for name in names:
        js_bridge_call(bridge, name, [], "id")
    time.sleep(0.5)  # pywebview runs each call on its own thread
    return recorder.calls, bridge.replies


def test_public_names_still_reach_the_controls():
    calls, replies = _dispatch(["minimize_window", "choose_save_folder"])
    assert calls == ["minimize_window", "choose_save_folder"]
    assert len(replies) == 2


@pytest.mark.parametrize("name", [
    "_stored", "_attach", "attach_chrome", "_save_folder_refusal", "_on_app_page",
    "save_markdown.__self__", "save_markdown.__closure__", "minimize_window.__globals__",
    "nonexistent",
])
def test_other_names_reach_nothing(name):
    calls, replies = _dispatch([name])
    assert calls == [] and replies == []


def test_builtin_names_reach_no_inkdoc_code():
    # With no js_api, pywebview's fallback lookup runs on None, so a name such
    # as "__class__" resolves to Python's own NoneType; nothing of InkDoc's runs.
    calls, _ = _dispatch(["__class__", "__init_subclass__"])
    assert calls == []


def test_real_pywebview_window_gets_only_the_exposed_functions():
    # A Window object opens nothing until webview.start(), so this needs no GUI.
    import webview

    window = webview.create_window("test", url="http://127.0.0.1:1/InkDoc")
    try:
        window.expose(*runner._page_functions(runner.WindowControls()))
        assert window._js_api is None
        assert set(window._functions) == PAGE_NAMES
    finally:
        webview.windows.remove(window)
