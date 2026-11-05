"""Keeping helper processes from outliving InkDoc.

Shared by the engines that run in a process of their own (GLM-OCR's
llama-server, the MarkItDown worker). Standard library only, so importing it
costs nothing at startup.

- Windows: a Job Object with kill-on-close. The child dies with InkDoc, even
  when InkDoc crashes or is ended from Task Manager. The MarkItDown worker's
  job also caps its memory (conversion_memory_limit()).
- Linux: PR_SET_PDEATHSIG (llama-server). Note that Linux sends it when the
  *thread* that started the child ends, not the process; only use it for a
  child started from a thread that lives as long as InkDoc. The MarkItDown
  worker is started from server threads that come and go, so it doesn't use
  it: it exits when its stdin closes or its parent changes
  (app/core/engines/markitdown_worker.py), which also covers macOS.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable


class WindowsJob:
    """A Job Object with kill-on-close: the child dies when InkDoc does, even on a crash.

    `memory_limit_bytes`, if given, caps the memory each process in the job may
    commit: past it, allocations fail (Python raises MemoryError) instead of the
    computer running out of memory.
    """

    def __init__(self, memory_limit_bytes: int | None = None) -> None:
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
        if memory_limit_bytes:
            info.BasicLimitInformation.LimitFlags |= 0x0100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = int(memory_limit_bytes)
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


MIN_CONVERSION_MEMORY_BYTES = 2 * 1024**3


def conversion_memory_limit() -> int | None:
    """Memory one conversion process may use on Windows: half of this computer's RAM, at least 2 GB.

    Leaves the other half to everything else, so a huge or malicious file can't
    make the computer swap. INKDOC_CONVERSION_MEMORY_LIMIT_MB overrides it ("0"
    turns it off). None (no limit) on other systems, or when the RAM size can't
    be read: there is no reliable per-process limit there, since RLIMIT_AS also
    counts address space that libraries such as onnxruntime reserve but never use.
    """
    override = os.environ.get("INKDOC_CONVERSION_MEMORY_LIMIT_MB", "").strip()
    if override:
        try:
            mb = int(override)
        except ValueError:
            mb = -1
        if mb >= 0:
            return mb * 1024 * 1024 or None
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        total = int(status.ullTotalPhys)
    except Exception:
        return None
    if total <= 0:
        return None
    return max(total // 2, MIN_CONVERSION_MEMORY_BYTES)


def linux_parent_death_preexec() -> Callable[[], None] | None:
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
