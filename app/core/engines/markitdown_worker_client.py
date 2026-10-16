"""Runs MarkItDown conversions in the MarkItDown worker process.

MarkItDown loads ~20 libraries when it is imported (pandas, onnxruntime and
magika among them): about 95 MB that Python never gives back. InkDoc's own
process therefore never imports it. Conversions go to one worker process
(app/core/engines/markitdown_worker.py) that starts on demand, on the first
conversion or on a sign that one is coming (POST /convert/warmup, sent by the
UI when a file is dragged over the window, the file picker opens or the URL
box gets focus), and exits after IDLE_TIMEOUT_S without work.

- One worker converts several files at once on its threads, as InkDoc's own
  process did before: the same throughput, one copy of markitdown in memory.
- A conversion that arrives while the worker is starting waits for that same
  start; it never launches a second worker.
- The idle clock runs only while nothing is being converted.
- Cancel kills the worker when the cancelled conversion is the only one
  running (the CPU is freed at once); otherwise its result is thrown away.
- A worker that dies mid-conversion fails that conversion with a clear message,
  and the next conversion starts a new one. A worker that can't start at all
  makes InkDoc convert in its own process instead, as it did before 1.3.
- It never outlives InkDoc: on Windows a kill-on-close Job Object; everywhere,
  the worker exits when its stdin closes (InkDoc's process ended) and, on
  macOS and Linux, when its parent process changes. Not PR_SET_PDEATHSIG: Linux
  sends that when the *thread* that started the child ends, and the server's
  threads come and go, so it killed healthy workers.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.converter import UserFacingConversionError

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


# How long the worker stays after its last conversion. Long enough to cover the
# pauses of a working session; short enough that an InkDoc left open gives the
# memory back soon. INKDOC_MARKITDOWN_IDLE_S overrides it.
IDLE_TIMEOUT_S = _env_float("INKDOC_MARKITDOWN_IDLE_S", 300.0)
# A cold start takes 1-3 s; a first launch of the frozen app from a slow disk,
# with an antivirus scanning every file, can take much longer.
STARTUP_TIMEOUT_S = 90.0
# Conversions the worker runs at once (more wait in its queue). InkDoc's lanes
# allow two MarkItDown jobs, plus one from a Docling fallback.
WORKER_THREADS = 3
# After the worker failed to start, convert in-process for this long before
# trying again, so every conversion doesn't wait for a start that won't work.
RETRY_START_AFTER_S = 60.0


class WorkerUnavailableError(RuntimeError):
    """The worker could not be started; the caller converts in-process instead."""


class MarkItDownWorkerCrashedError(UserFacingConversionError):
    """The worker died while converting (a damaged or huge file, out of memory)."""


class MarkItDownConversionError(RuntimeError):
    """MarkItDown raised in the worker. str() is its message, unchanged."""

    def __init__(self, message: str, error_type: str = "") -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass
class _Pending:
    output: Path
    generation: int
    event: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None
    abandoned: bool = False


TMPDIR_PREFIX = "inkdoc-markitdown-"
STALE_TMPDIR_S = 24 * 3600


def _unlink_quietly(path: Path) -> None:
    """Delete a result file; never let a failure (a handle still open) end a conversion."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not delete %s yet: %s", path.name, exc)


def _is_private_dir(path: Path | None) -> bool:
    """Our own results folder: a real directory, and on macOS/Linux ours alone.

    Checked before every use: on a shared /tmp, a folder deleted by a cleaner
    could be recreated (or replaced by a link) by another local user.
    """
    if path is None:
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    import stat

    if not stat.S_ISDIR(st.st_mode):
        return False
    if os.name == "posix":
        return st.st_uid == os.getuid() and (st.st_mode & 0o077) == 0
    return True


def _remove_stale_tmpdirs() -> None:
    """Results folders left by an InkDoc that crashed, more than a day old."""
    cutoff = time.time() - STALE_TMPDIR_S
    try:
        for entry in Path(tempfile.gettempdir()).glob(f"{TMPDIR_PREFIX}*"):
            try:
                if _is_private_dir(entry) and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def worker_command() -> list[str]:
    """How to start the worker: this executable, with --markitdown-worker."""
    args = ["--markitdown-worker", "--threads", str(WORKER_THREADS)]
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(_app_root() / "main.py"), *args]


def _app_root() -> Path:
    """The folder of the frozen executable, or of main.py in a source checkout."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


class MarkItDownWorker:
    """The one MarkItDown worker of this InkDoc process."""

    _instance: MarkItDownWorker | None = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> MarkItDownWorker:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._generation = 0
        self._ready = threading.Event()
        self._start_error: str | None = None
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._active = 0
        self._idle_timer: threading.Timer | None = None
        self._failed_at: float | None = None  # monotonic time of the last failed start
        self._tmpdir: Path | None = None
        self._job: Any = None
        self._job_tried = False
        self._atexit = False
        self.version: str | None = None

    # ─── State ───────────────────────────────────────────────────────────────

    @staticmethod
    def enabled() -> bool:
        """INKDOC_MARKITDOWN_WORKER=0 converts in InkDoc's own process, as before 1.3."""
        return os.environ.get("INKDOC_MARKITDOWN_WORKER", "1").strip() != "0"

    def usable(self) -> bool:
        if not self.enabled():
            return False
        return self._failed_at is None or time.monotonic() - self._failed_at >= RETRY_START_AFTER_S

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    # ─── Starting ────────────────────────────────────────────────────────────

    def warm(self) -> str:
        """Start the worker if it isn't running. Returns at once.

        "ready" (a running idle worker's clock starts again: the user is about
        to convert), "starting", or "unavailable".
        """
        if not self.usable():
            return "unavailable"
        with self._lock:
            if self._proc is not None and self._ready.is_set() and self._start_error is None:
                if self._active == 0:
                    self._arm_idle_timer_locked()
                return "ready"
            if self._proc is not None:
                return "starting"
            try:
                self._spawn_locked()
            except OSError as exc:
                self._failed_at = time.monotonic()
                logger.warning("Could not start the MarkItDown worker: %s", exc)
                return "unavailable"
        return "starting"

    def _private_tmpdir_locked(self) -> Path:
        """The folder results are written to: 0700 on macOS and Linux, inside the
        user's own temp folder on Windows. Made again if it changed or vanished."""
        if not _is_private_dir(self._tmpdir):
            if self._tmpdir is None:
                _remove_stale_tmpdirs()
            self._tmpdir = Path(tempfile.mkdtemp(prefix=TMPDIR_PREFIX))
        return self._tmpdir  # type: ignore[return-value]

    def _spawn_locked(self) -> None:
        tmpdir = self._private_tmpdir_locked()
        env = dict(os.environ)
        env.pop("INKDOC_SESSION_TOKEN", None)
        stderr: Any = subprocess.DEVNULL
        log_path = os.environ.get("INKDOC_MARKITDOWN_WORKER_LOG")
        if log_path:
            stderr = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - handed to the child
        try:
            proc = subprocess.Popen(
                worker_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                errors="replace",
                # The private, empty results folder: nothing next to the app
                # (an ffmpeg.exe in Downloads, say) can be picked up from it.
                cwd=str(tmpdir),
                env=env,
                # A console Python (source checkouts) would open a Command Prompt.
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        finally:
            if stderr is not subprocess.DEVNULL:
                stderr.close()
        self._contain(proc)
        self._generation += 1
        self._proc = proc
        self._ready = threading.Event()
        self._start_error = None
        threading.Thread(
            target=self._read_responses, args=(proc, self._generation, self._ready),
            daemon=True, name="MarkItDownWorkerReader",
        ).start()
        if not self._atexit:
            atexit.register(self.shutdown)
            self._atexit = True
        logger.info("Started the MarkItDown worker (pid %s).", proc.pid)

    def _contain(self, proc: subprocess.Popen) -> None:
        """Windows: put the worker in a kill-on-close Job Object.

        macOS and Linux need nothing here: the worker exits when its stdin
        closes and when its parent changes (markitdown_worker._watch_parent).
        """
        if sys.platform != "win32":
            return
        try:
            if self._job is None and not self._job_tried:
                self._job_tried = True
                from app.core.engines.process_guard import WindowsJob

                self._job = WindowsJob()
            if self._job is not None:
                self._job.assign(proc)
        except OSError as exc:
            # Old Windows versions can't nest jobs. The worker still exits when
            # its stdin closes, which happens when InkDoc's process ends.
            logger.debug("MarkItDown worker not placed in a job object: %s", exc)

    def _wait_ready(self, job: Any = None) -> subprocess.Popen:
        """Start the worker (or join a start in progress) and wait until it is warm.

        A worker stopped on purpose meanwhile (the idle timer, another
        conversion's cancel) is simply started again. Only a start that really
        failed raises WorkerUnavailableError. A cancel is honoured while waiting.
        """
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while True:
            with self._lock:
                if self._proc is not None and self._proc.poll() is not None:
                    # Gone (killed from outside, crashed while idle) before its
                    # reader noticed: start a new one rather than write to it.
                    self._stop_locked(kill=True)
                if self._proc is None:
                    try:
                        self._spawn_locked()
                    except OSError as exc:
                        self._failed_at = time.monotonic()
                        raise WorkerUnavailableError(f"could not start it ({exc})") from exc
                proc, ready, generation = self._proc, self._ready, self._generation
            while not ready.wait(0.2):
                if job is not None and job.cancelled():
                    from app.core.jobs import ConversionCancelledError

                    raise ConversionCancelledError("conversion_cancelled")
                if time.monotonic() >= deadline:
                    with self._lock:
                        if self._generation == generation:
                            self._failed_at = time.monotonic()
                            self._stop_locked(kill=True)
                    raise WorkerUnavailableError(f"it did not start within {int(STARTUP_TIMEOUT_S)} s")
            with self._lock:
                if self._generation == generation:
                    if self._start_error is None:
                        return proc
                    self._failed_at = time.monotonic()
                    raise WorkerUnavailableError(self._start_error)
            if time.monotonic() >= deadline:
                raise WorkerUnavailableError("it was stopped while starting")
            # Stopped on purpose while we waited: start it again.

    # ─── Responses ───────────────────────────────────────────────────────────

    def _read_responses(self, proc: subprocess.Popen, generation: int, ready: threading.Event) -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("status") == "ready":
                    with self._lock:
                        if generation == self._generation:
                            self.version = message.get("markitdown")
                            if self._active == 0:
                                self._arm_idle_timer_locked()
                    ready.set()
                elif message.get("status") == "error":
                    with self._lock:
                        if generation == self._generation:
                            self._start_error = str(message.get("error") or "it could not load MarkItDown")
                    ready.set()
                elif "id" in message:
                    self._deliver(message)
        except (OSError, ValueError):
            pass
        # The worker is gone: crashed, killed, or stopped when idle.
        try:
            proc.wait(timeout=5)  # reaped, not left as a zombie
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        with self._lock:
            lost = [rid for rid, p in self._pending.items() if p.generation == generation]
            for rid in lost:
                pending = self._pending.pop(rid)
                if pending.abandoned:
                    _unlink_quietly(pending.output)
                pending.response = {"crashed": True}
                pending.event.set()
            if generation == self._generation:
                if not ready.is_set():
                    self._start_error = f"it exited while starting (exit code {proc.poll()})"
                self._proc = None
                self._cancel_idle_timer_locked()
        ready.set()

    def _deliver(self, message: dict) -> None:
        with self._lock:
            pending = self._pending.pop(message.get("id"), None)
        if pending is None:
            return
        if pending.abandoned:
            _unlink_quietly(pending.output)
            return
        pending.response = message
        pending.event.set()

    # ─── Converting ──────────────────────────────────────────────────────────

    def convert(
        self,
        source: str,
        options: Any,
        stream_info: dict[str, str],
        url: str | None,
    ) -> str:
        """Convert `source` in the worker. Raises WorkerUnavailableError if it can't start."""
        job = getattr(options, "job", None)
        while True:
            proc = self._wait_ready(job)
            with self._lock:
                # Registered against the worker just waited for. If the idle
                # timer stopped it in between, start again rather than wait on
                # a worker that is going away.
                if self._proc is not proc:
                    continue
                tmpdir = self._private_tmpdir_locked()
                self._next_id += 1
                rid = self._next_id
                pending = _Pending(output=tmpdir / f"{rid}-{uuid.uuid4().hex}.md", generation=self._generation)
                self._pending[rid] = pending
                self._active += 1
                self._cancel_idle_timer_locked()
                break
        request = {
            "id": rid,
            "action": "convert",
            "source": str(source),
            "output": str(pending.output),
            "options": {
                "enable_plugins": bool(options.enable_plugins),
                "keep_data_uris": bool(options.keep_data_uris),
                "docintel_enabled": bool(options.docintel_enabled),
                "docintel_endpoint": str(options.docintel_endpoint or ""),
                "cu_enabled": bool(options.cu_enabled),
                "cu_endpoint": str(options.cu_endpoint or ""),
                "cu_analyzer_id": str(options.cu_analyzer_id or ""),
            },
            "stream_info": dict(stream_info or {}),
            "url": url,
        }
        try:
            try:
                with self._write_lock:
                    proc.stdin.write(json.dumps(request) + "\n")  # type: ignore[union-attr]
                    proc.stdin.flush()  # type: ignore[union-attr]
            except (OSError, ValueError):
                # The worker went away between starting and now; the reader
                # thread reports the pending conversion as lost.
                pass
            cancelled = False
            while not pending.event.wait(0.2):
                if job is not None and not cancelled and job.cancelled():
                    cancelled = True
                    if self._stop_for_cancel(rid, pending, proc):
                        break
                if proc.poll() is not None and not pending.event.wait(1.0):
                    # Gone, and its reader had nothing more for this conversion.
                    with self._lock:
                        self._pending.pop(rid, None)
                    pending.response = {"crashed": True}
                    break
            if cancelled:
                from app.core.jobs import ConversionCancelledError

                raise ConversionCancelledError("conversion_cancelled")
            response = pending.response or {"crashed": True}
            if response.get("crashed"):
                raise MarkItDownWorkerCrashedError(
                    "MarkItDown stopped unexpectedly while converting this file. It may be "
                    "damaged or too large; try again, or try another engine."
                )
            if not response.get("ok"):
                raise MarkItDownConversionError(str(response.get("error", "")), str(response.get("type", "")))
            if not pending.output.exists():
                raise MarkItDownWorkerCrashedError("MarkItDown finished without writing its result. Try again.")
            # newline="": exactly the text the worker wrote, line endings included.
            with open(pending.output, encoding="utf-8", errors="replace", newline="") as f:
                text = f.read()
            return None if response.get("none") else text  # type: ignore[return-value]
        finally:
            _unlink_quietly(pending.output)
            with self._lock:
                self._active -= 1
                if self._active == 0 and self._proc is not None and self._ready.is_set():
                    self._arm_idle_timer_locked()

    def _stop_for_cancel(self, rid: int, pending: _Pending, proc: subprocess.Popen) -> bool:
        """Cancel. True: the worker was stopped (this was its only conversion).

        It is waited for until it has exited, so its handles on the document
        are released before the server deletes its temporary copy. False:
        other conversions share the worker, so this one is let finish and its
        result thrown away, as before 1.3.
        """
        with self._lock:
            if not (self._active == 1 and rid in self._pending and self._proc is proc):
                return False
            pending.abandoned = True
            self._pending.pop(rid, None)
            self._stop_locked(kill=True)
        logger.info("Stopped the MarkItDown worker to cancel its conversion.")
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        return True

    # ─── Stopping ────────────────────────────────────────────────────────────

    def _arm_idle_timer_locked(self) -> None:
        self._cancel_idle_timer_locked()
        timer = threading.Timer(IDLE_TIMEOUT_S, self._on_idle, args=(self._generation,))
        timer.daemon = True
        timer.start()
        self._idle_timer = timer

    def _cancel_idle_timer_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _on_idle(self, generation: int) -> None:
        with self._lock:
            if generation == self._generation and self._active == 0 and self._proc is not None:
                logger.info("MarkItDown worker idle for %d s; stopping it to free memory.", int(IDLE_TIMEOUT_S))
                self._stop_locked(kill=False)

    def _stop_locked(self, *, kill: bool) -> None:
        self._cancel_idle_timer_locked()
        proc = self._proc
        self._proc = None
        self._generation += 1
        self._ready.set()
        if proc is None or proc.poll() is not None:
            return
        if kill:
            try:
                proc.kill()
            except OSError:
                pass
            return
        # Ask it to stop; if it doesn't within a moment, end it. Off this
        # thread, so nothing waits on it.
        def _finish(p: subprocess.Popen) -> None:
            try:
                with self._write_lock:
                    p.stdin.write('{"action": "shutdown"}\n')  # type: ignore[union-attr]
                    p.stdin.flush()  # type: ignore[union-attr]
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except OSError:
                    pass

        threading.Thread(target=_finish, args=(proc,), daemon=True, name="MarkItDownWorkerStop").start()

    def shutdown(self) -> None:
        """Stop the worker now (InkDoc is exiting)."""
        with self._lock:
            proc = self._proc
            self._stop_locked(kill=True)
            tmpdir, self._tmpdir = self._tmpdir, None
        if proc is not None:
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def convert_with_markitdown(
    source: str,
    options: Any,
    stream_info: dict[str, str],
    url: str | None,
) -> str:
    """One MarkItDown conversion: in the worker, or here if it can't be started."""
    worker = MarkItDownWorker.get_instance()
    if worker.usable():
        try:
            return worker.convert(source, options, stream_info, url)
        except WorkerUnavailableError as exc:
            logger.warning("MarkItDown worker unavailable (%s); converting in InkDoc's own process.", exc)
    from app.core.converter import run_markitdown

    return run_markitdown(source, options, stream_info, url)
