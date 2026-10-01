"""llama-server process management for the GLM-OCR engine.

GLM-OCR runs in llama.cpp's llama-server, a separate process (the same
isolation the Docling worker has), reached over its OpenAI-compatible HTTP API
on the loopback interface. This module owns that process.

Security and robustness, each of which has a test in tests/test_glm_ocr_server.py:

- Every runtime file the catalogue lists is re-hashed before each launch, not
  only the executable: a swapped DLL or dylib runs just as much code. ~20-33 MB,
  so it costs milliseconds.
- The server binds to 127.0.0.1 only (no firewall prompt, unreachable from the
  network) and requires a random per-session API key, so other local processes
  cannot use it. No web UI.
- It can never download anything: `--offline`, never `-hf`, and LLAMA_CACHE
  points at an empty directory. The environment is minimal and allowlisted, so
  stray LLAMA_ARG_* variables cannot change how it starts.
- It cannot outlive InkDoc: a Windows Job Object with kill-on-close, a parent
  death signal on Linux, and a PID file that the next start reaps (macOS).
- Images are sent as base64 data URIs; local paths and URLs never are.

Lifecycle: started lazily on the first GLM-OCR page, stopped after
IDLE_TIMEOUT_SECONDS without requests (giving back ~2 GB), on removal, and on
app shutdown. A crash mid-request restarts it once and retries the page; a GPU
build that crashes twice in a session is replaced by the CPU build until the
app restarts. On macOS a failed Metal start retries with -ngl 0 on the same
binary, and the choice is remembered.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 300.0
READY_TIMEOUT_CPU_S = 90.0
READY_TIMEOUT_GPU_S = 120.0
PAGE_TIMEOUT_CPU_S = 300.0
PAGE_TIMEOUT_GPU_S = 60.0
PORT_RETRIES = 3
DEFAULT_CTX_SIZE = 8192
TEMPERATURE = 0.02
# A fixed seed makes output reproducible: without it, three identical requests
# gave three different outputs, one switching table format (Prajnya's finding).
SEED = 3407
GPU_ALL_LAYERS = 999
LOW_MEMORY_BYTES = 16 * 1024**3
STDERR_TAIL_LINES = 40
GPU_CRASH_LIMIT = 2

_WIN_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Windows system DLLs the llama.cpp builds import that do not ship in their zips.
# MSVCP140/VCRUNTIME140 come with the Visual C++ Redistributable (or with
# InkDoc's own bundle); vulkan-1.dll with the GPU driver.
_WINDOWS_CHECKED_DLLS = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "vulkan-1.dll"}
_LINUX_CHECKED_LIBS = {
    "libgomp.so.1": "gomp",
    "libssl.so.3": "ssl",
    "libcrypto.so.3": "crypto",
    "libvulkan.so.1": "vulkan",
}


class GlmOcrServerError(RuntimeError):
    """llama-server could not be started or did not answer."""


class RuntimeIntegrityError(GlmOcrServerError):
    """A runtime file is missing or does not match its catalogue hash."""


class MissingSystemLibraryError(GlmOcrServerError):
    """The runtime needs a system library that is not installed."""


def default_threads() -> int:
    """Threads for llama-server: about the number of physical cores, at most 8.

    llama.cpp runs best on physical cores. On a typical SMT PC (8+ logical
    CPUs) that is half the logical count; Apple Silicon has no SMT, so all but
    two cores; on a small machine (4 or fewer) all but one, because halving 3
    or 4 CPUs leaves 1-2 threads and pages slow to a crawl. Capped at 8 so a
    conversion never takes every core from the rest of the desktop.
    """
    cpus = os.cpu_count() or 2
    if cpus <= 4:
        n = cpus - 1
    elif sys.platform == "darwin":
        n = cpus - 2
    else:
        n = cpus // 2
    return max(1, min(8, n))


@dataclass
class LaunchSpec:
    """Everything needed to start one llama-server for GLM-OCR."""

    runtime_dir: Path
    entry: str
    sha256_files: dict[str, str]
    model_path: Path
    mmproj_path: Path
    accel: str = "cpu"                 # "cpu" | "vulkan" | "metal"
    gpu_layers: int = 0                # 0 = CPU only
    ctx_size: int = DEFAULT_CTX_SIZE
    threads: int = field(default_factory=default_threads)
    system_deps: list[str] = field(default_factory=list)
    run_dir: Path | None = None        # PID file and the empty LLAMA_CACHE live here

    @property
    def uses_gpu(self) -> bool:
        return self.gpu_layers > 0

    def cpu_fallback(self) -> LaunchSpec:
        """Same binary, no GPU layers (Metal's fallback)."""
        return LaunchSpec(
            runtime_dir=self.runtime_dir, entry=self.entry, sha256_files=self.sha256_files,
            model_path=self.model_path, mmproj_path=self.mmproj_path, accel="cpu",
            gpu_layers=0, ctx_size=self.ctx_size, threads=self.threads,
            system_deps=self.system_deps, run_dir=self.run_dir,
        )


# ─── Pure pieces ─────────────────────────────────────────────────────────────
def build_command(spec: LaunchSpec, port: int, api_key: str) -> list[str]:
    """The exact llama-server command line. Every option is documented upstream.

    glm-ocr-track.yml runs this same command against every new pinned build
    before it can ship, because CLI flags change between llama.cpp builds.
    """
    cmd = [
        str(spec.runtime_dir / spec.entry),
        "-m", str(spec.model_path),
        "--mmproj", str(spec.mmproj_path),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--api-key", api_key,
        "--offline",
        "--no-webui",
        "--no-cache-prompt",
        "--ctx-size", str(spec.ctx_size),
        "--temp", str(TEMPERATURE),
        "--seed", str(SEED),
        "--threads", str(spec.threads),
        "--parallel", "1",
        "--flash-attn", "auto",
        "-ngl", str(spec.gpu_layers),
    ]
    if not spec.uses_gpu:
        # The Vulkan build would otherwise still put the vision projector on the GPU.
        cmd.append("--no-mmproj-offload")
    return cmd


def build_env(spec: LaunchSpec, cache_dir: Path) -> dict[str, str]:
    """A minimal, allowlisted environment for the child.

    Deliberately not os.environ: llama-server reads LLAMA_ARG_* variables as
    options, and HF_* variables as download settings.
    """
    env: dict[str, str] = {"LLAMA_CACHE": str(cache_dir), "HF_HUB_OFFLINE": "1", "DO_NOT_TRACK": "1"}
    for var in ("SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA", "USERPROFILE",
                "HOME", "TMPDIR", "USER", "LANG", "LC_ALL"):
        val = os.environ.get(var)
        if val:
            env[var] = val
    path_parts = [str(spec.runtime_dir)]
    if sys.platform == "win32":
        # The Visual C++ runtime ships with InkDoc's own bundle; let the child
        # find it there when the redistributable is not installed system-wide.
        path_parts += [str(d) for d in _app_runtime_dirs()]
        sysroot = os.environ.get("SYSTEMROOT", r"C:\Windows")
        path_parts += [str(Path(sysroot) / "System32"), sysroot]
    else:
        path_parts += ["/usr/bin", "/bin"]
    env["PATH"] = os.pathsep.join(dict.fromkeys(path_parts))
    if sys.platform.startswith("linux"):
        env["LD_LIBRARY_PATH"] = str(spec.runtime_dir)
    elif sys.platform == "darwin":
        env["DYLD_FALLBACK_LIBRARY_PATH"] = str(spec.runtime_dir)
    return env


def _app_runtime_dirs() -> list[Path]:
    """Folders of the running app that carry the Visual C++ runtime DLLs."""
    dirs = []
    for candidate in (getattr(sys, "_MEIPASS", None), os.path.dirname(sys.executable), sys.base_prefix):
        if candidate and Path(candidate).is_dir():
            dirs.append(Path(candidate))
    return dirs


def verify_runtime(spec: LaunchSpec) -> None:
    """Re-hash every listed runtime file. Raises RuntimeIntegrityError on any mismatch."""
    if not spec.sha256_files or spec.entry not in spec.sha256_files:
        raise RuntimeIntegrityError("The GLM-OCR runtime has no file list to verify against.")
    for rel, expected in spec.sha256_files.items():
        path = spec.runtime_dir / rel
        if not path.is_file():
            raise RuntimeIntegrityError(f"GLM-OCR runtime file is missing: {rel}. Reinstall GLM-OCR in Settings.")
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
        if h.hexdigest().lower() != expected.lower():
            raise RuntimeIntegrityError(
                f"GLM-OCR runtime file failed its integrity check: {rel}. "
                "It may have been modified; reinstall GLM-OCR in Settings."
            )
    for p in (spec.model_path, spec.mmproj_path):
        if not p.is_file():
            raise RuntimeIntegrityError(f"GLM-OCR model file is missing: {p.name}. Reinstall GLM-OCR in Settings.")


def missing_system_libraries(system_deps: list[str]) -> list[str]:
    """System libraries the runtime imports that this machine does not have."""
    missing = []
    if sys.platform == "win32":
        sysroot = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        search = [sysroot / "System32", *_app_runtime_dirs()]
        for dep in system_deps:
            if dep.lower() not in _WINDOWS_CHECKED_DLLS:
                continue
            if not any((d / dep).is_file() or (d / dep.lower()).is_file() for d in search):
                missing.append(dep)
    elif sys.platform.startswith("linux"):
        import ctypes.util

        for dep in system_deps:
            stem = _LINUX_CHECKED_LIBS.get(dep)
            if stem and not ctypes.util.find_library(stem):
                missing.append(dep)
    return missing


def describe_missing(missing: list[str]) -> str:
    lower = {m.lower() for m in missing}
    if lower & {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"}:
        return (
            "GLM-OCR needs the Microsoft Visual C++ Redistributable (x64), which is not installed. "
            "Install it from https://aka.ms/vs/17/release/vc_redist.x64.exe and try again."
        )
    if "vulkan-1.dll" in lower or "libvulkan.so.1" in lower:
        return "The Vulkan driver was not found, so GPU acceleration is unavailable. The CPU runtime still works."
    return "GLM-OCR needs system libraries that are not installed: " + ", ".join(missing) + "."


def find_free_port() -> int:
    """A free loopback port, chosen the way the desktop runner chooses its own."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def total_memory_bytes() -> int | None:
    """Physical RAM, with no new dependency. None when unknown."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys)
            return None
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except Exception:
        return None


def is_low_memory_machine() -> bool:
    total = total_memory_bytes()
    return total is not None and total < LOW_MEMORY_BYTES


def image_to_data_uri(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


# ─── Process containment ─────────────────────────────────────────────────────
class _WindowsJob:
    """A Job Object with kill-on-close: the child dies when InkDoc does, even on a crash."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateJobObjectW.restype = wintypes.HANDLE
        self.handle = self._k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("CreateJobObjectW failed")

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._k32.SetInformationJobObject(
            self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)  # JobObjectExtendedLimitInformation
        )
        if not ok:
            self._k32.CloseHandle(self.handle)
            raise OSError("SetInformationJobObject failed")

    def assign(self, proc: subprocess.Popen) -> None:
        import ctypes
        from ctypes import wintypes

        self._k32.OpenProcess.restype = wintypes.HANDLE
        h = self._k32.OpenProcess(0x0100 | 0x0001, False, proc.pid)  # SET_QUOTA | TERMINATE
        if not h:
            raise OSError("OpenProcess failed")
        try:
            if not self._k32.AssignProcessToJobObject(self.handle, h):
                raise OSError(f"AssignProcessToJobObject failed ({ctypes.get_last_error()})")
        finally:
            self._k32.CloseHandle(h)

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


def _linux_parent_death_preexec() -> Callable[[], None] | None:
    """preexec_fn asking the kernel to SIGKILL the child when InkDoc exits.

    libc and prctl are resolved here, in the parent, so the child only calls an
    already-loaded function pointer between fork and exec.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes
        import signal

        prctl = ctypes.CDLL("libc.so.6", use_errno=True).prctl
        sigkill = int(signal.SIGKILL)

        def _preexec() -> None:
            prctl(1, sigkill, 0, 0, 0)  # PR_SET_PDEATHSIG

        return _preexec
    except Exception:
        return None


def reap_stale_process(run_dir: Path, runtime_root: Path) -> bool:
    """Kill a llama-server left running by a crashed InkDoc (PID file). True if one was stopped.

    Only a process whose executable lies inside our own runtime folder is
    touched, so a reused PID can never hit an unrelated program.
    """
    pid_file = run_dir / "llama-server.pid"
    if not pid_file.is_file():
        return False
    try:
        pid = int(json.loads(pid_file.read_text(encoding="utf-8")).get("pid", 0))
    except Exception:
        pid = 0
    stopped = False
    if pid > 0 and pid != os.getpid():
        exe = _process_executable(pid)
        if exe is not None:
            try:
                inside = Path(exe).resolve().is_relative_to(runtime_root.resolve())
            except Exception:
                inside = False
            if inside:
                try:
                    if sys.platform == "win32":
                        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                                       timeout=10, creationflags=_WIN_NO_WINDOW)
                    else:
                        import signal

                        os.kill(pid, signal.SIGKILL)
                    stopped = True
                    logger.info("Stopped a GLM-OCR server left running by a previous session (pid %d).", pid)
                except Exception as exc:
                    logger.debug("Could not stop stale llama-server %d: %s", pid, exc)
    try:
        pid_file.unlink()
    except OSError:
        pass
    return stopped


def _process_executable(pid: int) -> str | None:
    try:
        if sys.platform.startswith("linux"):
            return os.readlink(f"/proc/{pid}/exe")
        if sys.platform == "darwin":
            out = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, timeout=5)
            return out.stdout.strip() or None
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return buf.value
            finally:
                k32.CloseHandle(h)
    except Exception:
        return None
    return None


# ─── One running server ──────────────────────────────────────────────────────
class LlamaServerProcess:
    """A single llama-server child: start, wait until ready, request, stop."""

    def __init__(self, spec: LaunchSpec) -> None:
        self.spec = spec
        self.api_key = secrets.token_urlsafe(32)
        self.port = 0
        self.proc: subprocess.Popen | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
        self._job: _WindowsJob | None = None
        self._cache_dir: Path | None = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.load_seconds = 0.0
        self._started_at = 0.0

    # -- lifecycle --
    def start(self, ready_timeout: float | None = None) -> None:
        if self.spec.uses_gpu and os.environ.get("INKDOC_GLM_OCR_FORCE_GPU_FAILURE", "").strip() in ("1", "true"):
            # Test switch (manual checklist, CI on macOS runners without a usable
            # Metal GPU): behave exactly like a GPU that cannot start.
            raise GlmOcrServerError("GPU start disabled by INKDOC_GLM_OCR_FORCE_GPU_FAILURE.")
        verify_runtime(self.spec)
        missing = missing_system_libraries(self.spec.system_deps)
        if missing:
            raise MissingSystemLibraryError(describe_missing(missing))
        timeout = ready_timeout or (READY_TIMEOUT_GPU_S if self.spec.uses_gpu else READY_TIMEOUT_CPU_S)
        last_error: Exception | None = None
        for attempt in range(PORT_RETRIES):
            self.port = find_free_port()
            try:
                self._spawn()
                self._wait_ready(timeout)
                return
            except GlmOcrServerError as exc:
                last_error = exc
                tail = self.stderr_tail().lower()
                self.stop()
                port_clash = "bind" in tail or "address already in use" in tail or "couldn't bind" in tail
                if port_clash and attempt + 1 < PORT_RETRIES:
                    logger.info("GLM-OCR server port %d was taken; retrying with another.", self.port)
                    continue
                raise
        raise last_error or GlmOcrServerError("GLM-OCR server failed to start.")

    def _spawn(self) -> None:
        run_dir = self.spec.run_dir or Path(tempfile.gettempdir())
        run_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir = Path(tempfile.mkdtemp(prefix="llama-cache-", dir=str(run_dir)))
        cmd = build_command(self.spec, self.port, self.api_key)
        env = build_env(self.spec, self._cache_dir)
        self._stderr_tail.clear()
        logger.info(
            "Starting GLM-OCR server (%s, %d GPU layers) on 127.0.0.1:%d",
            self.spec.accel, self.spec.gpu_layers, self.port,
        )
        started = time.time()
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.spec.runtime_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_WIN_NO_WINDOW,
            preexec_fn=_linux_parent_death_preexec(),
            shell=False,
        )
        self._started_at = started
        if sys.platform == "win32":
            try:
                self._job = _WindowsJob()
                self._job.assign(self.proc)
            except Exception as exc:
                logger.warning("Could not tie the GLM-OCR server to InkDoc's lifetime: %s", exc)
        threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True,
                         name="GlmOcrServerStderr").start()
        try:
            (run_dir / "llama-server.pid").write_text(
                json.dumps({"pid": self.proc.pid, "started": started}), encoding="utf-8"
            )
        except OSError:
            pass

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
        except Exception:
            pass

    def stderr_tail(self, lines: int = 20) -> str:
        return "\n".join(list(self._stderr_tail)[-lines:])

    def _wait_ready(self, timeout: float) -> None:
        """Poll /health: 503 while the model loads, 200 when ready."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is None or self.proc.poll() is not None:
                code = None if self.proc is None else self.proc.returncode
                logger.warning("GLM-OCR server exited during startup (code %s):\n%s", code, self.stderr_tail())
                raise GlmOcrServerError(
                    f"The GLM-OCR server stopped while starting (exit code {code})."
                )
            try:
                status_code = self._get("/health", timeout=2.0)
            except Exception:
                status_code = None
            if status_code == 200:
                self.load_seconds = time.time() - self._started_at
                logger.info("GLM-OCR server ready in %.1f s", self.load_seconds)
                return
            if status_code not in (None, 503):
                logger.warning("GLM-OCR server health check answered %s:\n%s", status_code, self.stderr_tail())
                raise GlmOcrServerError(f"The GLM-OCR server answered its health check with HTTP {status_code}.")
            time.sleep(0.25)
        logger.warning("GLM-OCR server did not become ready in %.0f s:\n%s", timeout, self.stderr_tail())
        raise GlmOcrServerError(f"The GLM-OCR server did not become ready within {int(timeout)} seconds.")

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
        if self._job is not None:
            self._job.close()
            self._job = None
        run_dir = self.spec.run_dir
        if run_dir is not None:
            try:
                (run_dir / "llama-server.pid").unlink(missing_ok=True)
            except OSError:
                pass
        if self._cache_dir is not None:
            import shutil

            shutil.rmtree(self._cache_dir, ignore_errors=True)
            self._cache_dir = None

    # -- HTTP --
    def _get(self, path: str, timeout: float) -> int:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as err:
            return int(err.code)

    def chat(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            detail = ""
            try:
                detail = err.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise GlmOcrServerError(f"The GLM-OCR server answered HTTP {err.code}: {detail}") from err


@dataclass
class OcrResult:
    text: str
    finish_reason: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    predicted_per_second: float = 0.0


def ocr_payload(
    data_uri: str, prompt: str, max_tokens: int, *, repeat_penalty: float | None = None
) -> dict[str, Any]:
    """The request body. Sampling settings are sent per request so results never
    depend on server defaults."""
    payload: dict[str, Any] = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": TEMPERATURE,
        "seed": SEED,
        "max_tokens": int(max_tokens),
        "stream": False,
        "cache_prompt": False,
    }
    if repeat_penalty is not None:
        payload["repeat_penalty"] = repeat_penalty
    return payload


def parse_ocr_response(resp: dict[str, Any], seconds: float) -> OcrResult:
    try:
        choice = resp["choices"][0]
        text = choice.get("message", {}).get("content") or ""
        finish = str(choice.get("finish_reason") or "stop")
    except (KeyError, IndexError, TypeError) as exc:
        raise GlmOcrServerError(f"Unexpected reply from the GLM-OCR server: {exc}") from exc
    usage = resp.get("usage") or {}
    timings = resp.get("timings") or {}
    return OcrResult(
        text=text,
        finish_reason=finish,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        seconds=seconds,
        predicted_per_second=float(timings.get("predicted_per_second") or 0.0),
    )


# ─── The engine's server (singleton) ─────────────────────────────────────────
SpecFactory = Callable[[bool], LaunchSpec]   # (want_gpu) -> spec


class GlmOcrServer:
    """The long-lived server GLM-OCR conversions use. One request at a time."""

    _instance: GlmOcrServer | None = None
    _instance_lock = threading.Lock()

    def __init__(self, spec_factory: SpecFactory | None = None) -> None:
        self._spec_factory = spec_factory
        self._proc: LlamaServerProcess | None = None
        self._lock = threading.RLock()
        self._busy = False
        self._idle_timer: threading.Timer | None = None
        self._gpu_crashes = 0
        self.force_cpu = False           # set after repeated GPU crashes, for this session
        self.notices: list[str] = []     # user-facing notes for the next result
        self.on_metal_fallback: Callable[[], None] | None = None

    @classmethod
    def get_instance(cls) -> GlmOcrServer:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def peek_instance(cls) -> GlmOcrServer | None:
        """The singleton if it exists; never creates one (used at shutdown)."""
        return cls._instance

    def set_spec_factory(self, factory: SpecFactory) -> None:
        self._spec_factory = factory

    # -- state --
    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.is_alive()

    def is_busy(self) -> bool:
        return self._busy

    @property
    def accel(self) -> str:
        proc = self._proc
        return proc.spec.accel if proc else "cpu"

    def take_notices(self) -> list[str]:
        notes, self.notices = self.notices, []
        return notes

    # -- lifecycle --
    def _want_gpu(self) -> bool:
        if self.force_cpu:
            return False
        try:
            from app.core.glm_ocr_manager import GlmOcrManager

            return GlmOcrManager.get_instance().gpu_selected()
        except Exception:
            return False

    def _factory(self) -> SpecFactory:
        if self._spec_factory is not None:
            return self._spec_factory
        from app.core.glm_ocr_manager import GlmOcrManager

        return GlmOcrManager.get_instance().launch_spec

    def ensure_running(self) -> LlamaServerProcess:
        with self._lock:
            if self._proc is not None and self._proc.is_alive():
                return self._proc
            if self._proc is not None:
                self._proc.stop()
                self._proc = None
            self._coordinate_memory()
            spec = self._factory()(self._want_gpu())
            if spec.run_dir is not None:
                reap_stale_process(spec.run_dir, spec.runtime_dir.parent)
            self._proc = self._start_with_fallbacks(spec)
            return self._proc

    def _start_with_fallbacks(self, spec: LaunchSpec) -> LlamaServerProcess:
        proc = LlamaServerProcess(spec)
        try:
            proc.start()
            return proc
        except (RuntimeIntegrityError, MissingSystemLibraryError):
            if not spec.uses_gpu:
                raise
            # A missing Vulkan loader or a bad GPU runtime: the CPU build still works.
            err = sys.exc_info()[1]
        except GlmOcrServerError as exc:
            if not spec.uses_gpu:
                raise
            err = exc
        logger.warning("GLM-OCR could not start on the GPU (%s); using the CPU instead.", err)
        if spec.accel == "metal":
            cpu_spec = spec.cpu_fallback()
            if self.on_metal_fallback:
                self.on_metal_fallback()
            else:
                self._remember_metal_fallback()
            self.notices.append("GPU (Metal) could not start on this Mac; GLM-OCR used the CPU instead.")
        else:
            cpu_spec = self._factory()(False)
            self.notices.append("GPU acceleration could not start; GLM-OCR used the CPU instead.")
        self.force_cpu = True
        proc = LlamaServerProcess(cpu_spec)
        proc.start()
        return proc

    def _remember_metal_fallback(self) -> None:
        try:
            from app.core.glm_ocr_manager import GlmOcrManager

            GlmOcrManager.get_instance().record_metal_fallback()
        except Exception as exc:
            logger.debug("Could not record the Metal fallback: %s", exc)

    def _coordinate_memory(self) -> None:
        """On a machine under 16 GB, let an idle Docling worker go before starting."""
        if not is_low_memory_machine():
            return
        try:
            from app.core.engines.docling_worker_client import DoclingWorkerClient

            client = DoclingWorkerClient._instance
            if client is not None and client.is_running():
                client.shutdown_if_idle()
        except Exception as exc:
            logger.debug("Memory coordination with Docling skipped: %s", exc)

    def shutdown_if_idle(self) -> bool:
        """Stop the server only when no request is running. True when it was stopped."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._busy or self._proc is None:
                return False
            logger.info("Stopping the idle GLM-OCR server to free memory for another engine.")
            self._stop_locked()
            return True
        finally:
            self._lock.release()

    def shutdown(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        if self._proc is not None:
            self._proc.stop()
            self._proc = None

    def _reset_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._idle_timer = threading.Timer(IDLE_TIMEOUT_SECONDS, self._on_idle)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _on_idle(self) -> None:
        if self.shutdown_if_idle():
            logger.info("GLM-OCR server stopped after %d s idle.", int(IDLE_TIMEOUT_SECONDS))

    # -- requests --
    def ocr(
        self,
        data_uri: str,
        prompt: str,
        max_tokens: int,
        *,
        repeat_penalty: float | None = None,
    ) -> OcrResult:
        """Recognise one page. Restarts once and retries if the server dies mid-request."""
        with self._lock:
            self._busy = True
            try:
                attempt = 0
                while True:
                    proc = self.ensure_running()
                    timeout = PAGE_TIMEOUT_GPU_S if proc.spec.uses_gpu else PAGE_TIMEOUT_CPU_S
                    started = time.time()
                    try:
                        resp = proc.chat(
                            ocr_payload(data_uri, prompt, max_tokens, repeat_penalty=repeat_penalty),
                            timeout=timeout,
                        )
                        return parse_ocr_response(resp, time.time() - started)
                    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError, GlmOcrServerError) as exc:
                        timed_out = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
                        if not timed_out and not isinstance(exc, GlmOcrServerError) and proc.proc is not None:
                            # A crash resets the connection a moment before the
                            # process is reaped; give it that moment to exit.
                            try:
                                proc.proc.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                pass
                        died = not proc.is_alive()
                        logger.warning(
                            "GLM-OCR request failed (%s; server %s):\n%s",
                            exc, "exited" if died else "running", proc.stderr_tail(),
                        )
                        if not died and not timed_out:
                            raise GlmOcrServerError(str(exc)) from exc
                        if died and proc.spec.uses_gpu:
                            self._gpu_crashes += 1
                            if self._gpu_crashes >= GPU_CRASH_LIMIT and not self.force_cpu:
                                self.force_cpu = True
                                self.notices.append(
                                    "The GPU runtime crashed twice, so GLM-OCR switched to the CPU for the rest of this session."
                                )
                        self._stop_locked()
                        attempt += 1
                        if timed_out:
                            raise GlmOcrServerError(
                                f"GLM-OCR took longer than {int(timeout)} seconds on one page and was stopped."
                            ) from exc
                        if attempt > 1:
                            raise GlmOcrServerError(
                                "The GLM-OCR server stopped unexpectedly twice while reading this page."
                            ) from exc
            finally:
                self._busy = False
                self._reset_idle_timer()


# ─── Self-test ───────────────────────────────────────────────────────────────
@dataclass
class SelfTestResult:
    ran: bool                          # False when the process could not even start
    passed: bool
    accel: str
    seconds_per_page: float = 0.0      # estimated full-page time on this machine
    load_seconds: float = 0.0
    image_tokens: int = 0
    output: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran, "passed": self.passed, "accel": self.accel,
            "seconds_per_page": round(self.seconds_per_page, 2),
            "load_seconds": round(self.load_seconds, 2),
            "image_tokens": self.image_tokens,
            "output": self.output[:300], "error": self.error,
        }


# Output tokens a typical dense page produces; with the measured generation
# speed it turns the self-test into a realistic per-page estimate.
TYPICAL_PAGE_OUTPUT_TOKENS = 700


def run_selftest(
    spec: LaunchSpec,
    page_png: bytes,
    prompt: str,
    expect_all: list[str],
    *,
    cancel_event: threading.Event | None = None,
) -> SelfTestResult:
    """Start a private server, read the self-test page, check the tokens, stop.

    `page_png` is the self-test text placed on a page-sized canvas at the render
    size real pages use, so the vision encoder does a full page's work and the
    measured time is a usable per-page estimate.
    """
    proc = LlamaServerProcess(spec)
    try:
        try:
            proc.start()
        except GlmOcrServerError as exc:
            return SelfTestResult(ran=False, passed=False, accel=spec.accel, error=str(exc))
        if cancel_event is not None and cancel_event.is_set():
            return SelfTestResult(ran=False, passed=False, accel=spec.accel, error="Cancelled.")
        started = time.time()
        try:
            resp = proc.chat(ocr_payload(image_to_data_uri(page_png), prompt, 256), timeout=PAGE_TIMEOUT_CPU_S)
        except Exception as exc:
            return SelfTestResult(ran=True, passed=False, accel=spec.accel, load_seconds=proc.load_seconds,
                                  error=f"The model did not answer: {exc}")
        result = parse_ocr_response(resp, time.time() - started)
        text = result.text
        lowered = text.lower()
        missing = [t for t in expect_all if t.lower() not in lowered]
        first_seconds = result.seconds
        if spec.uses_gpu and not missing and not (cancel_event is not None and cancel_event.is_set()):
            # A GPU's first request also compiles its shaders (Vulkan) or kernels
            # (Metal): measured 39 s cold vs 4 s warm on a Radeon iGPU. Time a second,
            # warm request for the per-page figure; the difference is start-up cost.
            try:
                again = time.time()
                warm = parse_ocr_response(
                    proc.chat(ocr_payload(image_to_data_uri(page_png), prompt, 256), timeout=PAGE_TIMEOUT_CPU_S),
                    time.time() - again,
                )
                result = warm
            except Exception as exc:
                logger.debug("Warm GPU timing failed; keeping the cold figure: %s", exc)
        tps = result.predicted_per_second or (
            result.completion_tokens / result.seconds if result.seconds > 0 else 0.0
        )
        per_page = result.seconds + (TYPICAL_PAGE_OUTPUT_TOKENS / tps if tps > 0 else 0.0)
        warmup = max(0.0, first_seconds - result.seconds)
        return SelfTestResult(
            ran=True,
            passed=not missing,
            accel=spec.accel,
            seconds_per_page=per_page,
            # Cold start = process and model load + first-request warm-up.
            load_seconds=proc.load_seconds + warmup,
            image_tokens=result.prompt_tokens,
            output=text,
            error="" if not missing else f"The test text was not read correctly (missing: {', '.join(missing)}).",
        )
    finally:
        proc.stop()


__all__ = [
    "GlmOcrServer",
    "GlmOcrServerError",
    "LaunchSpec",
    "LlamaServerProcess",
    "MissingSystemLibraryError",
    "OcrResult",
    "RuntimeIntegrityError",
    "SelfTestResult",
    "build_command",
    "build_env",
    "image_to_data_uri",
    "is_low_memory_machine",
    "ocr_payload",
    "run_selftest",
    "verify_runtime",
]
