"""Moving from one Docling pack to the next.

An InkDoc release names one pack in manifest.json. A user who installed the
previous one must keep a working Docling until they choose to update: the pack is
checked against the launch hashes this app keeps for it (EARLIER_PACKS), the
card offers the update, and the update keeps the add-on installed inside the old
pack and stops the worker before the swap.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
import threading
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core import engine_manager as em
from app.core.addon_manager import AddonError, AddonManager
from app.core.engine_manifest import (
    EarlierPackFiles,
    EngineManifest,
    PlatformPackInfo,
    docling_version_in,
)
from app.core.engines.docling_worker_client import DoclingWorkerClient

ADDON = "code_enrichment"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(mgr: em.EngineManager, *, version: str, files: dict[str, bytes], sha256: str = "0" * 64,
              size: int = 100) -> EngineManifest:
    pack = PlatformPackInfo(
        url="https://github.com/AbdoslamB/InkDoc/releases/download/docling-pack-v2/pack.zip",
        archive_format="zip",
        sha256=sha256,
        size_bytes=size,
        uncompressed_size_bytes=size * 2,
        interpreter_path="python.exe",
        worker_script="worker.py",
        sha256_files={name: _sha(data) for name, data in files.items()},
    )
    return EngineManifest("1.0.0", version, "1.0.0", {mgr.platform_key: pack})


def _install_old_pack(target: Path, *, worker: bytes = b"old worker") -> None:
    target.mkdir(parents=True)
    (target / "python.exe").write_bytes(b"old python")
    (target / "worker.py").write_bytes(worker)
    (target / "pack_meta.json").write_text(json.dumps({"pack_version": "1.0.0"}), encoding="utf-8")


def _earlier(mgr: em.EngineManager) -> dict[str, dict[str, EarlierPackFiles]]:
    return {"1.0.0": {mgr.platform_key: EarlierPackFiles(
        "python.exe", _sha(b"old python"), "worker.py", _sha(b"old worker"),
    )}}


@pytest.fixture
def mgr(tmp_path):
    m = em.EngineManager.get_instance()
    target = tmp_path / "engines" / "docling"
    new_files = {"python.exe": b"new python", "worker.py": b"new worker"}
    with (
        patch.object(m, "get_engine_dir", return_value=target),
        patch.object(m, "manifest", _manifest(m, version="2.0.0", files=new_files)),
        patch.object(em, "EARLIER_PACKS", _earlier(m)),
    ):
        yield m, target


def test_an_earlier_pack_keeps_launching(mgr):
    m, target = mgr
    _install_old_pack(target)

    assert m.is_pack_outdated()
    assert m.is_pack_installed()
    # Checked against its own hashes, not the new manifest's.
    assert m.verify_launch_integrity()

    (target / "worker.py").write_bytes(b"tampered")
    assert not m.verify_launch_integrity()


def test_an_unknown_pack_version_is_checked_against_the_manifest(mgr):
    m, target = mgr
    _install_old_pack(target)
    (target / "pack_meta.json").write_text(json.dumps({"pack_version": "0.9.0"}), encoding="utf-8")

    assert not m.is_pack_outdated()
    assert not m.verify_launch_integrity()


def test_an_earlier_pack_launches_the_files_that_were_checked(mgr):
    m, target = mgr
    _install_old_pack(target)
    earlier = {"1.0.0": {m.platform_key: EarlierPackFiles(
        "old/python.exe", _sha(b"old python"), "old/worker.py", _sha(b"old worker"),
    )}}
    with patch.object(em, "EARLIER_PACKS", earlier):
        assert m.launch_paths() == (target / "old/python.exe", target / "old/worker.py")


def test_an_empty_launch_hash_fails_closed(mgr):
    m, target = mgr
    target.mkdir(parents=True)
    (target / "python.exe").write_bytes(b"new python")
    (target / "worker.py").write_bytes(b"new worker")
    (target / "pack_meta.json").write_text(json.dumps({"pack_version": "2.0.0"}), encoding="utf-8")
    manifest = _manifest(m, version="2.0.0", files={"python.exe": b"new python", "worker.py": b"new worker"})
    manifest.supported_platforms[m.platform_key].sha256_files["worker.py"] = ""
    with patch.object(m, "manifest", manifest):
        assert not m.verify_launch_integrity()
        # A file the manifest lists no hash for is still not checked.
        del manifest.supported_platforms[m.platform_key].sha256_files["worker.py"]
        assert m.verify_launch_integrity()


def test_a_pack_version_that_is_not_text_is_unknown(mgr):
    m, target = mgr
    _install_old_pack(target)
    (target / "pack_meta.json").write_text(json.dumps({"pack_version": ["1.0.0"]}), encoding="utf-8")
    assert m.installed_pack_version() is None
    assert not m.is_pack_outdated()


def test_the_current_pack_is_not_outdated(mgr):
    m, target = mgr
    target.mkdir(parents=True)
    (target / "python.exe").write_bytes(b"new python")
    (target / "worker.py").write_bytes(b"new worker")
    (target / "pack_meta.json").write_text(json.dumps({"pack_version": "2.0.0"}), encoding="utf-8")

    assert not m.is_pack_outdated()
    assert m.verify_launch_integrity()


def test_verify_of_an_earlier_pack_says_what_it_checked(mgr):
    m, target = mgr
    _install_old_pack(target)

    res = m.verify_full_installed_tree()
    assert res["valid"] is True
    assert res["outdated"] is True
    assert res["pack_version"] == "1.0.0"
    assert "only its interpreter and worker" in res["note"]

    (target / "python.exe").write_bytes(b"tampered")
    res = m.verify_full_installed_tree()
    assert res["valid"] is False
    assert res["outdated"] is True


def test_the_engines_list_offers_the_update(mgr):
    m, target = mgr
    _install_old_pack(target)
    site = target / "python" / "Lib" / "site-packages"
    (site / "docling-2.130.0.dist-info").mkdir(parents=True)
    (site / "docling_core-2.98.0.dist-info").mkdir()

    with patch.object(m, "latest_docling_version", return_value="2.140.0"):
        info = m.get_all_engines_info()["engines"]["docling"]

    assert info["status"] == "installed"
    assert info["update_available"] is True
    assert info["version"] == "1.0.0"
    assert info["pack_version"] == "2.0.0"
    assert info["docling_version"] == "2.130.0"
    assert info["latest_docling_version"] == "2.140.0"
    assert info["security_fixes"] == ["GHSA-cgc7-9qp3-86m3", "GHSA-3cr3-8m4c-fpxw"]


def test_docling_version_in_a_file_list():
    files = {
        "python/Lib/site-packages/docling_core-2.103.0.dist-info/RECORD": "x",
        "python/Lib/site-packages/docling-2.140.0.dist-info/RECORD": "x",
    }
    assert docling_version_in(files) == "2.140.0"
    assert docling_version_in({"worker.py": "x"}) == ""


# ─── The update itself ───────────────────────────────────────────────────────


def _pack_zip(dest: Path) -> tuple[bytes, dict[str, bytes]]:
    files = {"python.exe": b"new python", "worker.py": b"new worker"}
    with zipfile.ZipFile(dest, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return dest.read_bytes(), files


class _Response:
    def __init__(self, data: bytes):
        self._data = data
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int = -1) -> bytes:
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk


def _add_addon(pack: Path, addon_mgr: AddonManager) -> Path:
    addon = addon_mgr.get_addon(ADDON)
    model = pack / "models" / addon.model_dir
    model.mkdir(parents=True)
    (model / "weights.bin").write_bytes(b"w" * 10)
    (pack / "models" / f".inkdoc-addon-{ADDON}.json").write_text(
        json.dumps({"addon": ADDON, "docling_version": "2.130.0", "worker_verified": True}), encoding="utf-8"
    )
    return model


def test_an_update_keeps_the_addon_and_pauses_the_worker(tmp_path):
    m = em.EngineManager.get_instance()
    target = tmp_path / "docling"
    _install_old_pack(target)
    addon_mgr = AddonManager()
    _add_addon(target, addon_mgr)

    data, files = _pack_zip(tmp_path / "pack.zip")
    manifest = _manifest(m, version="2.0.0", files=files, sha256=_sha(data), size=len(data))

    events: list[str] = []
    client = MagicMock()
    client.paused.return_value.__enter__.side_effect = lambda *a: events.append("paused")
    client.paused.return_value.__exit__.side_effect = lambda *a: events.append("resumed")

    def reconfirm(name):
        events.append(f"reconfirm {name}")
        return True

    with (
        patch.object(m, "manifest", manifest),
        patch.object(m, "get_engine_dir", return_value=target),
        patch.object(m, "_open_safe_download_stream", return_value=_Response(data)),
        patch.object(m, "_check_disk_space"),
        patch.object(DoclingWorkerClient, "get_instance", return_value=client),
        patch.object(AddonManager, "get_instance", return_value=addon_mgr),
        patch.object(addon_mgr, "reconfirm", side_effect=reconfirm),
        patch.object(addon_mgr, "_models_dir", return_value=target / "models"),
    ):
        res = m.install_engine("docling")

        assert res["status"] == "success"
        assert (target / "worker.py").read_bytes() == b"new worker"
        assert json.loads((target / "pack_meta.json").read_text(encoding="utf-8"))["pack_version"] == "2.0.0"
        # The add-on moved across, metadata included, and the old pack is gone.
        assert addon_mgr.is_installed(ADDON)
        assert not (tmp_path / "docling.old.tmp").exists()
        # The worker was stopped for the swap, and the add-on confirmed afterwards.
        assert events == ["paused", "resumed", f"reconfirm {ADDON}"]


def test_a_first_install_does_not_touch_the_worker(tmp_path):
    m = em.EngineManager.get_instance()
    target = tmp_path / "docling"
    data, files = _pack_zip(tmp_path / "pack.zip")
    manifest = _manifest(m, version="2.0.0", files=files, sha256=_sha(data), size=len(data))
    client = MagicMock()

    with (
        patch.object(m, "manifest", manifest),
        patch.object(m, "get_engine_dir", return_value=target),
        patch.object(m, "_open_safe_download_stream", return_value=_Response(data)),
        patch.object(m, "_check_disk_space"),
        patch.object(DoclingWorkerClient, "get_instance", return_value=client),
    ):
        assert m.install_engine("docling")["status"] == "success"
    client.paused.assert_not_called()


def test_a_busy_worker_leaves_the_installed_pack_alone(tmp_path):
    m = em.EngineManager.get_instance()
    target = tmp_path / "docling"
    _install_old_pack(target)
    data, files = _pack_zip(tmp_path / "pack.zip")
    manifest = _manifest(m, version="2.0.0", files=files, sha256=_sha(data), size=len(data))
    client = MagicMock()
    client.paused.side_effect = RuntimeError("Docling is still converting a file.")

    with (
        patch.object(m, "manifest", manifest),
        patch.object(m, "get_engine_dir", return_value=target),
        patch.object(m, "_open_safe_download_stream", return_value=_Response(data)),
        patch.object(m, "_check_disk_space"),
        patch.object(DoclingWorkerClient, "get_instance", return_value=client),
        pytest.raises(em.EngineInstallError, match="still converting"),
    ):
        m.install_engine("docling")
    assert (target / "worker.py").read_bytes() == b"old worker"
    assert not (tmp_path / "docling.staging.tmp").exists()


def test_the_old_pack_is_deleted_after_docling_resumes(tmp_path):
    # Deleting the old pack can take minutes (thousands of files, antivirus);
    # conversions must not wait for it.
    m = em.EngineManager.get_instance()
    target = tmp_path / "docling"
    _install_old_pack(target)
    data, files = _pack_zip(tmp_path / "pack.zip")
    manifest = _manifest(m, version="2.0.0", files=files, sha256=_sha(data), size=len(data))
    events: list[str] = []
    client = MagicMock()
    client.paused.return_value.__enter__.side_effect = lambda *a: events.append("paused")
    client.paused.return_value.__exit__.side_effect = lambda *a: events.append("resumed")
    real_rmtree = em.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path).name == "docling.old.tmp" and Path(path).exists():
            events.append("old pack deleted")
        return real_rmtree(path, *args, **kwargs)

    with (
        patch.object(m, "manifest", manifest),
        patch.object(m, "get_engine_dir", return_value=target),
        patch.object(m, "_open_safe_download_stream", return_value=_Response(data)),
        patch.object(m, "_check_disk_space"),
        patch.object(DoclingWorkerClient, "get_instance", return_value=client),
        patch.object(em.shutil, "rmtree", side_effect=rmtree),
    ):
        assert m.install_engine("docling")["status"] == "success"
    assert events == ["paused", "resumed", "old pack deleted"]
    assert not (tmp_path / "docling.old.tmp").exists()


def test_an_update_that_stopped_halfway_is_restored_by_the_next_install(tmp_path):
    # InkDoc closed between the two renames: no pack, the old one in docling.old.tmp.
    m = em.EngineManager.get_instance()
    target = tmp_path / "docling"
    _install_old_pack(tmp_path / "docling.old.tmp")
    data, files = _pack_zip(tmp_path / "pack.zip")
    manifest = _manifest(m, version="2.0.0", files=files, sha256=_sha(data), size=len(data))
    client = MagicMock()

    with (
        patch.object(m, "manifest", manifest),
        patch.object(m, "get_engine_dir", return_value=target),
        patch.object(m, "_open_safe_download_stream", return_value=_Response(data)),
        patch.object(m, "_check_disk_space"),
        patch.object(DoclingWorkerClient, "get_instance", return_value=client),
    ):
        assert m.install_engine("docling")["status"] == "success"
    # Restored first, so the install ran as an update of it.
    client.paused.assert_called_once()
    assert (target / "worker.py").read_bytes() == b"new worker"
    assert not (tmp_path / "docling.old.tmp").exists()


def test_a_leftover_backup_beside_a_working_pack_is_removed(tmp_path):
    m = em.EngineManager.get_instance()
    target = tmp_path / "docling"
    _install_old_pack(target, worker=b"current")
    _install_old_pack(tmp_path / "docling.old.tmp")
    m._recover_interrupted_update("docling", target)
    assert (target / "worker.py").read_bytes() == b"current"
    assert not (tmp_path / "docling.old.tmp").exists()


def test_a_rename_held_up_by_a_lock_is_retried(tmp_path):
    attempts = []

    def rename(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("in use")

    with patch.object(em.os, "rename", side_effect=rename), patch.object(em.time, "sleep"):
        em._rename_retrying(tmp_path / "a", tmp_path / "b")
    assert len(attempts) == 3

    with (
        patch.object(em.os, "rename", side_effect=PermissionError("in use")),
        patch.object(em.time, "sleep"),
        pytest.raises(PermissionError),
    ):
        em._rename_retrying(tmp_path / "a", tmp_path / "b")


def test_one_addon_that_fails_to_confirm_does_not_stop_the_others():
    addon_mgr = MagicMock()
    addon_mgr.reconfirm.side_effect = [RuntimeError("worker died"), True]
    with patch.object(AddonManager, "get_instance", return_value=addon_mgr):
        em.EngineManager._confirm_carried_addons(["first", "second"])
    assert [c.args[0] for c in addon_mgr.reconfirm.call_args_list] == ["first", "second"]


# ─── The worker's pause ──────────────────────────────────────────────────────


def test_paused_stops_the_worker_and_holds_conversions_off():
    client = DoclingWorkerClient()
    with patch.object(client, "_shutdown_locked") as shutdown:
        with client.paused(timeout=1):
            shutdown.assert_called_once()
            assert not client._mutex.acquire(blocking=False)
        assert client._mutex.acquire(blocking=False)
        client._mutex.release()


def test_paused_gives_up_while_a_conversion_runs():
    client = DoclingWorkerClient()
    held = threading.Event()
    release = threading.Event()

    def convert():
        with client._mutex:
            held.set()
            release.wait(5)

    t = threading.Thread(target=convert)
    t.start()
    held.wait(5)
    try:
        with pytest.raises(RuntimeError, match="still converting"), client.paused(timeout=0.05):
            pass
    finally:
        release.set()
        t.join(5)


# ─── The add-on's side ───────────────────────────────────────────────────────


def test_carry_over_skips_a_pack_that_already_has_the_model(tmp_path):
    addon_mgr = AddonManager()
    old, new = tmp_path / "old", tmp_path / "new"
    _add_addon(old, addon_mgr)
    model_dir = addon_mgr.get_addon(ADDON).model_dir
    (new / "models" / model_dir).mkdir(parents=True)

    assert addon_mgr.carry_over(old, new) == []
    assert (old / "models" / model_dir / "weights.bin").is_file()


def _record(addon_mgr: AddonManager, pack: Path) -> dict:
    return json.loads((pack / "models" / f".inkdoc-addon-{ADDON}.json").read_text(encoding="utf-8"))


def test_reconfirm_records_the_new_docling_only_when_the_worker_loads_it(tmp_path):
    addon_mgr = AddonManager()
    pack = tmp_path / "docling"
    _add_addon(pack, addon_mgr)

    with (
        patch.object(addon_mgr, "_models_dir", return_value=pack / "models"),
        # The test model is not the catalogue's; its hashing is tested below.
        patch.object(addon_mgr, "_verify_tree"),
    ):
        with patch.object(addon_mgr, "_confirm_with_worker", side_effect=AddonError("cannot load")):
            assert addon_mgr.reconfirm(ADDON) is False
        assert _record(addon_mgr, pack)["docling_version"] == "2.130.0"

        # The probe could not run: not a confirmation either.
        unverified = {"docling_version": "2.140.0", "worker_verified": False, "worker_model_dir": ""}
        with patch.object(addon_mgr, "_confirm_with_worker", return_value=unverified):
            assert addon_mgr.reconfirm(ADDON) is False
        assert _record(addon_mgr, pack)["docling_version"] == "2.130.0"

        verified = {"docling_version": "2.140.0", "worker_verified": True, "worker_model_dir": "x"}
        with patch.object(addon_mgr, "_confirm_with_worker", return_value=verified):
            assert addon_mgr.reconfirm(ADDON) is True
        assert _record(addon_mgr, pack)["docling_version"] == "2.140.0"
        assert _record(addon_mgr, pack)["addon"] == ADDON


def test_a_carried_addon_whose_files_changed_is_not_confirmed(tmp_path):
    # The test model's files are not the ones the catalogue pins.
    addon_mgr = AddonManager()
    pack = tmp_path / "docling"
    _add_addon(pack, addon_mgr)
    confirm = MagicMock(return_value={"docling_version": "2.140.0", "worker_verified": True})
    with (
        patch.object(addon_mgr, "_models_dir", return_value=pack / "models"),
        patch.object(addon_mgr, "_confirm_with_worker", confirm),
    ):
        assert addon_mgr.reconfirm(ADDON) is False
    confirm.assert_not_called()
    assert _record(addon_mgr, pack)["docling_version"] == "2.130.0"


def test_carry_over_keeps_to_the_models_folders(tmp_path):
    addon_mgr = AddonManager()
    old, new = tmp_path / "old", tmp_path / "new"
    escaping = dataclasses.replace(addon_mgr.get_addon(ADDON), model_dir="../outside")
    with patch.object(addon_mgr, "_load_catalogue", return_value={ADDON: escaping}):
        (old / "outside").mkdir(parents=True)
        (old / "models").mkdir()
        (old / "models" / f".inkdoc-addon-{ADDON}.json").write_text("{}", encoding="utf-8")
        assert addon_mgr.carry_over(old, new) == []
    assert (old / "outside").is_dir()
