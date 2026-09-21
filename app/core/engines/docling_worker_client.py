"""Docling Worker Client for InkDoc.

Manages the lifecycle of the out-of-process Docling worker, launches with -I isolated mode,
performs launch-time integrity checks, enforces concurrency serialization and idle timeouts,
and dispatches file-based IPC requests.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.engine_manager import EngineManager, SecurityError

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 600.0  # 10 minutes idle timeout
CONVERSION_TIMEOUT_SECONDS = 180.0  # 3 minutes maximum per conversion


class DoclingWorkerClient:
    """Client that communicates with the isolated Docling worker subprocess."""

    _instance: DoclingWorkerClient | None = None
    _singleton_lock = threading.Lock()

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._mutex = threading.Lock()
        self._idle_timer: threading.Timer | None = None
        self._last_active = 0.0
        self._stdout_queue: queue.Queue[str] = queue.Queue()
        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None


    @classmethod
    def get_instance(cls) -> DoclingWorkerClient:
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ─── Process Lifecycle ───────────────────────────────────────────────────

    def _reset_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._idle_timer = threading.Timer(IDLE_TIMEOUT_SECONDS, self._on_idle_timeout)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _on_idle_timeout(self) -> None:
        with self._mutex:
            if self._proc is not None:
                logger.info("Docling worker reached idle timeout (%d s). Shutting down.", int(IDLE_TIMEOUT_SECONDS))
                self._shutdown_locked()

    def _shutdown_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

        self._stop_event.set()

        if self._proc is not None:
            proc = self._proc
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.write('{"action": "shutdown"}\n')
                    proc.stdin.flush()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            finally:
                try:
                    if proc.stdout and not proc.stdout.closed:
                        proc.stdout.close()
                except Exception:
                    pass
                self._proc = None

        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        self._reader_thread = None

        # Drain any residual lines from queue
        while not self._stdout_queue.empty():
            try:
                self._stdout_queue.get_nowait()
            except queue.Empty:
                break

    def shutdown(self) -> None:
        """Terminate the running worker subprocess."""
        with self._mutex:
            self._shutdown_locked()

    def _spawn_process(
        self,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        stderr_target: Any = subprocess.DEVNULL,
    ) -> subprocess.Popen:
        """Spawn isolated worker process with reader thread and redirected/drained stderr."""
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_target,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            cwd=cwd,
            env=env,
        )
        self._stop_event = threading.Event()
        self._stdout_queue = queue.Queue()

        def _read_stdout(stream: Any, q: queue.Queue[str], stop_evt: threading.Event) -> None:
            try:
                for line in iter(stream.readline, ""):
                    q.put(line)
                    if stop_evt.is_set():
                        break
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        self._reader_thread = threading.Thread(
            target=_read_stdout,
            args=(proc.stdout, self._stdout_queue, self._stop_event),
            daemon=True,
            name="DoclingWorkerStdoutReader",
        )
        self._reader_thread.start()
        return proc

    def _ensure_worker_running(self, startup_timeout: float = 15.0) -> subprocess.Popen:
        """Launch the worker if not already running, validating launch-time integrity."""
        mgr = EngineManager.get_instance()
        if not mgr.is_engine_installed("docling"):
            raise RuntimeError(
                "Docling engine is not installed. Please install it in Settings before use."
            )

        # Launch-time cryptographic verification (Security Requirement #7)
        if not mgr.verify_launch_integrity("docling"):
            raise SecurityError(
                "Launch integrity check failed for Docling worker executable or script. "
                "Files may be corrupted or modified. Please verify or reinstall the engine."
            )

        if self._proc is not None and self._proc.poll() is None:
            return self._proc

        pack_info = mgr.get_platform_pack_info("docling")
        if not pack_info:
            raise RuntimeError("Platform unsupported for Docling engine.")

        target_dir = mgr.get_engine_dir("docling")
        interpreter = target_dir / pack_info.interpreter_path
        worker_script = target_dir / pack_info.worker_script

        # If worker script not yet copied to pack dir, copy it from repository
        if not worker_script.is_file():
            repo_worker = Path(__file__).resolve().parent / "worker.py"
            if repo_worker.is_file():
                worker_script.write_text(repo_worker.read_text(encoding="utf-8"), encoding="utf-8")

        # Build minimal allowlisted environment (Security Requirement #8)
        minimal_env: dict[str, str] = {
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "SCARF_NO_ANALYTICS": "1",
        }

        # Transfer only essential OS variables
        for var in ("SystemRoot", "PATH", "TEMP", "TMP", "LOCALAPPDATA", "USERPROFILE"):
            val = os.environ.get(var)
            if val:
                minimal_env[var] = val

        for var in ("HOME", "TMPDIR", "USER", "LANG", "LC_ALL"):
            val = os.environ.get(var)
            if val:
                minimal_env[var] = val

        # Isolated Python launch.
        #
        # -I is isolated mode: it implies -E (ignore PYTHONPATH/PYTHONHOME), -s (no user
        # site-packages) and -P (do not prepend the script directory to sys.path). That
        # is the isolation this worker needs.
        #
        # -S must NOT be added. It suppresses the `site` module, and `site` is what puts
        # a virtualenv's own site-packages on sys.path. With -S the interpreter starts
        # and answers ping (the docling import is lazy, inside _get_converter), then
        # every conversion fails with ModuleNotFoundError: No module named 'docling'.
        # The pack ships its dependencies in env/, so loading them is the entire point.
        #
        # scripts/build_pack.py launches the worker the same way in its post-build smoke
        # test. Keep the two argument lists identical, or the smoke test stops validating
        # what production actually runs.
        cmd = [
            str(interpreter),
            "-I",
            str(worker_script),
        ]

        logger.info("Spawning isolated Docling worker: %s", cmd)
        proc = self._spawn_process(
            cmd, cwd=str(target_dir), env=minimal_env, stderr_target=subprocess.DEVNULL
        )

        # Wait for worker "ready" signal via reader thread queue
        start_wait = time.time()
        ready = False
        while time.time() - start_wait < startup_timeout:
            if proc.poll() is not None:
                raise RuntimeError(f"Docling worker exited prematurely with exit code {proc.returncode}")

            remaining = max(0.05, startup_timeout - (time.time() - start_wait))
            try:
                line = self._stdout_queue.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue

            if line:
                try:
                    data = json.loads(line.strip())
                    if data.get("status") == "ready":
                        ready = True
                        break
                except Exception:
                    pass

        if not ready:
            self._shutdown_locked()
            raise RuntimeError("Docling worker failed to initialize within timeout.")

        self._proc = proc
        return proc

    # ─── Conversion IPC ──────────────────────────────────────────────────────

    def convert_file(
        self,
        source_path: str,
        ocr: bool = True,
        table_structure: bool = True,
        timeout: float = CONVERSION_TIMEOUT_SECONDS,
    ) -> str:
        """Send conversion request to warm worker subprocess over file-based IPC."""
        with self._mutex:
            self._last_active = time.time()
            self._reset_idle_timer()

            proc = self._ensure_worker_running()
            job_id = str(uuid.uuid4())

            # Create private temporary output file
            tmp_fd, tmp_out_path = tempfile.mkstemp(prefix="inkdoc_res_", suffix=".md")
            os.close(tmp_fd)

            req_payload = {
                "action": "convert",
                "job_id": job_id,
                "source_file": str(Path(source_path).resolve()),
                "output_file": tmp_out_path,
                "ocr": ocr,
                "table_structure": table_structure,
            }

            try:
                # Write command to worker stdin
                if not proc.stdin:
                    raise RuntimeError("Worker stdin is unavailable")

                proc.stdin.write(json.dumps(req_payload) + "\n")
                proc.stdin.flush()

                # Read response from worker stdout via reader thread queue with real timeout
                start_time = time.time()
                resp_line = ""

                while time.time() - start_time < timeout:
                    if proc.poll() is not None:
                        raise RuntimeError(f"Worker crashed during conversion with exit code {proc.returncode}")

                    remaining = max(0.05, timeout - (time.time() - start_time))
                    try:
                        resp_line = self._stdout_queue.get(timeout=min(0.2, remaining))
                        if resp_line:
                            break
                    except queue.Empty:
                        continue

                if not resp_line:
                    self._shutdown_locked()
                    raise TimeoutError(f"Docling conversion timed out after {int(timeout)} seconds.")

                resp_data = json.loads(resp_line.strip())
                if resp_data.get("status") != "ok":
                    error_msg = resp_data.get("error", "Unknown worker error")
                    raise RuntimeError(f"Docling conversion failed: {error_msg}")

                # Read generated Markdown from the private temporary output file
                out_file = Path(tmp_out_path)
                if not out_file.is_file():
                    raise RuntimeError("Docling output file was not created by worker.")

                markdown_content = out_file.read_text(encoding="utf-8", errors="replace")
                return markdown_content

            finally:
                if os.path.exists(tmp_out_path):
                    try:
                        os.remove(tmp_out_path)
                    except OSError:
                        pass

