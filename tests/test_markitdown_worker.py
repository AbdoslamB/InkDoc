"""The MarkItDown worker process (app/core/engines/markitdown_worker*.py).

InkDoc's own process never loads markitdown; conversions run in a worker that
starts on demand and stops when idle. These tests run real workers (a few
seconds each to start) and check that nothing about a conversion changes:
the same text, the same errors, cancel and crashes handled, and no worker
left behind.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.core import converter
from app.core.converter import ConversionOptions, run_markitdown
from app.core.engines import markitdown_worker_client as mwc
from app.core.jobs import ConversionCancelledError

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _results_folders_in_tmp_path(tmp_path, monkeypatch):
    """Every worker results folder a test makes goes into pytest's tmp_path,
    never the real temp folder (some tests end a client without shutdown())."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _child_env(tmp_path) -> dict:
    """Environment for a child InkDoc process that writes its temp folders into tmp_path."""
    return {**os.environ, "TMP": str(tmp_path), "TEMP": str(tmp_path), "TMPDIR": str(tmp_path)}


@pytest.fixture
def worker():
    """A worker of its own (not the shared one), stopped afterwards."""
    w = mwc.MarkItDownWorker()
    yield w
    w.shutdown()


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.WinDLL("kernel32")
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie (exited, not yet reaped) counts as gone
        status = Path(f"/proc/{pid}/status").read_text()
        return "\nState:\tZ" not in status
    except OSError:
        return True


def _wait(predicate, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


class _Job:
    def __init__(self, after: float) -> None:
        self._at = time.time() + after

    def cancelled(self) -> bool:
        return time.time() >= self._at


# ─── Same results as converting in InkDoc's own process ──────────────────────

def test_same_text_as_in_process_including_line_endings(worker, tmp_path):
    src = tmp_path / "crlf.txt"
    src.write_bytes("Line one\r\nLine two é文\r\n\r\nEnd".encode())
    options = ConversionOptions()
    assert worker.convert(str(src), options, {}, None) == run_markitdown(str(src), options, {}, None)


def test_stream_info_hints_reach_the_worker(worker, tmp_path):
    src = tmp_path / "page.bin"
    src.write_text("<html><body><h1>Title</h1><p>Body text</p></body></html>", encoding="utf-8")
    hints = {"extension": ".html", "mimetype": "text/html"}
    out = worker.convert(str(src), ConversionOptions(), hints, "https://example.com/page")
    assert out == run_markitdown(str(src), ConversionOptions(), hints, "https://example.com/page")
    assert "# Title" in out


def test_the_table_finder_gives_the_same_result_in_the_worker(worker, tmp_path):
    from app.core.table_finder import find_tables
    from tests._pdf_fixtures import write_pdf
    from tests._table_fixtures import rating_grid

    pages, _ = rating_grid()
    pdf = str(write_pdf(tmp_path / "rating.pdf", pages))

    def same(result: dict) -> dict:
        return {k: v for k, v in result.items() if k != "elapsed_ms"}

    for detectors in ([], ["rating_grids"], ["segments", "borderless_tables"]):
        assert same(worker.find_tables(pdf, [1], detectors=detectors)) == same(
            find_tables(pdf, [1], detectors=detectors)
        ), detectors
    found = worker.find_tables(pdf, [1], detectors=["rating_grids"])
    assert [t["kind"] for b in found["pages"][0]["bands"] for t in b["tables"]] == ["rating_grids"]


def test_the_layout_model_runs_in_the_worker(worker, tmp_path):
    from app.core.addon_manager import AddonManager
    from app.core.engines.layout_model import ADDON, MODEL_FILE, detect_tables
    from tests._pdf_fixtures import write_pdf
    from tests._table_fixtures import side_by_side

    pages, _ = side_by_side(True)
    pdf = str(write_pdf(tmp_path / "pair.pdf", pages))
    # No model there: an answer that says so, not a crashed worker.
    missing = worker.find_layout(pdf, [1], str(tmp_path / "no-model"), "0" * 64)
    assert missing["pages"] == [] and missing["error"]

    location = AddonManager().model_location(ADDON)
    if location is None:
        pytest.skip("the layout model is not on this computer")
    folder, digests = location
    found = worker.find_layout(pdf, [1], str(folder), digests[MODEL_FILE])

    def same(result: dict) -> dict:
        return {k: v for k, v in result.items() if k != "elapsed_ms"}

    assert same(found) == same(detect_tables(pdf, [1], str(folder), digests[MODEL_FILE]))
    assert len(found["pages"][0]["tables"]) == 2


def test_errors_keep_markitdown_s_own_message(worker, tmp_path):
    src = tmp_path / "blob.unknownext"
    src.write_bytes(os.urandom(2048))
    with pytest.raises(Exception) as here:
        run_markitdown(str(src), ConversionOptions(), {}, None)
    with pytest.raises(mwc.MarkItDownConversionError) as there:
        worker.convert(str(src), ConversionOptions(), {}, None)
    assert str(there.value) == str(here.value), "the server words its error from this text"
    assert there.value.error_type == type(here.value).__name__


def test_missing_file_is_an_ordinary_error(worker, tmp_path):
    with pytest.raises(mwc.MarkItDownConversionError):
        worker.convert(str(tmp_path / "nope.txt"), ConversionOptions(), {}, None)
    # And the worker is still there for the next one.
    good = tmp_path / "ok.txt"
    good.write_text("fine", encoding="utf-8")
    assert worker.convert(str(good), ConversionOptions(), {}, None) == "fine"


# ─── One worker, several conversions ─────────────────────────────────────────

def test_parallel_conversions_share_one_worker(worker, tmp_path):
    files = []
    for i in range(6):
        f = tmp_path / f"n{i}.txt"
        f.write_text(f"document {i}", encoding="utf-8")
        files.append(f)
    results: dict[int, str] = {}
    pids = set()

    def run(i):
        results[i] = worker.convert(str(files[i]), ConversionOptions(), {}, None)
        pids.add(worker.pid())

    threads = [threading.Thread(target=run, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert results == {i: f"document {i}" for i in range(6)}
    assert len(pids) == 1, "a conversion during start-up waits for that start; never a second worker"
    assert not list(worker._tmpdir.iterdir()), "result files are deleted once read"


def test_warm_starts_once_and_returns_at_once(worker):
    t0 = time.time()
    assert worker.warm() == "starting"
    assert time.time() - t0 < 2, "never waits for the worker"
    assert worker.warm() == "starting"
    pid = worker.pid()
    assert _wait(lambda: worker.warm() == "ready", 90)
    assert worker.pid() == pid, "no second worker"


def test_idle_worker_stops_and_the_next_conversion_restarts_it(worker, tmp_path, monkeypatch):
    monkeypatch.setattr(mwc, "IDLE_TIMEOUT_S", 1.0)
    src = tmp_path / "a.txt"
    src.write_text("one", encoding="utf-8")
    assert worker.convert(str(src), ConversionOptions(), {}, None) == "one"
    first = worker.pid()
    assert _wait(lambda: not _alive(first), 15), "stopped after the idle timeout"
    assert worker.pid() is None
    assert worker.convert(str(src), ConversionOptions(), {}, None) == "one"
    assert worker.pid() not in (None, first)


def test_idle_clock_does_not_run_during_a_conversion(tmp_path, monkeypatch):
    monkeypatch.setenv("INKDOC_MARKITDOWN_WORKER_TEST_DELAY_S", "3")
    monkeypatch.setattr(mwc, "IDLE_TIMEOUT_S", 1.0)
    w = mwc.MarkItDownWorker()
    try:
        src = tmp_path / "slow.txt"
        src.write_text("slow", encoding="utf-8")
        assert w.convert(str(src), ConversionOptions(), {}, None) == "slow"
    finally:
        w.shutdown()


# ─── Cancel and crashes ──────────────────────────────────────────────────────

def test_cancel_kills_the_worker_when_it_is_the_only_conversion(tmp_path, monkeypatch):
    monkeypatch.setenv("INKDOC_MARKITDOWN_WORKER_TEST_DELAY_S", "30")
    w = mwc.MarkItDownWorker()
    try:
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        w.warm()
        assert _wait(lambda: w.warm() == "ready", 90)
        pid = w.pid()
        options = ConversionOptions()
        options.job = _Job(after=0.5)
        t0 = time.time()
        with pytest.raises(ConversionCancelledError):
            w.convert(str(src), options, {}, None)
        assert time.time() - t0 < 5, "a hard stop, not the end of the 30 s conversion"
        assert not _alive(pid), "gone before cancel returns"
        # The worker held the document open; it is released, so the server can
        # delete its temporary copy right away (Windows refuses while it's open).
        src.unlink()
        assert not list(w._tmpdir.iterdir())
    finally:
        w.shutdown()


def test_cancel_spares_other_conversions(tmp_path, monkeypatch):
    """Another conversion shares the worker: the cancelled one is let finish and
    discarded (as before 1.3), so its document is no longer open afterwards."""
    monkeypatch.setenv("INKDOC_MARKITDOWN_WORKER_TEST_DELAY_S", "2")
    w = mwc.MarkItDownWorker()
    try:
        keep, drop = tmp_path / "keep.txt", tmp_path / "drop.txt"
        keep.write_text("kept", encoding="utf-8")
        drop.write_text("dropped", encoding="utf-8")
        result = {}
        t = threading.Thread(target=lambda: result.update(v=w.convert(str(keep), ConversionOptions(), {}, None)))
        t.start()
        assert _wait(lambda: w._active == 1, 90)
        options = ConversionOptions()
        options.job = _Job(after=0.2)
        with pytest.raises(ConversionCancelledError):
            w.convert(str(drop), options, {}, None)
        drop.unlink()  # released: the server can delete its copy at once
        t.join(60)
        assert result.get("v") == "kept", "the other conversion finished"
        assert not list(w._tmpdir.iterdir()), "no result left behind"
    finally:
        w.shutdown()


def test_a_crash_fails_that_conversion_and_the_next_one_works(tmp_path, monkeypatch):
    monkeypatch.setenv("INKDOC_MARKITDOWN_WORKER_TEST_DELAY_S", "30")
    w = mwc.MarkItDownWorker()
    try:
        src = tmp_path / "a.txt"
        src.write_text("after", encoding="utf-8")
        w.warm()
        assert _wait(lambda: w.warm() == "ready", 90)
        crashed = w._proc
        threading.Timer(1.0, crashed.kill).start()
        with pytest.raises(mwc.MarkItDownWorkerCrashedError) as err:
            w.convert(str(src), ConversionOptions(), {}, None)
        assert "stopped unexpectedly" in str(err.value)
        assert isinstance(err.value, converter.UserFacingConversionError), "shown to the user as written"
        monkeypatch.delenv("INKDOC_MARKITDOWN_WORKER_TEST_DELAY_S")
        assert w.convert(str(src), ConversionOptions(), {}, None) == "after"
        assert w._proc is not crashed
    finally:
        w.shutdown()


# ─── When the worker can't start ─────────────────────────────────────────────

def test_a_worker_that_cannot_start_falls_back_to_in_process(tmp_path, monkeypatch):
    shared = mwc.MarkItDownWorker()
    monkeypatch.setattr(mwc.MarkItDownWorker, "_instance", shared)
    monkeypatch.setattr(mwc, "worker_command", lambda: [str(tmp_path / "no-such-program.exe")])
    src = tmp_path / "a.txt"
    src.write_text("still converts", encoding="utf-8")
    assert mwc.convert_with_markitdown(str(src), ConversionOptions(), {}, None) == "still converts"
    assert not shared.usable(), "not retried on every conversion"
    assert shared.warm() == "unavailable"


def test_a_worker_that_reports_a_load_error_falls_back(tmp_path, monkeypatch):
    shared = mwc.MarkItDownWorker()
    monkeypatch.setattr(mwc.MarkItDownWorker, "_instance", shared)
    script = 'print(\'{"status": "error", "error": "ImportError: no markitdown"}\', flush=True); import time; time.sleep(5)'
    monkeypatch.setattr(mwc, "worker_command", lambda: [sys.executable, "-c", script])
    src = tmp_path / "a.txt"
    src.write_text("fallback text", encoding="utf-8")
    assert mwc.convert_with_markitdown(str(src), ConversionOptions(), {}, None) == "fallback text"
    shared.shutdown()


def test_worker_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("INKDOC_MARKITDOWN_WORKER", "0")
    shared = mwc.MarkItDownWorker()
    monkeypatch.setattr(mwc.MarkItDownWorker, "_instance", shared)
    src = tmp_path / "a.txt"
    src.write_text("in process", encoding="utf-8")
    assert mwc.convert_with_markitdown(str(src), ConversionOptions(), {}, None) == "in process"
    assert shared.pid() is None


# ─── The protocol and the process ────────────────────────────────────────────

def test_nothing_written_to_stdout_can_reach_the_protocol():
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from app.core.engines.markitdown_worker import claim_stdout
        ipc = claim_stdout()
        print("a stray print")
        os.write(1, b"C-level noise on fd 1\\n")
        ipc.write('{{"status": "ready"}}\\n')
        ipc.flush()
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert out.stdout.splitlines() == ['{"status": "ready"}']
    assert "a stray print" in out.stderr and "C-level noise" in out.stderr


def test_output_file_is_never_written_through(tmp_path):
    """The worker creates its result file exclusively; one already there is refused."""
    src = tmp_path / "a.txt"
    src.write_text("text", encoding="utf-8")
    out = tmp_path / "taken.md"
    out.write_text("someone else's file", encoding="utf-8")
    proc = subprocess.Popen(mwc.worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, cwd=str(REPO_ROOT))
    try:
        assert json.loads(proc.stdout.readline())["status"] == "ready"
        proc.stdin.write(json.dumps({"id": 1, "action": "convert", "source": str(src), "output": str(out),
                                     "options": {}, "stream_info": {}, "url": None}) + "\n")
        proc.stdin.flush()
        reply = json.loads(proc.stdout.readline())
        assert reply["ok"] is False and reply["type"] == "FileExistsError"
        assert out.read_text(encoding="utf-8") == "someone else's file"
    finally:
        proc.kill()
        proc.wait(10)


def test_worker_gets_no_session_token(worker, monkeypatch):
    seen = {}
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        seen["env"] = kwargs.get("env")
        return real_popen(*args, **kwargs)

    monkeypatch.setenv("INKDOC_SESSION_TOKEN", "secret-token")
    monkeypatch.setattr(mwc.subprocess, "Popen", spy)
    worker.warm()
    assert "INKDOC_SESSION_TOKEN" not in seen["env"]


def test_worker_dies_with_inkdoc(tmp_path):
    """Windows: the Job Object; Linux: PR_SET_PDEATHSIG; macOS: the worker's own watch."""
    script = textwrap.dedent(f"""
        import os, sys, time
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from app.core.engines.markitdown_worker_client import MarkItDownWorker
        w = MarkItDownWorker.get_instance()
        w.warm()
        for _ in range(900):
            if w.warm() == "ready":
                break
            time.sleep(0.1)
        print(w.pid(), flush=True)
        os._exit(0)   # a crash: no shutdown, no atexit
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
                         env=_child_env(tmp_path))
    pid = int(out.stdout.strip().splitlines()[-1])
    assert _wait(lambda: not _alive(pid), 15), f"worker {pid} outlived InkDoc"


@pytest.mark.skipif(os.name != "posix", reason="Windows uses the Job Object; there is no parent change to watch")
def test_worker_watches_its_parent_on_its_own(tmp_path):
    """macOS and Linux: the worker notices the parent change itself, even while converting.

    (No PR_SET_PDEATHSIG: Linux sends it when the spawning *thread* ends.)
    """
    script = textwrap.dedent(f"""
        import os, sys, time
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from app.core.engines.markitdown_worker_client import MarkItDownWorker
        w = MarkItDownWorker.get_instance()
        w.warm()
        for _ in range(900):
            if w.warm() == "ready":
                break
            time.sleep(0.1)
        print(w.pid(), flush=True)
        os._exit(0)
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
                         env=_child_env(tmp_path))
    pid = int(out.stdout.strip().splitlines()[-1])
    assert _wait(lambda: not _alive(pid), 15), f"worker {pid} outlived InkDoc without a parent-death signal"


def test_worker_outlives_the_thread_that_started_it(worker, tmp_path):
    """Linux once killed it here: PR_SET_PDEATHSIG fires when the spawning thread ends."""
    starter = threading.Thread(target=worker.warm)
    starter.start()
    starter.join()
    assert _wait(lambda: worker.warm() == "ready", 90)
    pid = worker.pid()
    time.sleep(2)  # the starting thread is long gone
    src = tmp_path / "a.txt"
    src.write_text("still here", encoding="utf-8")
    assert worker.convert(str(src), ConversionOptions(), {}, None) == "still here"
    assert worker.pid() == pid


def test_cancel_while_the_worker_starts(tmp_path):
    w = mwc.MarkItDownWorker()
    try:
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        options = ConversionOptions()
        options.job = _Job(after=0.0)
        t0 = time.time()
        with pytest.raises(ConversionCancelledError):
            w.convert(str(src), options, {}, None)
        assert time.time() - t0 < 1.5, "not after the whole start-up"
    finally:
        w.shutdown()


def test_usable_right_after_the_computer_starts(monkeypatch):
    """time.monotonic() counts from boot on Windows and Linux; it can be under a minute."""
    monkeypatch.setattr(mwc.time, "monotonic", lambda: 5.0)
    assert mwc.MarkItDownWorker().usable()


def test_worker_runs_in_its_private_empty_folder(worker, monkeypatch):
    """Not next to the app: nothing there (an ffmpeg.exe in Downloads) is picked up."""
    seen = {}
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(mwc.subprocess, "Popen", spy)
    worker.warm()
    assert Path(seen["cwd"]) == worker._tmpdir
    assert Path(seen["cwd"]).name.startswith(mwc.TMPDIR_PREFIX)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_results_folder_is_private_and_replaced_if_tampered_with(worker, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    assert worker.convert(str(src), ConversionOptions(), {}, None) == "x"
    first = worker._tmpdir
    assert (os.stat(first).st_mode & 0o777) == 0o700
    # A cleaner removed it and someone put a link in its place.
    first.rmdir()
    first.symlink_to(tmp_path)
    try:
        assert worker.convert(str(src), ConversionOptions(), {}, None) == "x"
        assert worker._tmpdir != first, "a fresh private folder, not the link"
        assert not [p for p in tmp_path.iterdir() if p.suffix == ".md"], "nothing written through the link"
    finally:
        first.unlink()


def test_stale_results_folders_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(mwc.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / f"{mwc.TMPDIR_PREFIX}old"
    old.mkdir(mode=0o700)
    (old / "left.md").write_text("from a crash", encoding="utf-8")
    fresh = tmp_path / f"{mwc.TMPDIR_PREFIX}fresh"
    fresh.mkdir(mode=0o700)
    day_ago = time.time() - 2 * 24 * 3600
    os.utime(old, (day_ago, day_ago))
    mwc._remove_stale_tmpdirs()
    assert not old.exists()
    assert fresh.exists(), "another InkDoc's current folder is left alone"


# ─── The endpoint the UI calls ───────────────────────────────────────────────

def test_warmup_endpoint_needs_the_token_and_starts_the_worker(monkeypatch):
    from fastapi.testclient import TestClient

    import app.server.server as server_module

    shared = mwc.MarkItDownWorker()
    monkeypatch.setattr(mwc.MarkItDownWorker, "_instance", shared)
    client = TestClient(server_module.app)
    try:
        assert client.post("/convert/warmup").status_code == 403
        resp = client.post("/InkDoc/convert/warmup", headers={"X-InkDoc-Token": server_module.SESSION_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["status"] in ("starting", "ready")
        assert shared.pid() is not None
    finally:
        shared.shutdown()


# ─── Memory cap (large files) ────────────────────────────────────────────────

def test_conversion_memory_limit_override(monkeypatch):
    from app.core.engines import process_guard as pg

    monkeypatch.setenv("INKDOC_CONVERSION_MEMORY_LIMIT_MB", "0")
    assert pg.conversion_memory_limit() is None
    monkeypatch.setenv("INKDOC_CONVERSION_MEMORY_LIMIT_MB", "512")
    assert pg.conversion_memory_limit() == 512 * 1024 * 1024
    monkeypatch.setenv("INKDOC_CONVERSION_MEMORY_LIMIT_MB", "lots")
    default = pg.conversion_memory_limit()
    if sys.platform == "win32":
        assert default is not None and default >= pg.MIN_CONVERSION_MEMORY_BYTES
    else:
        assert default is None


@pytest.mark.skipif(sys.platform != "win32", reason="the per-process memory cap is a Windows Job Object limit")
def test_job_memory_limit_turns_a_runaway_allocation_into_memory_error():
    from app.core.engines.process_guard import WindowsJob

    child = textwrap.dedent("""
        import sys
        sys.stdin.readline()            # wait until the parent has put us in the job
        try:
            block = bytearray(600 * 1024 * 1024)
            print("allocated", flush=True)
        except MemoryError:
            print("MemoryError", flush=True)
    """)
    job = WindowsJob(memory_limit_bytes=256 * 1024 * 1024)
    proc = subprocess.Popen([sys.executable, "-c", child], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        job.assign(proc)
        out, _ = proc.communicate("go\n", timeout=60)
        assert out.strip() == "MemoryError"
    finally:
        if proc.poll() is None:
            proc.kill()
        job.close()


class _FakeStdin:
    """Answers every request the way a worker that ran out of memory does."""

    def __init__(self, worker) -> None:
        self.worker = worker

    def write(self, line: str) -> None:
        rid = json.loads(line)["id"]
        pending = self.worker._pending.pop(rid)
        pending.response = {"id": rid, "ok": False, "error": "", "type": "MemoryError"}
        pending.event.set()

    def flush(self) -> None:
        pass


class _FakeProc:
    def __init__(self, worker) -> None:
        self.stdin = _FakeStdin(worker)

    def poll(self):
        return None


def test_out_of_memory_in_the_worker_is_a_clear_error(worker, tmp_path, monkeypatch):
    proc = _FakeProc(worker)
    worker._proc = proc
    monkeypatch.setattr(worker, "_wait_ready", lambda job=None: proc)

    worker.memory_limit_bytes = 8 * 1024**3
    with pytest.raises(mwc.MarkItDownOutOfMemoryError) as caught:
        worker.convert(str(tmp_path / "big.txt"), ConversionOptions(), {}, None)
    message = str(caught.value)
    assert "more memory than InkDoc lets one conversion use (8 GB" in message
    assert isinstance(caught.value, converter.UserFacingConversionError)  # shown as worded

    worker.memory_limit_bytes = None
    with pytest.raises(mwc.MarkItDownOutOfMemoryError, match="ran out of memory"):
        worker.convert(str(tmp_path / "big.txt"), ConversionOptions(), {}, None)
    worker._proc = None  # nothing real to shut down


def test_worker_reports_memory_error_even_when_markitdown_wraps_it():
    from app.core.engines.markitdown_worker import _error_type

    class Attempt:
        def __init__(self, exc):
            self.converter = object()
            self.exc_info = (type(exc), exc, None)

    class FileConversionException(Exception):
        def __init__(self, attempts):
            super().__init__("File conversion failed after 1 attempts")
            self.attempts = attempts

    assert _error_type(MemoryError()) == "MemoryError"
    assert _error_type(FileConversionException([Attempt(ValueError("bad")), Attempt(MemoryError())])) == "MemoryError"
    assert _error_type(FileConversionException([Attempt(ValueError("bad"))])) == "FileConversionException"
    assert _error_type(FileConversionException(None)) == "FileConversionException"
    assert _error_type(ValueError("x")) == "ValueError"
