"""Package the PyInstaller Linux bundle (dist/inkdoc) as one AppImage.

    python scripts/build_appimage.py dist/inkdoc inkdoc-linux-x86_64.AppImage [--tools DIR]

Run after scripts/linux_host_libs.py has pruned the bundle: the AppImage carries InkDoc
and its Python, and the window uses the host's GTK, WebKit2GTK and GLib. The AppDir:

    AppRun                        runs usr/lib/inkdoc/inkdoc; sets no library paths
    inkdoc.desktop, inkdoc.svg    what appimagetool and desktop integration read
    .DirIcon -> inkdoc.svg
    usr/lib/inkdoc/               the PyInstaller onedir, unchanged
    usr/share/icons/hicolor/scalable/apps/inkdoc.svg

appimagetool 1.9.1 and the static type2 runtime 20251108 (no libfuse2 needed on the
user's machine) are downloaded once into --tools, checked against the SHA-256 pinned
below, and run offline (--runtime-file, so appimagetool fetches nothing itself).
appimagetool is an AppImage too; APPIMAGE_EXTRACT_AND_RUN=1 runs it without FUSE.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ICON = REPO_ROOT / "assets" / "logo.svg"

TOOLS = {
    "appimagetool": (
        "https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage",
        "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0",
    ),
    "runtime": (
        "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64",
        "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d",
    ),
}

APP_RUN = """\
#!/bin/sh
# InkDoc's AppImage entry point. Sets no library paths on purpose: GTK, WebKit2GTK and
# GLib come from this system (scripts/linux_host_libs.py, app/core/linux_host.py).
HERE="${APPDIR:-$(dirname "$(readlink -f "$0")")}"
exec "$HERE/usr/lib/inkdoc/inkdoc" "$@"
"""

# No %F: InkDoc's command line takes no file names (app/desktop/runner.py main()).
# StartupWMClass: the class GTK gives the window (measured with xwininfo: "inkdoc" "Inkdoc").
DESKTOP_ENTRY = """\
[Desktop Entry]
Type=Application
Name=InkDoc
GenericName=Markdown converter
Comment=Convert documents, web pages and scans to Markdown
Exec=inkdoc
Icon=inkdoc
Terminal=false
Categories=Office;Utility;
StartupWMClass=Inkdoc
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(name: str, folder: Path) -> Path:
    """The pinned tool `name` in `folder`, downloaded if missing; refuses any other bytes."""
    url, expected = TOOLS[name]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / url.rsplit("/", 1)[1]
    if not path.is_file() or sha256(path) != expected:
        part = path.with_name(path.name + ".part")
        with urllib.request.urlopen(url, timeout=120) as res, open(part, "wb") as out:  # noqa: S310 - pinned https URL
            shutil.copyfileobj(res, out)
        actual = sha256(part)
        if actual != expected:
            part.unlink()
            raise SystemExit(f"{name}: SHA-256 {actual} does not match the pinned {expected} ({url}).")
        os.replace(part, path)
    path.chmod(0o755)
    return path


def make_appdir(bundle: Path, appdir: Path, icon: Path = ICON) -> Path:
    """Lay out `appdir` around the onedir `bundle` (its `inkdoc` executable inside)."""
    if not (bundle / "inkdoc").is_file():
        raise SystemExit(f"{bundle} has no 'inkdoc' executable; pass PyInstaller's dist/inkdoc.")
    if appdir.exists():
        shutil.rmtree(appdir)
    shutil.copytree(bundle, appdir / "usr" / "lib" / "inkdoc", symlinks=True)
    (appdir / "AppRun").write_text(APP_RUN, encoding="utf-8", newline="\n")
    (appdir / "AppRun").chmod(0o755)
    (appdir / "inkdoc.desktop").write_text(DESKTOP_ENTRY, encoding="utf-8", newline="\n")
    shutil.copyfile(icon, appdir / "inkdoc.svg")
    hicolor = appdir / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    hicolor.mkdir(parents=True)
    shutil.copyfile(icon, hicolor / "inkdoc.svg")
    try:
        (appdir / ".DirIcon").symlink_to("inkdoc.svg")
    except OSError:  # no symlinks (Windows test runs): a copy serves the same purpose
        shutil.copyfile(icon, appdir / ".DirIcon")
    return appdir


def build(appdir: Path, output: Path, tools: Path) -> Path:
    appimagetool = fetch("appimagetool", tools)
    runtime = fetch("runtime", tools)
    env = dict(os.environ, APPIMAGE_EXTRACT_AND_RUN="1", ARCH="x86_64")
    output.unlink(missing_ok=True)
    subprocess.run([str(appimagetool), "--no-appstream", "--runtime-file", str(runtime), "--comp", "zstd",
                    str(appdir), str(output)], check=True, env=env)
    output.chmod(0o755)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("bundle", type=Path, help="PyInstaller's dist/inkdoc")
    parser.add_argument("output", type=Path, help="the .AppImage to write")
    parser.add_argument("--tools", type=Path, default=REPO_ROOT / "build" / "appimage-tools",
                        help="where the pinned appimagetool and runtime are kept")
    parser.add_argument("--appdir", type=Path, default=REPO_ROOT / "build" / "InkDoc.AppDir")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        raise SystemExit("build_appimage.py runs on the Linux build host only.")
    make_appdir(args.bundle.resolve(), args.appdir.resolve())
    out = build(args.appdir.resolve(), args.output.resolve(), args.tools.resolve())
    print(f"Wrote {out} ({out.stat().st_size} bytes, SHA-256 {sha256(out)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
