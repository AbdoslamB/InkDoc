"""Catalogue validation, capability reporting, and a real-HTTP install.

The add-on's failure modes were all in the seams between components that each
looked correct alone: `get_status` and `install` disagreed about what a usable
URL was, the download path had only ever been exercised with `stream_download`
stubbed out, and nothing checked that the pack could load what was installed.
These tests cover those seams.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import tarfile
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.core.addon_manager as am  # noqa: E402
from app.core.addon_manager import (  # noqa: E402
    AddonInfo,
    AddonManager,
    CatalogueState,
    validate_addon_entry,
)

GOOD_SHA = "a" * 64
GOOD_URL = "https://github.com/AbdoslamB/InkDoc/releases/download/docling-addon-v1/a.tar.gz"


def make_entry(**overrides) -> dict:
    entry = {
        "title": "Code & Formula Recognition",
        "description": "Transcribes code and formulas.",
        "model_dir": "docling-project--CodeFormulaV2",
        "url": GOOD_URL,
        "sha256": GOOD_SHA,
        "archive_format": "tar.gz",
        "size_bytes": 671088640,
        "uncompressed_size_bytes": 1400000000,
        "min_pack_version": "6.0.0",
        "min_app_version": "",
        "sha256_files": {"config.json": GOOD_SHA},
    }
    entry.update(overrides)
    return entry


def info(**overrides) -> AddonInfo:
    entry = make_entry(**overrides)
    return AddonInfo(name="code_enrichment", **entry)


# ─── Catalogue validation ────────────────────────────────────────────────────

def test_fully_specified_entry_is_published():
    state, defects = validate_addon_entry(info())
    assert state is CatalogueState.PUBLISHED, defects
    assert defects == []


def test_empty_url_and_sha_is_the_valid_unreleased_state():
    """The pre-release state must be accepted, not reported as a defect."""
    state, defects = validate_addon_entry(
        info(url="", sha256="", size_bytes=0, uncompressed_size_bytes=0, sha256_files={})
    )
    assert state is CatalogueState.UNRELEASED
    assert defects == []


@pytest.mark.parametrize(
    "overrides, expected_fragment",
    [
        # The literal string build_addon.py used to print for a human to replace.
        ({"url": "<publish the archive and put its URL here>"}, "url rejected"),
        ({"url": GOOD_URL, "sha256": ""}, "url is set but sha256 is empty"),
        ({"url": "", "sha256": GOOD_SHA}, "sha256 is set but url is empty"),
        ({"sha256": "placeholder_abc"}, "not a 64-character hex digest"),
        ({"sha256": "deadbeef"}, "not a 64-character hex digest"),
        ({"url": "https://evil.example.com/a.tar.gz"}, "not in the trusted CDN allowlist"),
        ({"url": "http://github.com/a.tar.gz"}, "Only HTTPS"),
        ({"sha256_files": {}}, "sha256_files is empty"),
        ({"sha256_files": {"config.json": "nope"}}, "malformed digest"),
        ({"size_bytes": 0}, "size_bytes must be positive"),
        ({"uncompressed_size_bytes": 0}, "uncompressed_size_bytes must be positive"),
        ({"model_dir": ""}, "model_dir is empty"),
        ({"min_pack_version": ""}, "min_pack_version is empty"),
        ({"archive_format": "rar"}, "is not tar.gz or zip"),
        # Half-filled: no artifact, but the size fields claim otherwise.
        (
            {"url": "", "sha256": "", "size_bytes": 1, "uncompressed_size_bytes": 0,
             "sha256_files": {}},
            "partially filled in",
        ),
    ],
)
def test_defective_entries_are_rejected(overrides, expected_fragment):
    state, defects = validate_addon_entry(info(**overrides))
    assert state is CatalogueState.DEFECTIVE, f"{overrides} should be defective"
    assert any(expected_fragment in d for d in defects), defects


def write_catalogue(tmp_path: Path, entry: dict) -> Path:
    path = tmp_path / "addons.json"
    path.write_text(
        json.dumps({"addons": {"code_enrichment": entry}}), encoding="utf-8"
    )
    return path


def test_defective_entry_is_never_offered_for_install(tmp_path):
    """status and install must agree. They previously did not.

    `get_status` asked `bool(url) and bool(sha256)`, which a placeholder string
    satisfies, so the UI showed an enabled Install button for an entry that
    `install` would reject as soon as it validated the URL.
    """
    path = write_catalogue(
        tmp_path, make_entry(url="<publish the archive and put its URL here>")
    )
    mgr = AddonManager(catalogue_path=path)

    status = mgr.get_status("code_enrichment")
    assert status["catalogue_state"] == "defective"
    assert status["available"] is False
    assert status["installable"] is False
    assert status["usable"] is False
    assert status["catalogue_defects"], "defects must be reported to the caller"

    # install() refuses via the installable check first and the catalogue-state
    # check second; both are fail-closed and either is correct here.
    with pytest.raises((am.AddonError, am.SecurityError)):
        mgr.install("code_enrichment")


def test_shipped_catalogue_passes_the_release_guard():
    """The catalogue in the repo must be valid in whatever state it is in."""
    from scripts.verify_addon_catalogue import verify

    ok, messages = verify(am.ADDONS_CATALOGUE_PATH, require_published=False)
    assert ok, "\n".join(messages)


# ─── Pack drift ──────────────────────────────────────────────────────────────

def test_pack_drift_makes_an_installed_addon_unusable(tmp_path, monkeypatch):
    """A pack carrying a different Docling version invalidates the install.

    Docling picks the model directory name, and has already changed where this
    model lives once. An add-on installed under the old pack's name may sit
    somewhere the new Docling never looks, so reporting it usable would enable
    toggles whose conversions then fail.
    """
    path = write_catalogue(tmp_path, make_entry())
    mgr = AddonManager(catalogue_path=path)

    models = tmp_path / "engines" / "docling" / "models"
    (models / "docling-project--CodeFormulaV2").mkdir(parents=True)
    (models / ".inkdoc-addon-code_enrichment.json").write_text(
        json.dumps({"addon": "code_enrichment", "docling_version": "2.130.0"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mgr, "_models_dir", lambda: models)
    monkeypatch.setattr(
        am.AddonManager, "pack_docling_version", lambda self: "2.140.0"
    )

    from app.core.engine_manager import EngineManager

    monkeypatch.setattr(EngineManager, "is_pack_installed", lambda self, n: True)
    monkeypatch.setattr(
        EngineManager, "get_engine_dir", lambda self, n: models.parent
    )
    (models.parent / "pack_meta.json").write_text(
        json.dumps({"pack_version": "6.0.0"}), encoding="utf-8"
    )

    assert mgr.is_installed("code_enrichment") is True
    status = mgr.get_status("code_enrichment")
    assert status["pack_drift"], "drift should be detected"
    assert status["usable"] is False, "a drifted install must not enable the toggles"
    assert "Reinstall" in status["reason"]


def test_matching_docling_version_is_not_drift(tmp_path, monkeypatch):
    path = write_catalogue(tmp_path, make_entry())
    mgr = AddonManager(catalogue_path=path)

    models = tmp_path / "engines" / "docling" / "models"
    (models / "docling-project--CodeFormulaV2").mkdir(parents=True)
    (models / ".inkdoc-addon-code_enrichment.json").write_text(
        json.dumps({"addon": "code_enrichment", "docling_version": "2.130.0"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mgr, "_models_dir", lambda: models)
    monkeypatch.setattr(
        am.AddonManager, "pack_docling_version", lambda self: "2.130.0"
    )
    assert mgr.pack_drift("code_enrichment") == ""


def test_pack_docling_version_reads_dist_info(tmp_path, monkeypatch):
    """Version comes from the dist-info directory, with no interpreter launch."""
    mgr = AddonManager(catalogue_path=write_catalogue(tmp_path, make_entry()))
    models = tmp_path / "engines" / "docling" / "models"
    site = models.parent / "python" / "Lib" / "site-packages"
    for name in (
        "docling-2.130.0.dist-info",
        "docling_core-2.98.0.dist-info",  # must not be mistaken for docling
        "docling_parse-7.21.0.dist-info",
    ):
        (site / name).mkdir(parents=True)
    monkeypatch.setattr(mgr, "_models_dir", lambda: models)
    assert mgr.pack_docling_version() == "2.130.0"


# ─── A real download, end to end ─────────────────────────────────────────────

def build_archive(tmp_path: Path) -> tuple[Path, str, dict[str, str], int]:
    """A real tar.gz laid out exactly as build_addon.py produces one."""
    model = tmp_path / "src" / "docling-project--CodeFormulaV2"
    model.mkdir(parents=True)
    (model / "config.json").write_text('{"model_type": "codeformula"}', encoding="utf-8")
    (model / "weights.bin").write_bytes(os.urandom(4096))

    files = sorted(p for p in model.rglob("*") if p.is_file())
    sha_files = {
        p.relative_to(model).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in files
    }
    uncompressed = sum(p.stat().st_size for p in files)

    archive = tmp_path / "addon.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(model, arcname=model.name, recursive=True)
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest(), sha_files, uncompressed


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.send_header("Content-Type", "application/gzip")
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args):
        pass


def allow_loopback(monkeypatch):
    """Extend the CDN allowlist to 127.0.0.1 for the duration of a test.

    validate_download_url already permits http for loopback when the host is in
    allowed_hosts, so this exercises the real validator on a real URL rather than
    replacing it. Its default argument is bound at definition time, so the
    function has to be wrapped rather than the module constant reassigned.
    """
    import app.core.download_utils as du

    real = du.validate_download_url
    allowed = du.ALLOWED_DOWNLOAD_HOSTS | {"127.0.0.1"}

    def permissive(url, allowed_hosts=None):
        return real(url, allowed if allowed_hosts is None else allowed_hosts | {"127.0.0.1"})

    monkeypatch.setattr(du, "validate_download_url", permissive)
    monkeypatch.setattr(am, "validate_download_url", permissive)


def serve(payload: bytes):
    _Handler.payload = payload
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def install_fixture(tmp_path, monkeypatch, *, corrupt=False):
    """Set up a manager that installs over real HTTP from a local server."""
    archive, sha, sha_files, uncompressed = build_archive(tmp_path)
    payload = archive.read_bytes()
    if corrupt:
        payload = payload[:-64] + b"\x00" * 64  # same length, wrong bytes

    server = serve(payload)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/addon.tar.gz"

    entry = make_entry(
        url=url,
        sha256=sha,
        size_bytes=len(archive.read_bytes()),
        uncompressed_size_bytes=uncompressed,
        sha256_files=sha_files,
    )
    mgr = AddonManager(catalogue_path=write_catalogue(tmp_path, entry))

    models = tmp_path / "engines" / "docling" / "models"
    models.mkdir(parents=True)
    monkeypatch.setattr(mgr, "_models_dir", lambda: models)
    allow_loopback(monkeypatch)
    monkeypatch.setattr(
        am.AddonManager, "pack_docling_version", lambda self: "2.130.0"
    )
    # The worker cannot run in a unit test; record the install as unverified.
    monkeypatch.setattr(
        am.AddonManager,
        "probe_worker_capability",
        lambda self, timeout=60.0: (_ for _ in ()).throw(RuntimeError("no worker here")),
    )

    from app.core.engine_manager import EngineManager

    monkeypatch.setattr(EngineManager, "is_pack_installed", lambda self, n: True)
    monkeypatch.setattr(EngineManager, "get_engine_dir", lambda self, n: models.parent)
    (models.parent / "pack_meta.json").write_text(
        json.dumps({"pack_version": "6.0.0"}), encoding="utf-8"
    )
    return mgr, models, server


def test_real_http_install_verify_and_remove(tmp_path, monkeypatch):
    """Exercises stream_download, extraction and per-file hashing for real.

    The existing add-on tests replace stream_download with a stub, so the actual
    download, disk-space check, archive extraction and tree verification had never
    run in CI.
    """
    mgr, models, server = install_fixture(tmp_path, monkeypatch)
    try:
        assert mgr.get_status("code_enrichment")["installable"] is True
        result = mgr.install("code_enrichment")
        assert result["status"] == "installed"

        model_dir = models / "docling-project--CodeFormulaV2"
        assert (model_dir / "config.json").is_file()
        assert (model_dir / "weights.bin").is_file()
        assert mgr.is_installed("code_enrichment") is True
        assert mgr.verify("code_enrichment")["valid"] is True

        meta = mgr.read_install_metadata("code_enrichment")
        assert meta["docling_version"] == "2.130.0"
        # The probe failed, so the install must not claim it was confirmed.
        assert meta["worker_verified"] is False

        mgr.remove("code_enrichment")
        assert mgr.is_installed("code_enrichment") is False
        assert not model_dir.exists()
    finally:
        server.shutdown()


def test_real_http_checksum_mismatch_installs_nothing(tmp_path, monkeypatch):
    """A corrupted download must leave no trace behind."""
    mgr, models, server = install_fixture(tmp_path, monkeypatch, corrupt=True)
    try:
        with pytest.raises(am.SecurityError) as caught:
            mgr.install("code_enrichment")
        assert "hecksum" in str(caught.value), str(caught.value)
        assert mgr.is_installed("code_enrichment") is False
        assert not (models / "docling-project--CodeFormulaV2").exists()
        assert mgr.get_progress("code_enrichment")["status"] == "error"
    finally:
        server.shutdown()


def test_tampered_file_after_extraction_is_caught(tmp_path, monkeypatch):
    """Per-file hashes must be checked, not just the archive's."""
    archive, sha, sha_files, uncompressed = build_archive(tmp_path)
    bad_files = dict(sha_files)
    bad_files["config.json"] = "b" * 64  # archive is intact; catalogue disagrees

    server = serve(archive.read_bytes())
    try:
        entry = make_entry(
            url=f"http://127.0.0.1:{server.server_address[1]}/addon.tar.gz",
            sha256=sha,
            size_bytes=archive.stat().st_size,
            uncompressed_size_bytes=uncompressed,
            sha256_files=bad_files,
        )
        mgr = AddonManager(catalogue_path=write_catalogue(tmp_path, entry))
        models = tmp_path / "engines" / "docling" / "models"
        models.mkdir(parents=True)
        monkeypatch.setattr(mgr, "_models_dir", lambda: models)
        allow_loopback(monkeypatch)
        from app.core.engine_manager import EngineManager

        monkeypatch.setattr(EngineManager, "is_pack_installed", lambda self, n: True)
        monkeypatch.setattr(EngineManager, "get_engine_dir", lambda self, n: models.parent)
        (models.parent / "pack_meta.json").write_text(
            json.dumps({"pack_version": "6.0.0"}), encoding="utf-8"
        )

        with pytest.raises(am.SecurityError):
            mgr.install("code_enrichment")
        assert mgr.is_installed("code_enrichment") is False
        assert not (models / "docling-project--CodeFormulaV2").exists()
    finally:
        server.shutdown()


def test_cancelled_install_leaves_nothing(tmp_path, monkeypatch):
    mgr, models, server = install_fixture(tmp_path, monkeypatch)
    try:
        real_download = am.stream_download

        def cancel_midway(url, destination_path, **kwargs):
            event = kwargs.get("cancel_event")
            if event is not None:
                event.set()
            return real_download(url, destination_path, **kwargs)

        monkeypatch.setattr(am, "stream_download", cancel_midway)
        # Cancellation surfaces as DownloadError when the download itself notices
        # the event, and as AddonError at the later checkpoints. Both are correct;
        # what matters is that it aborts and cleans up.
        with pytest.raises((am.DownloadError, am.AddonError)):
            mgr.install("code_enrichment")
        assert mgr.is_installed("code_enrichment") is False
        assert not (models / "docling-project--CodeFormulaV2").exists()
    finally:
        server.shutdown()


# ─── Worker capability reporting ─────────────────────────────────────────────

def load_worker_module():
    """Import the pack worker as a module, without running it."""
    import importlib.util

    path = REPO_ROOT / "app" / "core" / "engines" / "worker.py"
    spec = importlib.util.spec_from_file_location("_inkdoc_worker_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_capability_reports_missing_artifacts_path(monkeypatch):
    worker = load_worker_module()
    monkeypatch.delenv("DOCLING_ARTIFACTS_PATH", raising=False)
    cap = worker._code_formula_capability()
    assert cap["loadable"] is False
    assert cap["present"] is False
    assert cap["detail"], "a negative result must say why"


def test_capability_reports_missing_model_directory(monkeypatch, tmp_path):
    worker = load_worker_module()
    monkeypatch.setenv("DOCLING_ARTIFACTS_PATH", str(tmp_path))
    cap = worker._code_formula_capability()
    assert cap["loadable"] is False
    assert cap["detail"]


def test_convert_refuses_enrichment_without_the_model(monkeypatch, tmp_path):
    """The worker must refuse, not convert without what was asked for.

    Converting anyway would return a document with no transcribed code or
    formulas and no indication that the enrichment never happened.
    """
    worker = load_worker_module()
    monkeypatch.setattr(
        worker,
        "_code_formula_capability",
        lambda: {"model_dir": "d", "present": False, "loadable": False,
                 "detail": "directory missing"},
    )
    source = tmp_path / "in.pdf"
    source.write_bytes(b"%PDF-1.4")
    out = tmp_path / "out.md"

    resp = worker._handle_convert({
        "job_id": "j1",
        "source_file": str(source),
        "output_file": str(out),
        "code_enrichment": True,
    })
    assert resp["status"] == "error"
    assert resp["error_code"] == "enrichment_model_unavailable"
    assert "Settings" in resp["error"], "the error must tell the user what to do"
    assert not out.exists(), "no output may be produced"


def test_convert_without_enrichment_does_not_consult_the_model(monkeypatch, tmp_path):
    """The precheck must not affect ordinary conversions."""
    worker = load_worker_module()
    called = []
    monkeypatch.setattr(
        worker, "_code_formula_capability",
        lambda: called.append(1) or {"loadable": False, "detail": "x"},
    )
    monkeypatch.setattr(
        worker, "_get_converter",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("stop here")),
    )
    source = tmp_path / "in.pdf"
    source.write_bytes(b"%PDF-1.4")

    resp = worker._handle_convert({
        "job_id": "j2",
        "source_file": str(source),
        "output_file": str(tmp_path / "o.md"),
        "code_enrichment": False,
        "formula_enrichment": False,
    })
    assert called == [], "capability was probed for a non-enrichment conversion"
    assert resp["status"] == "error"
    assert resp.get("error_code") != "enrichment_model_unavailable"
