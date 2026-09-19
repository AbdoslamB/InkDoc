"""Desktop runner for InkDoc.

Launches the embedded FastAPI server in a background daemon thread
and presents the unified web interface inside a native desktop window.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time

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


def run_desktop(
    port: int | None = None,
    headless: bool = False,
    url_to_open: str | None = None,
) -> int:
    """Entry point to launch the desktop application."""
    host = "127.0.0.1"
    target_port = port if port else find_available_port(13118, host)

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

    try:
        import webview
    except ImportError:
        print("[Error] pywebview is not installed. Please run: pip install pywebview", file=sys.stderr)
        if server_thread:
            server_thread.stop()
        return 1

    # Locate icon
    icon_path: str | None = None
    candidate_icons = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        mei = sys._MEIPASS
        candidate_icons.extend([
            os.path.join(mei, "assets", "logo.ico"),
            os.path.join(mei, "assets", "logo.svg"),
            os.path.join(mei, "app", "ui", "logo.svg"),
        ])
    candidate_icons.extend([
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets", "logo.ico")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets", "logo.svg")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ui", "logo.svg")),
    ])
    for p in candidate_icons:
        if os.path.exists(p):
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

    def on_closed():
        if server_thread:
            server_thread.stop()

    window.events.closed += on_closed

    try:
        # On Windows, Edge WebView2 is the default modern renderer
        webview.start(debug=False, icon=icon_path)
    finally:
        if server_thread:
            server_thread.stop()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="InkDoc Multi-Engine Workbench")
    parser.add_argument("--port", type=int, default=None, help="Port to run the local server on")
    parser.add_argument("--headless", action="store_true", help="Run in headless server mode without window")
    parser.add_argument("file_or_url", nargs="?", default=None, help="Initial file or URL to open")

    args = parser.parse_args()
    return run_desktop(port=args.port, headless=args.headless, url_to_open=args.file_or_url)


if __name__ == "__main__":
    sys.exit(main())
