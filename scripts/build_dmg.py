"""Package the PyInstaller macOS bundle (dist/inkdoc.app) as one read-only disk image.

    python scripts/build_dmg.py dist/inkdoc.app inkdoc-macos.dmg [--format ULFO]
    python scripts/build_dmg.py dist/inkdoc.app --compare-formats

The image holds inkdoc.app and a link to /Applications, so installing is one drag. It is
compressed with lzfse (ULFO, macOS 10.11+; InkDoc needs macOS 12). --compare-formats
builds ULFO, ULMO (lzma, 10.15+) and UDZO (zlib) side by side and reports each one's size
and mount time, for choosing between them; it writes no release artifact.

The app's signature (PyInstaller's ad-hoc one) is checked before packaging and again inside
the mounted image, together with the /Applications link and the bundle's own symlinks
(PyInstaller links Contents/Frameworks and Contents/Resources to each other): a copy that
resolved them would still pass codesign but carry every file twice.

hdiutil on GitHub's macOS runners sometimes fails with "Resource busy" while something
scans the new image; creating and detaching are retried.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

VOLUME_NAME = "InkDoc"
FORMATS = ("ULFO", "ULMO", "UDZO")
DEFAULT_FORMAT = "ULFO"
CREATE_ATTEMPTS = 4
DETACH_ATTEMPTS = 3
RETRY_DELAY_S = 5.0


class DmgError(RuntimeError):
    """A packaging step failed; the message says which."""


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _output(res: subprocess.CompletedProcess[str]) -> str:
    return (res.stderr or res.stdout or "").strip()


def _call(cmd: list[str], what: str) -> None:
    res = _run(cmd)
    if res.returncode != 0:
        raise DmgError(f"{what} failed (exit {res.returncode}): {_output(res)}")


def verify_signature(app: Path) -> None:
    _call(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
          f"codesign --verify of {app}")


def copy_app(src: Path, dest: Path) -> None:
    """ditto keeps the bundle's symlinks, permissions and extended attributes."""
    _call(["ditto", str(src), str(dest)], f"copying {src.name}")


def attach_image(dmg: Path, mountpoint: Path) -> None:
    mountpoint.mkdir(parents=True, exist_ok=True)
    _call(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
           "-mountpoint", str(mountpoint), str(dmg)], f"attaching {dmg.name}")


def detach_image(mountpoint: Path) -> None:
    """Detach, retrying while something still holds the volume; -force as the last try."""
    for _ in range(DETACH_ATTEMPTS):
        if _run(["hdiutil", "detach", str(mountpoint)]).returncode == 0:
            return
        time.sleep(RETRY_DELAY_S)
    _call(["hdiutil", "detach", "-force", str(mountpoint)], f"detaching {mountpoint}")


@contextmanager
def mounted(dmg: Path, mountpoint: Path) -> Iterator[Path]:
    attach_image(dmg, mountpoint)
    try:
        yield mountpoint
    except BaseException:
        # The failure inside is the one to report, not a detach that failed after it.
        try:
            detach_image(mountpoint)
        except DmgError as exc:
            print(f"[!] {exc}", file=sys.stderr)
        raise
    detach_image(mountpoint)


def count_symlinks(root: Path) -> int:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):  # never follows the links it counts
        count += sum(os.path.islink(os.path.join(dirpath, name)) for name in dirnames + filenames)
    return count


def tree_size(root: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(root):
        total += sum(os.lstat(os.path.join(dirpath, name)).st_size for name in filenames)
    return total


def stage(app: Path, root: Path) -> None:
    """The image's contents: the app and a link to /Applications to drag it onto."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    copy_app(app, root / app.name)
    os.symlink("/Applications", root / "Applications")


def create_image(root: Path, dmg: Path, fmt: str) -> None:
    command = ["hdiutil", "create", "-volname", VOLUME_NAME, "-srcfolder", str(root),
               "-fs", "HFS+", "-format", fmt, "-ov"]
    size: list[str] = []
    error = ""
    for attempt in range(1, CREATE_ATTEMPTS + 1):
        res = _run([*command, *size, str(dmg)])
        if res.returncode == 0:
            return
        error = _output(res)
        if "No space left" in error and not size:
            # hdiutil's estimate was too small for this many files: give it room.
            size = ["-size", f"{tree_size(root) * 5 // 4 // 2**20 + 64}m"]
        elif "Resource busy" not in error:
            raise DmgError(f"hdiutil create ({fmt}) failed (exit {res.returncode}): {error}")
        if attempt < CREATE_ATTEMPTS:
            time.sleep(RETRY_DELAY_S)
    raise DmgError(f"hdiutil create ({fmt}) failed {CREATE_ATTEMPTS} times: {error}")


def check_image(dmg: Path, app: Path, work: Path) -> None:
    _call(["hdiutil", "verify", str(dmg)], f"hdiutil verify of {dmg.name}")
    with mounted(dmg, work / "mnt") as volume:
        visible = sorted(p.name for p in volume.iterdir() if not p.name.startswith("."))
        if visible != sorted([app.name, "Applications"]):
            raise DmgError(f"the image holds {visible}, not {app.name} and Applications")
        link = volume / "Applications"
        if not link.is_symlink() or os.readlink(link) != "/Applications":
            raise DmgError("Applications in the image is not a link to /Applications")
        inside = volume / app.name
        expected, found = count_symlinks(app), count_symlinks(inside)
        if found != expected:
            raise DmgError(f"{app.name} in the image has {found} symlinks; the build has {expected}")
        verify_signature(inside)


def check_bundle(app: Path) -> None:
    if not (app / "Contents" / "MacOS" / "inkdoc").is_file():
        raise DmgError(f"{app} is not InkDoc's app bundle (no Contents/MacOS/inkdoc)")


def build(app: Path, dmg: Path, fmt: str = DEFAULT_FORMAT) -> None:
    check_bundle(app)
    verify_signature(app)
    with tempfile.TemporaryDirectory(prefix="inkdoc-dmg-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        stage(app, work / "root")
        create_image(work / "root", dmg, fmt)
        check_image(dmg, app, work)


def compare_formats(app: Path) -> list[tuple[str, int, float, float]]:
    """(format, bytes, seconds to build, seconds to attach and detach) for each format."""
    check_bundle(app)
    rows = []
    with tempfile.TemporaryDirectory(prefix="inkdoc-dmg-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        stage(app, work / "root")
        for fmt in FORMATS:
            dmg = work / f"inkdoc-{fmt}.dmg"
            start = time.monotonic()
            create_image(work / "root", dmg, fmt)
            built = time.monotonic() - start
            start = time.monotonic()
            with mounted(dmg, work / f"mnt-{fmt}"):
                pass
            rows.append((fmt, dmg.stat().st_size, built, time.monotonic() - start))
            dmg.unlink()
    return rows


def format_table(rows: list[tuple[str, int, float, float]]) -> str:
    lines = ["### macOS disk image formats", "",
             "| Format | Bytes | MiB | Build (s) | Attach + detach (s) |",
             "| :--- | ---: | ---: | ---: | ---: |"]
    for fmt, size, built, mount in rows:
        lines.append(f"| {fmt} | {size} | {size / 2**20:.2f} | {built:.1f} | {mount:.1f} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Package inkdoc.app as a read-only disk image.")
    parser.add_argument("app", type=Path, help="The PyInstaller app bundle (dist/inkdoc.app).")
    parser.add_argument("dmg", type=Path, nargs="?", help="The disk image to write.")
    parser.add_argument("--format", choices=FORMATS, default=DEFAULT_FORMAT)
    parser.add_argument("--compare-formats", action="store_true",
                        help="Build every format and report sizes and mount times; writes no image.")
    args = parser.parse_args()
    if sys.platform != "darwin":
        print("build_dmg.py runs on macOS only (hdiutil, ditto, codesign).", file=sys.stderr)
        return 1
    try:
        if args.compare_formats:
            table = format_table(compare_formats(args.app))
            print(table)
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a", encoding="utf-8") as f:
                    f.write(table)
            return 0
        if args.dmg is None:
            parser.error("the disk image path is required unless --compare-formats is given")
        build(args.app, args.dmg, args.format)
    except DmgError as err:
        print(f"[Error] {err}", file=sys.stderr)
        return 1
    print(f"{args.dmg}: {args.dmg.stat().st_size} bytes ({args.format})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
