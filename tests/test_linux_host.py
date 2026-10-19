"""app/core/linux_host.py: the frozen Linux app's environment points at the host, not the bundle."""
from __future__ import annotations

from app.core.linux_host import HOST_TYPELIB_DIRS, host_env, use_host_gtk

BUNDLE = "/tmp/.mount_InkDocA1/usr/lib/inkdoc/_internal"
FEDORA = "/usr/lib64/girepository-1.0"
DEBIAN = "/usr/lib/x86_64-linux-gnu/girepository-1.0"


def test_bundle_variables_are_dropped_and_host_typelibs_added():
    env = {
        "GI_TYPELIB_PATH": f"{BUNDLE}/gi_typelibs",
        "GTK_PATH": BUNDLE,
        "GTK_DATA_PREFIX": BUNDLE,
        "GIO_MODULE_DIR": f"{BUNDLE}/gio_modules",
        "GDK_PIXBUF_MODULE_FILE": f"{BUNDLE}/lib/gdk-pixbuf/loaders.cache",
        "XDG_DATA_DIRS": f"{BUNDLE}/share:/usr/local/share:/usr/share",
    }
    use_host_gtk(env, BUNDLE, isdir=lambda d: d == FEDORA)
    assert env == {"GI_TYPELIB_PATH": FEDORA, "XDG_DATA_DIRS": "/usr/local/share:/usr/share"}


def test_the_users_own_settings_are_kept():
    env = {"GI_TYPELIB_PATH": "/opt/gnome/typelibs", "GTK_PATH": "/opt/gtk-modules", "XDG_DATA_DIRS": "/usr/share"}
    use_host_gtk(env, BUNDLE, isdir=lambda d: d == DEBIAN)
    assert env["GI_TYPELIB_PATH"] == f"/opt/gnome/typelibs:{DEBIAN}"
    assert env["GTK_PATH"] == "/opt/gtk-modules"
    assert env["XDG_DATA_DIRS"] == "/usr/share"


def test_no_typelib_folder_at_all_unsets_the_variable():
    env = {"GI_TYPELIB_PATH": f"{BUNDLE}/gi_typelibs", "XDG_DATA_DIRS": f"{BUNDLE}/share"}
    use_host_gtk(env, BUNDLE, isdir=lambda d: False)
    assert env == {}


def test_a_folder_named_like_the_bundle_is_not_inside_it():
    env = {"GTK_PATH": BUNDLE + "-other"}
    use_host_gtk(env, BUNDLE, isdir=lambda d: False)
    assert env == {"GTK_PATH": BUNDLE + "-other"}


def test_path_lists_keep_the_users_entries():
    env = {"GTK_PATH": f"/home/u/gtk:{BUNDLE}", "GIO_MODULE_DIR": f"{BUNDLE}/gio_modules:/usr/lib/gio"}
    use_host_gtk(env, BUNDLE, isdir=lambda d: False)
    assert env == {"GTK_PATH": "/home/u/gtk", "GIO_MODULE_DIR": "/usr/lib/gio"}


def test_host_typelib_dirs_cover_debian_and_fedora():
    assert DEBIAN in HOST_TYPELIB_DIRS and FEDORA in HOST_TYPELIB_DIRS


def test_host_env_restores_ld_library_path():
    env = host_env({"LD_LIBRARY_PATH": f"{BUNDLE}:/opt/lib", "LD_LIBRARY_PATH_ORIG": "/opt/lib", "HOME": "/home/u"})
    assert env == {"LD_LIBRARY_PATH": "/opt/lib", "HOME": "/home/u"}


def test_host_env_removes_ld_library_path_the_user_never_set():
    env = host_env({"LD_LIBRARY_PATH": BUNDLE, "HOME": "/home/u"})
    assert env == {"HOME": "/home/u"}
    assert host_env({"LD_LIBRARY_PATH": BUNDLE, "LD_LIBRARY_PATH_ORIG": ""}) == {}
