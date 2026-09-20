#!/usr/bin/env python3
"""Smoke test packaged InkDoc release artifacts as shipped.

Supports:
- Windows installer (inkdoc-setup.exe): silent install to temp dir, launch installed exe
- Windows onefile (inkdoc.exe): launch directly
- Windows zip (inkdoc-windows.zip): extract and launch unpacked exe
- macOS zip (inkdoc-macos.zip): extract and launch unpacked app/binary
- Linux zip (inkdoc-linux.zip): extract and launch unpacked binary

Verification steps:
1. Start in --headless mode on an ephemeral port
2. Poll /health for up to 60s, assert HTTP 200
3. Assert reported version matches expected tag/version
4. Convert a small test document through /convert/file
5. Kill the entire process tree cleanly
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


def get_free_port() -> int:
    """Find a random available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def kill_process_tree(pid: int) -> None:
    """Terminate the process and all its descendants."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(0.5)
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass


def find_executable(search_dir: Path) -> Path:
    """Find the main inkdoc binary inside an extracted directory."""
    if sys.platform == "win32":
        matches = list(search_dir.rglob("inkdoc.exe"))
        if not matches:
            raise FileNotFoundError(f"Could not find inkdoc.exe in {search_dir}")
        return matches[0]

    # macOS or Linux
    matches = [
        p for p in search_dir.rglob("inkdoc")
        if p.is_file() and not p.name.endswith(".py") and not p.name.endswith(".sh")
    ]
    if not matches:
        raise FileNotFoundError(f"Could not find inkdoc executable in {search_dir}")
    exe = matches[0]
    exe.chmod(exe.stat().st_mode | 0o755)
    return exe


def prepare_target_executable(artifact_path: Path, artifact_type: str, temp_dir: Path) -> Path:
    """Prepare executable path based on artifact type."""
    if artifact_type == "onefile":
        if not artifact_path.is_file():
            raise FileNotFoundError(f"Onefile artifact does not exist: {artifact_path}")
        return artifact_path

    if artifact_type == "zip":
        extract_dir = temp_dir / "unpacked"
        extract_dir.mkdir(parents=True, exist_ok=True)
        print(f"[*] Unpacking {artifact_path.name} to {extract_dir} ...")
        with zipfile.ZipFile(artifact_path, "r") as z:
            z.extractall(extract_dir)
        return find_executable(extract_dir)

    if artifact_type == "installer":
        if sys.platform != "win32":
            raise RuntimeError("Installer smoke testing is only supported on Windows runners.")
        install_dir = temp_dir / "installed"
        install_dir.mkdir(parents=True, exist_ok=True)
        print(f"[*] Running silent Inno Setup installation to {install_dir} ...")
        cmd = [
            str(artifact_path),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f"/DIR={install_dir}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(
                f"Inno Setup installer failed with code {res.returncode}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
            )
        return find_executable(install_dir)

    raise ValueError(f"Unknown artifact type '{artifact_type}'. Expected: onefile, zip, or installer.")


def send_sample_conversion(port: int) -> dict:
    """Test file conversion route via multipart upload."""
    boundary = "----SmokeTestBoundary123456789"
    content = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="sample.txt"\r\n'
        f"Content-Type: text/plain\r\n\r\n"
        f"# Sample Heading\nSmoke test body content for InkDoc verification.\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    url = f"http://127.0.0.1:{port}/convert/file?save_to_downloads=false&response_format=json"
    req = urllib.request.Request(
        url,
        data=content,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        if resp.getcode() != 200:
            raise RuntimeError(f"/convert/file returned status {resp.getcode()}")
        return json.loads(resp.read().decode("utf-8"))


def run_smoke_test(
    artifact_path: Path,
    artifact_type: str,
    expected_version: str,
    timeout: float = 60.0,
    log_dir: Path | None = None,
) -> None:
    """Execute end-to-end smoke test on the specified artifact."""
    if not artifact_path.is_file():
        raise FileNotFoundError(f"Target artifact does not exist: {artifact_path}")

    log_dir = log_dir or (Path.cwd() / "smoke-logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{artifact_path.stem}.smoke.log"

    print(f"\n{'='*70}")
    print(f"SMOKE TEST: {artifact_path.name}")
    print(f"Type: {artifact_type} | Expected Version: {expected_version}")
    print(f"Log:  {log_file}")
    print(f"{'='*70}")

    with tempfile.TemporaryDirectory(prefix="inkdoc-smoke-") as tmp:
        temp_dir = Path(tmp)
        exe_path = prepare_target_executable(artifact_path, artifact_type, temp_dir)
        print(f"[*] Prepared executable: {exe_path}")

        port = get_free_port()
        print(f"[*] Selected ephemeral port: {port}")

        with open(log_file, "w", encoding="utf-8", errors="replace") as log_f:
            kwargs: dict = {
                "stdout": log_f,
                "stderr": subprocess.STDOUT,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True

            cmd = [str(exe_path), "--headless", "--port", str(port)]
            print(f"[*] Launching: {' '.join(cmd)}")
            proc = subprocess.Popen(cmd, **kwargs)

            start_time = time.time()
            health_ok = False
            reported_version = None

            try:
                # 1. Poll /health
                print(f"[*] Polling http://127.0.0.1:{port}/health (timeout={timeout}s) ...")
                while (time.time() - start_time) < timeout:
                    # Check if process terminated unexpectedly
                    if proc.poll() is not None:
                        raise RuntimeError(f"Process exited prematurely with code {proc.returncode} before /health was ready")

                    try:
                        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
                        with urllib.request.urlopen(req, timeout=2.0) as resp:
                            if resp.getcode() == 200:
                                data = json.loads(resp.read().decode("utf-8"))
                                reported_version = data.get("version")
                                if data.get("status") == "ok":
                                    health_ok = True
                                    print(f"[PASS] /health responded HTTP 200 OK (version='{reported_version}')")
                                    break
                    except (urllib.error.URLError, ConnectionResetError, OSError):
                        pass

                    time.sleep(0.5)

                if not health_ok:
                    raise TimeoutError(f"Timed out after {timeout}s waiting for /health to become available.")

                # 2. Verify reported version
                if reported_version != expected_version:
                    raise AssertionError(
                        f"Version mismatch in /health: expected '{expected_version}', got '{reported_version}'"
                    )
                print(f"[PASS] Version verification passed: '{reported_version}' == '{expected_version}'")

                # 3. Test document conversion
                print("[*] Testing sample document conversion via /convert/file ...")
                conv_result = send_sample_conversion(port)
                if not conv_result.get("success"):
                    raise RuntimeError(f"Document conversion failed: {conv_result}")
                markdown = conv_result.get("markdown", "")
                if "Sample Heading" not in markdown:
                    raise AssertionError(f"Expected converted content in markdown output, got: {markdown[:200]}")
                print(f"[PASS] Sample document converted successfully ({len(markdown)} bytes markdown).")

            finally:
                print(f"[*] Terminating process tree for PID {proc.pid} ...")
                kill_process_tree(proc.pid)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

    print(f"[ALL PASS] Smoke test succeeded for {artifact_path.name}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test shipped InkDoc artifacts.")
    parser.add_argument("--artifact", type=Path, required=True, help="Path to artifact to test.")
    parser.add_argument(
        "--type",
        choices=["installer", "onefile", "zip"],
        required=True,
        help="Artifact type.",
    )
    parser.add_argument(
        "--expected-version",
        required=True,
        help="Expected version string reported by /health.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Timeout in seconds to wait for /health.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Directory to store process output logs.",
    )

    args = parser.parse_args()

    try:
        run_smoke_test(
            artifact_path=args.artifact,
            artifact_type=args.type,
            expected_version=args.expected_version,
            timeout=args.timeout,
            log_dir=args.log_dir,
        )
        return 0
    except Exception as err:
        print(f"\n[FAIL] Smoke test failed: {err}", file=sys.stderr)
        log_dir = args.log_dir or (Path.cwd() / "smoke-logs")
        log_file = log_dir / f"{args.artifact.stem}.smoke.log"
        if log_file.is_file():
            print(f"\n--- Process Log ({log_file}) ---", file=sys.stderr)
            print(log_file.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
