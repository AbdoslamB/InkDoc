"""Download, verify, self-test, update and remove the GLM-OCR engine.

GLM-OCR is the fourth engine: the zai-org GLM-OCR model (GGUF build by
ggml-org) run by llama.cpp's llama-server. Like Docling it is not bundled; the
user downloads it from Settings with one click (~1.45 GB), after a warning
about its size and speed.

Layout on disk (EngineManager.get_engines_base_dir() / "glm-ocr"):

    runtime/cpu/        llama-server + the libraries the catalogue lists, nothing else
    runtime/gpu/        the Vulkan build (Windows/Linux, optional)
    models/<rev8>/      GLM-OCR-Q8_0.gguf (or f16), mmproj-GLM-OCR-Q8_0.gguf
    licenses/           LICENSE-GLM-OCR.txt, LICENSE-llama.cpp.txt
    selftest/           output of the last self-test, for diagnostics
    run/                PID file and an empty LLAMA_CACHE (runtime only)
    .partial/           resumable partial downloads, one per file *and source*
    .staging/           everything a running install builds before the switch
    .inkdoc-glm-ocr.json   completion marker, written LAST

Contracts kept here:
- Installed means the marker exists. It is written only after every file is in
  place and the CPU self-test ran, so an interrupted install never looks
  installed. Removal deletes the marker first for the same reason.
- Usable means installed and the CPU self-test passed. Auto and the server
  check this on every conversion; it reads the marker only, never hashes.
- Upstream first, mirror second, per file. Any failure on a source (network,
  HTTP >= 400, rate limit, disallowed redirect, stall, size or hash mismatch)
  moves to the next source. A wrong hash is never accepted from anywhere.
- Partial files are kept per source (.upstream / .mirror) so a resume never
  splices bytes from two servers.
- Nothing downloads without the user's click, and updates are never automatic.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.download_utils import (
    MODEL_DOWNLOAD_HOST_SUFFIXES,
    MODEL_DOWNLOAD_HOSTS,
    DownloadError,
    SecurityError,
    check_disk_space,
    extract_selected_files,
    stream_download,
)
from app.core.glm_ocr_catalogue import (
    ASSETS_DIR,
    SOURCE_LABELS,
    Catalogue,
    CatalogueProvider,
    CatalogueState,
    DownloadFile,
    RuntimeVariant,
    runtime_platform_key,
    vulkan_loader_present,
)

logger = logging.getLogger(__name__)

ENGINE_DIR_NAME = "glm-ocr"
MARKER_NAME = ".inkdoc-glm-ocr.json"
MARKER_SCHEMA = 1
LICENSE_FILES = ("LICENSE-GLM-OCR.txt", "LICENSE-llama.cpp.txt")

# Used by the long-job confirmation in the UI and by Auto's routing: GLM-OCR is
# chosen automatically only when a file is estimated to finish within this.
LONG_JOB_SECONDS = 300.0
# Render size of a page sent to the model (long side, pixels), by acceleration.
# Phase 0 (scripts/calibrate_glm_ocr.py, 2026-10-01, Ryzen 9 4900HS, 8 threads):
# nearly all CPU time is the vision encoder, and it grows faster than the pixel
# count (a page took 18.8 s at 896 px, 45 s at 1280 px, 79 s at 1600 px), while a
# dense 460-word page read with token recall 1.000 at 1024, 1152 and 1280 px.
# 1024 px is therefore the CPU default: 2x faster than 1600 px at no measured
# loss. GPUs encode an order of magnitude faster and keep more detail.
RENDER_LONG_SIDE = {"cpu": 1024, "vulkan": 1600, "metal": 1600}
DEFAULT_SECONDS_PER_PAGE = 30.0     # before any measurement exists
RUNTIME_UNPACK_FACTOR = 3           # extracted runtime vs archive size, for the disk check
DISK_HEADROOM = 1.2

DOWNLOAD_SOURCES = ("auto", "mirror_only")


class GlmOcrInstallError(Exception):
    """An install, update or GPU setup failed. The message is shown to the user."""


class GlmOcrUnsupportedError(GlmOcrInstallError):
    """No llama-server build exists for this platform."""


@dataclass
class GlmOcrProgress:
    status: str = "idle"   # idle, downloading, extracting, verifying, selftest, installing, complete, error, cancelled
    message: str = ""
    bytes_downloaded: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    current_file: str = ""
    source: str = ""                 # "upstream" | "mirror"
    source_label: str = ""           # "Hugging Face" | "llama.cpp releases" | "InkDoc mirror"
    fallback_reason: str = ""
    error_message: str | None = None
    can_try_mirror: bool = False
    started_at: float = 0.0
    operation: str = ""              # "install" | "update" | "gpu" | "selftest"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "bytes_downloaded": self.bytes_downloaded,
            "total_bytes": self.total_bytes,
            "percent": round(self.percent, 1),
            "current_file": self.current_file,
            "source": self.source,
            "source_label": self.source_label,
            "fallback_reason": self.fallback_reason,
            "error_message": self.error_message,
            "can_try_mirror": self.can_try_mirror,
            "elapsed_seconds": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "operation": self.operation,
        }


BUSY_STATES = ("downloading", "extracting", "verifying", "selftest", "installing")


@dataclass
class _Plan:
    """What one install/update has to fetch."""

    catalogue: Catalogue
    platform: str
    variant_id: str
    model_files: list[DownloadFile]          # files to download (missing or changed)
    keep_model_files: list[DownloadFile]     # already installed with the right hash
    cpu_runtime: RuntimeVariant
    fetch_cpu_runtime: bool
    gpu_runtime: RuntimeVariant | None
    fetch_gpu_runtime: bool
    sources_used: dict[str, str] = field(default_factory=dict)

    @property
    def download_bytes(self) -> int:
        total = sum(f.size for f in self.model_files)
        if self.fetch_cpu_runtime:
            total += self.cpu_runtime.size
        if self.fetch_gpu_runtime and self.gpu_runtime is not None:
            total += self.gpu_runtime.size
        return total


def _short_rev(revision: str) -> str:
    return (revision or "unknown")[:8]


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".marker-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rename_with_retry(src: Path, dst: Path, attempts: int = 10) -> None:
    """os.replace with retries: on Windows a just-exited process or an antivirus
    scan can hold a file in the folder for a moment."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i + 1 == attempts:
                raise
            time.sleep(0.3)


def selftest_page_png(long_side: int) -> bytes:
    """The self-test text placed on a page-sized canvas at the real render size.

    The vision encoder's cost depends on image size, not content, so the
    measured time is a usable per-page estimate for this machine.
    """
    from PIL import Image

    src = Image.open(ASSETS_DIR / "glm_ocr_selftest.png").convert("RGB")
    height = long_side
    width = int(long_side / 1.414)
    page = Image.new("RGB", (width, height), (255, 255, 255))
    scale = min(1.0, (width * 0.85) / src.width)
    if scale != 1.0:
        src = src.resize((int(src.width * scale), int(src.height * scale)), Image.LANCZOS)
    page.paste(src, ((width - src.width) // 2, int(height * 0.08)))
    buf = io.BytesIO()
    page.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class GlmOcrManager:
    """Install state and operations for GLM-OCR. Status reads the marker only."""

    _instance: GlmOcrManager | None = None
    _instance_lock = threading.Lock()

    def __init__(self, base_dir: Path | None = None, provider: CatalogueProvider | None = None) -> None:
        self._base_override = base_dir
        self.provider = provider or CatalogueProvider()
        self._op_lock = threading.Lock()
        self._progress = GlmOcrProgress()
        self._cancel_event: threading.Event | None = None

    @classmethod
    def get_instance(cls) -> GlmOcrManager:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ─── Locations ───────────────────────────────────────────────────────────
    @property
    def base_dir(self) -> Path:
        if self._base_override is not None:
            return self._base_override
        from app.core.engine_manager import EngineManager

        return EngineManager.get_instance().get_engines_base_dir() / ENGINE_DIR_NAME

    @property
    def marker_path(self) -> Path:
        return self.base_dir / MARKER_NAME

    def _settings(self) -> dict[str, Any]:
        from app.core.engine_manager import EngineManager

        try:
            return EngineManager.get_instance().get_settings()
        except Exception:
            return {}

    def _update_settings(self, updates: dict[str, Any]) -> None:
        from app.core.engine_manager import EngineManager

        EngineManager.get_instance().update_settings(updates)

    # ─── Marker (cheap state) ────────────────────────────────────────────────
    def read_marker(self) -> dict[str, Any]:
        try:
            if self.marker_path.is_file():
                data = json.loads(self.marker_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("schema") == MARKER_SCHEMA:
                    return data
        except Exception as exc:
            logger.debug("Could not read the GLM-OCR marker: %s", exc)
        return {}

    def is_installed(self) -> bool:
        return bool(self.read_marker())

    def is_busy(self) -> bool:
        return self._op_lock.locked() or self._progress.status in BUSY_STATES

    def is_usable(self) -> bool:
        """Installed, CPU self-test passed, no operation running. Never raises; no hashing."""
        try:
            if self.is_busy():
                return False
            marker = self.read_marker()
            if not marker:
                return False
            if marker.get("platform") != runtime_platform_key():
                return False
            return bool(((marker.get("selftest") or {}).get("cpu") or {}).get("passed"))
        except Exception:
            return False

    def gpu_selected(self) -> bool:
        """Whether conversions should start on the GPU (Vulkan, or Metal on macOS)."""
        marker = self.read_marker()
        if not marker:
            return False
        cpu = (marker.get("runtime") or {}).get("cpu") or {}
        if cpu.get("accel") == "metal":
            return not marker.get("metal_cpu_fallback", False)
        gpu = (marker.get("runtime") or {}).get("gpu")
        return bool(gpu) and bool(marker.get("gpu_usable")) and bool(self._settings().get("glm_ocr_use_gpu", False))

    def active_accel(self) -> str:
        """The acceleration conversions will actually use right now.

        A GPU that crashed twice this session is replaced by the CPU until the
        app restarts (GlmOcrServer.force_cpu); estimates must follow that, or
        Auto would plan a long scan at GPU speed and then run it on the CPU.
        """
        marker = self.read_marker()
        cpu = (marker.get("runtime") or {}).get("cpu") or {}
        if self.gpu_selected():
            try:
                from app.core.engines.glm_ocr_server import GlmOcrServer

                inst = GlmOcrServer.peek_instance()
                if inst is not None and inst.force_cpu:
                    return "cpu"
            except Exception:
                pass
            return "metal" if cpu.get("accel") == "metal" else "vulkan"
        return "cpu"

    def render_long_side(self) -> int:
        return RENDER_LONG_SIDE.get(self.active_accel(), RENDER_LONG_SIDE["cpu"])

    def seconds_per_page(self) -> float:
        """Best current estimate of seconds per page for the active acceleration.

        A measured average from real conversions wins over the self-test, which
        wins over a conservative default.
        """
        accel = self.active_accel()
        measured = (self._settings().get("glm_ocr_measured_spp") or {}).get(accel)
        try:
            if measured and float(measured) > 0:
                return float(measured)
        except (TypeError, ValueError):
            pass
        marker = self.read_marker()
        selftest = marker.get("selftest") or {}
        entry = selftest.get("gpu" if accel == "vulkan" else "cpu") or {}
        spp = entry.get("seconds_per_page")
        try:
            if spp and float(spp) > 0:
                return float(spp)
        except (TypeError, ValueError):
            pass
        return DEFAULT_SECONDS_PER_PAGE

    def load_seconds(self) -> float:
        """Cold-start cost of the active acceleration (load + first-request warm-up)."""
        selftest = self.read_marker().get("selftest") or {}
        entry = (selftest.get("gpu") if self.active_accel() != "cpu" else None) or selftest.get("cpu") or {}
        try:
            return float(entry.get("load_seconds") or 10.0)
        except (TypeError, ValueError):
            return 10.0

    def estimate_seconds(self, pages: int, *, server_running: bool | None = None) -> float:
        """Estimated wall time for `pages` pages, including a cold start if needed."""
        if server_running is None:
            try:
                from app.core.engines.glm_ocr_server import GlmOcrServer

                inst = GlmOcrServer.peek_instance()
                server_running = bool(inst and inst.is_running())
            except Exception:
                server_running = False
        cold = 0.0 if server_running else self.load_seconds()
        return cold + max(1, pages) * self.seconds_per_page()

    def record_measurement(self, seconds_per_page: float, pages: int, accel: str | None = None) -> None:
        """Fold a real conversion's speed into the estimate (weighted by pages).

        `accel` is what the server actually ran on. It can differ from the
        selected one (a GPU that fell back to the CPU), and filing a CPU speed
        under the GPU would make every later GPU estimate ten times too long.
        """
        if seconds_per_page <= 0 or pages <= 0:
            return
        accel = accel or self.active_accel()
        try:
            store = dict(self._settings().get("glm_ocr_measured_spp") or {})
            old = float(store.get(accel) or 0)
            weight = min(0.7, 0.2 * pages)
            store[accel] = round(seconds_per_page if old <= 0 else old * (1 - weight) + seconds_per_page * weight, 2)
            self._update_settings({"glm_ocr_measured_spp": store})
        except Exception as exc:
            logger.debug("Could not record GLM-OCR speed: %s", exc)

    def record_metal_fallback(self) -> None:
        marker = self.read_marker()
        if marker and not marker.get("metal_cpu_fallback"):
            marker["metal_cpu_fallback"] = True
            _atomic_write_json(self.marker_path, marker)

    # ─── Launch ──────────────────────────────────────────────────────────────
    def launch_spec(self, want_gpu: bool):
        """LaunchSpec for the installed runtime (CPU, or GPU when asked and usable)."""
        from app.core.engines.glm_ocr_server import GPU_ALL_LAYERS, LaunchSpec

        marker = self.read_marker()
        if not marker:
            raise GlmOcrInstallError("GLM-OCR is not installed. Download it in Settings.")
        model = marker.get("model") or {}
        runtime = marker.get("runtime") or {}
        model_dir = self.base_dir / model.get("dir", "")
        cpu = runtime.get("cpu") or {}
        gpu = runtime.get("gpu") or None
        slot, layers = cpu, 0
        if cpu.get("accel") == "metal":
            if want_gpu and not marker.get("metal_cpu_fallback"):
                layers = GPU_ALL_LAYERS
            slot = cpu
        elif want_gpu and gpu and marker.get("gpu_usable"):
            slot, layers = gpu, GPU_ALL_LAYERS
        accel = slot.get("accel", "cpu") if layers else "cpu"
        return LaunchSpec(
            runtime_dir=self.base_dir / slot.get("dir", "runtime/cpu"),
            entry=slot.get("entry", ""),
            sha256_files=dict(slot.get("sha256_files") or {}),
            model_path=model_dir / model.get("model_file", ""),
            mmproj_path=model_dir / model.get("mmproj_file", ""),
            accel=accel,
            gpu_layers=layers,
            system_deps=list(slot.get("system_deps") or []),
            run_dir=self.base_dir / "run",
        )

    # ─── Status ──────────────────────────────────────────────────────────────
    def catalogue(self, *, check_remote: bool = False) -> Catalogue:
        if check_remote:
            return self.provider.refresh_remote()
        return self.provider.current()

    def get_status(self, *, check_remote: bool = False) -> dict[str, Any]:
        """Everything the Settings card needs. Reads the marker; hashes nothing.

        check_remote=True also checks the signed online catalogue (once per
        session); the card asks for it when opened, /engines never does.
        """
        cat = self.catalogue(check_remote=check_remote)
        platform_key = runtime_platform_key()
        marker = self.read_marker()
        installed = bool(marker)
        cpu_rv = cat.runtime_for(platform_key, "cpu")
        gpu_rv = cat.runtime_for(platform_key, "gpu")
        supported = cpu_rv is not None or (installed and marker.get("platform") == platform_key)
        variant = cat.model_variant()
        settings = self._settings()
        progress = self._progress.to_dict()
        busy = self.is_busy()
        usable = self.is_usable()

        if busy:
            status = "installing"
        elif installed:
            status = "installed"
        elif cat.state is CatalogueState.UNRELEASED:
            status = "unreleased"
        elif cat.state is CatalogueState.DEFECTIVE:
            status = "defective"
        elif not supported:
            status = "unsupported"
        else:
            status = "installable"

        reason = ""
        selftest = marker.get("selftest") or {}
        cpu_test = selftest.get("cpu") or {}
        if status == "unreleased":
            reason = "GLM-OCR is coming soon."
        elif status == "defective":
            reason = "The GLM-OCR catalogue in this build is invalid, so it cannot be installed."
        elif status == "unsupported":
            reason = f"GLM-OCR is not available on this platform ({platform_key})."
        elif installed and not cpu_test.get("passed"):
            reason = (
                "The self-test could not run: " + (cpu_test.get("error") or "unknown reason")
                if not cpu_test.get("ran")
                else "The self-test failed: " + (cpu_test.get("error") or "unknown reason")
            )

        # Update available: the catalogue pins a different model or runtime.
        update: dict[str, Any] | None = None
        if installed and cat.published:
            m = marker.get("model") or {}
            r = (marker.get("runtime") or {})
            want_variant = cat.model_variant(m.get("variant"))
            model_changed = bool(want_variant) and (
                m.get("revision") != cat.model_revision
                or {f.name: f.sha256 for f in want_variant.files}
                != {k: v.get("sha256") for k, v in (m.get("files") or {}).items()}
            )
            runtime_changed = bool(cpu_rv) and (r.get("cpu") or {}).get("sha256") != cpu_rv.sha256
            if model_changed or runtime_changed:
                size = (want_variant.total_bytes if model_changed and want_variant else 0) + (
                    cpu_rv.size if runtime_changed and cpu_rv else 0
                )
                update = {
                    "model_changed": model_changed,
                    "runtime_changed": runtime_changed,
                    "model_version": cat.model_version,
                    "runtime_version": cat.runtime_version,
                    "size_bytes": size,
                }

        is_mac = bool(cpu_rv and cpu_rv.accel == "metal") or (
            ((marker.get("runtime") or {}).get("cpu") or {}).get("accel") == "metal"
        )
        gpu_installed = bool((marker.get("runtime") or {}).get("gpu")) or is_mac
        if is_mac:
            gpu_reason = "CPU fallback in use (Metal could not start)" if marker.get("metal_cpu_fallback") else ""
        elif gpu_rv is None:
            gpu_reason = "No GPU build for this platform."
        elif not vulkan_loader_present():
            gpu_reason = "Vulkan driver not found."
        else:
            gpu_reason = marker.get("gpu_reason", "") if gpu_installed else ""

        return {
            "name": "GLM-OCR",
            "status": status,
            "installed": installed,
            "usable": usable,
            "available": cat.published and supported,
            "supported": supported,
            "reason": reason,
            "platform": platform_key,
            "bundled": False,
            "catalogue_state": cat.state.value,
            "catalogue_defects": cat.defects[:10],
            "catalogue_version": cat.catalogue_version,
            "catalogue_origin": cat.origin,
            "remote_catalogue": self.provider.remote_status(),
            "description": (
                "Best-in-class OCR for scans, photos of documents, math and complex tables. "
                "Runs locally."
            ),
            "license": cat.model_license or "MIT",
            "version": {
                "model": (marker.get("model") or {}).get("version") or cat.model_version,
                "runtime": (marker.get("runtime") or {}).get("version") or cat.runtime_version,
            },
            "variant": (marker.get("model") or {}).get("variant") or cat.default_variant,
            "variants": [
                {"id": v.id, "label": v.label, "size_bytes": v.total_bytes}
                for v in cat.model_variants.values()
            ],
            "download_size_bytes": (variant.total_bytes if variant else 0) + (cpu_rv.size if cpu_rv else 0),
            "sources": sorted({src.kind for f in (variant.files if variant else []) for src in f.sources}),
            "download_source": settings.get("glm_ocr_download_source", "auto"),
            "install_path": str(self.base_dir) if installed else None,
            "accel_in_use": self.active_accel() if installed else "cpu",
            "gpu": {
                "kind": "metal" if is_mac else ("vulkan" if gpu_rv else None),
                "available": is_mac or (gpu_rv is not None and vulkan_loader_present()),
                "installed": gpu_installed,
                "selected": self.gpu_selected() if installed else False,
                "usable": bool(marker.get("gpu_usable")) or (is_mac and not marker.get("metal_cpu_fallback")),
                "locked": is_mac,
                "size_bytes": gpu_rv.size if gpu_rv else 0,
                "reason": gpu_reason,
            },
            "selftest": {k: v for k, v in selftest.items() if isinstance(v, dict)},
            "seconds_per_page": round(self.seconds_per_page(), 1) if installed else None,
            "load_seconds": round(self.load_seconds(), 1) if installed else None,
            "long_job_seconds": LONG_JOB_SECONDS,
            "update_available": update is not None,
            "update": update,
            "progress": progress,
        }

    def get_progress(self) -> dict[str, Any]:
        return self._progress.to_dict()

    def cancel(self) -> bool:
        evt = self._cancel_event
        if evt is not None and self.is_busy():
            evt.set()
            self._progress.message = "Cancelling…"
            return True
        return False

    # ─── Install / update / GPU ──────────────────────────────────────────────
    def _plan(self, *, gpu: bool, variant: str | None, catalogue: Catalogue) -> _Plan:
        if not catalogue.published:
            raise GlmOcrInstallError(
                "GLM-OCR is not available yet." if catalogue.state is CatalogueState.UNRELEASED
                else "The GLM-OCR catalogue is invalid; it cannot be installed."
            )
        platform_key = runtime_platform_key()
        cpu_rv = catalogue.runtime_for(platform_key, "cpu")
        if cpu_rv is None:
            raise GlmOcrUnsupportedError(f"GLM-OCR is not available on this platform ({platform_key}).")
        marker = self.read_marker()
        installed_model = marker.get("model") or {}
        variant_id = variant or installed_model.get("variant") or catalogue.default_variant
        mv = catalogue.model_variant(variant_id)
        if mv is None:
            raise GlmOcrInstallError(f"Unknown GLM-OCR precision '{variant_id}'.")

        same_rev = installed_model.get("revision") == catalogue.model_revision
        have = (installed_model.get("files") or {}) if same_rev else {}
        model_dir = self.base_dir / installed_model.get("dir", "")
        fetch, keep = [], []
        for f in mv.files:
            entry = have.get(f.name) or {}
            if entry.get("sha256") == f.sha256 and (model_dir / f.name).is_file():
                keep.append(f)
            else:
                fetch.append(f)

        runtime = marker.get("runtime") or {}
        fetch_cpu = (runtime.get("cpu") or {}).get("sha256") != cpu_rv.sha256 or marker.get("platform") != platform_key
        gpu_rv = catalogue.runtime_for(platform_key, "gpu") if gpu else None
        fetch_gpu = bool(gpu_rv) and (
            (runtime.get("gpu") or {}).get("sha256") != gpu_rv.sha256 or fetch_cpu
        )
        return _Plan(
            catalogue=catalogue, platform=platform_key, variant_id=variant_id,
            model_files=fetch, keep_model_files=keep,
            cpu_runtime=cpu_rv, fetch_cpu_runtime=fetch_cpu,
            gpu_runtime=gpu_rv, fetch_gpu_runtime=fetch_gpu,
        )

    def install(self, *, gpu: bool | None = None, variant: str | None = None, operation: str = "install") -> dict[str, Any]:
        """Download what is missing or changed, self-test it, then switch over.

        Used for the first install, for updates (only changed parts are fetched
        into new folders; the old install keeps working until the new one passes
        its self-test) and for changing precision.
        """
        if not self._op_lock.acquire(blocking=False):
            raise GlmOcrInstallError("A GLM-OCR download is already running.")
        cancel = threading.Event()
        self._cancel_event = cancel
        progress = GlmOcrProgress(status="downloading", message="Preparing…", started_at=time.time(), operation=operation)
        self._progress = progress
        staging = self.base_dir / ".staging"
        try:
            self._ensure_not_converting()
            if gpu is None:
                gpu = bool(self._settings().get("glm_ocr_use_gpu", False)) or bool(
                    (self.read_marker().get("runtime") or {}).get("gpu")
                )
            catalogue = self.catalogue(check_remote=True)
            plan = self._plan(gpu=gpu, variant=variant, catalogue=catalogue)
            self._check_disk(plan)

            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            progress.total_bytes = plan.download_bytes
            done_bytes = 0

            model_stage = staging / "models" / _short_rev(catalogue.model_revision)
            for f in plan.model_files:
                self._fetch_file(f, model_stage / f.name, "model", plan, progress, cancel, done_bytes)
                done_bytes += f.size

            runtime_stage: dict[str, Path] = {}
            for slot, rv, wanted in (
                ("cpu", plan.cpu_runtime, plan.fetch_cpu_runtime),
                ("gpu", plan.gpu_runtime, plan.fetch_gpu_runtime),
            ):
                if rv is None or not wanted:
                    continue
                archive = staging / "downloads" / f"{rv.id}.{rv.archive}"
                archive_file = DownloadFile(name=archive.name, size=rv.size, sha256=rv.sha256, sources=rv.sources)
                self._fetch_file(archive_file, archive, "runtime", plan, progress, cancel, done_bytes)
                done_bytes += rv.size
                progress.status = "extracting"
                progress.message = f"Unpacking the {'GPU' if slot == 'gpu' else 'CPU'} runtime…"
                dest = staging / "runtime" / slot
                self._extract_runtime(archive, dest, rv)
                runtime_stage[slot] = dest
                archive.unlink(missing_ok=True)
            self._raise_if_cancelled(cancel)

            licenses_stage = staging / "licenses"
            licenses_stage.mkdir(parents=True, exist_ok=True)
            for name in LICENSE_FILES:
                src = ASSETS_DIR / "licenses" / name
                if src.is_file():
                    shutil.copyfile(src, licenses_stage / name)

            # Self-test on the staged files, before the live install is touched.
            progress.status = "selftest"
            progress.message = "Testing GLM-OCR on this computer…"
            progress.percent = 100.0
            marker_old = self.read_marker()
            new_marker = self._marker_for(plan, marker_old, model_stage, runtime_stage)
            selftest = self._selftests(catalogue, new_marker, staging / "run", runtime_stage, model_stage, cancel)
            self._raise_if_cancelled(cancel)
            cpu_result = selftest["cpu"]
            if cpu_result.get("ran") and not cpu_result.get("passed"):
                progress.can_try_mirror = True
                raise GlmOcrInstallError(
                    "GLM-OCR was downloaded but failed its self-test: "
                    + (cpu_result.get("error") or "the test text was not read correctly.")
                )
            if not cpu_result.get("ran") and "Visual C++" in (cpu_result.get("error") or ""):
                raise GlmOcrInstallError(cpu_result["error"])
            new_marker["selftest"] = selftest
            new_marker.update(self._gpu_verdict(selftest, plan.cpu_runtime.accel == "metal"))

            progress.status = "installing"
            progress.message = "Finishing…"
            self._switch_over(staging, plan, new_marker, marker_old, model_stage, runtime_stage)
            self._write_selftest_log(selftest)

            if gpu and plan.gpu_runtime is not None and new_marker.get("gpu_usable"):
                self._update_settings({"glm_ocr_use_gpu": True})
            progress.status = "complete"
            progress.message = "GLM-OCR is ready."
            logger.info("GLM-OCR %s complete (%s)", operation, plan.sources_used)
            return {"status": "installed", "sources": plan.sources_used, "selftest": selftest}
        except Exception as exc:
            progress.status = "cancelled" if cancel.is_set() else "error"
            progress.error_message = str(exc) if not cancel.is_set() else "Cancelled."
            progress.message = progress.error_message or ""
            logger.error("GLM-OCR %s failed: %s", operation, exc)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            self._cancel_event = None
            self._op_lock.release()

    def _ensure_not_converting(self) -> None:
        from app.core.engines.glm_ocr_server import GlmOcrServer

        inst = GlmOcrServer.peek_instance()
        if inst is not None and inst.is_busy():
            raise GlmOcrInstallError("Wait for the running GLM-OCR conversion to finish, then try again.")

    def _check_disk(self, plan: _Plan) -> None:
        need = int(plan.download_bytes * DISK_HEADROOM)
        for rv, wanted in ((plan.cpu_runtime, plan.fetch_cpu_runtime), (plan.gpu_runtime, plan.fetch_gpu_runtime)):
            if rv is not None and wanted:
                need += rv.size * RUNTIME_UNPACK_FACTOR
        try:
            check_disk_space(self.base_dir, need, multiplier=1.01, safety_margin_bytes=200 * 1024 * 1024)
        except DownloadError as exc:
            raise GlmOcrInstallError(str(exc)) from exc

    @staticmethod
    def _raise_if_cancelled(cancel: threading.Event) -> None:
        if cancel.is_set():
            raise GlmOcrInstallError("Download cancelled.")

    def _fetch_file(
        self,
        f: DownloadFile,
        dest: Path,
        kind: str,
        plan: _Plan,
        progress: GlmOcrProgress,
        cancel: threading.Event,
        done_before: int,
    ) -> None:
        """Fetch one file: each source in order until one delivers the right bytes."""
        mirror_only = self._settings().get("glm_ocr_download_source", "auto") == "mirror_only"
        partial_dir = self.base_dir / ".partial"
        partial_dir.mkdir(parents=True, exist_ok=True)
        reasons: list[str] = []
        progress.current_file = f.name
        progress.fallback_reason = ""
        for source in f.sources:
            self._raise_if_cancelled(cancel)
            label = SOURCE_LABELS.get((source.kind, kind), source.kind)
            if mirror_only and source.kind == "upstream":
                reasons.append(f"{label}: skipped (InkDoc mirror only)")
                continue
            progress.status = "downloading"
            progress.source = source.kind
            progress.source_label = label
            progress.message = f"Downloading {f.name}"
            target = partial_dir / f"{f.name}.{source.kind}"

            def on_progress(done: int, _total: int) -> None:
                progress.bytes_downloaded = done_before + done
                if progress.total_bytes:
                    progress.percent = min(99.9, progress.bytes_downloaded / progress.total_bytes * 100.0)

            try:
                sha = stream_download(
                    url=source.url,
                    destination_path=target,
                    expected_size=f.size,
                    cancel_event=cancel,
                    progress_callback=on_progress,
                    user_agent="InkDoc-GlmOcr/1.0",
                    allowed_hosts=MODEL_DOWNLOAD_HOSTS,
                    allowed_suffixes=MODEL_DOWNLOAD_HOST_SUFFIXES,
                    max_retries=2,
                    fail_fast_on_rate_limit=True,
                )
                if sha.lower() != f.sha256.lower():
                    target.unlink(missing_ok=True)
                    raise SecurityError(f"hash mismatch (got {sha[:12]}…, expected {f.sha256[:12]}…)")
            except Exception as exc:
                if cancel.is_set():
                    raise GlmOcrInstallError("Download cancelled.") from exc
                reason = _short_reason(exc)
                reasons.append(f"{label}: {reason}")
                logger.warning("GLM-OCR %s from %s failed: %s; trying the next source", f.name, label, exc)
                if isinstance(exc, SecurityError):
                    # Never resume from bytes that failed verification.
                    target.unlink(missing_ok=True)
                    Path(str(target) + ".part").unlink(missing_ok=True)
                progress.fallback_reason = f"{label} unreachable" if "hash" not in reason else f"{label} sent a bad file"
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            _rename_with_retry(target, dest)
            plan.sources_used[f.name] = source.kind
            progress.bytes_downloaded = done_before + f.size
            return
        progress.can_try_mirror = not mirror_only
        raise GlmOcrInstallError(f"Could not download {f.name} from any source. " + "; ".join(reasons))

    def _extract_runtime(self, archive: Path, dest: Path, rv: RuntimeVariant) -> None:
        shutil.rmtree(dest, ignore_errors=True)
        written = extract_selected_files(archive, dest, list(rv.sha256_files))
        for rel, expected in rv.sha256_files.items():
            if written.get(rel, "").lower() != expected.lower():
                raise SecurityError(f"Runtime file {rel} failed its integrity check after extraction.")
        if os.name == "posix":
            try:
                (dest / rv.entry).chmod(0o755)
            except OSError:
                pass

    def _marker_for(
        self, plan: _Plan, old: dict[str, Any], model_stage: Path, runtime_stage: dict[str, Path]
    ) -> dict[str, Any]:
        cat = plan.catalogue
        mv = cat.model_variant(plan.variant_id)
        assert mv is not None
        old_runtime = old.get("runtime") or {}

        def slot(rv: RuntimeVariant | None, name: str) -> dict[str, Any] | None:
            if rv is None:
                return None
            if name not in runtime_stage and (old_runtime.get(name) or {}).get("sha256") == rv.sha256:
                return old_runtime.get(name)
            return {
                "variant": rv.id,
                "accel": rv.accel,
                "dir": f"runtime/{name}",
                "entry": rv.entry,
                "sha256": rv.sha256,
                "sha256_files": dict(rv.sha256_files),
                "system_deps": list(rv.system_deps),
                "source": plan.sources_used.get(f"{rv.id}.{rv.archive}", "kept"),
            }

        gpu_slot = slot(plan.gpu_runtime, "gpu") if plan.gpu_runtime else old_runtime.get("gpu")
        return {
            "schema": MARKER_SCHEMA,
            "installed_at": time.time(),
            "platform": plan.platform,
            "catalogue_version": cat.catalogue_version,
            "catalogue_origin": cat.origin,
            "model": {
                "version": cat.model_version,
                "revision": cat.model_revision,
                "variant": plan.variant_id,
                "dir": f"models/{_short_rev(cat.model_revision)}",
                "model_file": mv.model_file,
                "mmproj_file": mv.mmproj_file,
                "files": {
                    f.name: {"sha256": f.sha256, "size": f.size, "source": plan.sources_used.get(f.name, "kept")}
                    for f in mv.files
                },
            },
            "runtime": {
                "version": cat.runtime_version,
                "cpu": slot(plan.cpu_runtime, "cpu"),
                "gpu": gpu_slot,
            },
            "metal_cpu_fallback": bool(old.get("metal_cpu_fallback")) and not plan.fetch_cpu_runtime,
        }

    def _staged_spec(
        self, marker: dict[str, Any], slot_name: str, run_dir: Path, runtime_stage: dict[str, Path],
        model_stage: Path, gpu_layers: int,
    ):
        from app.core.engines.glm_ocr_server import LaunchSpec

        slot = (marker.get("runtime") or {}).get(slot_name) or {}
        model = marker["model"]
        runtime_dir = runtime_stage.get(slot_name) or (self.base_dir / slot.get("dir", f"runtime/{slot_name}"))

        def model_file(name: str) -> Path:
            staged = model_stage / name
            if staged.is_file():
                return staged
            return self.base_dir / (self.read_marker().get("model") or {}).get("dir", "") / name

        return LaunchSpec(
            runtime_dir=runtime_dir,
            entry=slot.get("entry", ""),
            sha256_files=dict(slot.get("sha256_files") or {}),
            model_path=model_file(model["model_file"]),
            mmproj_path=model_file(model["mmproj_file"]),
            accel=slot.get("accel", "cpu") if gpu_layers else "cpu",
            gpu_layers=gpu_layers,
            system_deps=list(slot.get("system_deps") or []),
            run_dir=run_dir,
        )

    def _selftests(
        self, cat: Catalogue, marker: dict[str, Any], run_dir: Path, runtime_stage: dict[str, Path],
        model_stage: Path, cancel: threading.Event,
    ) -> dict[str, Any]:
        """CPU self-test (must pass), plus Metal or Vulkan when that runtime is present."""
        from app.core.engines.glm_ocr_server import GPU_ALL_LAYERS, run_selftest

        results: dict[str, Any] = {}
        cpu_slot = (marker.get("runtime") or {}).get("cpu") or {}
        is_metal = cpu_slot.get("accel") == "metal"

        if is_metal:
            spec = self._staged_spec(marker, "cpu", run_dir, runtime_stage, model_stage, GPU_ALL_LAYERS)
            png = selftest_page_png(RENDER_LONG_SIDE["metal"])
            metal = run_selftest(spec, png, cat.selftest_prompt, cat.selftest_expect, cancel_event=cancel)
            if metal.passed:
                results["cpu"] = metal.to_dict()
                results["gpu"] = metal.to_dict()
                return results
            logger.warning("GLM-OCR Metal self-test failed (%s); testing with -ngl 0.", metal.error)
            marker["metal_cpu_fallback"] = True
            results["gpu"] = metal.to_dict()

        spec = self._staged_spec(marker, "cpu", run_dir, runtime_stage, model_stage, 0)
        cpu = run_selftest(spec, selftest_page_png(RENDER_LONG_SIDE["cpu"]), cat.selftest_prompt,
                           cat.selftest_expect, cancel_event=cancel)
        results["cpu"] = cpu.to_dict()

        gpu_slot = (marker.get("runtime") or {}).get("gpu")
        if gpu_slot and not is_metal and not cancel.is_set():
            gspec = self._staged_spec(marker, "gpu", run_dir, runtime_stage, model_stage, GPU_ALL_LAYERS)
            gpu = run_selftest(gspec, selftest_page_png(RENDER_LONG_SIDE["vulkan"]), cat.selftest_prompt,
                               cat.selftest_expect, cancel_event=cancel)
            results["gpu"] = gpu.to_dict()
        return results

    @staticmethod
    def _gpu_verdict(selftest: dict[str, Any], is_metal: bool) -> dict[str, Any]:
        """Keep a GPU runtime installed but unselected when it fails or is slower than the CPU."""
        gpu = selftest.get("gpu")
        cpu = selftest.get("cpu") or {}
        if is_metal:
            # macOS: Metal is the default; a failure is recorded as metal_cpu_fallback.
            return {"gpu_usable": bool(gpu and gpu.get("passed")), "gpu_reason": ""}
        if not gpu:
            return {"gpu_usable": False, "gpu_reason": ""}
        if not gpu.get("ran"):
            return {"gpu_usable": False, "gpu_reason": _gpu_failure_reason(gpu.get("error", ""))}
        if not gpu.get("passed"):
            return {"gpu_usable": False, "gpu_reason": "The GPU self-test failed: " + (gpu.get("error") or "")}
        g, c = float(gpu.get("seconds_per_page") or 0), float(cpu.get("seconds_per_page") or 0)
        if c and g >= c:
            return {"gpu_usable": False,
                    "gpu_reason": f"Slower than the CPU on this machine ({g:.1f} s vs {c:.1f} s per page)."}
        return {"gpu_usable": True, "gpu_reason": ""}

    def _switch_over(
        self, staging: Path, plan: _Plan, new_marker: dict[str, Any], old_marker: dict[str, Any],
        model_stage: Path, runtime_stage: dict[str, Path],
    ) -> None:
        """Move staged parts into place, write the marker, then delete what was replaced."""
        from app.core.engines.glm_ocr_server import GlmOcrServer

        inst = GlmOcrServer.peek_instance()
        if inst is not None:
            inst.shutdown()
        base = self.base_dir
        base.mkdir(parents=True, exist_ok=True)
        replaced: list[Path] = []
        moved: list[tuple[Path, Path | None]] = []   # (live path, backup or None)
        try:
            # Model: a new revision gets a new folder; same revision gets files merged.
            live_models = base / new_marker["model"]["dir"]
            live_models.mkdir(parents=True, exist_ok=True)
            for f in plan.model_files:
                src = model_stage / f.name
                dst = live_models / f.name
                if dst.exists():
                    backup = dst.with_name(dst.name + ".old")
                    _rename_with_retry(dst, backup)
                    moved.append((dst, backup))
                else:
                    moved.append((dst, None))
                _rename_with_retry(src, dst)
            for slot, src in runtime_stage.items():
                dst = base / "runtime" / slot
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    backup = dst.with_name(f"{slot}.old")
                    shutil.rmtree(backup, ignore_errors=True)
                    _rename_with_retry(dst, backup)
                    moved.append((dst, backup))
                else:
                    moved.append((dst, None))
                _rename_with_retry(src, dst)
            lic_dst = base / "licenses"
            shutil.rmtree(lic_dst, ignore_errors=True)
            shutil.copytree(staging / "licenses", lic_dst)

            _atomic_write_json(self.marker_path, new_marker)
        except Exception:
            # Put the previous install back so it keeps working.
            for live, backup in reversed(moved):
                try:
                    if live.is_dir():
                        shutil.rmtree(live, ignore_errors=True)
                    elif live.exists():
                        live.unlink()
                    if backup is not None and backup.exists():
                        _rename_with_retry(backup, live)
                except Exception as exc:
                    logger.error("Could not restore %s after a failed GLM-OCR update: %s", live, exc)
            raise
        # Committed: delete backups and an old model revision folder.
        for _live, backup in moved:
            if backup is not None:
                replaced.append(backup)
        old_model_dir = (old_marker.get("model") or {}).get("dir")
        if old_model_dir and old_model_dir != new_marker["model"]["dir"]:
            replaced.append(base / old_model_dir)
        for path in replaced:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        if new_marker["runtime"].get("gpu") is None:
            shutil.rmtree(base / "runtime" / "gpu", ignore_errors=True)

    def _write_selftest_log(self, selftest: dict[str, Any]) -> None:
        try:
            d = self.base_dir / "selftest"
            d.mkdir(parents=True, exist_ok=True)
            (d / "last.json").write_text(json.dumps(selftest, indent=2), encoding="utf-8")
        except OSError:
            pass

    def install_gpu(self) -> dict[str, Any]:
        """Add the Vulkan runtime (Windows/Linux) and test it against the CPU."""
        if not self.is_installed():
            raise GlmOcrInstallError("Download GLM-OCR first.")
        return self.install(gpu=True, operation="gpu")

    def set_gpu(self, enabled: bool) -> dict[str, Any]:
        """Turn GPU acceleration on or off. Returns {"needs_download": bool, ...}."""
        marker = self.read_marker()
        if not marker:
            raise GlmOcrInstallError("Download GLM-OCR first.")
        cpu = (marker.get("runtime") or {}).get("cpu") or {}
        if cpu.get("accel") == "metal":
            raise GlmOcrInstallError("On macOS GLM-OCR always uses Metal when it can.")
        if not enabled:
            self._update_settings({"glm_ocr_use_gpu": False})
            self._restart_server()
            return {"needs_download": False, "selected": False}
        if (marker.get("runtime") or {}).get("gpu"):
            if not marker.get("gpu_usable"):
                raise GlmOcrInstallError(marker.get("gpu_reason") or "The GPU runtime did not pass its self-test.")
            self._update_settings({"glm_ocr_use_gpu": True})
            self._restart_server()
            return {"needs_download": False, "selected": True}
        return {"needs_download": True, "selected": False}

    def _restart_server(self) -> None:
        from app.core.engines.glm_ocr_server import GlmOcrServer

        inst = GlmOcrServer.peek_instance()
        if inst is not None:
            inst.force_cpu = False
            inst.shutdown_if_idle()

    def rerun_selftest(self) -> dict[str, Any]:
        """Self-test the installed files again (the card's "Self-test again")."""
        if not self._op_lock.acquire(blocking=False):
            raise GlmOcrInstallError("A GLM-OCR operation is already running.")
        cancel = threading.Event()
        self._cancel_event = cancel
        self._progress = GlmOcrProgress(status="selftest", message="Testing GLM-OCR on this computer…",
                                        started_at=time.time(), operation="selftest", percent=100.0)
        try:
            self._ensure_not_converting()
            marker = self.read_marker()
            if not marker:
                raise GlmOcrInstallError("GLM-OCR is not installed.")
            from app.core.engines.glm_ocr_server import GlmOcrServer

            inst = GlmOcrServer.peek_instance()
            if inst is not None:
                inst.shutdown()
            is_metal = ((marker.get("runtime") or {}).get("cpu") or {}).get("accel") == "metal"
            marker["metal_cpu_fallback"] = False
            selftest = self._selftests(self.catalogue(), marker, self.base_dir / "run", {},
                                       self.base_dir / marker["model"]["dir"], cancel)
            marker["selftest"] = selftest
            marker.update(self._gpu_verdict(selftest, is_metal))
            _atomic_write_json(self.marker_path, marker)
            self._write_selftest_log(selftest)
            self._progress.status = "complete"
            self._progress.message = "Self-test finished."
            return {"selftest": selftest}
        except Exception as exc:
            self._progress.status = "error"
            self._progress.error_message = str(exc)
            raise
        finally:
            self._cancel_event = None
            self._op_lock.release()

    # ─── Verify / remove ─────────────────────────────────────────────────────
    def verify(self) -> dict[str, Any]:
        """Re-hash every installed file (the card's Verify button)."""
        from app.core.engines.glm_ocr_server import RuntimeIntegrityError, verify_runtime

        marker = self.read_marker()
        if not marker:
            return {"valid": False, "reason": "GLM-OCR is not installed."}
        problems: list[str] = []
        model = marker.get("model") or {}
        model_dir = self.base_dir / model.get("dir", "")
        import hashlib

        for name, entry in (model.get("files") or {}).items():
            path = model_dir / name
            if not path.is_file():
                problems.append(f"Missing model file: {name}")
                continue
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                while chunk := fh.read(4 << 20):
                    h.update(chunk)
            if h.hexdigest().lower() != str(entry.get("sha256", "")).lower():
                problems.append(f"Model file failed its integrity check: {name}")
        for slot in ("cpu", "gpu"):
            if (marker.get("runtime") or {}).get(slot):
                try:
                    verify_runtime(self._slot_spec(marker, slot))
                except RuntimeIntegrityError as exc:
                    problems.append(str(exc))
        if problems:
            return {"valid": False, "mismatches": problems[:20]}
        return {"valid": True, "engine": "glm_ocr", "path": str(self.base_dir)}

    def _slot_spec(self, marker: dict[str, Any], slot_name: str):
        from app.core.engines.glm_ocr_server import LaunchSpec

        slot = marker["runtime"][slot_name]
        model = marker["model"]
        model_dir = self.base_dir / model["dir"]
        return LaunchSpec(
            runtime_dir=self.base_dir / slot["dir"], entry=slot["entry"],
            sha256_files=dict(slot.get("sha256_files") or {}),
            model_path=model_dir / model["model_file"], mmproj_path=model_dir / model["mmproj_file"],
        )

    def remove(self) -> dict[str, Any]:
        """Delete GLM-OCR: stop the server, delete the marker first, then the files."""
        if self.is_busy():
            raise GlmOcrInstallError("Wait for the running GLM-OCR download to finish or cancel it first.")
        from app.core.engines.glm_ocr_server import GlmOcrServer

        inst = GlmOcrServer.peek_instance()
        if inst is not None:
            if inst.is_busy():
                raise GlmOcrInstallError("Wait for the running GLM-OCR conversion to finish, then remove it.")
            inst.shutdown()
        try:
            self.marker_path.unlink(missing_ok=True)
        except OSError as exc:
            raise GlmOcrInstallError(f"Could not remove GLM-OCR: {exc}") from exc
        shutil.rmtree(self.base_dir, ignore_errors=True)
        self._progress = GlmOcrProgress()
        self._update_settings({"glm_ocr_use_gpu": False, "glm_ocr_measured_spp": {}})
        logger.info("GLM-OCR removed")
        return {"status": "removed", "engine": "glm_ocr"}


def _short_reason(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:160] or type(exc).__name__


def _gpu_failure_reason(error: str) -> str:
    low = error.lower()
    if "vulkan" in low:
        return "Vulkan driver not found."
    return "The GPU runtime could not start: " + (error or "unknown reason")


def glm_ocr_usable() -> bool:
    """For Auto and the server: installed, self-tested, idle. Never raises."""
    try:
        return GlmOcrManager.get_instance().is_usable()
    except Exception:
        return False


def glm_ocr_installable() -> bool:
    """Not installed, but could be downloaded from Settings on this platform."""
    try:
        mgr = GlmOcrManager.get_instance()
        _ = mgr.base_dir      # no engines folder, no install
        return mgr.get_status()["status"] == "installable"
    except Exception:
        return False


__all__ = [
    "GlmOcrInstallError",
    "GlmOcrManager",
    "GlmOcrUnsupportedError",
    "LONG_JOB_SECONDS",
    "glm_ocr_installable",
    "glm_ocr_usable",
    "selftest_page_png",
]

