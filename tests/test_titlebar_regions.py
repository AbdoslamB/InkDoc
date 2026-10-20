"""The custom Windows title bar resizes, drags and maximises natively.

The whole header is a caption region (CSS app-region: drag), which WebView2 hands
to Windows: dragging, double-click to maximise and the top resize edge (HTTOP,
which WebView2 answers itself over a caption region) are all the shell's own.
These tests keep it that way: every clickable element in the header opts out of
the region, and neither of the JavaScript stand-ins comes back, since each would
act a second time on top of the native behaviour.
"""
from __future__ import annotations

import re
import types
from html.parser import HTMLParser
from pathlib import Path

from app.desktop import win_chrome

UI = Path(__file__).resolve().parent.parent / "app" / "ui"
CSS = (UI / "style.css").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")
JS = (UI / "app.js").read_text(encoding="utf-8")

# Elements that take their own clicks.
INTERACTIVE = {"a", "button", "input", "label", "select", "summary", "textarea"}
# Classes the no-drag rule names, and the containers it opts out whole.
NO_DRAG_CLASSES = {"btn-icon", "window-control"}
NO_DRAG_CONTAINERS = {"settings-drawer", "settings-scrim"}


def css_rule(selector: str) -> tuple[str, str]:
    """(selector list, body) of the rule that names `selector` and sets app-region."""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)):
        selectors, body = match.group(1), match.group(2)
        names = [s.strip() for s in selectors.split(",")]
        if selector in names and "app-region" in body:
            return selectors, body
    raise AssertionError(f"no app-region rule for {selector}")


def test_whole_header_is_the_caption_region():
    _selectors, body = css_rule(".has-custom-titlebar .app-header")
    assert "app-region: drag" in body
    assert "-webkit-app-region: drag" in body


def test_no_drag_rule_covers_buttons_and_the_settings_drawer():
    selectors, body = css_rule(".has-custom-titlebar .settings-drawer")
    assert "app-region: no-drag" in body
    assert "-webkit-app-region: no-drag" in body
    for name in NO_DRAG_CLASSES | NO_DRAG_CONTAINERS:
        assert f".has-custom-titlebar .{name}" in selectors, name


class _HeaderControls(HTMLParser):
    """Interactive elements inside <header>, outside the no-drag containers."""

    VOID = {"img", "input", "br", "hr", "meta", "link", "source", "use", "path",
            "circle", "line", "rect"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, set[str]]] = []
        self.found: list[tuple[str, str, set[str]]] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        in_header = any(t == "header" for t, _ in self.stack) or tag == "header"
        opted_out = any(c & NO_DRAG_CONTAINERS for _, c in self.stack)
        if in_header and not opted_out and tag in INTERACTIVE:
            self.found.append((tag, attrs.get("id") or "", classes))
        if tag not in self.VOID:
            self.stack.append((tag, classes))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def test_every_header_control_opts_out_of_the_caption():
    parser = _HeaderControls()
    parser.feed(HTML)
    assert parser.found, "header controls not found"
    ids = {element_id for _, element_id, _ in parser.found}
    assert {"themeToggle", "settingsBtn", "winMinimize", "winMaximize", "winClose"} <= ids
    for tag, element_id, classes in parser.found:
        assert classes & NO_DRAG_CLASSES, (
            f"<{tag} id={element_id!r}> sits in the header's caption region; "
            "give it a class the no-drag rule in style.css names"
        )


def test_pywebview_javascript_drag_loop_stays_off():
    # pywebview moves the window from JavaScript on mousedown over this class:
    # alongside the native caption it would drag the window twice.
    assert "pywebview-drag-region" not in HTML
    assert "pywebview-drag-region" not in JS
    assert "pywebview-drag-region" not in CSS


def test_double_click_is_left_to_windows():
    # Windows maximises on a caption double-click; a page handler would undo it.
    # toggleMaximize is declared once and used only by the maximise button.
    assert len(re.findall(r"\btoggleMaximize\b", JS)) == 2
    assert re.search(r"""maximizeBtn\.addEventListener\(\s*["']click["']\s*,\s*toggleMaximize""", JS)
    assert not re.search(r"""["']dblclick["']\s*,\s*(?:toggleMaximize|[^)]*toggle_maximize_window)""", JS)


class _User32:
    """GetDpiForWindow and GetSystemMetricsForDpi, both reporting their arguments."""

    def __init__(self, dpi: int) -> None:
        self.dpi = dpi

    def GetDpiForWindow(self, hwnd):  # noqa: N802 - the Win32 name
        return self.dpi

    def GetSystemMetricsForDpi(self, index, dpi):  # noqa: N802
        return {win_chrome.SM_CYSIZEFRAME: 4, win_chrome.SM_CXPADDEDBORDER: 4,
                win_chrome.SM_CYCAPTION: 23}[index] * dpi // 96

    def GetSystemMetrics(self, index):  # noqa: N802
        raise AssertionError("the system-wide metric was used")


class _OldUser32:
    """Windows before 10 1607: neither per-DPI function exists."""

    def GetSystemMetrics(self, index):  # noqa: N802
        return {win_chrome.SM_CYSIZEFRAME: 4, win_chrome.SM_CXPADDEDBORDER: 4}[index]


def test_frame_thickness_follows_the_window_dpi(monkeypatch):
    for dpi, expected in ((96, 8), (120, 10), (144, 12), (192, 16)):
        monkeypatch.setattr(win_chrome, "user32", _User32(dpi))
        assert win_chrome._window_dpi(1) == dpi
        assert win_chrome._frame_thickness(win_chrome._window_dpi(1)) == expected


def test_dpi_helpers_fall_back_on_older_windows(monkeypatch):
    monkeypatch.setattr(win_chrome, "user32", _OldUser32())
    assert win_chrome._window_dpi(1) == 96
    assert win_chrome._frame_thickness(144) == 8

    monkeypatch.setattr(win_chrome, "user32", types.SimpleNamespace(GetDpiForWindow=lambda hwnd: 0))
    assert win_chrome._window_dpi(1) == 96
