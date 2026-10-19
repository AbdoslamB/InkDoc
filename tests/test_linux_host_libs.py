"""scripts/linux_host_libs.py: which bundled files the Linux release leaves to the host."""
from __future__ import annotations

from scripts.linux_host_libs import (
    FORBIDDEN_LIBS,
    is_forbidden,
    lib_stem,
    parse_ldconfig,
    parse_ldd,
    select_prunable,
)

LDD = """\
\tlinux-vdso.so.1 (0x00007ffd3b5f2000)
\tlibgtk-3.so.0 => /lib/x86_64-linux-gnu/libgtk-3.so.0 (0x00007f6e3a000000)
\tlibglib-2.0.so.0 => /lib/x86_64-linux-gnu/libglib-2.0.so.0 (0x00007f6e39e00000)
\tlibstdc++.so.6 => /lib/x86_64-linux-gnu/libstdc++.so.6 (0x00007f6e39a00000)
\tlibmissing.so.3 => not found
\t/lib64/ld-linux-x86-64.so.2 (0x00007f6e3c000000)
"""

LDCONFIG = """\
1834 libs found in cache `/etc/ld.so.cache'
\tlibwebkit2gtk-4.1.so.0 (libc6,x86-64) => /lib/x86_64-linux-gnu/libwebkit2gtk-4.1.so.0
\tlibgtk-3.so.0 (libc6) => /lib/i386-linux-gnu/libgtk-3.so.0
\tlibgtk-3.so.0 (libc6,x86-64) => /lib/x86_64-linux-gnu/libgtk-3.so.0
"""


def test_parse_ldd_keeps_resolved_and_missing_sonames():
    found = parse_ldd(LDD)
    assert found["libgtk-3.so.0"] == "/lib/x86_64-linux-gnu/libgtk-3.so.0"
    assert found["libstdc++.so.6"] == "/lib/x86_64-linux-gnu/libstdc++.so.6"
    assert found["libmissing.so.3"] is None
    assert "linux-vdso.so.1" not in found  # no "=>": the kernel's, never a file


def test_parse_ldconfig_picks_this_architecture():
    paths = parse_ldconfig(LDCONFIG)
    assert paths["libgtk-3.so.0"] == "/lib/x86_64-linux-gnu/libgtk-3.so.0"
    assert paths["libwebkit2gtk-4.1.so.0"].endswith("libwebkit2gtk-4.1.so.0")


def test_lib_stem():
    assert lib_stem("libglib-2.0.so.0.8000.0") == "libglib-2.0"
    assert lib_stem("libstdc++.so.6") == "libstdc++"
    assert lib_stem("libpng16-1d1ba958.so.16.58.0") == "libpng16-1d1ba958"


def test_select_prunable_removes_the_host_closure_only():
    entries = [
        "libglib-2.0.so.0", "libgtk-3.so.0", "libstdc++.so.6", "libX11.so.6",
        "libgirepository-2.0.so.0",          # kept: PyGObject's, the hosts may not have it
        "libpython3.14.so.1.0",              # kept: the interpreter
        "libpng16-1d1ba958.so.16.58.0",      # kept: a wheel's vendored copy (hashed name)
        "libssl.so.3",                       # kept: not in the closure
        "gi", "base_library.zip",            # not libraries
    ]
    closure = {"libglib-2.0.so.0", "libgtk-3.so.0", "libstdc++.so.6", "libX11.so.6", "libpng16.so.16",
               "libgirepository-2.0.so.0", "libpython3.14.so.1.0"}
    assert select_prunable(entries, closure) == [
        "libX11.so.6", "libglib-2.0.so.0", "libgtk-3.so.0", "libstdc++.so.6",
    ]


def test_select_prunable_matches_other_versions_of_the_same_library():
    # PyInstaller may bundle a fuller version name than the host's soname.
    assert select_prunable(["libglib-2.0.so.0.8000.0"], {"libglib-2.0.so.0"}) == ["libglib-2.0.so.0.8000.0"]


def test_is_forbidden_by_exact_stem():
    assert is_forbidden("libgtk-3.so.0")
    assert is_forbidden("libicudata.so.70")
    assert is_forbidden("libstdc++.so.6")
    assert not is_forbidden("libfreetype-57f805ba.so.6.20.6")  # Pillow's own copy
    assert not is_forbidden("libgirepository-2.0.so.0")
    assert not is_forbidden("gi_typelibs")  # folders are checked separately
    assert "libwebkit2gtk-4.1" in FORBIDDEN_LIBS
