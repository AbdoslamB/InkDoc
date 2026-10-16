"""MarkItDown worker: every MarkItDown conversion runs in this process.

Started on demand by app/core/engines/markitdown_worker_client.py (the frozen
app runs its own executable with --markitdown-worker; a source checkout runs
main.py --markitdown-worker) and stopped after a few idle minutes. InkDoc's
own process therefore never loads markitdown, and the ~95 MB it costs (pandas,
onnxruntime, magika and ~20 other libraries; Python never returns it) goes back
to the system when no one is converting.

Protocol, one JSON object per line:

    stdin   {"id": 7, "action": "convert", "source": "...", "output": "...",
             "options": {...}, "stream_info": {...}, "url": "..."}
            {"action": "shutdown"}
    stdout  {"status": "ready", "pid": 1234, "markitdown": "0.1.7"}   once, when warm
            {"status": "error", "error": "..."}                         instead, if it can't load
            {"id": 7, "ok": true, "none": false}
            {"id": 7, "ok": false, "error": "...", "type": "FileConversionException"}

The Markdown never goes through the pipe: it is written to the private file
named by "output", created here exclusively. Conversions run on a few threads
at once, as they did inside InkDoc's own process.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

DEFAULT_THREADS = 3

# The ConversionOptions fields a conversion may set, with their types.
_OPTION_FIELDS: dict[str, type] = {
    "enable_plugins": bool,
    "keep_data_uris": bool,
    "docintel_enabled": bool,
    "docintel_endpoint": str,
    "cu_enabled": bool,
    "cu_endpoint": str,
    "cu_analyzer_id": str,
}


def claim_stdout():
    """The protocol stream: the original stdout. fd 1 then points at stderr.

    markitdown wraps many libraries, and some (onnxruntime and other C/C++
    code, a stray print()) write straight to file descriptor 1. Moving fd 1
    before anything is imported means none of that can land in the protocol.
    """
    ipc_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(ipc_fd, "w", encoding="utf-8", newline="\n")


def _watch_parent() -> None:
    """Exit when InkDoc is gone, even in the middle of a conversion (macOS, Linux).

    A POSIX process whose parent dies is handed to another parent, so a change
    of parent pid means InkDoc exited or crashed. (stdin closing ends the
    request loop too, but only between requests is that noticed at once.)
    Windows has no such change; there the Job Object the client puts us in
    does the job.
    """
    if os.name != "posix":
        return
    parent = os.getppid()

    def run() -> None:
        while True:
            time.sleep(2.0)
            if os.getppid() != parent:
                os._exit(0)

    threading.Thread(target=run, daemon=True, name="markitdown-parent-watch").start()


def _options(raw: Any) -> Any:
    from app.core.converter import ConversionOptions

    kwargs = {}
    if isinstance(raw, dict):
        for key, kind in _OPTION_FIELDS.items():
            if key in raw and isinstance(raw[key], kind):
                kwargs[key] = raw[key]
    return ConversionOptions(**kwargs)


def _convert(request: dict, send) -> None:
    rid = request.get("id")
    try:
        from app.core.converter import run_markitdown

        # Test hook (tests/test_markitdown_worker.py): a conversion that takes a
        # while, holding the document open as markitdown does, to exercise
        # cancel and a crash mid-conversion. Never set otherwise.
        delay = os.environ.get("INKDOC_MARKITDOWN_WORKER_TEST_DELAY_S")
        if delay:
            with open(str(request["source"]), "rb"):
                time.sleep(float(delay))
        stream_info = request.get("stream_info")
        text = run_markitdown(
            str(request["source"]),
            _options(request.get("options")),
            stream_info if isinstance(stream_info, dict) else {},
            request.get("url") if isinstance(request.get("url"), str) else None,
        )
        # O_EXCL: the file must not exist yet, so nothing already at that name
        # (a link included) is written through; 0600: readable by this user
        # only. newline="": the text is stored exactly as converted.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(str(request["output"]), flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text or "")
        send({"id": rid, "ok": True, "none": text is None})
    except Exception as exc:
        # The message exactly as an in-process conversion raised it: the server
        # words its error for the user from this text.
        send({"id": rid, "ok": False, "error": str(exc), "type": type(exc).__name__})


def main(argv: list[str]) -> int:
    try:
        ipc = claim_stdout()
        # By file descriptor: in a windowed app sys.stdin may be a stand-in
        # (main.py), but the pipe the client created is fd 0.
        requests = open(0, encoding="utf-8", closefd=False)  # noqa: SIM115
    except OSError:
        return 3

    threads = DEFAULT_THREADS
    if "--threads" in argv:
        try:
            threads = max(1, min(8, int(argv[argv.index("--threads") + 1])))
        except (IndexError, ValueError):
            pass

    write_lock = threading.Lock()

    def send(message: dict) -> None:
        with write_lock:
            ipc.write(json.dumps(message) + "\n")
            ipc.flush()

    _watch_parent()

    try:
        import markitdown  # noqa: F401  (the whole point of this process)

        from app.core.converter import ConversionOptions, get_markitdown_version

        # One instance up front loads what MarkItDown() loads lazily (magika's
        # model), so the first real conversion doesn't pay for it.
        ConversionOptions().build_markitdown()
    except Exception as exc:
        send({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
        return 4

    send({"status": "ready", "pid": os.getpid(), "markitdown": get_markitdown_version()})

    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="markitdown")
    for line in requests:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        action = request.get("action")
        if action == "convert":
            pool.submit(_convert, request, send)
        elif action == "shutdown":
            break

    # Asked to stop (only ever when idle), or stdin closed because InkDoc is
    # gone: nothing left is worth finishing.
    try:
        ipc.flush()
    except OSError:
        pass
    os._exit(0)
