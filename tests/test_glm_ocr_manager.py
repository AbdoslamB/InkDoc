"""GLM-OCR install, fallback, update, verify and remove (downloads mocked at stream_download)."""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.core.glm_ocr_manager as gm  # noqa: E402
from app.core.download_utils import DownloadError, RateLimitedError, SecurityError  # noqa: E402
from app.core.engines.glm_ocr_server import SelfTestResult  # noqa: E402
from app.core.glm_ocr_catalogue import CatalogueProvider  # noqa: E402
from app.core.glm_ocr_manager import GlmOcrInstallError, GlmOcrManager  # noqa: E402

MODEL = b"model-bytes" * 10
MMPROJ = b"mmproj-bytes" * 10
HF = "https://huggingface.co/ggml-org/GLM-OCR-GGUF/resolve/rev1"
MIRROR = "https://github.com/AbdoslamB/InkDoc/releases/download/glm-ocr-v1"
LLAMA = "https://github.com/ggml-org/llama.cpp/releases/download/b1"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def runtime_zip(path: Path, files: dict[str, bytes]) -> bytes:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path.read_bytes()


class World:
    """A fake catalogue, its download payloads and a settings store."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.cpu_files = {"llama-server.exe": b"server", "ggml-base.dll": b"base", "ggml-cpu-x64.dll": b"cpu"}
        self.gpu_files = {**self.cpu_files, "ggml-vulkan.dll": b"vulkan"}
        self.cpu_zip = runtime_zip(tmp / "cpu.zip", {**self.cpu_files, "llama-cli.exe": b"never extracted"})
        self.gpu_zip = runtime_zip(tmp / "gpu.zip", self.gpu_files)
        self.payloads: dict[str, object] = {
            f"{HF}/m.gguf": MODEL, f"{MIRROR}/m.gguf": MODEL,
            f"{HF}/mm.gguf": MMPROJ, f"{MIRROR}/mm.gguf": MMPROJ,
            f"{LLAMA}/cpu.zip": self.cpu_zip, f"{MIRROR}/cpu.zip": self.cpu_zip,
            f"{LLAMA}/gpu.zip": self.gpu_zip, f"{MIRROR}/gpu.zip": self.gpu_zip,
        }
        self.calls: list[tuple[str, str]] = []
        self.settings = {"glm_ocr_download_source": "auto", "glm_ocr_use_gpu": False, "glm_ocr_measured_spp": {}}
        self.catalogue_path = tmp / "catalogue.json"
        self.write_catalogue()

    def catalogue(self, cpu_sha256_files=None) -> dict:
        def file(name, data):
            return {"name": name, "size": len(data), "sha256": sha(data), "sources": [
                {"kind": "upstream", "url": f"{HF}/{name}"}, {"kind": "mirror", "url": f"{MIRROR}/{name}"}]}

        def runtime(name, data, files, accel):
            return {"platform": "windows-x86_64", "accel": accel, "archive": "zip", "size": len(data),
                    "sha256": sha(data), "entry": "llama-server.exe",
                    "sha256_files": {k: sha(v) for k, v in files.items()}, "system_deps": [],
                    "sources": [{"kind": "upstream", "url": f"{LLAMA}/{name}"},
                                {"kind": "mirror", "url": f"{MIRROR}/{name}"}]}

        cpu = runtime("cpu.zip", self.cpu_zip, self.cpu_files, "cpu")
        if cpu_sha256_files is not None:
            cpu["sha256_files"] = cpu_sha256_files
        return {
            "catalogue_version": 1, "min_app_version": "1.1.0",
            "model": {"version": "2026.03.10-rev1", "license": "MIT", "source_repo": "ggml-org/GLM-OCR-GGUF",
                      "source_revision": "rev1aaaabbbb", "default_variant": "q8",
                      "variants": {"q8": {"label": "Standard (Q8)", "model_file": "m.gguf", "mmproj_file": "mm.gguf",
                                          "files": [file("m.gguf", MODEL), file("mm.gguf", MMPROJ)]}}},
            "runtime": {"version": "b1", "license": "MIT", "source_repo": "ggml-org/llama.cpp",
                        "variants": {"win-x64-cpu": cpu,
                                     "win-x64-vulkan": runtime("gpu.zip", self.gpu_zip, self.gpu_files, "vulkan")}},
            "selftest": {"image": "glm_ocr_selftest.png", "prompt": "Text Recognition:", "expect_all": ["InkDoc"]},
        }

    def write_catalogue(self, **kw) -> None:
        self.catalogue_path.write_text(json.dumps(self.catalogue(**kw)), encoding="utf-8")

    # -- fakes --
    def stream_download(self, url, destination_path, expected_size=None, cancel_event=None, **kw):
        self.calls.append((url, destination_path.name))
        payload = self.payloads[url]
        if isinstance(payload, BaseException):
            raise payload
        if callable(payload):
            payload = payload(cancel_event)
        if expected_size is not None and len(payload) != expected_size:
            raise SecurityError("size mismatch")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(payload)
        return sha(payload)


class FakeSettings:
    def __init__(self, store):
        self.store = store

    def get_settings(self):
        return dict(self.store)

    def update_settings(self, updates):
        self.store.update(updates)
        return dict(self.store)


def passing_selftest(spec, png, prompt, expect, cancel_event=None):
    seconds = 4.0 if spec.gpu_layers else 6.0
    return SelfTestResult(ran=True, passed=True, accel=spec.accel, seconds_per_page=seconds, load_seconds=1.0,
                          output="InkDoc self-test 2026")


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(gm, "stream_download", w.stream_download)
    monkeypatch.setattr(gm, "runtime_platform_key", lambda: "windows-x86_64")
    monkeypatch.setattr(gm, "vulkan_loader_present", lambda: True)
    monkeypatch.setattr(gm, "check_disk_space", lambda *a, **k: None)
    monkeypatch.setattr("app.core.engines.glm_ocr_server.run_selftest", passing_selftest)
    monkeypatch.setattr("app.core.engine_manager.EngineManager.get_instance", lambda: FakeSettings(w.settings))
    return w


@pytest.fixture
def mgr(world, tmp_path):
    provider = CatalogueProvider(bundled_path=world.catalogue_path)
    provider.refresh_remote = lambda force=False: provider.current()   # never the network
    return GlmOcrManager(base_dir=tmp_path / "engines" / "glm-ocr", provider=provider)


def sources_used(mgr) -> dict:
    marker = mgr.read_marker()
    return {name: f["source"] for name, f in marker["model"]["files"].items()} | {
        "runtime": marker["runtime"]["cpu"]["source"]}


def nothing_installed(mgr) -> bool:
    base = mgr.base_dir
    return (not mgr.marker_path.exists() and not (base / "models").exists()
            and not (base / "runtime").exists() and not (base / ".staging").exists())


# ─── Happy path ──────────────────────────────────────────────────────────────

def test_upstream_ok_never_touches_the_mirror(world, mgr):
    mgr.install()
    assert not any(url.startswith(MIRROR) for url, _ in world.calls)
    assert set(sources_used(mgr).values()) == {"upstream"}
    assert mgr.is_installed() and mgr.is_usable()
    base = mgr.base_dir
    assert (base / "models" / "rev1aaaa" / "m.gguf").read_bytes() == MODEL
    assert (base / "licenses" / "LICENSE-GLM-OCR.txt").is_file()
    assert mgr.get_progress()["status"] == "complete"


def test_runtime_extraction_keeps_only_listed_files(world, mgr):
    mgr.install()
    files = sorted(p.name for p in (mgr.base_dir / "runtime" / "cpu").iterdir())
    assert files == sorted(world.cpu_files)
    assert "llama-cli.exe" not in files


def test_partial_files_are_kept_per_source(world, mgr):
    world.payloads[f"{HF}/m.gguf"] = DownloadError("connection reset")
    mgr.install()
    names = [dest for url, dest in world.calls if url.endswith("/m.gguf")]
    assert names == ["m.gguf.upstream", "m.gguf.mirror"]


# ─── Fallback ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("failure", [
    DownloadError("Network error after 2 retries: getaddrinfo failed"),
    DownloadError("HTTP 404 Not Found downloading. Client errors are not retried."),
    TimeoutError("timed out"),
    SecurityError("Host 'evil.example' is not in the trusted CDN allowlist"),
    RateLimitedError("HTTP 429 from 'huggingface.co': rate limited or unavailable."),
    b"wrong bytes of the same size!!" * 4,     # wrong size or hash: never accepted
])
def test_any_upstream_failure_falls_back_to_the_mirror(world, mgr, failure):
    world.payloads[f"{HF}/m.gguf"] = failure
    mgr.install()
    assert sources_used(mgr)["m.gguf"] == "mirror"
    assert sources_used(mgr)["mm.gguf"] == "upstream"
    assert mgr.is_usable()


def test_hash_mismatch_from_upstream_is_treated_as_an_outage(world, mgr):
    world.payloads[f"{HF}/m.gguf"] = b"x" * len(MODEL)          # right size, wrong hash
    mgr.install()
    assert sources_used(mgr)["m.gguf"] == "mirror"
    assert (mgr.base_dir / "models" / "rev1aaaa" / "m.gguf").read_bytes() == MODEL


def test_both_sources_failing_names_both_and_leaves_nothing(world, mgr):
    world.payloads[f"{HF}/m.gguf"] = DownloadError("HTTP 403 Forbidden")
    world.payloads[f"{MIRROR}/m.gguf"] = DownloadError("HTTP 404 Not Found")
    with pytest.raises(GlmOcrInstallError) as err:
        mgr.install()
    assert "Hugging Face" in str(err.value) and "InkDoc mirror" in str(err.value)
    assert nothing_installed(mgr)
    prog = mgr.get_progress()
    assert prog["status"] == "error" and prog["can_try_mirror"]


def test_mirror_only_skips_upstream(world, mgr):
    world.settings["glm_ocr_download_source"] = "mirror_only"
    mgr.install()
    assert all(url.startswith(MIRROR) for url, _ in world.calls)
    assert set(sources_used(mgr).values()) == {"mirror"}


def test_progress_reports_source_and_fallback(world, mgr):
    seen = []

    def watching(cancel):
        seen.append(dict(mgr.get_progress()))
        return MODEL

    world.payloads[f"{HF}/m.gguf"] = DownloadError("blocked")
    world.payloads[f"{MIRROR}/m.gguf"] = watching
    mgr.install()
    assert seen[0]["source"] == "mirror" and seen[0]["source_label"] == "InkDoc mirror"
    assert "Hugging Face" in seen[0]["fallback_reason"]


# ─── Failure modes ───────────────────────────────────────────────────────────

def test_cancel_mid_download_installs_nothing(world, mgr):
    def cancel_then_fail(cancel):
        cancel.set()
        raise DownloadError("Download cancelled by user.")

    world.payloads[f"{HF}/mm.gguf"] = cancel_then_fail
    with pytest.raises(GlmOcrInstallError):
        mgr.install()
    assert mgr.get_progress()["status"] == "cancelled"
    assert nothing_installed(mgr)


def test_crash_before_the_marker_never_reads_as_installed(world, mgr, monkeypatch):
    def boom(path, data):
        raise OSError("power cut")

    monkeypatch.setattr(gm, "_atomic_write_json", boom)
    with pytest.raises(OSError):
        mgr.install()
    assert not mgr.is_installed() and not mgr.is_usable()


def test_tampered_runtime_file_fails_the_install(world, mgr):
    bad = {k: sha(v) for k, v in world.cpu_files.items()}
    bad["ggml-base.dll"] = "0" * 64
    world.write_catalogue(cpu_sha256_files=bad)
    with pytest.raises(SecurityError, match="ggml-base.dll"):
        mgr.install()
    assert nothing_installed(mgr)


def test_disk_space_refusal_downloads_nothing(world, mgr, monkeypatch):
    def full(*a, **k):
        raise DownloadError("Insufficient disk space")

    monkeypatch.setattr(gm, "check_disk_space", full)
    with pytest.raises(GlmOcrInstallError, match="disk space"):
        mgr.install()
    assert world.calls == []


def test_failed_selftest_fails_the_install(world, mgr, monkeypatch):
    monkeypatch.setattr("app.core.engines.glm_ocr_server.run_selftest",
                        lambda *a, **k: SelfTestResult(ran=True, passed=False, accel="cpu", error="missing: InkDoc"))
    with pytest.raises(GlmOcrInstallError, match="self-test"):
        mgr.install()
    assert nothing_installed(mgr)
    assert mgr.get_progress()["can_try_mirror"]


def test_selftest_that_could_not_run_installs_but_is_not_usable(world, mgr, monkeypatch):
    monkeypatch.setattr("app.core.engines.glm_ocr_server.run_selftest",
                        lambda *a, **k: SelfTestResult(ran=False, passed=False, accel="cpu",
                                                       error="The GLM-OCR server stopped while starting."))
    mgr.install()
    assert mgr.is_installed() and not mgr.is_usable()
    assert "could not run" in mgr.get_status()["reason"]


# ─── GPU, update, status, remove ─────────────────────────────────────────────

def test_gpu_runtime_installed_and_selected_when_faster(world, mgr):
    mgr.install(gpu=True)
    marker = mgr.read_marker()
    assert marker["runtime"]["gpu"]["accel"] == "vulkan" and marker["gpu_usable"]
    assert world.settings["glm_ocr_use_gpu"] is True
    assert mgr.launch_spec(True).gpu_layers > 0
    assert mgr.launch_spec(False).gpu_layers == 0


def test_gpu_slower_than_cpu_is_kept_but_not_selected(world, mgr, monkeypatch):
    def slow_gpu(spec, *a, **k):
        secs = 9.0 if spec.gpu_layers else 6.0
        return SelfTestResult(ran=True, passed=True, accel=spec.accel, seconds_per_page=secs, load_seconds=1)

    monkeypatch.setattr("app.core.engines.glm_ocr_server.run_selftest", slow_gpu)
    mgr.install(gpu=True)
    marker = mgr.read_marker()
    assert marker["runtime"]["gpu"] and not marker["gpu_usable"]
    assert "Slower than the CPU" in marker["gpu_reason"]
    assert not mgr.gpu_selected()


def test_update_downloads_only_what_changed(world, mgr, tmp_path):
    mgr.install()
    world.cpu_files["llama-server.exe"] = b"server v2"
    world.cpu_zip = runtime_zip(tmp_path / "cpu2.zip", world.cpu_files)
    world.payloads[f"{LLAMA}/cpu.zip"] = world.cpu_zip
    world.write_catalogue()
    mgr.provider._bundled = None                      # re-read the catalogue
    status = mgr.get_status()
    assert status["update_available"] and status["update"]["runtime_changed"]
    assert not status["update"]["model_changed"]
    world.calls.clear()
    mgr.install(operation="update")
    assert [url for url, _ in world.calls] == [f"{LLAMA}/cpu.zip"]
    assert (mgr.base_dir / "runtime" / "cpu" / "llama-server.exe").read_bytes() == b"server v2"
    assert not mgr.get_status()["update_available"]


def test_failed_update_keeps_the_old_install_working(world, mgr, tmp_path, monkeypatch):
    mgr.install()
    world.cpu_files["llama-server.exe"] = b"server v2"
    world.cpu_zip = runtime_zip(tmp_path / "cpu2.zip", world.cpu_files)
    world.payloads[f"{LLAMA}/cpu.zip"] = world.cpu_zip
    world.payloads[f"{MIRROR}/cpu.zip"] = world.cpu_zip
    world.write_catalogue()
    mgr.provider._bundled = None
    monkeypatch.setattr("app.core.engines.glm_ocr_server.run_selftest",
                        lambda *a, **k: SelfTestResult(ran=True, passed=False, accel="cpu", error="bad build"))
    with pytest.raises(GlmOcrInstallError):
        mgr.install(operation="update")
    assert (mgr.base_dir / "runtime" / "cpu" / "llama-server.exe").read_bytes() == b"server"
    assert mgr.is_usable()


def test_status_reads_the_marker_and_never_hashes(world, mgr):
    mgr.install()
    with patch("hashlib.sha256", side_effect=AssertionError("status must not hash")):
        st = mgr.get_status()
    assert st["status"] == "installed" and st["usable"]
    assert st["version"]["model"] == "2026.03.10-rev1"
    assert st["long_job_seconds"] == gm.LONG_JOB_SECONDS


def test_verify_rehashes_everything(world, mgr):
    mgr.install()
    assert mgr.verify()["valid"]
    (mgr.base_dir / "models" / "rev1aaaa" / "mm.gguf").write_bytes(b"tampered")
    res = mgr.verify()
    assert not res["valid"] and any("mm.gguf" in m for m in res["mismatches"])


def test_remove_deletes_the_marker_first(world, mgr, monkeypatch):
    mgr.install()
    seen = []
    real_rmtree = gm.shutil.rmtree

    def recording(path, *a, **k):
        seen.append(mgr.marker_path.exists())
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(gm.shutil, "rmtree", recording)
    mgr.remove()
    assert seen and seen[0] is False, "the marker must be gone before any file is deleted"
    assert not mgr.base_dir.exists()
    assert mgr.get_status()["status"] == "installable"


def test_estimate_includes_cold_start(world, mgr):
    mgr.install()
    assert mgr.estimate_seconds(10, server_running=True) == pytest.approx(60.0)
    assert mgr.estimate_seconds(10, server_running=False) == pytest.approx(61.0)
    mgr.record_measurement(12.0, 5)
    assert 6.0 < mgr.seconds_per_page() <= 12.0


def test_speed_is_filed_under_the_accelerator_actually_used(world, mgr, monkeypatch):
    mgr.install(gpu=True)
    assert mgr.active_accel() == "vulkan"
    mgr.record_measurement(80.0, 3, accel="cpu")            # GPU fell back mid-session
    assert (world.settings["glm_ocr_measured_spp"] or {}).get("vulkan") is None
    assert world.settings["glm_ocr_measured_spp"]["cpu"] == 80.0

    from app.core.engines.glm_ocr_server import GlmOcrServer

    fallen_back = GlmOcrServer(spec_factory=lambda gpu: None)
    fallen_back.force_cpu = True
    monkeypatch.setattr(GlmOcrServer, "_instance", fallen_back)
    assert mgr.active_accel() == "cpu", "estimates follow the session's CPU fallback"
    assert mgr.seconds_per_page() == 80.0
