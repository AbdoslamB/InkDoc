"""Desktop runner for InkDoc.

Launches the embedded FastAPI server in a background daemon thread
and presents the unified web interface inside a native desktop window.
"""
from __future__ import annotations

import argparse
import io
import os
import socket
import sys
import threading
import time
import webbrowser
from urllib.parse import urlparse

# Configure thread pool limits before any native scientific libraries load.
# This prevents OpenBLAS "Memory allocation still failed after 10 retries" crashes on Windows.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

# In Windows GUI mode (e.g. PyInstaller --windowed / pythonw), standard streams
# (stdout, stderr, stdin) are None. Provide safe fallbacks so stdio writes or
# .isatty() checks in logging or third-party libraries do not crash the application.
if sys.stdout is None:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    except Exception:
        sys.stdout = io.StringIO()

if sys.stderr is None:
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    except Exception:
        sys.stderr = io.StringIO()

if sys.stdin is None:
    try:
        sys.stdin = open(os.devnull, encoding="utf-8", errors="replace")  # noqa: SIM115
    except Exception:
        sys.stdin = io.StringIO()

# Ensure repository root is on sys.path
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import uvicorn

from app.server.server import app


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a network port is actively in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def find_available_port(preferred: int = 13118, host: str = "127.0.0.1") -> int:
    """Return the preferred port if free, otherwise find an available port."""
    if not is_port_in_use(preferred, host):
        return preferred
    # Find an ephemeral free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class EmbeddedServer(threading.Thread):
    """Runs Uvicorn in a daemon thread."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(name="InkDoc-UvicornServer", daemon=True)
        self.host = host
        self.port = port
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def wait_for_server(host: str, port: int, timeout: float = 10.0) -> bool:
    """Wait until the HTTP server is responsive."""
    import urllib.request

    url = f"http://{host}:{port}/health"
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "InkDoc/HealthCheck"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.08)
    return False


def verify_inkdoc_server(host: str, port: int) -> bool:
    """Verify that a service listening on host:port is genuinely InkDoc."""
    import json
    import urllib.request

    url = f"http://{host}:{port}/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "InkDoc/ServerVerification"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return (
                    isinstance(data, dict)
                    and data.get("status") == "ok"
                    and "markitdown_version" in data
                )
    except Exception:
        pass
    return False


# A custom title bar replaces the native one only on Windows.
#
# frameless=True drops WS_CAPTION, which is the point, but it also drops
# WS_THICKFRAME and WS_MINIMIZEBOX -- measured, not assumed -- leaving a window that
# cannot be resized by its edges and cannot be minimised. Both are restored once the
# window is shown; a window may carry a sizing border without a caption.
#
# macOS and Linux keep their native frames. Their window managers own decoration
# behaviour in ways this has not been tested against, and shipping a window nobody
# can resize would be a far worse bug than a title bar that does not match.
USE_CUSTOM_TITLEBAR = sys.platform == "win32"

_WS_THICKFRAME = 0x00040000
_WS_MINIMIZEBOX = 0x00020000
_GWL_STYLE = -16
_SWP_FLAGS = 0x0002 | 0x0001 | 0x0004 | 0x0020  # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED


def _find_window_handle(title: str) -> int:
    """Return the top-level HWND for our window, or 0."""
    if not USE_CUSTOM_TITLEBAR:
        return 0
    try:
        import ctypes

        return ctypes.windll.user32.FindWindowW(None, title) or 0
    except Exception:
        return 0


def _restore_frameless_window_styles(hwnd: int) -> None:
    """Give a frameless Windows window back its sizing border and minimise box."""
    if not USE_CUSTOM_TITLEBAR or not hwnd:
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, _GWL_STYLE)
        user32.SetWindowLongW(hwnd, _GWL_STYLE, style | _WS_THICKFRAME | _WS_MINIMIZEBOX)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, _SWP_FLAGS)
    except Exception as exc:
        print(f"[Warn] Could not restore window styles: {exc}", file=sys.stderr)


# Event handlers handed to .NET must outlive this function: the CLR holds the
# delegate, and letting Python collect the callable behind it would fault from
# inside a WebView2 callback. Same hazard as WindowChrome._proc_ref.
_webview2_handlers: list = []


def _configure_webview2(window, origin: str) -> None:
    """Let the page declare its own non-client regions with the CSS app-region
    property.

    Without this, WebView2's child HWND swallows every mouse message over the
    title bar, which is why dragging runs as a JavaScript mousemove loop calling
    SetWindowPos -- a software drag the shell never sees, so dragging to a screen
    edge or to the top neither snaps nor maximises. With it, an app-region: drag
    area is hit-tested as caption by the host window and the drag becomes a real
    one.

    It also auto-grants the FileReadWrite permission that showDirectoryPicker()
    asks for. Chromium's own "upload N files?" confirmation, the one a
    webkitdirectory input triggers, is drawn outside the page and cannot be
    styled, suppressed or even observed from JavaScript -- WebView2 exposes no
    event for it. The File System Access API is the one folder-picking route
    that does surface as something we can answer, so the page prefers it and
    this grants it, leaving the app free to ask for confirmation in its own
    dialog instead of the browser's. Only requests from our own origin are
    granted.

    Needs WebView2 runtime 123+; on anything older the setting is absent and the
    write simply fails, which is not fatal.

    Threading is the whole difficulty here. pywebview dispatches its events on a
    worker thread, and the WebView2 is an STA COM object owned by the UI thread,
    so *any* call on it from this thread is marshalled back into the UI thread --
    which cannot run, because the window procedure is subclassed in Python and
    the marshalling thread is holding the GIL. That deadlocks the window outright
    (measured: IsHungAppWindow goes true before the page ever paints). Only
    BeginInvoke, which is documented safe from any thread and does not wait, may
    be called from here; everything else happens inside the UI-thread callback.
    """
    if not USE_CUSTOM_TITLEBAR:
        return
    try:
        from Microsoft.Web.WebView2.Core import (
            CoreWebView2PermissionKind,
            CoreWebView2PermissionState,
        )
        from System import Action
        from webview.platforms.winforms import BrowserView
    except Exception as exc:
        print(f"[Warn] WebView2 configuration unavailable: {exc}", file=sys.stderr)
        return

    def apply() -> None:
        # Plain dictionary and attribute lookups -- no .NET call crosses here.
        control = None
        deadline = time.time() + 20.0
        while time.time() < deadline:
            view = BrowserView.instances.get(window.uid)
            control = getattr(getattr(view, "browser", None), "webview", None)
            if control is not None:
                break
            time.sleep(0.2)
        if control is None:
            print("[Warn] WebView2 control never appeared.", file=sys.stderr)
            return

        def on_permission(_sender, args) -> None:
            try:
                if (
                    args.PermissionKind == CoreWebView2PermissionKind.FileReadWrite
                    and str(args.Uri).startswith(origin)
                ):
                    args.State = CoreWebView2PermissionState.Allow
                    args.Handled = True
            except Exception:
                pass

        def configure(core) -> None:
            core.Settings.IsNonClientRegionSupportEnabled = True
            _webview2_handlers.append(on_permission)
            core.PermissionRequested += on_permission

        def on_ui_thread() -> None:
            try:
                core = control.CoreWebView2
                if core is not None:
                    configure(core)
                    return

                def when_ready(_sender, _args) -> None:
                    try:
                        configure(control.CoreWebView2)
                    except Exception as exc:
                        print(f"[Warn] WebView2 configuration: {exc}", file=sys.stderr)

                _webview2_handlers.append(when_ready)
                control.CoreWebView2InitializationCompleted += when_ready
            except Exception as exc:
                print(f"[Warn] WebView2 configuration: {exc}", file=sys.stderr)

        try:
            control.BeginInvoke(Action(on_ui_thread))
        except Exception as exc:
            print(f"[Warn] Could not post to the UI thread: {exc}", file=sys.stderr)

    threading.Thread(target=apply, daemon=True, name="InkDocWebView2Config").start()


def _setup_window_chrome(title: str, controls: WindowControls, window, origin: str) -> None:
    """Restore the sizing border, then hand the window to the native chrome
    integration so Snap Layouts and the caption buttons behave like any other
    Windows app. Failure here is never fatal: the title bar still works, it just
    loses the shell integration.
    """
    if not USE_CUSTOM_TITLEBAR:
        return
    hwnd = _find_window_handle(title)
    if not hwnd:
        print("[Warn] Could not locate the window to restore resizing.", file=sys.stderr)
        return
    _restore_frameless_window_styles(hwnd)
    controls.attach_chrome(hwnd)
    _configure_webview2(window, origin)


class WindowControls:
    """Exposed to the page as pywebview.api, for the custom title bar buttons."""

    def __init__(self) -> None:
        self._window = None
        self._chrome = None
        self._hwnd = 0

    def attach(self, window) -> None:
        self._window = window

    def attach_chrome(self, hwnd: int) -> None:
        """Subclass the native window so the shell treats our drawn title bar as
        a real one (Snap Layouts, caption-button semantics).
        """
        from app.desktop.win_chrome import WindowChrome

        self._hwnd = hwnd
        chrome = WindowChrome()
        if chrome.install(
            hwnd,
            on_toggle_maximize=self.toggle_maximize_window,
            on_state=self._push_maximized_state,
        ):
            self._chrome = chrome

    def _is_maximized(self) -> bool:
        """Read the real window state. A cached flag goes stale the moment the
        shell maximises us on its own -- via Snap Layouts, Win+Up or aero snap.
        """
        if self._hwnd:
            from app.desktop.win_chrome import is_maximized

            return is_maximized(self._hwnd)
        return False

    def _eval_js(self, script: str) -> None:
        """Run JS without blocking: these are called from the window procedure,
        and evaluate_js there would re-enter the UI thread.
        """
        window = self._window
        if not window:
            return

        def run() -> None:
            try:
                window.evaluate_js(script)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _push_maximized_state(self, maximized: bool) -> None:
        self._eval_js(
            f"window.__inkdocMaximizeState && "
            f"window.__inkdocMaximizeState({'true' if maximized else 'false'})"
        )

    def set_titlebar_button_rect(self, left: float, top: float, right: float, bottom: float) -> bool:
        """Told by the page where it drew the maximise button, in client pixels."""
        if self._chrome:
            self._chrome.set_maximize_button_rect(int(left), int(top), int(right), int(bottom))
        return True

    def set_titlebar_colors(self, bg: int, bg_hover: int, fg: int, fg_hover: int) -> bool:
        """Told by the page which palette the active theme uses, so the natively
        drawn maximise button matches the rest of the title bar.
        """
        if self._chrome:
            self._chrome.set_titlebar_colors(bg, bg_hover, fg, fg_hover)
        return True

    def minimize_window(self) -> bool:
        if self._window:
            self._window.minimize()
        return True

    def toggle_maximize_window(self) -> bool:
        """Return True when the window ended up maximised."""
        if not self._window:
            return False
        if self._is_maximized():
            self._window.restore()
        else:
            self._window.maximize()
        return self._is_maximized()

    def close_window(self) -> bool:
        if self._window:
            self._window.destroy()
        return True

    def open_external_link(self, url: str) -> bool:
        """Open a URL in the user's OS-default browser rather than inside the
        app's own webview, which has no back/forward chrome to escape from.
        Restricted to http(s) since this is reachable from page JavaScript.
        """
        try:
            if urlparse(url).scheme not in ("http", "https"):
                return False
            webbrowser.open(url)
            return True
        except Exception:
            return False


_desktop_window = None


def get_desktop_window():
    return _desktop_window


def close_desktop_window() -> None:
    """Safely destroy desktop window and trigger shutdown."""
    global _desktop_window
    if _desktop_window:
        try:
            _desktop_window.destroy()
        except Exception:
            pass


def run_desktop(
    port: int | None = None,
    headless: bool = False,
) -> int:
    """Entry point to launch the desktop application."""
    global _desktop_window
    host = "127.0.0.1"
    target_port = port if port else find_available_port(13118, host)
    from app.server.server import get_update_manager, set_server_port
    set_server_port(target_port)

    # Set desktop runner environment marker
    if headless:
        os.environ["INKDOC_DESKTOP_RUNNER"] = "0"
    else:
        os.environ["INKDOC_DESKTOP_RUNNER"] = "1"

    # Configure update manager
    update_mgr = get_update_manager()
    update_mgr.set_desktop_runner(not headless)

    server_thread: EmbeddedServer | None = None
    if not is_port_in_use(target_port, host):
        server_thread = EmbeddedServer(host, target_port)
        server_thread.start()
        if not wait_for_server(host, target_port):
            print(f"[Error] Failed to bind internal server to {host}:{target_port}", file=sys.stderr)
            return 1
    else:
        # Port is in use: verify it is actually an InkDoc instance
        if verify_inkdoc_server(host, target_port):
            print(f"[Info] Connected to existing InkDoc server on {host}:{target_port}")
        else:
            # Port is occupied by a foreign/unverified process; fall back to an isolated ephemeral port
            print(
                f"[Warn] Port {target_port} is occupied by an unverified process. Selecting an ephemeral port.",
                file=sys.stderr,
            )
            target_port = find_available_port(0, host)
            set_server_port(target_port)
            server_thread = EmbeddedServer(host, target_port)
            server_thread.start()
            if not wait_for_server(host, target_port):
                print(f"[Error] Failed to bind internal server to {host}:{target_port}", file=sys.stderr)
                return 1

    app_url = f"http://{host}:{target_port}/InkDoc"

    if headless:
        print(f"InkDoc headless server running at {app_url}")
        print("Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            if server_thread:
                server_thread.stop()
        return 0

    # Decision 3: Opt-in daily update check in desktop mode on startup (after 5s delay)
    from app.core.engine_manager import EngineManager
    settings = EngineManager.get_instance().get_settings()
    if settings.get("check_for_updates_daily", False):
        def _daily_check() -> None:
            time.sleep(5.0)
            try:
                get_update_manager().check_for_updates(force=False)
            except Exception:
                pass

        threading.Thread(target=_daily_check, daemon=True, name="InkDocDailyUpdateCheck").start()

    try:
        import webview
    except ImportError:
        print("[Error] pywebview is not installed. Please run: pip install pywebview", file=sys.stderr)
        if server_thread:
            server_thread.stop()
        return 1

    # Locate icon
    icon_path: str | None = None
    candidate_icons: list[str] = []

    # 1. Search frozen PyInstaller bundle directories
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidate_icons.extend([
            os.path.join(exe_dir, "assets", "logo.ico"),
            os.path.join(exe_dir, "assets", "logo.png"),
            os.path.join(exe_dir, "assets", "logo.svg"),
            os.path.join(exe_dir, "_internal", "assets", "logo.ico"),
            os.path.join(exe_dir, "_internal", "assets", "logo.png"),
            os.path.join(exe_dir, "_internal", "assets", "logo.svg"),
        ])
        if hasattr(sys, "_MEIPASS"):
            mei = sys._MEIPASS
            candidate_icons.extend([
                os.path.join(mei, "assets", "logo.ico"),
                os.path.join(mei, "assets", "logo.png"),
                os.path.join(mei, "assets", "logo.svg"),
                os.path.join(mei, "app", "ui", "logo.svg"),
            ])

    # 2. Search local development / source tree directories
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    candidate_icons.extend([
        os.path.join(base_dir, "assets", "logo.ico"),
        os.path.join(base_dir, "assets", "logo.png"),
        os.path.join(base_dir, "assets", "logo.svg"),
        os.path.join(base_dir, "app", "ui", "logo.svg"),
    ])

    for p in candidate_icons:
        if os.path.exists(p):
            # On Windows, pywebview WinForms only accepts .ico files for window icon.
            # Passing .svg or unsupported formats will throw ArgumentException in System.Drawing.Icon.
            if sys.platform == "win32" and not p.lower().endswith(".ico"):
                continue
            icon_path = p
            break

    window_title = "InkDoc"
    window_controls = WindowControls()
    window = webview.create_window(
        title=window_title,
        url=app_url,
        width=1240,
        height=820,
        min_size=(960, 640),
        background_color="#101614",  # Matches dark theme paper color to avoid white flash
        text_select=True,
        frameless=USE_CUSTOM_TITLEBAR,
        # Only elements marked .pywebview-drag-region move the window. easy_drag would
        # make the whole surface draggable, so selecting text or dragging a file onto
        # the drop zone would move the window instead.
        easy_drag=False,
        # Exposed on every platform, not just the frameless custom titlebar:
        # open_external_link() is also used for regular in-page links.
        js_api=window_controls,
    )
    window_controls.attach(window)
    _desktop_window = window

    if USE_CUSTOM_TITLEBAR:
        window.events.shown += lambda: _setup_window_chrome(
            window_title, window_controls, window, f"http://{host}:{target_port}"
        )

    def on_closed():
        global _desktop_window
        _desktop_window = None
        if server_thread:
            server_thread.stop()

    window.events.closed += on_closed

    try:
        # On Windows, Edge WebView2 is the default modern renderer
        webview.start(debug=False, icon=icon_path)
    finally:
        _desktop_window = None
        if server_thread:
            server_thread.stop()

    return 0


def run_selftest() -> int:
    """Run headless self-test importing webview and the platform-specific GUI backend."""
    print("[*] Running InkDoc GUI backend self-test...")
    try:
        import webview
        print(f"[PASS] Successfully imported 'webview' (version={getattr(webview, '__version__', 'unknown')})")

        if sys.platform == "win32":
            import webview.platforms.winforms as backend
            print(f"[PASS] Successfully imported Windows GUI backend: {backend.__name__}")
        elif sys.platform == "darwin":
            import webview.platforms.cocoa as backend
            print(f"[PASS] Successfully imported macOS GUI backend: {backend.__name__}")
        elif sys.platform.startswith("linux"):
            import gi
            import webview.platforms.gtk as backend
            print(f"[PASS] Successfully imported Linux GI: {gi.__file__}")
            print(f"[PASS] Successfully imported Linux GUI backend: {backend.__name__}")

        print("[ALL PASS] GUI backend self-test succeeded.")
        return 0
    except Exception as e:
        print(f"[FAIL] GUI backend self-test failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="InkDoc Multi-Engine Workbench")
    parser.add_argument("--port", type=int, default=None, help="Port to run the local server on")
    parser.add_argument("--headless", action="store_true", help="Run in headless server mode without window")
    parser.add_argument("--selftest", action="store_true", help="Run self-test importing GUI backend and exit 0")

    args = parser.parse_args()
    if args.selftest:
        return run_selftest()
    return run_desktop(port=args.port, headless=args.headless)


if __name__ == "__main__":
    sys.exit(main())
