"""scripts/build_appimage.py (the AppDir) and the AppImage parts of scripts/smoke_test_artifact.py."""
from __future__ import annotations

import configparser
import os
import re
import struct
import sys

import pytest

from scripts.build_appimage import APP_RUN, TOOLS, make_appdir
from scripts.smoke_test_artifact import GUI_MIN_COLOURS, launch_env, xwd_colour_count


def _fake_bundle(tmp_path):
    bundle = tmp_path / "dist" / "inkdoc"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "inkdoc").write_bytes(b"\x7fELF")
    (bundle / "_internal" / "base_library.zip").write_bytes(b"zip")
    return bundle


def test_appdir_layout(tmp_path):
    appdir = make_appdir(_fake_bundle(tmp_path), tmp_path / "InkDoc.AppDir")
    for rel in ("AppRun", "inkdoc.desktop", "inkdoc.svg", ".DirIcon", "usr/lib/inkdoc/inkdoc",
                "usr/lib/inkdoc/_internal/base_library.zip",
                "usr/share/icons/hicolor/scalable/apps/inkdoc.svg"):
        assert (appdir / rel).exists(), rel
    if sys.platform != "win32":
        assert os.access(appdir / "AppRun", os.X_OK)


def test_desktop_entry(tmp_path):
    appdir = make_appdir(_fake_bundle(tmp_path), tmp_path / "InkDoc.AppDir")
    entry = configparser.ConfigParser(interpolation=None)
    entry.optionxform = str
    entry.read(appdir / "inkdoc.desktop", encoding="utf-8")
    desktop = entry["Desktop Entry"]
    assert desktop["Type"] == "Application"
    assert desktop["Exec"] == "inkdoc"  # no %F: the command line takes no file names
    assert desktop["Icon"] == "inkdoc"  # matches inkdoc.svg at the AppDir root
    assert desktop["StartupWMClass"] == "Inkdoc"


def test_app_run_sets_no_library_path():
    assert "LD_LIBRARY_PATH" not in APP_RUN.replace("Sets no library paths", "")
    assert 'exec "$HERE/usr/lib/inkdoc/inkdoc" "$@"' in APP_RUN
    assert "\r" not in APP_RUN


def test_rebuilding_replaces_the_appdir(tmp_path):
    bundle = _fake_bundle(tmp_path)
    appdir = make_appdir(bundle, tmp_path / "InkDoc.AppDir")
    (appdir / "stale.txt").write_text("old")
    make_appdir(bundle, appdir)
    assert not (appdir / "stale.txt").exists()


def test_a_folder_without_the_executable_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit):
        make_appdir(tmp_path / "empty", tmp_path / "InkDoc.AppDir")


def test_tools_are_pinned_https_downloads():
    for url, digest in TOOLS.values():
        assert url.startswith("https://github.com/AppImage/")
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_launch_env_extracts_unless_fuse_is_asked_for():
    assert launch_env("appimage")["APPIMAGE_EXTRACT_AND_RUN"] == "1"
    assert "APPIMAGE_EXTRACT_AND_RUN" not in launch_env("appimage", use_fuse=True) or \
        os.environ.get("APPIMAGE_EXTRACT_AND_RUN") == "1"
    assert launch_env("zip").get("APPIMAGE_EXTRACT_AND_RUN") == os.environ.get("APPIMAGE_EXTRACT_AND_RUN")


def _xwd(pixels: list[bytes], width: int, height: int) -> bytes:
    """A minimal 32 bpp ZPixmap XWD dump with no colormap."""
    name = b"test\x00"
    header_size = 100 + len(name)
    fields = [header_size, 7, 2, 24, width, height, 0, 0, 32, 0, 32, 32, width * 4, 4,
              0xFF0000, 0xFF00, 0xFF, 8, 0, 0, width, height, 0, 0, 0]
    return struct.pack(">25I", *fields) + name + b"".join(pixels)


def test_xwd_colour_count_tells_a_drawn_window_from_an_empty_screen():
    width = height = 50
    empty = _xwd([b"\x00\x00\x00\x00"] * (width * height), width, height)
    assert xwd_colour_count(empty) == 1
    drawn = _xwd([struct.pack(">I", (i * 2654435761) & 0xFFFFFF) for i in range(width * height)], width, height)
    assert xwd_colour_count(drawn) >= GUI_MIN_COLOURS


def test_xwd_colour_count_rejects_other_formats():
    with pytest.raises(ValueError):
        xwd_colour_count(b"P6 not an xwd")
