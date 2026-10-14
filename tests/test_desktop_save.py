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
    controls.attach(window, APP_URL)
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
    controls.attach(window)
    assert controls.save_markdown("a.pdf", CONTENT)["status"] == "error"
    assert window.dialog_calls == []


def test_no_window_attached():
    assert WindowControls().save_markdown("a.pdf", CONTENT)["status"] == "error"


def test_default_save_dir_is_downloads(monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setattr(runner, "get_downloads_dir", lambda: tmp_path / "Downloads")
    assert runner._default_save_dir() == tmp_path / "Downloads"


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
