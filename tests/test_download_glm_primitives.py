"""Download primitives added for GLM-OCR: selective extraction, rate-limit fail-fast, suffix hops."""
from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.download_utils import (  # noqa: E402
    RateLimitedError,
    SecurityError,
    StrictRedirectHandler,
    extract_selected_files,
    stream_download,
)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_tar(path: Path, members: list[tuple[str, bytes | str, str]]) -> Path:
    """members: (name, data-or-linktarget, kind) with kind in file|sym|hard|dir."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                info.mode = 0o755
                tf.addfile(info, io.BytesIO(data))
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = data
                tf.addfile(info)
            elif kind == "hard":
                info.type = tarfile.LNKTYPE
                info.linkname = data
                tf.addfile(info)
            else:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
    return path


def test_soname_symlinks_become_regular_files(tmp_path):
    real = b"\x7fELF real library"
    archive = make_tar(tmp_path / "rt.tar.gz", [
        ("llama-b1", b"", "dir"),
        ("llama-b1/llama-server", b"server", "file"),
        ("llama-b1/libllama.so.0.5.0", real, "file"),
        ("llama-b1/libllama.so.0", "libllama.so.0.5.0", "sym"),
        ("llama-b1/libllama.so", "libllama.so.0", "sym"),
        ("llama-b1/llama-cli", b"never extracted", "file"),
    ])
    out = tmp_path / "out"
    written = extract_selected_files(archive, out, ["llama-server", "libllama.so.0"])
    assert written == {"llama-server": sha(b"server"), "libllama.so.0": sha(real)}
    assert (out / "libllama.so.0").read_bytes() == real
    assert not (out / "libllama.so.0").is_symlink()
    assert sorted(p.name for p in out.iterdir()) == ["libllama.so.0", "llama-server"]


def test_hard_links_are_followed(tmp_path):
    archive = make_tar(tmp_path / "rt.tar.gz", [
        ("d/a.bin", b"payload", "file"),
        ("d/b.bin", "d/a.bin", "hard"),
    ])
    written = extract_selected_files(archive, tmp_path / "o", ["b.bin"])
    assert written["b.bin"] == sha(b"payload")


@pytest.mark.parametrize("target", ["/etc/passwd", "../../outside", "../x"])
def test_links_leaving_the_archive_are_refused(tmp_path, target):
    archive = make_tar(tmp_path / "rt.tar.gz", [("d/evil", target, "sym"), ("d/ok", b"x", "file")])
    with pytest.raises(SecurityError):
        extract_selected_files(archive, tmp_path / "o", ["evil"])


def test_link_loops_are_refused(tmp_path):
    archive = make_tar(tmp_path / "rt.tar.gz", [("d/a", "b", "sym"), ("d/b", "a", "sym"), ("d/c", b"x", "file")])
    with pytest.raises(SecurityError, match="Too many links"):
        extract_selected_files(archive, tmp_path / "o", ["a"])


def test_missing_wanted_file_is_an_error(tmp_path):
    archive = tmp_path / "rt.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("llama-server.exe", b"x")
    with pytest.raises(SecurityError, match="missing required file"):
        extract_selected_files(archive, tmp_path / "o", ["llama-server.exe", "ggml.dll"])


def test_flat_zip_extracts_only_what_is_listed(tmp_path):
    archive = tmp_path / "rt.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name in ("llama-server.exe", "ggml.dll", "llama-bench.exe", "rpc-server.exe"):
            zf.writestr(name, name.encode())
    written = extract_selected_files(archive, tmp_path / "o", ["llama-server.exe", "ggml.dll"])
    assert set(written) == {"llama-server.exe", "ggml.dll"}
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == ["ggml.dll", "llama-server.exe"]


@pytest.mark.parametrize("bad", ["../escape.dll", "/abs.dll", "C:/x.dll", "CON.dll"])
def test_unsafe_wanted_names_are_refused(tmp_path, bad):
    archive = tmp_path / "rt.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.dll", b"x")
    with pytest.raises(SecurityError):
        extract_selected_files(archive, tmp_path / "o", [bad])


def test_oversized_entry_is_refused(tmp_path):
    archive = tmp_path / "rt.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("big.bin", b"x" * 2048)
    with pytest.raises(SecurityError, match="limit"):
        extract_selected_files(archive, tmp_path / "o", ["big.bin"], max_file_bytes=1024)


# ─── stream_download policy ──────────────────────────────────────────────────

class _Opener:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        raise self.error


@pytest.mark.parametrize("code", [429, 503])
def test_rate_limit_fails_fast_when_another_source_exists(tmp_path, code):
    err = urllib.error.HTTPError("https://huggingface.co/x", code, "busy", {"Retry-After": "30"}, None)
    opener = _Opener(err)
    with patch("urllib.request.build_opener", return_value=opener), patch("time.sleep") as sleep,             pytest.raises(RateLimitedError):
        stream_download("https://huggingface.co/x", tmp_path / "f", expected_size=10,
                        allowed_hosts=frozenset({"huggingface.co"}), fail_fast_on_rate_limit=True)
    assert opener.calls == 1 and not sleep.called, "no retry storm, straight to the next source"


def test_redirect_hops_honour_the_suffix_rule():
    handler = StrictRedirectHandler(frozenset({"huggingface.co"}), (".hf.co",))
    import urllib.request

    req = urllib.request.Request("https://huggingface.co/x")
    handler.redirect_request(req, None, 302, "Found", {}, "https://us.aws.cdn.hf.co/blob")
    with pytest.raises(SecurityError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://evilhf.co/blob")
    with pytest.raises(SecurityError):
        StrictRedirectHandler().redirect_request(req, None, 302, "Found", {}, "https://us.aws.cdn.hf.co/blob")
