"""GLM-OCR catalogue: validation states, host rules and the signed remote catalogue."""
from __future__ import annotations

import base64
import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.download_utils import (  # noqa: E402
    ALLOWED_DOWNLOAD_HOSTS,
    MODEL_DOWNLOAD_HOST_SUFFIXES,
    MODEL_DOWNLOAD_HOSTS,
    SecurityError,
    host_matches_suffix,
    validate_download_url,
)
from app.core.glm_ocr_catalogue import (  # noqa: E402
    BUNDLED_CATALOGUE_PATH,
    MAX_FILE_BYTES,
    CatalogueProvider,
    CatalogueState,
    accept_remote_catalogue,
    load_bundled_catalogue,
    parse_catalogue,
    validate_glm_catalogue,
)

BUNDLED = json.loads(BUNDLED_CATALOGUE_PATH.read_text(encoding="utf-8"))


def cat() -> dict:
    return copy.deepcopy(BUNDLED)


def first_model_file(c: dict) -> dict:
    return c["model"]["variants"]["q8"]["files"][0]


def first_runtime(c: dict) -> dict:
    return c["runtime"]["variants"]["win-x64-cpu"]


# ─── Bundled baseline ────────────────────────────────────────────────────────

def test_bundled_catalogue_is_published():
    state, defects = validate_glm_catalogue(BUNDLED)
    assert state is CatalogueState.PUBLISHED, defects
    loaded = load_bundled_catalogue()
    assert loaded.published and loaded.origin == "bundled"
    assert loaded.model_variant().model_file == "GLM-OCR-Q8_0.gguf"
    assert loaded.selftest_expect == ["InkDoc", "2026", "self-test"]


def test_bundled_pins_match_the_plan():
    q8 = {f["name"]: f for f in BUNDLED["model"]["variants"]["q8"]["files"]}
    assert q8["GLM-OCR-Q8_0.gguf"]["sha256"] == "45bc244a6446aff850521dc41f18bc8d7105ad5f0c2c8c28af04e7cc4f4d50b1"
    assert q8["GLM-OCR-Q8_0.gguf"]["size"] == 950433408
    assert q8["mmproj-GLM-OCR-Q8_0.gguf"]["sha256"] == "9c4b58e33e316ed142eb5dcb41abec3844d3e6e5dc361ffb782c3fa9d175141f"
    assert BUNDLED["runtime"]["version"] == "b11307"
    for f in q8.values():
        assert [s["kind"] for s in f["sources"]] == ["upstream", "mirror"]


def test_every_runtime_lists_its_entry_and_backend():
    for vid, v in BUNDLED["runtime"]["variants"].items():
        assert v["entry"] in v["sha256_files"], vid
        assert any("ggml-cpu" in name for name in v["sha256_files"]), vid
        # Tools other than the server are never extracted.
        assert not any(n.startswith(("llama-cli", "llama-bench", "llama-quantize")) for n in v["sha256_files"]), vid


def test_runtime_for_picks_platform_builds():
    c = load_bundled_catalogue()
    assert c.runtime_for("windows-x86_64", "cpu").id == "win-x64-cpu"
    assert c.runtime_for("windows-x86_64", "gpu").id == "win-x64-vulkan"
    assert c.runtime_for("windows-arm64", "cpu").id == "win-arm64-cpu"
    assert c.runtime_for("windows-arm64", "gpu") is None
    assert c.runtime_for("macos-arm64", "cpu").accel == "metal"   # one build; -ngl 0 fallback
    assert c.runtime_for("macos-arm64", "gpu") is None
    assert c.runtime_for("linux-x86_64", "gpu").id == "linux-x64-vulkan"
    assert c.runtime_for("macos-x86_64", "cpu") is None           # Intel Mac: unsupported


# ─── Defects ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mutate, needle", [
    (lambda c: first_model_file(c).update(sha256="abc"), "64-character"),
    (lambda c: first_model_file(c).update(size=0), "size must be positive"),
    (lambda c: first_model_file(c).update(size=MAX_FILE_BYTES + 1), "exceeds"),
    (lambda c: first_model_file(c)["sources"].reverse(), "upstream sources must come before"),
    (lambda c: first_model_file(c)["sources"][0].update(url="https://evil.example.com/x.gguf"), "allowlist"),
    (lambda c: first_model_file(c)["sources"][0].update(url="http://huggingface.co/x.gguf"), "Insecure"),
    (lambda c: first_model_file(c)["sources"][1].update(url="https://github.com/someone/else/releases/download/v1/x.gguf"), "InkDoc GitHub release"),
    (lambda c: first_model_file(c)["sources"][0].update(kind="cdn"), "kind"),
    (lambda c: first_runtime(c).update(entry="missing.exe"), "entry"),
    (lambda c: first_runtime(c)["sha256_files"].update({"../evil.dll": "0" * 64}), "safe relative path"),
    (lambda c: first_runtime(c).update(accel="cuda"), "accel"),
    (lambda c: first_runtime(c).update(platform="windows-x86"), "platform"),
    (lambda c: c.update(surprise=True), "unknown keys"),
    (lambda c: c["model"]["variants"]["q8"].update(model_file="nope.gguf"), "model_file"),
    (lambda c: c.update(catalogue_version="1"), "catalogue_version"),
    (lambda c: first_model_file(c).update(sha256=""), "needs sha256"),
])
def test_each_defect_is_caught(mutate, needle):
    c = cat()
    mutate(c)
    state, defects = validate_glm_catalogue(c)
    assert state is CatalogueState.DEFECTIVE
    assert any(needle in d for d in defects), defects


def _strip_release_data(c: dict) -> dict:
    for v in c["model"]["variants"].values():
        for f in v["files"]:
            f.pop("size"), f.pop("sha256"), f.pop("sources")
    for v in c["runtime"]["variants"].values():
        for key in ("size", "sha256", "sources"):
            v.pop(key)
        v["sha256_files"] = {}
    return c


def test_unreleased_catalogue_is_not_installable():
    c = _strip_release_data(cat())
    state, defects = validate_glm_catalogue(c)
    assert state is CatalogueState.UNRELEASED, defects
    assert not parse_catalogue(c).published


def test_half_filled_catalogue_is_defective():
    c = _strip_release_data(cat())
    c["model"]["variants"]["q8"]["files"][0]["sha256"] = "a" * 64
    assert validate_glm_catalogue(c)[0] is CatalogueState.DEFECTIVE


# ─── Hosts ───────────────────────────────────────────────────────────────────

def test_hf_suffix_rule_is_on_a_dot_boundary():
    assert host_matches_suffix("us.aws.cdn.hf.co", (".hf.co",))
    assert host_matches_suffix("cas-bridge.xethub.hf.co", (".hf.co",))
    assert not host_matches_suffix("evilhf.co", (".hf.co",))
    assert not host_matches_suffix("hf.co.evil.com", (".hf.co",))
    assert not host_matches_suffix("hf.co", (".hf.co",))


def test_model_hosts_never_reach_app_updates():
    validate_download_url("https://us.aws.cdn.hf.co/x", MODEL_DOWNLOAD_HOSTS, MODEL_DOWNLOAD_HOST_SUFFIXES)
    validate_download_url("https://huggingface.co/x", MODEL_DOWNLOAD_HOSTS, MODEL_DOWNLOAD_HOST_SUFFIXES)
    for url in ("https://huggingface.co/x", "https://us.aws.cdn.hf.co/x"):
        with pytest.raises(SecurityError):
            validate_download_url(url)                       # app-update defaults
        with pytest.raises(SecurityError):
            validate_download_url(url, ALLOWED_DOWNLOAD_HOSTS)
    with pytest.raises(SecurityError):
        validate_download_url("https://evilhf.co/x", MODEL_DOWNLOAD_HOSTS, MODEL_DOWNLOAD_HOST_SUFFIXES)


# ─── Signed remote catalogue ─────────────────────────────────────────────────

def _keypair():
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv = ed25519.Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def _envelope(priv, payload: dict | bytes) -> bytes:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    b64 = base64.b64encode(raw).decode()
    sig = base64.b64encode(priv.sign(b64.encode())).decode()
    return json.dumps({"payload": b64, "signature": sig}).encode()


def _remote(version: int = 2, **extra) -> dict:
    c = cat()
    c.pop("_comment", None)
    c["catalogue_version"] = version
    c.update(extra)
    return c


def test_remote_good_signature_is_accepted():
    priv, pub = _keypair()
    got, reason = accept_remote_catalogue(
        _envelope(priv, _remote(2)), bundled_version=1, seen_version=0, app_version="1.1.0",
        trusted_public_keys=[pub],
    )
    assert got is not None and got.origin == "remote" and got.catalogue_version == 2, reason


def test_remote_bad_signature_is_rejected():
    priv, _ = _keypair()
    _, other = _keypair()
    got, reason = accept_remote_catalogue(
        _envelope(priv, _remote(2)), bundled_version=1, seen_version=0, app_version="1.1.0",
        trusted_public_keys=[other],
    )
    assert got is None and "signature" in reason.lower()


def test_remote_tampered_payload_is_rejected():
    priv, pub = _keypair()
    env = json.loads(_envelope(priv, _remote(2)))
    env["payload"] = base64.b64encode(json.dumps(_remote(3)).encode()).decode()
    got, _ = accept_remote_catalogue(json.dumps(env).encode(), bundled_version=1, seen_version=0,
                                     app_version="1.1.0", trusted_public_keys=[pub])
    assert got is None


@pytest.mark.parametrize("bundled, seen, version", [(1, 0, 1), (3, 0, 2), (1, 5, 4)])
def test_remote_rollback_is_blocked(bundled, seen, version):
    priv, pub = _keypair()
    got, reason = accept_remote_catalogue(
        _envelope(priv, _remote(version)), bundled_version=bundled, seen_version=seen,
        app_version="1.1.0", trusted_public_keys=[pub],
    )
    assert got is None and "not newer" in reason


def test_remote_needing_newer_app_is_rejected():
    priv, pub = _keypair()
    got, reason = accept_remote_catalogue(
        _envelope(priv, _remote(2, min_app_version="9.0.0")), bundled_version=1, seen_version=0,
        app_version="1.1.0", trusted_public_keys=[pub],
    )
    assert got is None and "9.0.0" in reason


def test_remote_malformed_or_invalid_payload_is_rejected():
    priv, pub = _keypair()
    got, _ = accept_remote_catalogue(_envelope(priv, b"not json"), bundled_version=1, seen_version=0,
                                     app_version="1.1.0", trusted_public_keys=[pub])
    assert got is None
    bad = _remote(2)
    bad["model"]["variants"]["q8"]["files"][0]["sha256"] = "zz"
    got, reason = accept_remote_catalogue(_envelope(priv, bad), bundled_version=1, seen_version=0,
                                          app_version="1.1.0", trusted_public_keys=[pub])
    assert got is None and "validation" in reason


class _Settings:
    def __init__(self):
        self.data = {"glm_ocr_catalogue_seen": 0}

    def get_settings(self):
        return dict(self.data)

    def update_settings(self, updates):
        self.data.update(updates)
        return dict(self.data)


def test_provider_uses_bundled_when_remote_fails():
    settings = _Settings()
    provider = CatalogueProvider()
    with patch("app.core.engine_manager.EngineManager.get_instance", return_value=settings), \
         patch("app.core.glm_ocr_catalogue.fetch_remote_catalogue_bytes", side_effect=OSError("offline")):
        got = provider.refresh_remote()
    assert got.origin == "bundled"
    assert "could not check online" in provider.remote_status()["reason"]


def test_provider_is_quiet_when_nothing_is_published_yet():
    import urllib.error

    provider = CatalogueProvider()
    not_found = urllib.error.HTTPError("https://github.com/x", 404, "Not Found", {}, None)
    with patch("app.core.engine_manager.EngineManager.get_instance", return_value=_Settings()),          patch("app.core.glm_ocr_catalogue.fetch_remote_catalogue_bytes", side_effect=not_found):
        assert provider.refresh_remote().origin == "bundled"
    assert provider.remote_status()["reason"] == ""


def test_provider_accepts_newer_remote_once_and_records_it():
    settings = _Settings()
    priv, pub = _keypair()
    provider = CatalogueProvider()
    calls = []

    def fetch(_url):
        calls.append(1)
        return _envelope(priv, _remote(7))

    real_accept = accept_remote_catalogue
    with patch("app.core.engine_manager.EngineManager.get_instance", return_value=settings), \
         patch("app._version.get_version", return_value="1.1.0"), \
         patch("app.core.glm_ocr_catalogue.fetch_remote_catalogue_bytes", side_effect=fetch), \
         patch("app.core.glm_ocr_catalogue.accept_remote_catalogue",
               side_effect=lambda raw, **kw: real_accept(raw, trusted_public_keys=[pub], **kw)):
        assert provider.refresh_remote().catalogue_version == 7
        provider.refresh_remote()                 # cached for the session
    assert len(calls) == 1
    assert settings.data["glm_ocr_catalogue_seen"] == 7
    assert provider.current().origin == "remote"
