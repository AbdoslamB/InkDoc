"""The save folder: where auto-save writes, and its fallback to Downloads.

Settings live in a temporary settings.json and Downloads is a temporary
folder, so nothing here reads the user's settings or writes to their real
Downloads.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.server.server as server_module
from app.core import converter, save_location
from app.core.engine_manager import DEFAULT_SETTINGS, EngineManager
from app.core.queue_model import QueueItem, SourceKind
from app.server.server import app

client = TestClient(app)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated settings.json and Downloads; returns (downloads, settings_file)."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    settings_file = tmp_path / "settings.json"
    mgr = EngineManager.get_instance()
    monkeypatch.setattr(mgr, "get_settings_file_path", lambda: settings_file)
    monkeypatch.setattr(mgr, "_settings_cache", None)
    monkeypatch.setattr(converter, "get_downloads_dir", lambda: downloads)
    monkeypatch.setattr(server_module, "_conversion_semaphores", {})
    return downloads, settings_file


def _choose(folder: Path | str) -> None:
    EngineManager.get_instance().update_settings({"save_dir": str(folder)})


def _item(name="report.pdf") -> QueueItem:
    return QueueItem(source=f"/x/{name}", kind=SourceKind.FILE, display_name=name)


# ─── save_location ───────────────────────────────────────────────────────────

def test_default_is_downloads(env):
    downloads, _ = env
    assert DEFAULT_SETTINGS["save_dir"] == ""
    assert save_location.folder_state() == {
        "path": str(downloads), "chosen": "", "custom": False, "problem": "",
    }
    path, fallback = save_location.save_markdown(_item(), "# hi")
    assert (path.parent, fallback) == (downloads, None)


def test_saves_to_chosen_folder(env, tmp_path):
    folder = tmp_path / "Notes"
    folder.mkdir()
    _choose(folder)

    first, fb1 = save_location.save_markdown(_item(), "# one")
    second, fb2 = save_location.save_markdown(_item(), "# two")

    assert (fb1, fb2) == (None, None)
    assert [first.name, second.name] == ["report.md", "report (1).md"]
    assert first.parent == folder and first.read_text(encoding="utf-8") == "# one"
    assert save_location.folder_state() == {
        "path": str(folder), "chosen": str(folder), "custom": True, "problem": "",
    }


def test_missing_folder_falls_back_and_is_not_recreated(env, tmp_path):
    downloads, settings_file = env
    folder = tmp_path / "Gone"
    _choose(folder)

    path, fallback = save_location.save_markdown(_item(), "# hi")

    assert path.parent == downloads
    assert fallback == f"Saved to Downloads because the save folder {folder} was not found."
    assert not folder.exists(), "a deleted save folder must not be re-created"
    # The choice is kept, so the folder works again once it is back.
    assert json.loads(settings_file.read_text(encoding="utf-8"))["save_dir"] == str(folder)
    state = save_location.folder_state()
    assert state["path"] == str(downloads) and state["chosen"] == str(folder)
    assert "not found" in state["problem"]

    folder.mkdir()
    assert save_location.save_markdown(_item(), "# back")[0].parent == folder


def test_write_error_falls_back(env, tmp_path, monkeypatch):
    downloads, _ = env
    folder = tmp_path / "Locked"
    folder.mkdir()
    _choose(folder)
    real = converter.auto_save_markdown

    def failing(item, text, downloads_dir=None):
        if downloads_dir == folder:
            raise PermissionError("denied")
        return real(item, text, downloads_dir)

    monkeypatch.setattr(converter, "auto_save_markdown", failing)
    path, fallback = save_location.save_markdown(_item(), "# hi")

    assert path.parent == downloads
    assert fallback == f"Saved to Downloads because the save folder {folder} can't be written to."


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permissions and a non-root user",
)
def test_read_only_folder_falls_back(env, tmp_path):
    downloads, _ = env
    folder = tmp_path / "ReadOnly"
    folder.mkdir()
    _choose(folder)
    folder.chmod(0o500)
    try:
        path, fallback = save_location.save_markdown(_item(), "# hi")
        assert path.parent == downloads and "can't be written to" in fallback
        assert "can't write" in save_location.folder_state()["problem"]
        assert save_location.check_usable(folder) == "InkDoc can't write to that folder."
    finally:
        folder.chmod(0o700)


def test_symlinked_folder_falls_back(env, tmp_path):
    downloads, _ = env
    target = tmp_path / "Target"
    target.mkdir()
    link = tmp_path / "Link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    _choose(link)

    path, fallback = save_location.save_markdown(_item(), "# hi")

    assert path.parent == downloads
    assert fallback == f"Saved to Downloads because the save folder {link} is a link to another place."
    assert list(target.iterdir()) == []
    state = save_location.folder_state()
    assert state["path"] == str(downloads) and "link" in state["problem"]


def test_failed_write_leaves_no_partial_file(env, tmp_path):
    # Text that cannot be encoded fails after the file was created; neither
    # the chosen folder nor Downloads may keep the empty file.
    downloads, _ = env
    folder = tmp_path / "Notes"
    folder.mkdir()
    _choose(folder)

    with pytest.raises(UnicodeEncodeError):
        save_location.save_markdown(_item(), "x" + chr(0xD800))

    assert list(folder.iterdir()) == []
    assert list(downloads.iterdir()) == []


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")


def test_replaced_parent_folder_is_not_followed(env, tmp_path):
    # The folder above the chosen one is swapped for a link after it was
    # chosen; is_symlink() on the folder itself would not notice.
    downloads, _ = env
    share = tmp_path / "Share"
    (share / "Notes").mkdir(parents=True)
    _choose((share / "Notes").resolve())
    elsewhere = tmp_path / "Elsewhere"
    (elsewhere / "Notes").mkdir(parents=True)
    share.rename(tmp_path / "Share-old")
    _symlink(share, elsewhere)

    path, fallback = save_location.save_markdown(_item(), "# hi")

    assert path.parent == downloads
    assert fallback.endswith("is a link to another place.")
    assert list((elsewhere / "Notes").iterdir()) == []
    assert "link" in save_location.folder_state()["problem"]


@pytest.mark.parametrize("error", [RuntimeError("Symlink loop"), OSError("ELOOP"), ValueError("bad")])
def test_unresolvable_folder_falls_back(env, tmp_path, monkeypatch, error):
    # resolve() can fail (a link loop raises RuntimeError on Python 3.10-3.12);
    # when the check can't tell, nothing is written to the chosen folder.
    downloads, _ = env
    folder = tmp_path / "Notes"
    folder.mkdir()
    _choose(folder)
    real_resolve = Path.resolve

    def failing(self, *args, **kwargs):
        if self == folder:
            raise error
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", failing)
    path, fallback = save_location.save_markdown(_item(), "# hi")

    assert path.parent == downloads
    assert fallback.endswith("is a link to another place.")
    assert list(folder.iterdir()) == []
    assert save_location.folder_state()["problem"]


def test_name_taken_after_the_check_is_not_overwritten(env, tmp_path, monkeypatch):
    # Something appears at the free name between unique_download_path() and
    # the write; the save moves on to the next name instead of replacing it.
    folder = tmp_path / "Notes"
    folder.mkdir()
    _choose(folder)
    real = converter.unique_download_path
    calls = []

    def racing(directory, base_name):
        path = real(directory, base_name)
        if not calls:
            path.write_text("someone else's file", encoding="utf-8")
        calls.append(path.name)
        return path

    monkeypatch.setattr(converter, "unique_download_path", racing)
    path, fallback = save_location.save_markdown(_item(), "# mine")

    assert fallback is None
    assert path.name == "report (1).md" and path.read_text(encoding="utf-8") == "# mine"
    assert (folder / "report.md").read_text(encoding="utf-8") == "someone else's file"


def test_link_planted_at_the_name_is_not_followed(env, tmp_path, monkeypatch):
    folder = tmp_path / "Notes"
    folder.mkdir()
    _choose(folder)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    _symlink(folder / "report.md", victim)
    real = converter.unique_download_path
    first = []

    def racing(directory, base_name):
        if not first:  # hand out the planted name once, as a lost race would
            first.append(True)
            return directory / "report.md"
        return real(directory, base_name)

    monkeypatch.setattr(converter, "unique_download_path", racing)
    path, _ = save_location.save_markdown(_item(), "# mine")

    assert victim.read_text(encoding="utf-8") == "precious"
    assert path.name != "report.md" and path.read_text(encoding="utf-8") == "# mine"


def test_downloads_failure_still_raises(env, tmp_path, monkeypatch):
    _choose(tmp_path / "Gone")

    def failing(item, text, downloads_dir=None):
        raise PermissionError("Downloads is read-only")

    monkeypatch.setattr(converter, "auto_save_markdown", failing)
    with pytest.raises(PermissionError):
        save_location.save_markdown(_item(), "# hi")


def test_check_usable(tmp_path):
    good = tmp_path / "Good"
    good.mkdir()
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")

    assert save_location.check_usable(good) == ""
    assert list(good.iterdir()) == [], "the write test must clean up"
    assert save_location.check_usable(Path("relative/dir")) == "Choose a full folder path."
    assert save_location.check_usable(a_file) == "That folder doesn't exist."
    assert save_location.check_usable(tmp_path / "missing") == "That folder doesn't exist."


def test_relative_or_bad_values_mean_downloads(env):
    downloads, _ = env
    for value in ("relative/dir", "", None, 42):
        EngineManager.get_instance().update_settings({"save_dir": value})
        assert save_location.configured_save_dir() is None
        assert save_location.folder_state()["path"] == str(downloads)


def test_non_ascii_folder_round_trips(env, tmp_path):
    _, settings_file = env
    folder = tmp_path / "ملاحظات 📝"
    folder.mkdir()
    assert save_location.store_save_dir(str(folder)) is True

    EngineManager.get_instance()._settings_cache = None  # read the file again
    assert save_location.configured_save_dir() == folder
    assert save_location.save_markdown(_item(), "# hi")[0].parent == folder


def test_store_reports_a_failed_write(env, monkeypatch):
    _, settings_file = env
    # update_settings() logs and keeps going when the file can't be written.
    monkeypatch.setattr(
        EngineManager.get_instance(), "get_settings_file_path",
        lambda: settings_file.parent / "missing-dir" / "deeper" / "\0bad" / "settings.json",
    )
    assert save_location.store_save_dir("/somewhere") is False


def test_old_settings_file_loads(env):
    _, settings_file = env
    settings_file.write_text(json.dumps({"fallback_to_markitdown": True}), encoding="utf-8")
    assert EngineManager.get_instance().get_settings()["save_dir"] == ""
    assert save_location.configured_save_dir() is None


# ─── Server ──────────────────────────────────────────────────────────────────

def _upload(engine="markitdown", name="My Report.txt", **params):
    query = "&".join(f"{k}={v}" for k, v in {"engine": engine, "save_to_downloads": "true",
                                             "response_format": "json", **params}.items())
    return client.post(f"/convert/file?{query}",
                       files={"file": (name, io.BytesIO(b"hello"), "text/plain")})


@pytest.mark.parametrize("engine", ["markitdown", "auto"])
def test_conversion_saves_to_chosen_folder(env, tmp_path, engine):
    folder = tmp_path / "Notes"
    folder.mkdir()
    _choose(folder)

    resp = _upload(engine)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert Path(data["saved_to_downloads"]).parent == folder
    assert data["save_fallback"] is None


@pytest.mark.parametrize("engine", ["markitdown", "auto"])
def test_conversion_falls_back_and_says_so(env, tmp_path, engine):
    downloads, _ = env
    folder = tmp_path / "Gone"
    _choose(folder)

    data = _upload(engine).json()

    assert Path(data["saved_to_downloads"]).parent == downloads
    assert data["save_fallback"] == f"Saved to Downloads because the save folder {folder} was not found."
    assert data["save_fallback"] in data["warnings"]


def test_text_response_carries_the_fallback(env, tmp_path):
    _choose(tmp_path / "Gone")
    resp = client.post(
        "/convert/file?save_to_downloads=true",
        files={"file": ("a.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert resp.status_code == 200
    assert "Saved to Downloads because" in resp.headers.get("X-Engine-Warnings", "")
    assert resp.headers["X-Save-Fallback"].startswith("Saved to Downloads because the save folder")


def test_text_response_has_no_fallback_header_normally(env):
    resp = client.post(
        "/convert/file?save_to_downloads=true",
        files={"file": ("a.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert "X-Save-Fallback" not in resp.headers


def test_batch_uses_chosen_folder(env, tmp_path):
    folder = tmp_path / "Notes"
    folder.mkdir()
    _choose(folder)
    files = [
        ("files", ("one.txt", io.BytesIO(b"one"), "text/plain")),
        ("files", ("two.txt", io.BytesIO(b"two"), "text/plain")),
    ]
    data = client.post("/convert/batch?save_to_downloads=true", files=files).json()
    assert sorted(p.name for p in folder.iterdir()) == ["one.md", "two.md"]
    assert all(r["save_fallback"] is None for r in data["results"].values())


def test_batch_fallback_is_reported_per_file(env, tmp_path):
    downloads, _ = env
    folder = tmp_path / "Gone"
    _choose(folder)
    files = [
        ("files", ("one.txt", io.BytesIO(b"one"), "text/plain")),
        ("files", ("two.txt", io.BytesIO(b"two"), "text/plain")),
    ]
    results = client.post("/convert/batch?save_to_downloads=true", files=files).json()["results"]

    assert sorted(p.name for p in downloads.iterdir()) == ["one.md", "two.md"]
    for result in results.values():
        assert result["save_fallback"].endswith("was not found.")
        # One note per file: options copied per item must not share the list.
        assert result["warnings"].count(result["save_fallback"]) == 1
    assert not folder.exists()


def test_no_save_when_not_asked(env, tmp_path):
    downloads, _ = env
    folder = tmp_path / "Notes"
    folder.mkdir()
    _choose(folder)
    resp = _upload(save_to_downloads="false")
    assert resp.json()["saved_to_downloads"] is None
    assert list(folder.iterdir()) == [] and list(downloads.iterdir()) == []


def test_get_settings_reports_the_folder(env, tmp_path):
    downloads, _ = env
    assert client.get("/settings").json()["save_folder"]["path"] == str(downloads)
    _choose(tmp_path / "Gone")
    state = client.get("/settings").json()["save_folder"]
    assert state["custom"] is True and state["problem"]


def test_post_settings_cannot_set_the_folder(env, tmp_path):
    target = tmp_path / "Elsewhere"
    target.mkdir()
    headers = {"X-InkDoc-Token": server_module.SESSION_TOKEN}

    resp = client.post("/settings", json={"save_dir": str(target), "quality_check_enabled": True},
                       headers=headers)

    assert resp.status_code == 200
    assert EngineManager.get_instance().get_settings()["save_dir"] == ""
    assert save_location.configured_save_dir() is None


def test_conversion_request_cannot_choose_the_folder(env, tmp_path):
    downloads, _ = env
    target = tmp_path / "Elsewhere"
    target.mkdir()

    resp = _upload(save_dir=str(target), downloads_dir=str(target), directory=str(target))

    assert Path(resp.json()["saved_to_downloads"]).parent == downloads
    assert list(target.iterdir()) == []
