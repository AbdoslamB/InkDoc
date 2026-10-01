"""llama-server management for GLM-OCR, against a fake llama-server.

The fake is a small Python HTTP server that speaks the two endpoints InkDoc
uses (/health, /v1/chat/completions) and reads its behaviour from
fake_config.json in its working directory (the runtime folder), because the
real launcher passes the child a minimal environment on purpose.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.core.engines.glm_ocr_server as gs  # noqa: E402
from app.core.engines.glm_ocr_server import (  # noqa: E402
    GlmOcrServer,
    GlmOcrServerError,
    LaunchSpec,
    LlamaServerProcess,
    RuntimeIntegrityError,
    build_command,
    build_env,
    describe_missing,
    ocr_payload,
    reap_stale_process,
    verify_runtime,
)

FAKE_SERVER = r'''
import json, os, sys, time, http.server
args = sys.argv[1:]
def arg(name):
    return args[args.index(name) + 1] if name in args else None
port = int(arg("--port")); key = arg("--api-key")
cfg = json.load(open("fake_config.json")) if os.path.exists("fake_config.json") else {}
with open("argv.jsonl", "a") as fh:
    fh.write(json.dumps(args) + "\n")
if cfg.get("exit_immediately"):
    sys.stderr.write("error: failed to load model\n"); sys.stderr.flush(); sys.exit(3)
if cfg.get("fail_if_gpu") and arg("-ngl") not in (None, "0"):
    sys.stderr.write("ggml_metal_init: error: failed to initialise\n"); sys.stderr.flush(); sys.exit(4)
if cfg.get("bind_fail_once") and not os.path.exists("bind.flag"):
    open("bind.flag", "w").close()
    sys.stderr.write("couldn't bind HTTP server socket\n"); sys.stderr.flush(); sys.exit(1)
loading_until = time.time() + cfg.get("load_seconds", 0.4)
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        code = 503 if time.time() < loading_until else 200
        if self.path != "/health":
            code = 404
        self.send_response(code); self.end_headers(); self.wfile.write(b"{}")
    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + key:
            self.send_response(401); self.end_headers(); return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        content = body["messages"][0]["content"]
        record = {k: v for k, v in body.items() if k != "messages"}
        record["prompt"] = content[1]["text"]
        record["image"] = content[0]["image_url"]["url"][:22]
        with open("requests.jsonl", "a") as fh:
            fh.write(json.dumps(record) + "\n")
        if cfg.get("crash_always") or (cfg.get("crash_once") and not os.path.exists("crash.flag")):
            open("crash.flag", "w").close()
            os._exit(9)
        if cfg.get("hang"):
            time.sleep(30)
        if cfg.get("first_delay") and not os.path.exists("warm.flag"):
            open("warm.flag", "w").close()
            time.sleep(cfg["first_delay"])
        reply = cfg.get("reply", "InkDoc self-test\nReading check 2026")
        data = json.dumps({
            "choices": [{"message": {"content": reply}, "finish_reason": cfg.get("finish", "stop")}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 20},
            "timings": {"predicted_per_second": 40.0},
        }).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """A runtime folder with a fake entry, a fake DLL and fake model files."""
    rt = tmp_path / "glm-ocr" / "runtime" / "cpu"
    rt.mkdir(parents=True)
    entry = rt / ("llama-server.exe" if sys.platform == "win32" else "llama-server")
    entry.write_text(FAKE_SERVER, encoding="utf-8")
    lib = rt / "ggml-base.dll"
    lib.write_bytes(b"pretend library")
    models = tmp_path / "glm-ocr" / "models" / "65a42de1"
    models.mkdir(parents=True)
    (models / "m.gguf").write_bytes(b"m")
    (models / "mm.gguf").write_bytes(b"mm")

    real_build = gs.build_command

    def launch_with_python(spec, port, api_key):
        cmd = real_build(spec, port, api_key)
        return [sys.executable, cmd[0]] + cmd[1:]

    monkeypatch.setattr(gs, "build_command", launch_with_python)
    # The fake is pure Python: no VC++ or Vulkan needed.
    monkeypatch.setattr(gs, "missing_system_libraries", lambda deps: [])
    return rt


def make_spec(rt: Path, **kw) -> LaunchSpec:
    entry = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    files = {p.name: _sha(p) for p in rt.iterdir() if p.name in (entry, "ggml-base.dll")}
    models = rt.parent.parent / "models" / "65a42de1"
    defaults = dict(
        runtime_dir=rt, entry=entry, sha256_files=files,
        model_path=models / "m.gguf", mmproj_path=models / "mm.gguf",
        run_dir=rt.parent.parent / "run", threads=2,
    )
    defaults.update(kw)
    return LaunchSpec(**defaults)


def configure(rt: Path, **cfg) -> None:
    (rt / "fake_config.json").write_text(json.dumps(cfg), encoding="utf-8")


def requests_made(rt: Path) -> list[dict]:
    path = rt / "requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def launches(rt: Path) -> list[list[str]]:
    path = rt / "argv.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ─── Pure: command line and environment ──────────────────────────────────────

def test_command_line_is_offline_loopback_and_keyed(tmp_path):
    spec = LaunchSpec(runtime_dir=tmp_path, entry="llama-server", sha256_files={},
                      model_path=tmp_path / "m.gguf", mmproj_path=tmp_path / "mm.gguf", threads=4)
    cmd = build_command(spec, 4242, "secret-key")
    assert cmd[0] == str(tmp_path / "llama-server")
    joined = " ".join(cmd)
    for flag in ("--offline", "--no-webui", "--no-cache-prompt", "--no-mmproj-offload"):
        assert flag in cmd, flag
    assert "-hf" not in cmd and "--hf-repo" not in joined
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--port") + 1] == "4242"
    assert cmd[cmd.index("--api-key") + 1] == "secret-key"
    assert cmd[cmd.index("--seed") + 1] == "3407"
    assert cmd[cmd.index("--temp") + 1] == "0.02"
    assert cmd[cmd.index("--parallel") + 1] == "1"
    assert cmd[cmd.index("-ngl") + 1] == "0"

    gpu = build_command(LaunchSpec(runtime_dir=tmp_path, entry="llama-server", sha256_files={},
                                   model_path=tmp_path / "m", mmproj_path=tmp_path / "mm",
                                   accel="vulkan", gpu_layers=999), 1, "k")
    assert gpu[gpu.index("-ngl") + 1] == "999" and "--no-mmproj-offload" not in gpu


def test_environment_is_minimal(tmp_path, monkeypatch):
    monkeypatch.setenv("LLAMA_ARG_HOST", "0.0.0.0")
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    spec = LaunchSpec(runtime_dir=tmp_path, entry="x", sha256_files={}, model_path=tmp_path, mmproj_path=tmp_path)
    env = build_env(spec, tmp_path / "cache")
    assert env["LLAMA_CACHE"] == str(tmp_path / "cache")
    assert env["HF_HUB_OFFLINE"] == "1"
    for leaked in ("LLAMA_ARG_HOST", "HF_TOKEN", "HTTPS_PROXY"):
        assert leaked not in env
    assert str(tmp_path) in env["PATH"]


def test_request_sends_sampling_settings_per_request():
    payload = ocr_payload("data:image/png;base64,AAAA", "Text Recognition:", 2048, repeat_penalty=1.1)
    assert payload["temperature"] == 0.02 and payload["seed"] == 3407 and payload["max_tokens"] == 2048
    assert payload["repeat_penalty"] == 1.1 and payload["cache_prompt"] is False
    content = payload["messages"][0]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1] == {"type": "text", "text": "Text Recognition:"}


def test_missing_vc_runtime_is_explained():
    assert "Visual C++" in describe_missing(["MSVCP140.dll"])
    assert "Vulkan" in describe_missing(["vulkan-1.dll"])


# ─── Integrity ───────────────────────────────────────────────────────────────

def test_every_runtime_file_is_rehashed_before_launch(runtime):
    spec = make_spec(runtime)
    verify_runtime(spec)
    (runtime / "ggml-base.dll").write_bytes(b"swapped library")      # not the .exe
    with pytest.raises(RuntimeIntegrityError, match="ggml-base.dll"):
        verify_runtime(spec)
    proc = LlamaServerProcess(spec)
    with pytest.raises(RuntimeIntegrityError):
        proc.start()
    assert launches(runtime) == [], "a tampered runtime must never be started"


def test_missing_runtime_or_model_file_blocks_launch(runtime):
    spec = make_spec(runtime)
    spec.model_path.unlink()
    with pytest.raises(RuntimeIntegrityError, match="model"):
        verify_runtime(spec)


# ─── A running fake server ───────────────────────────────────────────────────

def test_start_waits_through_503_then_answers(runtime):
    configure(runtime, load_seconds=1.0)
    proc = LlamaServerProcess(make_spec(runtime))
    try:
        proc.start()
        assert proc.load_seconds >= 0.9, "ready only after /health stopped answering 503"
        resp = proc.chat(ocr_payload("data:image/png;base64,AA", "Text Recognition:", 1024), timeout=10)
        assert "InkDoc" in resp["choices"][0]["message"]["content"]
        req = requests_made(runtime)[0]
        assert req["temperature"] == 0.02 and req["seed"] == 3407 and req["max_tokens"] == 1024
        assert req["image"].startswith("data:image/png;base64")
    finally:
        proc.stop()
    assert not proc.is_alive()
    assert not (runtime.parent.parent / "run" / "llama-server.pid").exists()


def test_requests_without_the_api_key_are_refused(runtime):
    import urllib.error
    import urllib.request

    proc = LlamaServerProcess(make_spec(runtime))
    try:
        proc.start()
        req = urllib.request.Request(
            f"http://127.0.0.1:{proc.port}/v1/chat/completions",
            data=json.dumps(ocr_payload("data:,", "x", 10)).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5)
        assert err.value.code == 401
    finally:
        proc.stop()


def test_startup_exit_fails_fast_with_stderr(runtime):
    configure(runtime, exit_immediately=True)
    proc = LlamaServerProcess(make_spec(runtime))
    started = time.time()
    with pytest.raises(GlmOcrServerError, match="stopped while starting"):
        proc.start()
    assert time.time() - started < 15
    assert "failed to load model" in proc.stderr_tail()


def test_port_clash_is_retried_with_a_new_port(runtime):
    configure(runtime, bind_fail_once=True)
    proc = LlamaServerProcess(make_spec(runtime))
    try:
        proc.start()
        ports = [argv[argv.index("--port") + 1] for argv in launches(runtime)]
        assert len(ports) == 2, "one failed bind, then a retry"
    finally:
        proc.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_server_is_tied_to_inkdoc_by_a_job_object(runtime):
    import ctypes
    from ctypes import wintypes

    proc = LlamaServerProcess(make_spec(runtime))
    try:
        proc.start()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, proc.proc.pid)
        in_job = wintypes.BOOL()
        assert k32.IsProcessInJob(h, None, ctypes.byref(in_job))
        k32.CloseHandle(h)
        assert in_job.value, "llama-server must be in a kill-on-close job"
    finally:
        proc.stop()


# ─── The singleton: restarts, fallbacks, idle ────────────────────────────────

def _server(runtime, monkeypatch, *, gpu_spec=None, want_gpu=False):
    cpu_spec = make_spec(runtime)

    def factory(want: bool) -> LaunchSpec:
        return gpu_spec if (want and gpu_spec is not None) else cpu_spec

    srv = GlmOcrServer(spec_factory=factory)
    monkeypatch.setattr(srv, "_want_gpu", lambda: want_gpu and not srv.force_cpu)
    return srv


def test_crash_mid_request_restarts_once_and_retries(runtime, monkeypatch):
    configure(runtime, crash_once=True)
    srv = _server(runtime, monkeypatch)
    try:
        result = srv.ocr("data:image/png;base64,AA", "Text Recognition:", 1024)
        assert "InkDoc" in result.text
        assert len(launches(runtime)) == 2
    finally:
        srv.shutdown()


def test_second_crash_fails_the_page_clearly(runtime, monkeypatch):
    configure(runtime, crash_always=True)
    srv = _server(runtime, monkeypatch)
    try:
        with pytest.raises(GlmOcrServerError, match="twice"):
            srv.ocr("data:image/png;base64,AA", "Text Recognition:", 1024)
    finally:
        srv.shutdown()


def test_gpu_crashing_twice_switches_to_cpu_for_the_session(runtime, monkeypatch):
    gpu_rt = runtime.parent / "gpu"
    gpu_rt.mkdir()
    for p in runtime.iterdir():
        if p.is_file():
            (gpu_rt / p.name).write_bytes(p.read_bytes())
    configure(gpu_rt, crash_always=True)
    gpu_spec = make_spec(gpu_rt, accel="vulkan", gpu_layers=999)
    srv = _server(runtime, monkeypatch, gpu_spec=gpu_spec, want_gpu=True)
    try:
        with pytest.raises(GlmOcrServerError):
            srv.ocr("data:image/png;base64,AA", "Text Recognition:", 1024)
        assert srv.force_cpu
        assert any("crashed twice" in n for n in srv.take_notices())
        result = srv.ocr("data:image/png;base64,AA", "Text Recognition:", 1024)
        assert "InkDoc" in result.text and srv.accel == "cpu"
    finally:
        srv.shutdown()


def test_metal_failure_relaunches_with_ngl_0_and_remembers(runtime, monkeypatch):
    configure(runtime, fail_if_gpu=True)
    metal = make_spec(runtime, accel="metal", gpu_layers=999)
    srv = _server(runtime, monkeypatch, gpu_spec=metal, want_gpu=True)
    remembered = []
    srv.on_metal_fallback = lambda: remembered.append(True)
    try:
        result = srv.ocr("data:image/png;base64,AA", "Text Recognition:", 1024)
        assert "InkDoc" in result.text
        ngls = [argv[argv.index("-ngl") + 1] for argv in launches(runtime)]
        assert ngls == ["999", "0"]
        assert remembered == [True]
        assert any("Metal" in n for n in srv.take_notices())
    finally:
        srv.shutdown()


def test_idle_server_shuts_down(runtime, monkeypatch):
    monkeypatch.setattr(gs, "IDLE_TIMEOUT_SECONDS", 0.5)
    srv = _server(runtime, monkeypatch)
    srv.ocr("data:image/png;base64,AA", "Text Recognition:", 1024)
    assert srv.is_running()
    deadline = time.time() + 30
    while srv.is_running() and time.time() < deadline:
        time.sleep(0.1)
    assert not srv.is_running(), "the idle timer must give the memory back"


def test_busy_server_is_never_stopped_for_memory(runtime, monkeypatch):
    configure(runtime, hang=True)
    srv = _server(runtime, monkeypatch)
    def hung_request():
        try:
            srv.ocr("data:,", "x", 10)
        except GlmOcrServerError:
            pass                     # ended by the test stopping the process

    worker = threading.Thread(target=hung_request, daemon=True)
    worker.start()
    # Generous: GitHub's macOS runners take many seconds just to start the fake
    # server's Python, and a 15 s limit made this flaky there.
    deadline = time.time() + 90
    while not (requests_made(runtime) and srv._proc is not None) and time.time() < deadline:
        time.sleep(0.05)
    assert requests_made(runtime), "the request never reached the fake server"
    assert srv.is_busy() and srv._proc is not None
    assert srv.shutdown_if_idle() is False
    srv._proc.stop()            # end the hung request
    worker.join(timeout=10)
    srv.shutdown()


def test_low_memory_stops_an_idle_docling_worker_first(runtime, monkeypatch):
    from app.core.engines.docling_worker_client import DoclingWorkerClient

    calls = []

    class FakeDocling:
        def is_running(self):
            return True

        def shutdown_if_idle(self):
            calls.append("idle-check")
            return True

    monkeypatch.setattr(gs, "is_low_memory_machine", lambda: True)
    monkeypatch.setattr(DoclingWorkerClient, "_instance", FakeDocling())
    srv = _server(runtime, monkeypatch)
    try:
        srv.ensure_running()
        assert calls == ["idle-check"]
    finally:
        srv.shutdown()


def test_docling_worker_busy_is_never_interrupted():
    from app.core.engines.docling_worker_client import DoclingWorkerClient

    client = DoclingWorkerClient()
    client._proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with client._mutex:                    # a conversion holds the mutex
            assert client.shutdown_if_idle() is False
        assert client._proc is not None and client._proc.poll() is None
        assert client.shutdown_if_idle() is True
        assert client._proc is None
    finally:
        if client._proc is not None:
            client._proc.kill()


def test_stale_pid_outside_the_runtime_is_never_killed(tmp_path):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        run = tmp_path / "run"
        run.mkdir()
        (run / "llama-server.pid").write_text(json.dumps({"pid": other.pid}))
        assert reap_stale_process(run, tmp_path / "runtime") is False
        assert other.poll() is None, "an unrelated process with the same PID must survive"
        assert not (run / "llama-server.pid").exists()
    finally:
        other.kill()


def test_low_memory_detection_never_raises():
    total = gs.total_memory_bytes()
    assert total is None or total > 0
    assert isinstance(gs.is_low_memory_machine(), bool)
    assert os.cpu_count() is None or gs.default_threads() >= 1


def test_gpu_selftest_times_a_warm_request(runtime):
    from app.core.engines.glm_ocr_server import run_selftest

    configure(runtime, first_delay=2.0)          # the first GPU request compiles shaders
    spec = make_spec(runtime, accel="vulkan", gpu_layers=999)
    result = run_selftest(spec, b"png", "Text Recognition:", ["InkDoc"])
    assert result.passed
    assert len(requests_made(runtime)) == 2, "a second, warm request is timed on the GPU"
    assert result.load_seconds >= 1.9, "the warm-up counts as start-up cost"
    assert result.seconds_per_page < 2.0 + 700 / 40.0, "per-page time comes from the warm request"


def test_cpu_selftest_needs_one_request(runtime):
    from app.core.engines.glm_ocr_server import run_selftest

    result = run_selftest(make_spec(runtime), b"png", "Text Recognition:", ["InkDoc"])
    assert result.passed and len(requests_made(runtime)) == 1


@pytest.mark.parametrize("cpus, platform, expected", [
    (3, "darwin", 2), (8, "darwin", 6), (4, "win32", 3), (16, "win32", 8), (12, "linux", 6), (1, "linux", 1), (64, "linux", 8),
])
def test_thread_count_suits_the_machine(monkeypatch, cpus, platform, expected):
    monkeypatch.setattr(gs.os, "cpu_count", lambda: cpus)
    monkeypatch.setattr(gs.sys, "platform", platform)
    assert gs.default_threads() == expected
