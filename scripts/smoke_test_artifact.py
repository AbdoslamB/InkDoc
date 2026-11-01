#!/usr/bin/env python3
"""End-to-end smoke test runner for shipped InkDoc artifacts."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

try:  # imported by the tests, from the repo root
    from scripts.build_dmg import copy_app, mounted, verify_signature
except ImportError:  # run as a script: scripts/ is sys.path[0]
    from build_dmg import copy_app, mounted, verify_signature


def get_free_port() -> int:
    """Find a random ephemeral port that is free on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def kill_process_tree(pid: int) -> None:
    """Forcefully terminate a process tree cross-platform."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            import signal

            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(0.5)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def find_executable(base_dir: Path) -> Path:
    """Locate the InkDoc executable inside an extracted directory."""
    if sys.platform == "win32":
        candidates = list(base_dir.rglob("inkdoc.exe"))
        if not candidates:
            raise FileNotFoundError(f"Could not find inkdoc.exe in {base_dir}")
        return candidates[0]

    if sys.platform == "darwin":
        # Check for macOS .app bundle executable (Contents/MacOS/inkdoc)
        app_candidates = list(base_dir.rglob("Contents/MacOS/inkdoc"))
        if app_candidates:
            exe = app_candidates[0]
            exe_str = str(exe).replace("\\", "/")
            if ".app/Contents/MacOS" not in exe_str:
                raise RuntimeError(f"macOS executable path does not contain .app/Contents/MacOS: {exe}")
            exe.chmod(0o755)
            return exe
        raise FileNotFoundError(f"Could not find macOS .app bundle executable (Contents/MacOS/inkdoc) in {base_dir}")

    # Linux
    candidates = list(base_dir.rglob("inkdoc"))
    for c in candidates:
        if c.is_file() and not c.name.endswith(".zip"):
            c.chmod(0o755)
            return c
    raise FileNotFoundError(f"Could not find Linux inkdoc executable in {base_dir}")


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
        if shutil.which("unzip"):
            subprocess.run(["unzip", "-q", str(artifact_path), "-d", str(extract_dir)], check=True)
        else:
            with zipfile.ZipFile(artifact_path, "r") as z:
                z.extractall(extract_dir)
        return find_executable(extract_dir)

    if artifact_type == "dmg":
        if sys.platform != "darwin":
            raise RuntimeError("Disk image smoke testing is only supported on macOS runners.")
        mountpoint = temp_dir / "mnt"
        app_dir = temp_dir / "app"
        app_dir.mkdir(parents=True, exist_ok=True)
        print(f"[*] Mounting {artifact_path.name} at {mountpoint} ...")
        with mounted(artifact_path, mountpoint):
            # What a user does: drag the app out of the image into a folder of theirs.
            copy_app(mountpoint / "inkdoc.app", app_dir / "inkdoc.app")
        verify_signature(app_dir / "inkdoc.app")
        return find_executable(app_dir)

    if artifact_type == "appimage":
        if not sys.platform.startswith("linux"):
            raise RuntimeError("AppImage smoke testing is only supported on Linux runners.")
        target = temp_dir / artifact_path.name
        shutil.copy2(artifact_path, target)
        target.chmod(0o755)  # as a browser download, it arrives without the executable bit
        return target

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

    raise ValueError(
        f"Unknown artifact type '{artifact_type}'. Expected: onefile, zip, dmg, appimage, or installer."
    )


def launch_env(artifact_type: str, use_fuse: bool = False) -> dict[str, str]:
    """The environment the artifact runs with. An AppImage normally mounts itself with FUSE,
    which CI runners and containers may lack; APPIMAGE_EXTRACT_AND_RUN=1 unpacks it instead
    (the same contents, so the same test). --appimage-fuse tests the mount path."""
    env = dict(os.environ)
    if artifact_type == "appimage" and not use_fuse:
        env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
    return env


def xwd_colour_count(data: bytes, step: int = 5) -> int:
    """Distinct pixel values in an X window dump (`xwd -root`), sampling every `step`-th pixel.

    A rendered InkDoc window (antialiased text, icons) has hundreds; an empty display or a
    WebKit view that drew nothing has a handful. ZPixmap with 24 or 32 bits per pixel only.
    """
    import struct

    if len(data) < 100:
        raise ValueError("not an XWD dump")
    fields = struct.unpack(">25I", data[:100])
    header_size, version, pixmap_format = fields[0], fields[1], fields[2]
    height, bits_per_pixel, bytes_per_line, ncolors = fields[5], fields[11], fields[12], fields[19]
    if version != 7 or pixmap_format != 2 or bits_per_pixel not in (24, 32):
        raise ValueError(f"unsupported XWD dump (version {version}, format {pixmap_format}, {bits_per_pixel} bpp)")
    offset, size = header_size + ncolors * 12, bits_per_pixel // 8
    seen = set()
    for row in range(0, height, step):
        line = data[offset + row * bytes_per_line: offset + (row + 1) * bytes_per_line]
        for col in range(0, len(line) - size + 1, size * step):
            seen.add(line[col:col + size])
    return len(seen)


GUI_MIN_COLOURS = 64


def check_window_rendered(artifact_name: str, log_dir: Path) -> None:
    """Fail when the X display shows no rendered window (needs `xwd`, from x11-apps)."""
    if not shutil.which("xwd"):
        record_summary_line(f"[GUI-RENDER] {artifact_name}: skipped (xwd is not installed)")
        return
    shot = subprocess.run(["xwd", "-root", "-silent"], capture_output=True, timeout=30, check=True).stdout
    (log_dir / f"{Path(artifact_name).stem}.screen.xwd").write_bytes(shot)
    colours = xwd_colour_count(shot)
    if colours < GUI_MIN_COLOURS:
        record_summary_line(f"[GUI-RENDER] {artifact_name}: FAIL ({colours} colours on screen)")
        raise RuntimeError(f"The window shows nothing ({colours} distinct colours on the X display).")
    record_summary_line(f"[GUI-RENDER] {artifact_name}: PASS ({colours} colours on screen)")


def generate_sample_docx() -> bytes:
    """Generate minimal valid .docx file."""
    import io

    import docx

    doc = docx.Document()
    doc.add_heading("DOCX Smoke Test", level=1)
    doc.add_paragraph("Smoke test paragraph content for docx conversion.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def generate_sample_xlsx() -> bytes:
    """Generate minimal valid .xlsx file."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["Metric", "Value", "Status"])
    ws.append(["TestRun", 42, "OK"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate_sample_pdf() -> bytes:
    """Generate minimal valid PDF with extractable text using fpdf2."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=14)
    pdf.cell(text="PDF Smoke Test Sample Document")
    return bytes(pdf.output())

def generate_sample_wav() -> bytes:
    """Generate a tiny silent WAV for the optional audio conversion path."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 800)
    return buf.getvalue()


def send_file_conversion(port: int, filename: str, content: bytes, content_type: str) -> str:
    """Test file conversion route via multipart upload and assert non-empty markdown."""
    boundary = "----SmokeTestBoundary123456789"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()

    url = f"http://127.0.0.1:{port}/convert/file?save_to_downloads=false&response_format=json"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20.0) as resp:
        if resp.getcode() != 200:
            raise RuntimeError(f"/convert/file for {filename} returned status {resp.getcode()}")
        data = json.loads(resp.read().decode("utf-8"))
        if not data.get("success"):

            raise RuntimeError(f"Conversion of {filename} failed: {data}")
        markdown = data.get("markdown", "")
        if not markdown.strip():
            raise RuntimeError(f"Conversion of {filename} returned empty markdown output!")
        return markdown


def send_audio_conversion(port: int, content: bytes) -> str:
    """Require HTTP 200 or a clean non-5xx no-transcription response."""
    try:
        return send_file_conversion(port, "sample.wav", content, "audio/x-wav")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code < 500 and "Audio transcription unavailable" in body:
            return f"[No transcription available] {body}"
        raise RuntimeError(f"Audio conversion returned HTTP {exc.code}: {body}") from exc
    except RuntimeError as exc:
        message = str(exc)
        if "empty markdown output" in message or "Speech transcription" in message:
            return "[No transcription available]"
        raise


def record_summary_line(line: str) -> None:
    """Print a labeled smoke result and expose it in GitHub Actions summaries."""
    print(line)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as summary:
            summary.write(f"{line}\n")


def run_smoke_test(
    artifact_path: Path,
    artifact_type: str,
    expected_version: str,
    timeout: float = 60.0,
    log_dir: Path | None = None,
    appimage_fuse: bool = False,
) -> None:
    """Execute end-to-end smoke test on the specified artifact."""
    env = launch_env(artifact_type, appimage_fuse)
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

    with tempfile.TemporaryDirectory(prefix="inkdoc-smoke-", ignore_cleanup_errors=True) as tmp:
        temp_dir = Path(tmp)
        exe_path = prepare_target_executable(artifact_path, artifact_type, temp_dir)
        speech_model = list(temp_dir.rglob("pocketsphinx-data"))
        record_summary_line(
            f"[AUDIO-MODEL] {artifact_path.name}: "
            f"{'included' if speech_model else 'absent'}"
        )
        if sys.platform == "darwin":
            exe_str = str(exe_path).replace("\\", "/")
            if ".app/Contents/MacOS" not in exe_str:
                raise RuntimeError(f"macOS executable path does not contain .app/Contents/MacOS: {exe_path}")
        print(f"[*] Prepared executable: {exe_path}")

        # 1. Run GUI backend self-test in frozen build
        print(f"[*] Running GUI backend self-test: {exe_path} --selftest")
        selftest_proc = subprocess.Popen(
            [str(exe_path), "--selftest"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        # An AppImage run with APPIMAGE_EXTRACT_AND_RUN unpacks itself (~280 MB) first.
        selftest_timeout = 60.0 if artifact_type == "appimage" else 15.0
        try:
            out, err = selftest_proc.communicate(timeout=selftest_timeout)
            if selftest_proc.returncode != 0:
                record_summary_line(f"[SELFTEST] {artifact_path.name}: FAIL (exit code {selftest_proc.returncode})")
                raise RuntimeError(
                    f"--selftest failed with code {selftest_proc.returncode}:\n"
                    f"STDOUT:\n{out}\n"
                    f"STDERR:\n{err}"
                )
            record_summary_line(f"[SELFTEST] {artifact_path.name}: PASS (exit code {selftest_proc.returncode})")
        except subprocess.TimeoutExpired as e:
            kill_process_tree(selftest_proc.pid)
            record_summary_line(f"[SELFTEST] {artifact_path.name}: TIMEOUT")
            raise RuntimeError(f"--selftest timed out after {selftest_timeout:.0f}s for {artifact_path.name}") from e

        # 1b. A real window under the page's own policy: one page -> app -> page round trip
        # (app/desktop/webview_bridge.py). The app gives up after 30 s by itself.
        print(f"[*] Running page bridge self-test: {exe_path} --selftest-bridge")
        bridge_proc = subprocess.Popen(
            [str(exe_path), "--selftest-bridge"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        bridge_timeout = selftest_timeout + 60.0
        try:
            out, err = bridge_proc.communicate(timeout=bridge_timeout)
        except subprocess.TimeoutExpired as e:
            kill_process_tree(bridge_proc.pid)
            record_summary_line(f"[BRIDGE] {artifact_path.name}: TIMEOUT")
            raise RuntimeError(f"--selftest-bridge timed out after {bridge_timeout:.0f}s for {artifact_path.name}") from e
        if bridge_proc.returncode != 0:
            record_summary_line(f"[BRIDGE] {artifact_path.name}: FAIL (exit code {bridge_proc.returncode})")
            raise RuntimeError(
                f"--selftest-bridge failed with code {bridge_proc.returncode}: the page could not reach "
                f"the window's functions.\nSTDOUT:\n{out}\nSTDERR:\n{err}"
            )
        record_summary_line(f"[BRIDGE] {artifact_path.name}: PASS")

        port = get_free_port()
        print(f"[*] Selected ephemeral port: {port}")

        with open(log_file, "w", encoding="utf-8", errors="replace") as log_f:
            kwargs: dict = {
                "stdout": log_f,
                "stderr": subprocess.STDOUT,
                "env": env,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True

            gui_smoke = sys.platform.startswith("linux") and bool(os.environ.get("DISPLAY"))
            cmd = [str(exe_path), "--port", str(port)] if gui_smoke else [str(exe_path), "--headless", "--port", str(port)]
            record_summary_line(f"[LAUNCH-PATH] {artifact_path.name}: {exe_path}")
            print(f"[*] Launching: {' '.join(cmd)}")
            proc = subprocess.Popen(cmd, **kwargs)

            start_time = time.time()
            health_ok = False
            reported_version = None

            try:
                # 2. Poll /health with fast-fail
                print(f"[*] Polling http://127.0.0.1:{port}/health (timeout={timeout}s) ...")
                while (time.time() - start_time) < timeout:
                    # Detect process having exited (poll return code instead of matching log text)
                    ret = proc.poll()
                    if ret is not None:
                        log_f.flush()
                        log_content = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
                        print(f"\n--- Process Log ({log_file}) ---\n{log_content}\n--- End Process Log ---", file=sys.stderr)
                        raise RuntimeError(
                            f"Process exited prematurely with code {ret} before /health was ready.\n"
                            f"Process Log:\n{log_content}"
                        )

                    try:
                        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
                        with urllib.request.urlopen(req, timeout=2.0) as resp:
                            if resp.getcode() == 200:
                                data = json.loads(resp.read().decode("utf-8"))
                                reported_version = data.get("version")
                                status_val = data.get("status")
                                if status_val == "ok":
                                    health_ok = True
                                    record_summary_line(
                                        f"[HEALTH] {artifact_path.name}: status={status_val} version='{reported_version}'"
                                    )
                                    break
                    except (urllib.error.URLError, ConnectionResetError, OSError):
                        pass

                    time.sleep(0.5)

                if not health_ok:
                    log_f.flush()
                    log_content = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
                    print(f"\n--- Process Log ({log_file}) ---\n{log_content}\n--- End Process Log ---", file=sys.stderr)
                    raise TimeoutError(
                        f"Timed out after {timeout}s waiting for /health to become available.\n"
                        f"Process Log:\n{log_content}"
                    )

                if gui_smoke:
                    gui_deadline = time.time() + 15.0
                    while time.time() < gui_deadline:
                        if proc.poll() is not None:
                            raise RuntimeError(f"GUI process exited before 15 seconds with code {proc.returncode}")
                        time.sleep(0.5)
                    record_summary_line(f"[GUI] {artifact_path.name}: stayed alive for 15 seconds")
                    check_window_rendered(artifact_path.name, log_dir)

                # 3. Verify reported version matches expected tag/dev version
                if reported_version != expected_version:
                    raise AssertionError(
                        f"Version mismatch in /health: expected '{expected_version}', got '{reported_version}'"
                    )
                print(f"[PASS] Version verification passed: '{reported_version}' == '{expected_version}'")

                # 4. Multi-format conversion testing
                print(f"[*] Testing document conversions for {artifact_path.name} via /convert/file...")

                # 4a. Plain text (.txt)
                txt_data = b"# Sample Heading\nSmoke test body content for InkDoc verification.\r\n"
                txt_md = send_file_conversion(port, "sample.txt", txt_data, "text/plain")
                if "Sample Heading" not in txt_md:
                    raise AssertionError(f"Expected content in text markdown output, got: {txt_md[:200]}")
                record_summary_line(f"[CONVERT-TXT] {artifact_path.name}: PASS (output length: {len(txt_md)} bytes)")

                # 4b. Word Document (.docx)
                docx_data = generate_sample_docx()
                docx_md = send_file_conversion(
                    port,
                    "sample.docx",
                    docx_data,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
                if "DOCX Smoke Test" not in docx_md:
                    raise AssertionError(f"Expected content in docx markdown output, got: {docx_md[:200]}")
                record_summary_line(f"[CONVERT-DOCX] {artifact_path.name}: PASS (output length: {len(docx_md)} bytes)")

                # 4c. Excel Workbook (.xlsx)
                xlsx_data = generate_sample_xlsx()
                xlsx_md = send_file_conversion(
                    port,
                    "sample.xlsx",
                    xlsx_data,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                if "TestRun" not in xlsx_md and "Metric" not in xlsx_md:
                    raise AssertionError(f"Expected content in xlsx markdown output, got: {xlsx_md[:200]}")
                record_summary_line(f"[CONVERT-XLSX] {artifact_path.name}: PASS (output length: {len(xlsx_md)} bytes)")

                # 4d. PDF Document (.pdf)
                pdf_data = generate_sample_pdf()
                pdf_md = send_file_conversion(port, "sample.pdf", pdf_data, "application/pdf")
                if "PDF Smoke Test" not in pdf_md:
                    raise AssertionError(f"Expected content in pdf markdown output, got: {pdf_md[:200]}")
                record_summary_line(f"[CONVERT-PDF] {artifact_path.name}: PASS (output length: {len(pdf_md)} bytes)")

                # 4e. WAV audio conversion; transcription is optional.
                audio_result = send_audio_conversion(port, generate_sample_wav())
                if not audio_result.strip():
                    raise AssertionError("Audio conversion returned no result")
                record_summary_line(f"[CONVERT-AUDIO] {artifact_path.name}: PASS ({audio_result[:120]})")

            finally:
                print(f"[*] Terminating process tree for PID {proc.pid} ...")
                kill_process_tree(proc.pid)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                time.sleep(1.0)

    record_summary_line(f"[ALL PASS] Smoke test succeeded for {artifact_path.name}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test shipped InkDoc artifacts.")
    parser.add_argument("--artifact", type=Path, required=True, help="Path to artifact to test.")
    parser.add_argument(
        "--type",
        choices=["onefile", "zip", "dmg", "appimage", "installer"],
        required=True,
        help="Artifact distribution packaging type.",
    )
    parser.add_argument("--appimage-fuse", action="store_true",
                        help="Run an AppImage through its FUSE mount instead of APPIMAGE_EXTRACT_AND_RUN.")
    parser.add_argument("--expected-version", required=True, help="Expected version reported by /health.")
    parser.add_argument("--timeout", type=float, default=60.0, help="Timeout in seconds for /health polling.")
    parser.add_argument("--log-dir", type=Path, default=None, help="Directory to save process logs.")

    args = parser.parse_args()

    try:
        run_smoke_test(
            artifact_path=args.artifact,
            artifact_type=args.type,
            expected_version=args.expected_version,
            timeout=args.timeout,
            log_dir=args.log_dir,
            appimage_fuse=args.appimage_fuse,
        )
        return 0
    except Exception as e:
        record_summary_line(f"[FAIL] {args.artifact.name}: {e}")
        print(f"\n[FAIL] Smoke test failed: {e}", file=sys.stderr)
        log_file = (args.log_dir or (Path.cwd() / "smoke-logs")) / f"{args.artifact.stem}.smoke.log"
        if log_file.is_file():
            print(f"\n--- Process Log ({log_file}) ---", file=sys.stderr)
            print(log_file.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
