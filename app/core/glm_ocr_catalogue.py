"""GLM-OCR catalogue: what to download, from where, and the hashes it must match.

Two sources, one format, one validator:

- The bundled baseline, app/core/glm_ocr_catalogue.json, ships inside the app
  and is covered by the app's own signed release. It always works offline as
  the last known-good pin.
- The signed remote catalogue ("latest tested", milestone 2) is an Ed25519
  envelope on a dedicated, never-Latest GitHub release. It is accepted only
  when its signature verifies, its catalogue_version is higher than both the
  bundled one and the highest this install ever accepted (no rollback), its
  min_app_version is satisfied, and it passes the same validation as the
  bundled file. Otherwise the bundled catalogue is used and the reason is kept
  for the Settings card.

Every downloadable file lists its sources in order: the official upstream
(Hugging Face for the model, the llama.cpp GitHub releases for the runtime)
first, then InkDoc's own GitHub mirror. Upstream hosts are allowed for these
downloads only (MODEL_DOWNLOAD_HOSTS), never for app updates.

Nothing here touches the network at import time, and the remote catalogue is
fetched only when the GLM-OCR card is opened or an install/update starts.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.addon_manager import CatalogueState, version_at_least
from app.core.download_utils import (
    MODEL_DOWNLOAD_HOST_SUFFIXES,
    MODEL_DOWNLOAD_HOSTS,
    SecurityError,
    StrictRedirectHandler,
    validate_download_url,
)
from app.core.engine_manifest import is_valid_sha256

logger = logging.getLogger(__name__)

BUNDLED_CATALOGUE_PATH = Path(__file__).resolve().parent / "glm_ocr_catalogue.json"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

REMOTE_CATALOGUE_URL = (
    "https://github.com/AbdoslamB/InkDoc/releases/download/"
    "glm-ocr-catalogue/inkdoc-glm-ocr-catalogue.json"
)
REMOTE_FETCH_TIMEOUT_S = 5.0
REMOTE_MAX_BYTES = 1024 * 1024

# Mirror files must live on InkDoc's own releases, nowhere else on GitHub.
MIRROR_PATH_PREFIX = "/abdoslamb/inkdoc/releases/download/"
# GitHub release assets max out at 2 GiB, and stream_download caps at 2000 MiB.
# Rejecting anything over 1.9 GB keeps a future model from silently breaking the
# mirror or the downloader.
MAX_FILE_BYTES = 1_900_000_000

SOURCE_KINDS = ("upstream", "mirror")
ACCELERATIONS = ("cpu", "vulkan", "metal")
ARCHIVE_FORMATS = ("zip", "tar.gz")
PLATFORMS = ("windows-x86_64", "windows-arm64", "macos-arm64", "linux-x86_64")

SOURCE_LABELS = {
    ("upstream", "model"): "Hugging Face",
    ("upstream", "runtime"): "llama.cpp releases",
    ("mirror", "model"): "InkDoc mirror",
    ("mirror", "runtime"): "InkDoc mirror",
}

_TOP_KEYS = {"catalogue_version", "min_app_version", "model", "runtime", "selftest", "_comment"}
_MODEL_KEYS = {"version", "license", "source_repo", "source_revision", "base_model", "default_variant", "variants", "_comment"}
_MODEL_VARIANT_KEYS = {"label", "model_file", "mmproj_file", "files", "_comment"}
_FILE_KEYS = {"name", "size", "sha256", "sources", "_comment"}
_SOURCE_KEYS = {"kind", "url"}
_RUNTIME_KEYS = {"version", "license", "source_repo", "variants", "_comment"}
_RUNTIME_VARIANT_KEYS = {
    "platform", "accel", "archive", "size", "sha256", "entry", "sha256_files",
    "system_deps", "sources", "_comment",
}
_SELFTEST_KEYS = {"image", "prompt", "expect_all", "_comment"}


# ─── Parsed form ─────────────────────────────────────────────────────────────
@dataclass
class Source:
    kind: str
    url: str


@dataclass
class DownloadFile:
    name: str
    size: int
    sha256: str
    sources: list[Source] = field(default_factory=list)


@dataclass
class ModelVariant:
    id: str
    label: str
    model_file: str
    mmproj_file: str
    files: list[DownloadFile] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class RuntimeVariant:
    id: str
    platform: str
    accel: str
    archive: str
    size: int
    sha256: str
    entry: str
    sha256_files: dict[str, str] = field(default_factory=dict)
    system_deps: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)

    @property
    def is_gpu(self) -> bool:
        return self.accel in ("vulkan", "metal")


@dataclass
class Catalogue:
    catalogue_version: int
    min_app_version: str
    model_version: str
    model_license: str
    model_repo: str
    model_revision: str
    base_model: str
    default_variant: str
    model_variants: dict[str, ModelVariant]
    runtime_version: str
    runtime_license: str
    runtime_repo: str
    runtime_variants: dict[str, RuntimeVariant]
    selftest_image: str
    selftest_prompt: str
    selftest_expect: list[str]
    origin: str = "bundled"            # "bundled" | "remote"
    state: CatalogueState = CatalogueState.DEFECTIVE
    defects: list[str] = field(default_factory=list)

    @property
    def published(self) -> bool:
        return self.state is CatalogueState.PUBLISHED

    def model_variant(self, variant: str | None = None) -> ModelVariant | None:
        return self.model_variants.get(variant or self.default_variant)

    def runtime_for(self, platform_key: str, accel: str) -> RuntimeVariant | None:
        """The runtime build for a platform and acceleration ("cpu" | "gpu")."""
        for rv in self.runtime_variants.values():
            if rv.platform != platform_key:
                continue
            if accel == "gpu" and rv.accel == "vulkan":
                return rv
            if accel == "cpu" and rv.accel in ("cpu", "metal"):
                # macOS ships one build: Metal, with an automatic -ngl 0 fallback.
                return rv
        return None

    def gpu_runtime_needed(self, platform_key: str) -> bool:
        """Whether GPU acceleration is a separate download (Vulkan) on this platform."""
        return self.runtime_for(platform_key, "gpu") is not None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _sources(raw: Any) -> list[Source]:
    out = []
    for s in raw if isinstance(raw, list) else []:
        if isinstance(s, dict):
            out.append(Source(kind=str(s.get("kind", "")), url=str(s.get("url", ""))))
    return out


def _files(raw: Any) -> list[DownloadFile]:
    out = []
    for f in raw if isinstance(raw, list) else []:
        if isinstance(f, dict):
            out.append(DownloadFile(
                name=str(f.get("name", "")),
                size=_int(f.get("size")),
                sha256=str(f.get("sha256", "")).lower().strip(),
                sources=_sources(f.get("sources")),
            ))
    return out


def parse_catalogue(data: Any, *, origin: str = "bundled") -> Catalogue:
    """Parse leniently (never raises on shape) and attach the validation verdict."""
    state, defects = validate_glm_catalogue(data)
    d = data if isinstance(data, dict) else {}
    model = d.get("model") if isinstance(d.get("model"), dict) else {}
    runtime = d.get("runtime") if isinstance(d.get("runtime"), dict) else {}
    selftest = d.get("selftest") if isinstance(d.get("selftest"), dict) else {}

    model_variants: dict[str, ModelVariant] = {}
    for vid, v in (model.get("variants") or {}).items():
        if isinstance(v, dict):
            model_variants[vid] = ModelVariant(
                id=vid,
                label=str(v.get("label", vid)),
                model_file=str(v.get("model_file", "")),
                mmproj_file=str(v.get("mmproj_file", "")),
                files=_files(v.get("files")),
            )
    runtime_variants: dict[str, RuntimeVariant] = {}
    for vid, v in (runtime.get("variants") or {}).items():
        if isinstance(v, dict):
            runtime_variants[vid] = RuntimeVariant(
                id=vid,
                platform=str(v.get("platform", "")),
                accel=str(v.get("accel", "")),
                archive=str(v.get("archive", "")),
                size=_int(v.get("size")),
                sha256=str(v.get("sha256", "")).lower().strip(),
                entry=str(v.get("entry", "")),
                sha256_files={str(k): str(h).lower().strip() for k, h in (v.get("sha256_files") or {}).items()},
                system_deps=[str(x) for x in (v.get("system_deps") or [])],
                sources=_sources(v.get("sources")),
            )
    return Catalogue(
        catalogue_version=_int(d.get("catalogue_version")),
        min_app_version=str(d.get("min_app_version", "")),
        model_version=str(model.get("version", "")),
        model_license=str(model.get("license", "")),
        model_repo=str(model.get("source_repo", "")),
        model_revision=str(model.get("source_revision", "")),
        base_model=str(model.get("base_model", "")),
        default_variant=str(model.get("default_variant", "q8")),
        model_variants=model_variants,
        runtime_version=str(runtime.get("version", "")),
        runtime_license=str(runtime.get("license", "")),
        runtime_repo=str(runtime.get("source_repo", "")),
        runtime_variants=runtime_variants,
        selftest_image=str(selftest.get("image", "")),
        selftest_prompt=str(selftest.get("prompt", "Text Recognition:")),
        selftest_expect=[str(x) for x in (selftest.get("expect_all") or [])],
        origin=origin,
        state=state,
        defects=defects,
    )


# ─── Validation ──────────────────────────────────────────────────────────────
def _unknown_keys(obj: dict, allowed: set[str], where: str, defects: list[str]) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        defects.append(f"{where}: unknown keys {extra}")


def _safe_file_name(name: str) -> bool:
    """A bare file name, or a forward-slash relative path with no traversal."""
    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name:
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def _check_sources(sources: Any, where: str, defects: list[str], counts: dict[str, int]) -> None:
    if not isinstance(sources, list) or not sources:
        if sources not in (None, []):
            defects.append(f"{where}: sources must be a list")
        return
    kinds: list[str] = []
    for i, src in enumerate(sources):
        if not isinstance(src, dict):
            defects.append(f"{where}: source {i} is not an object")
            continue
        _unknown_keys(src, _SOURCE_KEYS, f"{where} source {i}", defects)
        kind = src.get("kind")
        url = str(src.get("url", ""))
        if kind not in SOURCE_KINDS:
            defects.append(f"{where}: source {i} has kind {kind!r}, expected one of {SOURCE_KINDS}")
            continue
        kinds.append(kind)
        if url:
            counts["urls"] += 1
        try:
            validate_download_url(url, MODEL_DOWNLOAD_HOSTS, MODEL_DOWNLOAD_HOST_SUFFIXES)
        except SecurityError as exc:
            defects.append(f"{where}: {kind} url rejected: {exc}")
            continue
        except Exception as exc:
            defects.append(f"{where}: {kind} url could not be parsed: {exc}")
            continue
        if kind == "mirror":
            path = urllib.parse.urlparse(url).path.lower()
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if host != "github.com" or not path.startswith(MIRROR_PATH_PREFIX):
                defects.append(f"{where}: mirror url must be an InkDoc GitHub release asset, got {url!r}")
    if kinds and kinds != sorted(kinds, key=SOURCE_KINDS.index):
        defects.append(f"{where}: upstream sources must come before mirror sources")


def _check_file(f: Any, where: str, defects: list[str], counts: dict[str, int]) -> None:
    if not isinstance(f, dict):
        defects.append(f"{where}: not an object")
        return
    _unknown_keys(f, _FILE_KEYS, where, defects)
    name = str(f.get("name", ""))
    if not _safe_file_name(name) or "/" in name:
        defects.append(f"{where}: name {name!r} is not a plain file name")
    sha = str(f.get("sha256", "")).strip()
    size = _int(f.get("size"))
    if sha:
        counts["hashes"] += 1
        if not is_valid_sha256(sha):
            defects.append(f"{where}: sha256 {sha!r} is not a 64-character hex digest")
    if size or sha or f.get("sources"):
        if size <= 0:
            defects.append(f"{where}: size must be positive")
        elif size > MAX_FILE_BYTES:
            defects.append(f"{where}: size {size} exceeds the {MAX_FILE_BYTES} byte limit (GitHub mirror and downloader cap)")
    _check_sources(f.get("sources"), where, defects, counts)


def validate_glm_catalogue(data: Any) -> tuple[CatalogueState, list[str]]:
    """Classify a GLM-OCR catalogue and list every defect found.

    The one definition of "safe to offer", used by the app (bundled and
    remote), scripts/verify_glm_ocr_catalogue.py in CI, and the signing script.
    Modelled on addon_manager.validate_addon_entry and its three states:

    - UNRELEASED: no source URL and no hash anywhere, but otherwise well formed.
      The app hides the GLM-OCR pill and shows "Coming soon".
    - PUBLISHED: complete and valid.
    - DEFECTIVE: anything else, including half-filled. Never offered; CI fails.
    """
    defects: list[str] = []
    counts = {"urls": 0, "hashes": 0}
    if not isinstance(data, dict):
        return CatalogueState.DEFECTIVE, ["catalogue is not a JSON object"]
    _unknown_keys(data, _TOP_KEYS, "catalogue", defects)

    cv = data.get("catalogue_version")
    if not isinstance(cv, int) or isinstance(cv, bool) or cv < 1:
        defects.append("catalogue_version must be a positive integer")
    if not isinstance(data.get("min_app_version"), str) or not data.get("min_app_version"):
        defects.append("min_app_version must be a non-empty string")

    model = data.get("model")
    if not isinstance(model, dict):
        defects.append("model section is missing")
    else:
        _unknown_keys(model, _MODEL_KEYS, "model", defects)
        for key in ("version", "license", "source_repo", "source_revision"):
            if not model.get(key):
                defects.append(f"model.{key} is empty")
        variants = model.get("variants")
        if not isinstance(variants, dict) or not variants:
            defects.append("model.variants is empty")
            variants = {}
        default = model.get("default_variant", "q8")
        if variants and default not in variants:
            defects.append(f"model.default_variant {default!r} is not a listed variant")
        for vid, v in variants.items():
            where = f"model variant {vid!r}"
            if not isinstance(v, dict):
                defects.append(f"{where}: not an object")
                continue
            _unknown_keys(v, _MODEL_VARIANT_KEYS, where, defects)
            files = v.get("files")
            if not isinstance(files, list) or not files:
                defects.append(f"{where}: files is empty")
                files = []
            names = {str(f.get("name", "")) for f in files if isinstance(f, dict)}
            for key in ("model_file", "mmproj_file"):
                if v.get(key) not in names:
                    defects.append(f"{where}: {key} {v.get(key)!r} is not one of its files")
            for i, f in enumerate(files):
                _check_file(f, f"{where} file {i} ({f.get('name') if isinstance(f, dict) else '?'})", defects, counts)

    runtime = data.get("runtime")
    if not isinstance(runtime, dict):
        defects.append("runtime section is missing")
    else:
        _unknown_keys(runtime, _RUNTIME_KEYS, "runtime", defects)
        for key in ("version", "license", "source_repo"):
            if not runtime.get(key):
                defects.append(f"runtime.{key} is empty")
        variants = runtime.get("variants")
        if not isinstance(variants, dict) or not variants:
            defects.append("runtime.variants is empty")
            variants = {}
        for vid, v in variants.items():
            where = f"runtime variant {vid!r}"
            if not isinstance(v, dict):
                defects.append(f"{where}: not an object")
                continue
            _unknown_keys(v, _RUNTIME_VARIANT_KEYS, where, defects)
            if v.get("platform") not in PLATFORMS:
                defects.append(f"{where}: platform {v.get('platform')!r} is not one of {PLATFORMS}")
            if v.get("accel") not in ACCELERATIONS:
                defects.append(f"{where}: accel {v.get('accel')!r} is not one of {ACCELERATIONS}")
            if v.get("archive") not in ARCHIVE_FORMATS:
                defects.append(f"{where}: archive {v.get('archive')!r} is not one of {ARCHIVE_FORMATS}")
            _check_file(
                {"name": f"{vid}.{v.get('archive', 'zip')}", "size": v.get("size"),
                 "sha256": v.get("sha256", ""), "sources": v.get("sources")},
                where, defects, counts,
            )
            files = v.get("sha256_files")
            if not isinstance(files, dict):
                defects.append(f"{where}: sha256_files must be an object")
                files = {}
            if files:
                counts["hashes"] += 1
            entry = v.get("entry")
            if (not entry or entry not in files) and (files or v.get("sha256")):
                defects.append(f"{where}: entry {entry!r} is not listed in sha256_files")
            for rel, digest in files.items():
                if not _safe_file_name(str(rel)):
                    defects.append(f"{where}: sha256_files path {rel!r} is not a safe relative path")
                if not is_valid_sha256(str(digest)):
                    defects.append(f"{where}: sha256_files[{rel!r}] is not a 64-character hex digest")
            deps = v.get("system_deps", [])
            if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
                defects.append(f"{where}: system_deps must be a list of names")

    selftest = data.get("selftest")
    if not isinstance(selftest, dict):
        defects.append("selftest section is missing")
    else:
        _unknown_keys(selftest, _SELFTEST_KEYS, "selftest", defects)
        if not _safe_file_name(str(selftest.get("image", ""))) or "/" in str(selftest.get("image", "")):
            defects.append("selftest.image must be a plain file name")
        expect = selftest.get("expect_all")
        if not isinstance(expect, list) or not expect or not all(isinstance(x, str) and x for x in expect):
            defects.append("selftest.expect_all must be a non-empty list of strings")

    if counts["urls"] == 0 and counts["hashes"] == 0:
        # Nothing claims to be published (no sources, sizes or hashes at all).
        # Any defect found is structural, or a half-filled entry such as a source
        # with an empty URL, and still makes it defective.
        return (CatalogueState.DEFECTIVE, defects) if defects else (CatalogueState.UNRELEASED, [])

    # Published from here on: every file must carry its size, hash and sources.
    if isinstance(model, dict):
        for vid, v in (model.get("variants") or {}).items():
            for f in (v.get("files") or []) if isinstance(v, dict) else []:
                if isinstance(f, dict) and (not f.get("sha256") or not f.get("sources")):
                    defects.append(f"model variant {vid!r} file {f.get('name')!r}: published catalogue needs sha256 and sources")
    if isinstance(runtime, dict):
        for vid, v in (runtime.get("variants") or {}).items():
            if isinstance(v, dict) and (not v.get("sha256") or not v.get("sources") or not v.get("sha256_files")):
                defects.append(f"runtime variant {vid!r}: published catalogue needs sha256, sha256_files and sources")

    return (CatalogueState.DEFECTIVE, defects) if defects else (CatalogueState.PUBLISHED, [])


# ─── Loading ─────────────────────────────────────────────────────────────────
def bundled_catalogue_path() -> Path:
    """The bundled catalogue, or INKDOC_GLM_OCR_CATALOGUE in source runs (CI tests new pins).

    The override is ignored in packaged builds: a release only ever trusts the
    catalogue that shipped inside it (or a signed remote one).
    """
    override = os.environ.get("INKDOC_GLM_OCR_CATALOGUE", "").strip()
    if override and not getattr(sys, "frozen", False):
        return Path(override)
    return BUNDLED_CATALOGUE_PATH


def load_bundled_catalogue(path: Path | None = None) -> Catalogue:
    path = path or bundled_catalogue_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error("GLM-OCR catalogue not found at %s; this is a packaging defect.", path)
        data = {}
    except Exception as exc:
        logger.error("Could not read GLM-OCR catalogue %s: %s", path, exc)
        data = {}
    cat = parse_catalogue(data, origin="bundled")
    if cat.state is CatalogueState.DEFECTIVE:
        logger.error("Bundled GLM-OCR catalogue is defective: %s", "; ".join(cat.defects))
    return cat


def accept_remote_catalogue(
    envelope_raw: bytes,
    *,
    bundled_version: int,
    seen_version: int,
    app_version: str,
    trusted_public_keys: list[Any] | None = None,
) -> tuple[Catalogue | None, str]:
    """Verify a signed remote catalogue. Returns (catalogue, "") or (None, reason).

    Order matters: the signature is checked over the raw base64 payload before
    anything is parsed (verify_signed_envelope), then the rollback guard, the
    app version and finally the shared validation.
    """
    from app.core.update_verifier import UpdateVerificationError, verify_signed_envelope

    try:
        payload = verify_signed_envelope(envelope_raw, trusted_public_keys, what="GLM-OCR catalogue")
        data = json.loads(payload.decode("utf-8"))
    except UpdateVerificationError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"Malformed GLM-OCR catalogue payload: {exc}"
    if not isinstance(data, dict):
        return None, "GLM-OCR catalogue payload is not a JSON object."

    version = data.get("catalogue_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return None, "Remote catalogue has no integer catalogue_version."
    floor = max(bundled_version, seen_version)
    if version <= floor:
        return None, (
            f"Remote catalogue version {version} is not newer than the version in use ({floor}); "
            "kept the current pins."
        )
    if not version_at_least(app_version, str(data.get("min_app_version", ""))):
        return None, f"Remote catalogue needs InkDoc {data.get('min_app_version')} or newer."

    cat = parse_catalogue(data, origin="remote")
    if cat.state is not CatalogueState.PUBLISHED:
        return None, "Remote catalogue failed validation: " + "; ".join(cat.defects[:5])
    return cat, ""


def fetch_remote_catalogue_bytes(url: str = REMOTE_CATALOGUE_URL, timeout: float = REMOTE_FETCH_TIMEOUT_S) -> bytes:
    """Download the signed envelope. App-update host rules: InkDoc's GitHub only."""
    validate_download_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "InkDoc-GlmOcr", "Accept": "application/json, */*"})
    opener = urllib.request.build_opener(StrictRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read(REMOTE_MAX_BYTES + 1)
    if len(data) > REMOTE_MAX_BYTES:
        raise SecurityError("Remote GLM-OCR catalogue is larger than allowed.")
    return data


class CatalogueProvider:
    """The catalogue in effect: the bundled one, or a newer verified remote one.

    The remote check runs only when asked (card opened, install, update) and is
    cached for the session, so users who never open the card make no request.
    """

    def __init__(self, bundled_path: Path | None = None, remote_url: str = REMOTE_CATALOGUE_URL) -> None:
        self._bundled_path = bundled_path
        self._remote_url = remote_url
        self._bundled: Catalogue | None = None
        self._remote: Catalogue | None = None
        self._remote_checked = False
        self._remote_reason = ""
        self._lock = threading.Lock()

    @property
    def bundled(self) -> Catalogue:
        if self._bundled is None:
            self._bundled = load_bundled_catalogue(self._bundled_path)
        return self._bundled

    def current(self) -> Catalogue:
        """The remote catalogue if one was accepted this session, else the bundled one."""
        return self._remote or self.bundled

    def remote_status(self) -> dict[str, Any]:
        return {
            "checked": self._remote_checked,
            "in_use": self._remote is not None,
            "reason": self._remote_reason,
            "catalogue_version": self.current().catalogue_version,
        }

    def refresh_remote(self, *, force: bool = False) -> Catalogue:
        """Check the signed remote catalogue once per session (or again with force)."""
        with self._lock:
            if self._remote_checked and not force:
                return self.current()
            self._remote_checked = True
            from app._version import get_version
            from app.core.engine_manager import EngineManager

            mgr = EngineManager.get_instance()
            seen = _int(mgr.get_settings().get("glm_ocr_catalogue_seen", 0))
            try:
                raw = fetch_remote_catalogue_bytes(self._remote_url)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # Nothing newer has been published: the normal state until a
                    # signed catalogue exists. Not worth alarming anyone about.
                    self._remote_reason = ""
                    logger.debug("No online GLM-OCR catalogue published yet.")
                else:
                    self._remote_reason = f"Using the built-in catalogue (online check failed: HTTP {exc.code})."
                    logger.info("GLM-OCR remote catalogue unavailable: %s", exc)
                return self.current()
            except Exception as exc:
                self._remote_reason = "Using the built-in catalogue (could not check online)."
                logger.info("GLM-OCR remote catalogue unavailable: %s", exc)
                return self.current()
            cat, reason = accept_remote_catalogue(
                raw,
                bundled_version=self.bundled.catalogue_version,
                seen_version=seen,
                app_version=get_version(),
            )
            if cat is None:
                self._remote_reason = f"Using the built-in catalogue. {reason}"
                logger.info("GLM-OCR remote catalogue not used: %s", reason)
                return self.current()
            self._remote = cat
            self._remote_reason = f"Using the signed online catalogue v{cat.catalogue_version}."
            if cat.catalogue_version > seen:
                mgr.update_settings({"glm_ocr_catalogue_seen": cat.catalogue_version})
            return cat


# ─── Platform ────────────────────────────────────────────────────────────────
_IMAGE_FILE_MACHINE_ARM64 = 0xAA64


def windows_native_machine() -> int | None:
    """The OS's real machine type via IsWow64Process2, or None if unavailable.

    InkDoc ships as x64 and runs emulated on Windows ARM64, where
    platform.machine() says AMD64. IsWow64Process2's native-machine answer is
    the real OS architecture, so llama-server (a separate process) can run as
    native ARM64.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = getattr(kernel32, "IsWow64Process2", None)
        if fn is None:
            return None
        process_machine = wintypes.USHORT()
        native_machine = wintypes.USHORT()
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT)]
        fn.restype = wintypes.BOOL
        if not fn(kernel32.GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native_machine)):
            return None
        return int(native_machine.value)
    except Exception:
        return None


def runtime_platform_key() -> str:
    """Platform key for picking a llama-server build (the OS's, not this process's)."""
    from app.core.engine_manifest import get_current_platform_key

    key = get_current_platform_key()
    if key.startswith("windows") and windows_native_machine() == _IMAGE_FILE_MACHINE_ARM64:
        return "windows-arm64"
    if key == "macos-arm64" or (sys.platform == "darwin" and _macos_is_arm()):
        return "macos-arm64"
    return key


def _macos_is_arm() -> bool:
    """True on Apple Silicon even when Python runs under Rosetta."""
    try:
        import subprocess

        out = subprocess.run(
            ["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True, timeout=2
        )
        return out.stdout.strip() == "1"
    except Exception:
        return platform.machine().lower() == "arm64"


def vulkan_loader_present() -> bool:
    """Whether the system Vulkan loader exists (the Vulkan build cannot start without it)."""
    try:
        if sys.platform == "win32":
            root = os.environ.get("SYSTEMROOT", r"C:\Windows")
            return (Path(root) / "System32" / "vulkan-1.dll").is_file()
        if sys.platform.startswith("linux"):
            import ctypes.util

            if ctypes.util.find_library("vulkan"):
                return True
            return any(
                Path(d, "libvulkan.so.1").exists()
                for d in ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib", "/lib/x86_64-linux-gnu")
            )
    except Exception:
        return False
    return False


__all__ = [
    "Catalogue",
    "CatalogueProvider",
    "CatalogueState",
    "DownloadFile",
    "ModelVariant",
    "RuntimeVariant",
    "Source",
    "SOURCE_LABELS",
    "accept_remote_catalogue",
    "load_bundled_catalogue",
    "parse_catalogue",
    "runtime_platform_key",
    "validate_glm_catalogue",
    "vulkan_loader_present",
]
