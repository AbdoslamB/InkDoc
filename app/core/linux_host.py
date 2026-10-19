"""The frozen Linux app shows its window with the host's GTK, WebKit2GTK and GLib.

The release bundle carries none of them (scripts/linux_host_libs.py removes them), so
the process environment must point at the host's copies, not at the bundle:

- use_host_gtk() runs once at start-up, before gi is imported. It drops variables
  that PyInstaller's runtime hooks point into the bundle, and adds the host's typelib
  folders to GI_TYPELIB_PATH: the bundled libgirepository-2.0 only looks where the
  build machine (Ubuntu) keeps typelibs, while Fedora keeps them in /usr/lib64.
- host_env() is the environment for programs InkDoc starts on the user's behalf
  (xdg-open and the browser or file manager it starts). PyInstaller puts the bundle
  on LD_LIBRARY_PATH; inherited, it would load InkDoc's libraries into those programs.

Standard library only, imported by main.py before anything else, so it stays cheap.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, MutableMapping, Sequence

# Where distributions keep typelibs: Debian/Ubuntu, Fedora/openSUSE, Arch, /usr/local.
HOST_TYPELIB_DIRS = (
    "/usr/lib/x86_64-linux-gnu/girepository-1.0",
    "/usr/lib64/girepository-1.0",
    "/usr/lib/girepository-1.0",
    "/usr/local/lib/girepository-1.0",
)
# Set by PyInstaller's GTK runtime hooks to folders inside the bundle.
# Linux path lists, whatever OS the unit tests run on.
_SEP = ":"
_BUNDLE_VARS = ("GTK_DATA_PREFIX", "GTK_EXE_PREFIX", "GTK_PATH", "PANGO_LIBDIR", "PANGO_SYSCONFDIR",
                "GDK_PIXBUF_MODULE_FILE", "GIO_MODULE_DIR")


def is_frozen_linux() -> bool:
    return bool(getattr(sys, "frozen", False)) and sys.platform.startswith("linux")


def _inside(path: str, bundle: str) -> bool:
    bundle = bundle.rstrip("/")
    return bool(bundle) and (path == bundle or path.startswith(bundle + "/"))


def _without_bundle(value: str, bundle: str) -> list[str]:
    return [p for p in value.split(_SEP) if p and not _inside(p, bundle)]


def use_host_gtk(environ: MutableMapping[str, str], bundle: str,
                 isdir: Callable[[str], bool] = os.path.isdir) -> None:
    """Point GTK/GI at the host. `bundle` is sys._MEIPASS. Leaves the user's own settings alone."""

    def keep_outside(name: str, extra: Sequence[str] = ()) -> None:
        # These can be path lists: drop the bundle's entries only.
        if name not in environ and not extra:
            return
        paths = _without_bundle(environ.get(name, ""), bundle)
        paths += [d for d in extra if d not in paths]
        if paths:
            environ[name] = _SEP.join(paths)
        else:
            environ.pop(name, None)

    for name in _BUNDLE_VARS:
        keep_outside(name)
    keep_outside("GI_TYPELIB_PATH", [d for d in HOST_TYPELIB_DIRS if isdir(d)])
    keep_outside("XDG_DATA_DIRS")


def host_env(environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """`environ` with LD_LIBRARY_PATH as it was before PyInstaller's launcher changed it.

    Only meaningful under PyInstaller's launcher, which saves a user-set value in
    LD_LIBRARY_PATH_ORIG and sets none when there was none; callers check
    is_frozen_linux() first.
    """
    env = dict(os.environ if environ is None else environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def open_on_host(args: Sequence[str]) -> None:
    """Start a host program (xdg-open ...) detached, with the host's environment."""
    subprocess.Popen(list(args), env=host_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
