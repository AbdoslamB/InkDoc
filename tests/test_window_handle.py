"""The custom title bar is installed on this process's own window.

Every InkDoc window is titled "InkDoc", so a lookup by title alone can pick
another InkDoc's window: a second copy, or a window attached to this one's
server. The chrome must go on the window this process owns.
"""
from __future__ import annotations

import os
import sys

import pytest

from app.desktop import runner

ME = 4242
OTHER = 5151


def test_another_processs_window_on_top_is_skipped():
    windows = [
        (0x100, OTHER, True, "InkDoc"),  # another InkDoc, higher in z-order
        (0x200, ME, True, "InkDoc"),
    ]
    assert runner._pick_own_window(windows, "InkDoc", ME) == 0x200


def test_only_a_visible_window_with_the_exact_title_counts():
    windows = [
        (0x100, ME, False, "InkDoc"),  # hidden helper window of this process
        (0x200, ME, True, "InkDoc - draft"),
        (0x300, ME, True, ""),
        (0x400, ME, True, "InkDoc"),
    ]
    assert runner._pick_own_window(windows, "InkDoc", ME) == 0x400


def test_no_own_window_gives_zero():
    assert runner._pick_own_window([(0x100, OTHER, True, "InkDoc")], "InkDoc", ME) == 0
    assert runner._pick_own_window([], "InkDoc", ME) == 0


def test_lookup_uses_this_process_id(monkeypatch):
    monkeypatch.setattr(runner, "USE_CUSTOM_TITLEBAR", True)
    monkeypatch.setattr(runner, "_top_level_windows", lambda: [
        (0x100, os.getpid() + 1, True, "InkDoc"),
        (0x200, os.getpid(), True, "InkDoc"),
    ])
    assert runner._find_window_handle("InkDoc") == 0x200


def test_lookup_never_raises(monkeypatch):
    monkeypatch.setattr(runner, "USE_CUSTOM_TITLEBAR", True)

    def broken():
        raise OSError("EnumWindows failed")

    monkeypatch.setattr(runner, "_top_level_windows", broken)
    assert runner._find_window_handle("InkDoc") == 0


@pytest.mark.skipif(sys.platform != "win32", reason="enumerates real Windows windows")
def test_enumeration_runs_on_windows():
    rows = runner._top_level_windows()
    if not rows:
        pytest.skip("no top-level windows in this session")
    for hwnd, owner, visible, title in rows:
        assert isinstance(hwnd, int) and isinstance(owner, int)
        assert isinstance(visible, bool) and isinstance(title, str)
        if owner != os.getpid():
            assert title == "", "other processes' titles are never read"
    # Nothing of this test process is titled like an InkDoc window.
    assert runner._pick_own_window(rows, "InkDoc", os.getpid()) == 0
