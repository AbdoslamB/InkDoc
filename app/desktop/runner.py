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

    window = webview.create_window(
        title="InkDoc",
        url=app_url,
        width=1240,
        height=820,
        min_size=(960, 640),
        background_color="#101614",  # Matches dark theme paper color to avoid white flash
        text_select=True,
    )
    _desktop_window = window

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
            import webview.platforms.gtk as backend
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
