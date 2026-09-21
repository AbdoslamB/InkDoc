"""Tests for Docling worker client non-blocking reader, timeout enforcement, and stderr drainage (H-7 + M-7)."""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.engines.docling_worker_client import DoclingWorkerClient  # noqa: E402


def test_worker_launch_does_not_suppress_site_module():
    """The worker must never be launched with -S.

    -S suppresses the `site` module, and `site` is what puts a virtualenv's own
    site-packages on sys.path. With -S the pack's interpreter starts and answers
    ping -- the docling import is lazy, inside _get_converter -- and then every
    conversion fails with "No module named 'docling'". This shipped in
    docling-pack-v1 and was invisible because the build's smoke test asserted
    nothing about the conversion result.

    Asserted against the source of both launch sites, which must stay identical:
    if they drift, the build smoke test stops validating what production runs.
    """
    client_src = (
        REPO_ROOT / "app" / "core" / "engines" / "docling_worker_client.py"
    ).read_text(encoding="utf-8")
    build_src = (REPO_ROOT / "scripts" / "build_pack.py").read_text(encoding="utf-8")

    client_cmd = re.search(
        r"cmd = \[\s*str\(interpreter\),(.*?)\]", client_src, re.DOTALL
    )
    assert client_cmd, "could not locate the worker launch command in docling_worker_client.py"
    client_flags = re.findall(r'"(-[A-Za-z])"', client_cmd.group(1))

    build_cmd = re.search(r"cmd = \[str\(extracted_python\),(.*?)\]", build_src)
    assert build_cmd, "could not locate the worker launch command in build_pack.py"
    build_flags = re.findall(r'"(-[A-Za-z])"', build_cmd.group(1))

    assert "-S" not in client_flags, (
        "worker launched with -S: site-packages will be missing and every "
        "conversion will fail with No module named 'docling'"
    )
    assert "-S" not in build_flags, "build_pack.py smoke test launches the worker with -S"
    assert "-I" in client_flags, "worker must stay in isolated mode (-I)"
    assert client_flags == build_flags, (
        f"launch flags drifted: worker client {client_flags} vs build_pack {build_flags}. "
        "The post-build smoke test must run the worker exactly as production does."
    )
    print("[OK] test_worker_launch_does_not_suppress_site_module passed")


def test_docling_inference_device_is_pinned_to_cpu():
    """Docling's accelerator device must be pinned, not left on "auto".

    "auto" selects the Metal (MPS) backend on Apple Silicon. The pack installs
    CPU-only torch, and MPS is a path CI cannot exercise at all: the macOS pack
    build failed loading the layout model with "MPS backend out of memory" while
    trying to allocate 3.5 KiB. Pinning CPU keeps inference identical across the
    three platforms and keeps the build smoke test on the same path as production.

    Checked at all three places that launch or configure the worker, since a value
    set in only some of them reintroduces the divergence.
    """
    worker_src = (REPO_ROOT / "app" / "core" / "engines" / "worker.py").read_text(encoding="utf-8")
    client_src = (
        REPO_ROOT / "app" / "core" / "engines" / "docling_worker_client.py"
    ).read_text(encoding="utf-8")
    build_src = (REPO_ROOT / "scripts" / "build_pack.py").read_text(encoding="utf-8")

    assert re.search(r'DOCLING_DEVICE"\s*,\s*"cpu"', worker_src), (
        "worker.py must pin DOCLING_DEVICE=cpu; docling otherwise defaults to auto "
        "and selects MPS on Apple Silicon"
    )
    assert re.search(r'"DOCLING_DEVICE"\s*:\s*"cpu"', client_src), (
        "DoclingWorkerClient minimal_env must pin DOCLING_DEVICE=cpu"
    )
    assert re.search(r'"DOCLING_DEVICE"\s*:\s*"cpu"', build_src), (
        "build_pack.py smoke test env must pin DOCLING_DEVICE=cpu, or it stops "
        "validating the device production actually uses"
    )
    print("[OK] test_docling_inference_device_is_pinned_to_cpu passed")


def test_docling_worker_floods_stderr():
    """Verify that a worker flooding stderr (>100KB) does not deadlock on pipe buffer (M-7)."""
    with tempfile.TemporaryDirectory() as td:
        worker_script = Path(td) / "fake_stderr_flooder.py"
        worker_script.write_text(
            """
import sys, json

# Flood >200KB of diagnostic logging to stderr
sys.stderr.write("E" * 250000)
sys.stderr.flush()

# Send ready signal
print(json.dumps({"status": "ready"}), flush=True)

while True:
    line = sys.stdin.readline()
    if not line:
        break
    req = json.loads(line)
    if req.get("action") == "convert":
        # Flood more data to stderr during conversion
        sys.stderr.write("F" * 250000)
        sys.stderr.flush()

        with open(req["output_file"], "w", encoding="utf-8") as out:

            out.write("# Successful Markdown Output")
        print(json.dumps({"status": "ok", "job_id": req["job_id"]}), flush=True)
    elif req.get("action") == "shutdown":
        break
""",
            encoding="utf-8",
        )

        dummy_doc = Path(td) / "test.pdf"
        dummy_doc.write_bytes(b"%PDF-1.4 dummy")

        client = DoclingWorkerClient()

        # Patch _ensure_worker_running to spawn our fake worker script
        def mock_ensure_worker_running(*args, **kwargs):
            if client._proc is not None and client._proc.poll() is None:
                return client._proc
            cmd = [sys.executable, str(worker_script)]
            proc = client._spawn_process(cmd, cwd=str(td), env={})
            line = client._stdout_queue.get(timeout=5.0)
            assert json.loads(line).get("status") == "ready"
            client._proc = proc
            return proc

        with patch.object(client, "_ensure_worker_running", side_effect=mock_ensure_worker_running):
            # Run conversion: should succeed without deadlocking on the 500KB stderr flood
            start = time.time()
            result = client.convert_file(str(dummy_doc), timeout=10.0)
            duration = time.time() - start
            assert result == "# Successful Markdown Output"
            assert duration < 5.0, f"Conversion took unexpectedly long: {duration}s"
            client.shutdown()

    print("[OK] test_docling_worker_floods_stderr passed")


def test_docling_worker_hangs_mid_line():
    """Verify that a worker hanging mid-line without newline triggers real timeout and terminates (H-7)."""
    with tempfile.TemporaryDirectory() as td:
        worker_script = Path(td) / "fake_midline_hanger.py"
        worker_script.write_text(
            """
import sys, json, time

# Send ready signal
print(json.dumps({"status": "ready"}), flush=True)

while True:
    line = sys.stdin.readline()
    if not line:
        break
    req = json.loads(line)
    if req.get("action") == "convert":
        # Intentionally output partial line without newline and sleep indefinitely
        sys.stdout.write('{"status": "partial_hang')
        sys.stdout.flush()
        time.sleep(30.0)
    elif req.get("action") == "shutdown":
        break
""",
            encoding="utf-8",
        )

        dummy_doc = Path(td) / "test.pdf"
        dummy_doc.write_bytes(b"%PDF-1.4 dummy")

        client = DoclingWorkerClient()

        def mock_ensure_worker_running(*args, **kwargs):
            if client._proc is not None and client._proc.poll() is None:
                return client._proc
            cmd = [sys.executable, str(worker_script)]
            proc = client._spawn_process(cmd, cwd=str(td), env={})
            line = client._stdout_queue.get(timeout=5.0)
            assert json.loads(line).get("status") == "ready"
            client._proc = proc
            return proc

        with patch.object(client, "_ensure_worker_running", side_effect=mock_ensure_worker_running):
            start = time.time()
            try:
                # With reader thread + queue, timeout=1.0 MUST fire and raise TimeoutError
                client.convert_file(str(dummy_doc), timeout=1.0)
                raise AssertionError("Should have raised TimeoutError")
            except TimeoutError as exc:
                duration = time.time() - start
                assert "timed out" in str(exc).lower()
                assert duration < 3.0, f"Timeout took too long to fire: {duration}s"
                # Worker process must have been terminated
                assert client._proc is None

            client.shutdown()

    print("[OK] test_docling_worker_hangs_mid_line passed")


if __name__ == "__main__":
    test_worker_launch_does_not_suppress_site_module()
    test_docling_inference_device_is_pinned_to_cpu()
    test_docling_worker_floods_stderr()
    test_docling_worker_hangs_mid_line()
    print("\nALL DOCLING WORKER TESTS PASSED!")
