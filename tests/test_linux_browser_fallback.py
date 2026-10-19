"""Linux without WebKit2GTK: InkDoc opens its UI in the browser instead of failing silently."""
from __future__ import annotations

import sys
import types

import pytest

from app.desktop import runner


def _fake_gi(monkeypatch, missing: set[str]):
    gi = types.ModuleType("gi")

    def require_version(namespace, version):
        if (namespace, version) in missing or namespace in missing:
            raise ValueError(f"Namespace {namespace} not available for version {version}")

    gi.require_version = require_version
    repository = types.ModuleType("gi.repository")
    repository.Gtk = object()
    repository.WebKit2 = object()
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)


def test_no_problem_when_gtk_and_webkit_load(monkeypatch):
    _fake_gi(monkeypatch, missing=set())
    assert runner.linux_webview_problem() is None


def test_webkit_4_0_is_accepted_when_4_1_is_missing(monkeypatch):
    _fake_gi(monkeypatch, missing={("WebKit2", "4.1")})
    assert runner.linux_webview_problem() is None


def test_missing_webkit_is_reported(monkeypatch):
    _fake_gi(monkeypatch, missing={"WebKit2"})
    problem = runner.linux_webview_problem()
    assert problem and "WebKit2" in problem


def test_no_gi_at_all_is_reported(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)  # import gi -> ImportError
    problem = runner.linux_webview_problem()
    assert problem and "gi" in problem  # ModuleNotFoundError: import of gi halted


def test_run_desktop_takes_the_browser_path_in_the_linux_release(monkeypatch, tmp_path):
    monkeypatch.setenv("INKDOC_ENGINES_DIR", str(tmp_path / "engines"))
    monkeypatch.setattr(runner, "is_port_in_use", lambda port, host="127.0.0.1": True)
    monkeypatch.setattr(runner, "verify_inkdoc_server", lambda host, port: True)  # attach: no server thread
    monkeypatch.setattr(runner, "is_frozen_linux", lambda: True)
    monkeypatch.setattr(runner, "linux_webview_problem", lambda: "ValueError: Namespace WebKit2 not available")
    calls = []
    monkeypatch.setattr(runner, "run_in_browser", lambda url, server, problem: calls.append((url, server, problem)) or 0)
    assert runner.run_desktop(port=13987) == 0
    assert calls == [("http://127.0.0.1:13987/InkDoc", None, "ValueError: Namespace WebKit2 not available")]


def test_source_runs_never_take_the_browser_path(monkeypatch, tmp_path):
    # A checkout may use pywebview's Qt backend; only the release checks for GTK/WebKit2.
    monkeypatch.setenv("INKDOC_ENGINES_DIR", str(tmp_path / "engines"))
    monkeypatch.setattr(runner, "is_port_in_use", lambda port, host="127.0.0.1": True)
    monkeypatch.setattr(runner, "verify_inkdoc_server", lambda host, port: True)
    monkeypatch.setattr(runner, "is_frozen_linux", lambda: False)
    monkeypatch.setattr(runner, "linux_webview_problem", lambda: pytest.fail("checked outside the release"))

    class WindowReached(Exception):
        pass

    def create_window(**kwargs):
        raise WindowReached

    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace(create_window=create_window))
    with pytest.raises(WindowReached):
        runner.run_desktop(port=13988)


def test_links_in_the_linux_release_open_with_the_host_environment(monkeypatch):
    opened = []
    monkeypatch.setattr(runner, "is_frozen_linux", lambda: True)
    monkeypatch.setattr(runner, "open_on_host", lambda args: opened.append(list(args)))
    monkeypatch.setattr(runner.webbrowser, "open", lambda url: pytest.fail("webbrowser.open in the Linux release"))
    controls = runner.WindowControls()
    assert controls.open_external_link("https://example.com/a?b=c") is True
    assert opened == [["xdg-open", "https://example.com/a?b=c"]]
    assert controls.open_external_link("file:///etc/passwd") is False
    assert opened == [["xdg-open", "https://example.com/a?b=c"]]


@pytest.fixture
def opened(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(runner, "_open_for_user", lambda args: calls.append(list(args)) or True)
    monkeypatch.setattr(runner.webbrowser, "open", lambda url: calls.append(["webbrowser", url]))
    monkeypatch.setenv("INKDOC_DESKTOP_RUNNER", "1")
    manager = types.SimpleNamespace(set_desktop_runner=lambda value: calls.append(["runner", str(value)]))
    monkeypatch.setattr("app.server.server.get_update_manager", lambda: manager)
    return calls


def test_attached_to_another_instance_opens_the_browser_and_returns(opened):
    url = "http://127.0.0.1:13118/InkDoc"
    assert runner.run_in_browser(url, None, "ValueError: Namespace WebKit2 not available") == 0
    assert ["xdg-open", url] in opened
    assert opened[0] == ["runner", "False"]
    assert any(call[0] == "notify-send" for call in opened)
    import os

    assert os.environ["INKDOC_DESKTOP_RUNNER"] == "0"


def test_own_server_keeps_running_until_it_stops(opened, monkeypatch):
    stopped = []
    server = types.SimpleNamespace(is_alive=iter([True, False]).__next__, stop=lambda: stopped.append("server"))
    monkeypatch.setattr(runner, "stop_engine_processes", lambda: stopped.append("engines"))
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    assert runner.run_in_browser("http://127.0.0.1:5/InkDoc", server, "problem") == 0
    assert stopped == ["server", "engines"]


def test_no_xdg_open_falls_back_to_webbrowser(monkeypatch, opened):
    monkeypatch.setattr(runner, "_open_for_user", lambda args: False)
    runner.run_in_browser("http://127.0.0.1:7/InkDoc", None, "problem")
    assert ["webbrowser", "http://127.0.0.1:7/InkDoc"] in opened
