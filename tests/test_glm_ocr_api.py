"""GLM-OCR through the REST API: guards, engine endpoints, lane, payload, estimate."""
from __future__ import annotations

import asyncio
import io
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import app.server.server as server_module  # noqa: E402
from app.core.converter import UserFacingConversionError  # noqa: E402
from app.core.queue_model import EngineKind  # noqa: E402
from app.server.server import app  # noqa: E402

client = TestClient(app)
TOKEN = {"X-InkDoc-Token": server_module.SESSION_TOKEN}


def status(**overrides) -> dict:
    base = {
        "name": "GLM-OCR", "status": "installed", "installed": True, "usable": True, "reason": "",
        "variants": [{"id": "q8", "label": "Standard (Q8)", "size_bytes": 1}], "variant": "q8",
        "update_available": False, "progress": {"status": "idle"},
    }
    base.update(overrides)
    return base


class FakeMgr:
    def __init__(self, st=None, busy=False):
        self.st = st or status()
        self.busy = busy
        self.calls: list[str] = []
        self.started = threading.Event()

    def get_status(self, check_remote=False):
        return dict(self.st)

    def is_busy(self):
        return self.busy

    def is_installed(self):
        return self.st["installed"]

    def is_usable(self):
        return self.st["usable"]

    def read_marker(self):
        return {"model": {"version": "2026.03.10-65a42de1"}} if self.st["installed"] else {}

    def get_progress(self):
        return {"status": "idle"}

    def install(self, **kw):
        self.calls.append(f"install:{kw}")
        self.started.set()

    def install_gpu(self):
        self.calls.append("install_gpu")
        self.started.set()

    def set_gpu(self, enabled):
        self.calls.append(f"set_gpu:{enabled}")
        return {"needs_download": enabled, "selected": False}

    def rerun_selftest(self):
        self.calls.append("selftest")
        self.started.set()

    def cancel(self):
        self.calls.append("cancel")
        return True

    def verify(self):
        self.calls.append("verify")
        return {"valid": True}

    def remove(self):
        self.calls.append("remove")
        return {"status": "removed", "engine": "glm_ocr"}


@pytest.fixture
def mgr(monkeypatch):
    fake = FakeMgr()
    monkeypatch.setattr("app.core.glm_ocr_manager.GlmOcrManager.get_instance", lambda: fake)
    monkeypatch.setattr(server_module, "_conversion_semaphores", {})
    return fake


def post_file(engine="glm_ocr", name="scan.png", data=b"\x89PNG fake", **params):
    query = "&".join([f"engine={engine}", "response_format=json"] + [f"{k}={v}" for k, v in params.items()])
    return client.post(f"/convert/file?{query}", files={"file": (name, io.BytesIO(data))})


# ─── Availability guard ──────────────────────────────────────────────────────

def test_not_downloaded_is_409_not_installed(mgr):
    mgr.st = status(status="installable", installed=False, usable=False)
    resp = post_file()
    assert resp.status_code == 409 and resp.json()["detail"].startswith("engine_not_installed")


def test_failed_selftest_is_409_not_ready(mgr):
    mgr.st = status(usable=False, reason="The self-test failed")
    resp = post_file()
    assert resp.status_code == 409 and resp.json()["detail"].startswith("engine_not_ready")


@pytest.mark.parametrize("state", ["unsupported", "unreleased", "defective"])
def test_unavailable_platform_is_400_unsupported(mgr, state):
    mgr.st = status(status=state, installed=False, usable=False, reason="Not here")
    resp = post_file()
    assert resp.status_code == 400 and resp.json()["detail"].startswith("engine_unsupported")


def test_engine_aliases_resolve():
    for name in ("glm_ocr", "glm-ocr", "GLM-OCR"):
        assert server_module.resolve_engine(name) == EngineKind.GLM_OCR


# ─── Engine endpoints ────────────────────────────────────────────────────────

@pytest.mark.parametrize("method, path", [
    ("post", "/engines/glm_ocr/install"),
    ("post", "/engines/glm_ocr/remove"),
    ("post", "/engines/glm_ocr/verify"),
    ("post", "/engines/glm_ocr/cancel"),
    ("post", "/engines/glm_ocr/selftest"),
    ("post", "/engines/glm_ocr/update"),
])
def test_management_endpoints_need_the_session_token(mgr, method, path):
    resp = getattr(client, method)(path)
    assert resp.status_code == 403
    assert mgr.calls == []


def test_gpu_switch_needs_the_session_token(mgr):
    assert client.post("/engines/glm_ocr/gpu", json={"enabled": True}).status_code == 403
    assert mgr.calls == []


def test_install_starts_in_the_background(mgr):
    mgr.st = status(status="installable", installed=False, usable=False)
    resp = client.post("/engines/glm_ocr/install", headers=TOKEN, json={"gpu": False, "variant": "q8"})
    assert resp.status_code == 200 and resp.json()["status"] == "started"
    assert mgr.started.wait(5)
    assert "variant': 'q8'" in mgr.calls[0]


def test_install_refuses_an_unknown_precision(mgr):
    mgr.st = status(status="installable", installed=False, usable=False)
    resp = client.post("/engines/glm_ocr/install", headers=TOKEN, json={"variant": "q2"})
    assert resp.status_code == 400


def test_gpu_enable_downloads_the_runtime(mgr):
    resp = client.post("/engines/glm_ocr/gpu", headers=TOKEN, json={"enabled": True})
    assert resp.json()["status"] == "started"
    assert mgr.started.wait(5) and "install_gpu" in mgr.calls


def test_remove_verify_cancel_dispatch_to_glm(mgr):
    assert client.post("/engines/glm-ocr/verify", headers=TOKEN).json() == {"valid": True}
    assert client.post("/engines/glm_ocr/cancel", headers=TOKEN).json()["status"] == "cancelled"
    assert client.post("/engines/glm_ocr/remove", headers=TOKEN).json()["status"] == "removed"
    assert mgr.calls == ["verify", "cancel", "remove"]


def test_engines_list_includes_glm_and_never_hashes():
    with patch("hashlib.sha256", side_effect=AssertionError("status must not hash")):
        resp = client.get("/engines")
    assert resp.status_code == 200
    glm = resp.json()["engines"]["glm_ocr"]
    assert glm["name"] == "GLM-OCR" and "status" in glm and "long_job_seconds" in glm


def test_health_reports_glm(mgr):
    data = client.get("/health").json()
    assert data["glm_ocr_available"] is True and data["glm_ocr_version"].startswith("2026")


def test_download_source_setting_is_validated():
    bad = client.post("/settings", headers=TOKEN, json={"glm_ocr_download_source": "anywhere"})
    assert bad.status_code == 400


# ─── Conversions ─────────────────────────────────────────────────────────────

def test_warnings_and_engine_reach_the_response(mgr):
    def fake_convert(item, options):
        options.engine_used = EngineKind.GLM_OCR
        options.engine_warnings = ["Page 2: the model started repeating itself."]
        assert options.job is not None, "the engine gets the job handle for page progress"
        return "# Text"

    with patch("app.server.server.convert_item", side_effect=fake_convert):
        resp = post_file(job_id="ui-test-job-1234")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["engine_used"] == "glm_ocr"
        assert data["warnings"] == ["Page 2: the model started repeating itself."]
        text = client.post("/convert/file?engine=glm_ocr", files={"file": ("a.png", io.BytesIO(b"x"))})
        assert "repeating" in text.headers["X-Engine-Warnings"]


def test_user_facing_errors_are_shown_verbatim(mgr):
    message = "This PDF is password-protected. Remove the password and try again."
    with patch("app.server.server.convert_item", side_effect=UserFacingConversionError(message)):
        resp = post_file(name="locked.pdf")
    assert resp.status_code == 500 and resp.json()["detail"] == message


def test_glm_jobs_do_not_starve_markitdown(mgr):
    import httpx

    release = threading.Event()
    active, peak = 0, 0
    lock = threading.Lock()

    def fake_convert(item, options):
        nonlocal active, peak
        if item.engine != EngineKind.GLM_OCR:
            return "markitdown output"
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            release.wait(timeout=10)
        finally:
            with lock:
                active -= 1
        return "glm output"

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:13118") as http:
            with patch("app.server.server.convert_item", side_effect=fake_convert):
                jobs = [asyncio.create_task(http.post("/InkDoc/convert/file?engine=glm_ocr",
                                                      files={"file": (f"scan{i}.png", b"x")})) for i in range(3)]
                await asyncio.sleep(0.2)
                try:
                    quick = await asyncio.wait_for(
                        http.post("/InkDoc/convert/file", files={"file": ("quick.txt", b"hello")}), timeout=5)
                finally:
                    release.set()
                results = await asyncio.gather(*jobs)
        assert quick.status_code == 200 and quick.text == "markitdown output"
        assert all(r.status_code == 200 for r in results)
        assert peak == 1, f"{peak} GLM-OCR jobs ran at once; one server reads one page at a time"

    asyncio.run(run())


def test_estimate_endpoint(mgr, tmp_path):
    from tests._pdf_fixtures import build_pdf, prose_page

    class EstMgr(FakeMgr):
        def estimate_seconds(self, pages):
            return 15.0 * pages

        def seconds_per_page(self):
            return 15.0

        def active_accel(self):
            return "cpu"

    est = EstMgr()
    with patch("app.core.glm_ocr_manager.GlmOcrManager.get_instance", return_value=est):
        resp = client.post("/convert/estimate?engine=glm_ocr",
                           files={"file": ("doc.pdf", io.BytesIO(build_pdf([prose_page()] * 30)))})
    data = resp.json()
    assert resp.status_code == 200
    assert data["pages"] == 30 and data["seconds_est"] == 450.0 and data["long_job"] is True
    assert client.post("/convert/estimate?engine=markitdown",
                       files={"file": ("a.pdf", io.BytesIO(b"%PDF"))}).status_code == 400
