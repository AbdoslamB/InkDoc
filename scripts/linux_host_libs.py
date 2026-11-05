"""Make the Linux bundle use the host's GTK, WebKit2GTK and GLib instead of its own copies.

InkDoc's Linux window is the host's WebKit2GTK, which the user installs from their
distribution. Every library that WebKit loads must then come from the host too: a
bundled GLib, GTK or libstdc++ from the build machine gets loaded first (PyInstaller
puts _internal/ on LD_LIBRARY_PATH) and the host's WebKit fails against it
("undefined symbol", "CXXABI_1.3.15 not found"). That is how 1.2.0's Linux zip broke
on every distribution newer than the one it was built on.

    python scripts/linux_host_libs.py prune dist/inkdoc/_internal
    python scripts/linux_host_libs.py check dist/inkdoc/_internal

`prune` removes from the top of _internal/ every library that is part of the host's
WebKit2GTK / GTK 3 dependency closure (asked of the build host's own ldd), and the GTK
data PyInstaller may have collected. Anything in that closure is on every machine that
can show InkDoc's window at all, so the list keeps itself up to date. Wheel-vendored
copies (numpy.libs/, pillow.libs/, hashed names such as libpng16-1d1ba958.so.16) are
never touched, and neither is libpython.

One GLib library stays bundled: libgirepository-2.0, which PyGObject needs and which
Ubuntu 24.04 and Debian 13 don't install alongside WebKit2GTK (their own python3-gi
still uses girepository-1.0). It only uses GLib's stable API, so the build host's copy
works with a newer host GLib. It searches for typelibs where the build host keeps them,
so app/core/linux_host.py points GI_TYPELIB_PATH at the host's folders.

`check` fails when a GTK-stack library or GTK data is still bundled, or when any
bundled ELF file has a dependency that resolves nowhere ("not found" from ldd with
LD_LIBRARY_PATH=_internal). Release builds run it after `prune`.

Linux only; the parsing and selection are plain functions, unit-tested on any OS.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

# The libraries the host must provide; their dependency closure is what `prune` removes.
HOST_ROOTS = ("libwebkit2gtk-4.1.so.0", "libgtk-3.so.0")

# Never removed, even if a root happens to depend on them (see the module docstring).
KEEP = re.compile(r"^(libpython\d|ld-linux|libgirepository-2\.0\.so)")

# Must not be bundled after `prune`: the GTK stack, its C++ runtime and its data.
FORBIDDEN_LIBS = (
    "libgtk-3", "libgdk-3", "libgdk_pixbuf-2.0", "libglib-2.0", "libgobject-2.0", "libgio-2.0",
    "libgmodule-2.0", "libgirepository-1.0", "libwebkit2gtk-4.1",
    "libwebkit2gtk-4.0", "libjavascriptcoregtk-4.1", "libjavascriptcoregtk-4.0", "libsoup-3.0",
    "libsoup-2.4", "libpango-1.0", "libpangocairo-1.0", "libpangoft2-1.0", "libcairo",
    "libcairo-gobject", "libharfbuzz", "libatk-1.0", "libatk-bridge-2.0", "libepoxy", "librsvg-2",
    "libicuuc", "libicudata", "libicui18n", "libstdc++", "libgcc_s", "libX11", "libxcb",
    "libwayland-client", "libfontconfig", "libfreetype",
)
FORBIDDEN_DIRS = ("gi_typelibs", "gio_modules", "lib/gdk-pixbuf", "share/icons", "share/themes",
                  "share/glib-2.0", "etc/fonts", "etc/gtk-3.0")

_LDD_LINE = re.compile(r"^\s*(\S+)\s*=>\s*(not found|\S+)")
_LDCONFIG_LINE = re.compile(r"^\s*(\S+)\s+\((?P<flags>[^)]*)\)\s+=>\s+(\S+)")


def parse_ldd(output: str) -> dict[str, str | None]:
    """soname -> resolved path (None when ldd says "not found")."""
    found: dict[str, str | None] = {}
    for line in output.splitlines():
        m = _LDD_LINE.match(line)
        if m:
            found[m.group(1)] = None if m.group(2) == "not found" else m.group(2)
    return found


def parse_ldconfig(output: str, arch: str = "x86-64") -> dict[str, str]:
    """soname -> path for this architecture, from `ldconfig -p` (first entry wins)."""
    paths: dict[str, str] = {}
    for line in output.splitlines():
        m = _LDCONFIG_LINE.match(line)
        if m and arch in m.group("flags") and m.group(1) not in paths:
            paths[m.group(1)] = m.group(3)
    return paths


def lib_stem(name: str) -> str:
    """'libfoo-1.0.so.0.7' -> 'libfoo-1.0': the part before '.so'."""
    return name.split(".so", 1)[0]


def is_forbidden(name: str) -> bool:
    """A bundled file name that is one of FORBIDDEN_LIBS (exact stem, so hashed copies pass)."""
    return ".so" in name and lib_stem(name) in FORBIDDEN_LIBS


def select_prunable(entries: Iterable[str], closure: set[str]) -> list[str]:
    """Top-level _internal/ entries to remove: exact sonames of the host closure, by stem.

    By stem, because PyInstaller may bundle libfoo.so.1.2.3 or a symlink of another
    version than the host's soname; a hashed wheel copy (libfoo-1a2b3c4d.so.1) has a
    different stem and stays.
    """
    stems = {lib_stem(s) for s in closure}
    return sorted(e for e in entries if ".so" in e and not KEEP.match(e) and lib_stem(e) in stems)


def host_closure(roots: Iterable[str] = HOST_ROOTS) -> set[str]:
    """The sonames the host's roots load, the roots included (asks ldconfig and ldd)."""
    available = parse_ldconfig(subprocess.run(["ldconfig", "-p"], capture_output=True, text=True,
                                              check=True).stdout)
    closure: set[str] = set()
    for root in roots:
        path = available.get(root)
        if path is None:
            raise SystemExit(f"{root} is not installed on this build host; install its -dev or runtime package.")
        closure.add(root)
        closure.update(parse_ldd(subprocess.run(["ldd", path], capture_output=True, text=True, check=True).stdout))
    return closure


def prune(internal: Path) -> list[str]:
    removed = []
    for name in select_prunable(os.listdir(internal), host_closure()):
        (internal / name).unlink()
        removed.append(name)
    for rel in FORBIDDEN_DIRS:
        if (internal / rel).is_dir():
            shutil.rmtree(internal / rel)
            removed.append(rel + "/")
    return removed


def _elf_files(internal: Path) -> list[Path]:
    files = []
    for path in internal.rglob("*"):
        if path.is_file() and not path.is_symlink() and (".so" in path.name or os.access(path, os.X_OK)):
            with open(path, "rb") as fh:
                if fh.read(4) == b"\x7fELF":
                    files.append(path)
    return files


def check(internal: Path) -> list[str]:
    problems = [f"bundled: {name}" for name in sorted(os.listdir(internal)) if is_forbidden(name)]
    problems += [f"bundled: {rel}/" for rel in FORBIDDEN_DIRS if (internal / rel).exists()]
    env = dict(os.environ, LD_LIBRARY_PATH=str(internal))
    for path in _elf_files(internal):
        res = subprocess.run(["ldd", str(path)], capture_output=True, text=True, env=env)
        for soname, resolved in parse_ldd(res.stdout).items():
            if resolved is None:
                problems.append(f"unresolved: {path.relative_to(internal)} needs {soname}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("prune", "check"))
    parser.add_argument("internal", type=Path, help="the bundle's _internal/ folder")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        raise SystemExit("linux_host_libs.py runs on the Linux build host only.")
    if not args.internal.is_dir():
        raise SystemExit(f"Not a folder: {args.internal}")
    if args.command == "prune":
        removed = prune(args.internal)
        print(f"Removed {len(removed)} host-provided entries from {args.internal}:")
        for name in removed:
            print(f"  {name}")
        return 0
    problems = check(args.internal)
    for problem in problems:
        print(f"  {problem}")
    print("Host-library check: " + ("FAIL" if problems else "PASS"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
