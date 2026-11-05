"""scripts/build_dmg.py and the disk image parts of scripts/smoke_test_artifact.py.

hdiutil, ditto and codesign exist on macOS only, so they are replaced by a fake that
records each command and acts it out on the file system: these tests run everywhere.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import build_dmg, smoke_test_artifact


def _can_symlink(tmp_path: Path) -> bool:
    try:
        os.symlink(tmp_path, tmp_path / "probe")
    except (OSError, NotImplementedError):
        return False
    return True


class FakeTools:
    """Stands in for build_dmg._run. `fail` maps a command's first two words to the
    (exit code, stderr) answers to give, one per call, before it starts succeeding."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.fail: dict[tuple[str, str], list[tuple[int, str]]] = {}
        # What `hdiutil attach` shows at the mount point. With `snapshot` set, `hdiutil
        # create` copies its -srcfolder there (changed by `alter`) and attaching shows that.
        self.image_contents: Path | None = None
        self.snapshot: Path | None = None
        self.alter = None

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        answers = self.fail.get(tuple(cmd[:2]))
        if answers:
            code, err = answers.pop(0)
            return subprocess.CompletedProcess(cmd, code, "", err)
        if cmd[0] == "ditto":
            shutil.copytree(cmd[1], cmd[2], symlinks=True)
        elif cmd[:2] == ["hdiutil", "attach"] and self.image_contents is not None:
            shutil.copytree(self.image_contents, cmd[cmd.index("-mountpoint") + 1],
                            symlinks=True, dirs_exist_ok=True)
        elif cmd[:2] == ["hdiutil", "detach"]:
            mountpoint = Path(cmd[-1])
            for child in mountpoint.iterdir() if mountpoint.is_dir() else ():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        elif cmd[:2] == ["hdiutil", "create"]:
            Path(cmd[-1]).write_bytes(b"image")
            if self.snapshot is not None:
                shutil.copytree(cmd[cmd.index("-srcfolder") + 1], self.snapshot, symlinks=True)
                if self.alter:
                    self.alter(self.snapshot)
                self.image_contents = self.snapshot
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def commands(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[:len(prefix)]) == prefix]


@pytest.fixture
def tools(monkeypatch):
    fake = FakeTools()
    monkeypatch.setattr(build_dmg, "_run", fake)
    monkeypatch.setattr(build_dmg, "RETRY_DELAY_S", 0)
    return fake


def _fake_app(parent: Path, links: bool = False) -> Path:
    app = parent / "inkdoc.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir()
    (app / "Contents" / "MacOS" / "inkdoc").write_bytes(b"\xcf\xfa\xed\xfe")
    (app / "Contents" / "Resources" / "base_library.zip").write_bytes(b"zip")
    if links:
        os.symlink("../Resources/base_library.zip", app / "Contents" / "MacOS" / "base_library.zip")
    return app


def test_create_uses_the_chosen_format_and_retries_while_busy(tools, tmp_path):
    tools.fail[("hdiutil", "create")] = [(1, "hdiutil: create failed - Resource busy")] * 2
    build_dmg.create_image(tmp_path, tmp_path / "out.dmg", "ULFO")
    creates = tools.commands("hdiutil", "create")
    assert len(creates) == 3
    cmd = creates[-1]
    assert cmd[cmd.index("-format") + 1] == "ULFO"
    assert cmd[cmd.index("-fs") + 1] == "HFS+"
    assert cmd[cmd.index("-volname") + 1] == "InkDoc"
    assert cmd[cmd.index("-srcfolder") + 1] == str(tmp_path)
    assert "-size" not in cmd


def test_create_gives_up_after_its_attempts(tools, tmp_path):
    tools.fail[("hdiutil", "create")] = [(1, "Resource busy")] * build_dmg.CREATE_ATTEMPTS
    with pytest.raises(build_dmg.DmgError, match="4 times"):
        build_dmg.create_image(tmp_path, tmp_path / "out.dmg", "ULFO")


def test_create_stops_at_once_on_any_other_error(tools, tmp_path):
    tools.fail[("hdiutil", "create")] = [(1, "hdiutil: create failed - Permission denied")]
    with pytest.raises(build_dmg.DmgError, match="Permission denied"):
        build_dmg.create_image(tmp_path, tmp_path / "out.dmg", "ULFO")
    assert len(tools.commands("hdiutil", "create")) == 1


def test_create_gives_room_when_hdiutil_underestimates(tools, tmp_path):
    (tmp_path / "payload").write_bytes(b"x" * 1000)
    tools.fail[("hdiutil", "create")] = [(1, "No space left on device")]
    build_dmg.create_image(tmp_path, tmp_path / "out.dmg", "ULFO")
    first, second = tools.commands("hdiutil", "create")
    assert "-size" not in first
    assert second[second.index("-size") + 1] == "64m"  # 1000 bytes * 1.25, in MiB, + 64


def test_mounted_detaches_when_the_body_fails(tools, tmp_path):
    with pytest.raises(ValueError), build_dmg.mounted(tmp_path / "a.dmg", tmp_path / "mnt"):
        raise ValueError("copy failed")
    attach, = tools.commands("hdiutil", "attach")
    assert {"-nobrowse", "-readonly", "-noautoopen"} <= set(attach)
    assert tools.commands("hdiutil", "detach") == [["hdiutil", "detach", str(tmp_path / "mnt")]]


def test_detach_forces_after_its_attempts(tools, tmp_path):
    tools.fail[("hdiutil", "detach")] = [(16, "Resource busy")] * build_dmg.DETACH_ATTEMPTS
    build_dmg.detach_image(tmp_path)
    detaches = tools.commands("hdiutil", "detach")
    assert len(detaches) == build_dmg.DETACH_ATTEMPTS + 1
    assert detaches[-1] == ["hdiutil", "detach", "-force", str(tmp_path)]


def test_a_folder_that_is_not_the_bundle_is_refused(tools, tmp_path):
    with pytest.raises(build_dmg.DmgError, match="Contents/MacOS/inkdoc"):
        build_dmg.build(tmp_path, tmp_path / "out.dmg")
    assert tools.calls == []


def test_a_bad_signature_stops_the_build_before_packaging(tools, tmp_path):
    app = _fake_app(tmp_path)
    tools.fail[("codesign", "--verify")] = [(1, "a sealed resource is missing or invalid")]
    with pytest.raises(build_dmg.DmgError, match="sealed resource"):
        build_dmg.build(app, tmp_path / "out.dmg")
    assert tools.commands("hdiutil") == []


def _build(tools, tmp_path, alter=None):
    """Build from a fake app; the mounted image shows the staged folder, altered first."""
    tools.snapshot, tools.alter = tmp_path / "image", alter
    build_dmg.build(_fake_app(tmp_path / "dist", links=True), tmp_path / "inkdoc-macos.dmg")


@pytest.fixture
def symlinks(tmp_path):
    # Windows needs Developer Mode for symlinks, and the bundle's links are POSIX ones.
    if sys.platform == "win32" or not _can_symlink(tmp_path):
        pytest.skip("needs POSIX symlinks")


def test_a_good_build_is_checked_inside_the_image(tools, tmp_path, symlinks):
    _build(tools, tmp_path)
    verifies = tools.commands("codesign", "--verify")
    assert len(verifies) == 2  # the build, then the copy inside the mounted image
    assert verifies[0][-1].endswith(os.path.join("dist", "inkdoc.app"))
    assert verifies[1][-1].endswith(os.path.join("mnt", "inkdoc.app"))
    assert tools.commands("hdiutil", "verify")
    assert len(tools.commands("hdiutil", "detach")) == 1


@pytest.mark.parametrize("alter, message", [
    (lambda image: (image / "Applications").unlink(), "holds"),
    (lambda image: ((image / "Applications").unlink(),
                    os.symlink("/tmp", image / "Applications")), "not a link to /Applications"),
    (lambda image: ((image / "inkdoc.app/Contents/MacOS/base_library.zip").unlink(),
                    (image / "inkdoc.app/Contents/MacOS/base_library.zip").write_bytes(b"zip")),
     "symlinks"),
    (lambda image: (image / "notes.txt").write_text("stray"), "holds"),
])
def test_a_wrong_image_is_refused_and_still_detached(tools, tmp_path, symlinks, alter, message):
    with pytest.raises(build_dmg.DmgError, match=message):
        _build(tools, tmp_path, alter)
    assert len(tools.commands("hdiutil", "detach")) == 1


def test_stage_holds_the_app_and_a_link_to_applications(tools, tmp_path, symlinks):
    app = _fake_app(tmp_path / "dist", links=True)
    root = tmp_path / "root"
    (root / "stale").mkdir(parents=True)
    build_dmg.stage(app, root)
    assert sorted(p.name for p in root.iterdir()) == ["Applications", "inkdoc.app"]
    assert os.readlink(root / "Applications") == "/Applications"
    assert build_dmg.count_symlinks(root / "inkdoc.app") == 1


# ─── smoke_test_artifact.py --type dmg ─────────────────────────────────────────

def test_the_smoke_test_copies_the_app_out_and_detaches_first(tools, tmp_path, monkeypatch):
    image = tmp_path / "image"
    _fake_app(image)
    tools.image_contents = image
    monkeypatch.setattr(sys, "platform", "darwin")
    work = tmp_path / "work"
    exe = smoke_test_artifact.prepare_target_executable(tmp_path / "inkdoc-macos.dmg", "dmg", work)
    assert exe == work / "app" / "inkdoc.app" / "Contents" / "MacOS" / "inkdoc"
    order = [c[:2] for c in tools.calls]
    assert order.index(["hdiutil", "detach"]) < order.index(["codesign", "--verify"])
    assert list((work / "mnt").iterdir()) == []  # nothing runs from the mounted image


def test_the_smoke_test_detaches_when_the_copy_fails(tools, tmp_path, monkeypatch):
    tools.image_contents = tmp_path / "empty"
    tools.image_contents.mkdir()
    tools.fail[("ditto", str(tmp_path / "work" / "mnt" / "inkdoc.app"))] = [(1, "No such file")]
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(build_dmg.DmgError, match="copying"):
        smoke_test_artifact.prepare_target_executable(tmp_path / "x.dmg", "dmg", tmp_path / "work")
    assert len(tools.commands("hdiutil", "detach")) == 1
    assert tools.commands("codesign") == []


def test_the_smoke_test_refuses_a_disk_image_off_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="only supported on macOS"):
        smoke_test_artifact.prepare_target_executable(tmp_path / "x.dmg", "dmg", tmp_path)
