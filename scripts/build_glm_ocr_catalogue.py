#!/usr/bin/env python3
"""Generate the GLM-OCR catalogue from pinned upstream versions.

Usage:
    python scripts/build_glm_ocr_catalogue.py \
        --llama-build b11307 \
        --model-revision 65a42de1148dbed2297e922b5dbc7d9b70c36578 \
        --mirror-tag glm-ocr-v1 \
        --catalogue-version 1 \
        --out app/core/glm_ocr_catalogue.json

What it does, so nobody fills hashes in by hand:

1. Model files: size and sha256 come from the Hugging Face tree API (the LFS
   object id *is* the sha256), at the pinned revision. Nothing is downloaded;
   glm-ocr-track.yml downloads and re-hashes them before mirroring.
2. Runtime archives: each pinned llama.cpp asset is downloaded (12-33 MB),
   hashed, and compared with the sha256 digest GitHub publishes for it.
3. sha256_files: the exact set of files llama-server needs. The entry's import
   table is read (PE, ELF or Mach-O, parsed here with no dependency), followed
   transitively through the libraries in the archive, plus the ggml backends
   llama.cpp loads at runtime from its own folder (ggml-cpu-*, ggml-vulkan,
   ggml-metal, ggml-blas). Symlinked SONAMEs are recorded under the name the
   loader asks for, with the hash of the file they point to; the installer
   writes a regular file under that name. Everything else in the archive (other
   tools, RPC) is never extracted, so it can never run.
4. Writes the catalogue (or, with --payload, the unsigned payload for
   scripts/sign_glm_ocr_catalogue.py) and validates it with the same
   validate_glm_catalogue the app uses.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

MODEL_REPO = "ggml-org/GLM-OCR-GGUF"
BASE_MODEL = "zai-org/GLM-OCR"
LLAMA_REPO = "ggml-org/llama.cpp"
MIRROR_BASE = "https://github.com/AbdoslamB/InkDoc/releases/download"

MODEL_VARIANTS = {
    "q8": {"label": "Standard (Q8)", "model_file": "GLM-OCR-Q8_0.gguf", "mmproj_file": "mmproj-GLM-OCR-Q8_0.gguf"},
    "f16": {"label": "High precision (F16)", "model_file": "GLM-OCR-f16.gguf", "mmproj_file": "mmproj-GLM-OCR-Q8_0.gguf"},
}

# catalogue variant id -> (llama.cpp asset suffix, platform, accel, archive)
RUNTIME_VARIANTS = {
    "win-x64-cpu": ("win-cpu-x64.zip", "windows-x86_64", "cpu", "zip"),
    "win-x64-vulkan": ("win-vulkan-x64.zip", "windows-x86_64", "vulkan", "zip"),
    "win-arm64-cpu": ("win-cpu-arm64.zip", "windows-arm64", "cpu", "zip"),
    "macos-arm64": ("macos-arm64.tar.gz", "macos-arm64", "metal", "tar.gz"),
    "linux-x64-cpu": ("ubuntu-x64.tar.gz", "linux-x86_64", "cpu", "tar.gz"),
    "linux-x64-vulkan": ("ubuntu-vulkan-x64.tar.gz", "linux-x86_64", "vulkan", "tar.gz"),
}

SELFTEST = {
    "image": "glm_ocr_selftest.png",
    "prompt": "Text Recognition:",
    "expect_all": ["InkDoc", "2026", "self-test"],
}


def http_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "InkDoc-catalogue-builder", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url: str, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "InkDoc-catalogue-builder"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        while chunk := resp.read(1 << 20):
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# ─── Import tables (no dependencies) ─────────────────────────────────────────
def pe_imports(data: bytes) -> list[str]:
    """DLL names from a PE file's import and delay-import tables."""
    if data[:2] != b"MZ":
        return []
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    nsections = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    sections = []
    sec = opt + opt_size
    for i in range(nsections):
        off = sec + 40 * i
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", data, off + 8)
        sections.append((vaddr, max(vsize, rsize), raddr))

    def rva_to_off(rva: int) -> int | None:
        for vaddr, size, raddr in sections:
            if vaddr <= rva < vaddr + size:
                return raddr + (rva - vaddr)
        return None

    def cstr(off: int) -> str:
        end = data.index(b"\0", off)
        return data[off:end].decode("ascii", "replace")

    names = []
    imp_rva = struct.unpack_from("<I", data, dd + 8)[0]
    if imp_rva:
        off = rva_to_off(imp_rva)
        while off is not None:
            name_rva = struct.unpack_from("<I", data, off + 12)[0]
            if not name_rva:
                break
            name_off = rva_to_off(name_rva)
            if name_off is not None:
                names.append(cstr(name_off))
            off += 20
    delay_rva = struct.unpack_from("<I", data, dd + 13 * 8)[0]
    if delay_rva:
        off = rva_to_off(delay_rva)
        while off is not None:
            attrs, name_rva = struct.unpack_from("<II", data, off)
            if not name_rva:
                break
            name_off = rva_to_off(name_rva)
            if name_off is not None:
                names.append(cstr(name_off))
            off += 32
    return names


def elf_needed(data: bytes) -> tuple[list[str], list[str]]:
    """(DT_NEEDED, RUNPATH/RPATH) of a 64-bit little-endian ELF file."""
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return [], []
    phoff = struct.unpack_from("<Q", data, 0x20)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, 0x36)
    loads, dynamic = [], None
    for i in range(phnum):
        off = phoff + i * phentsize
        p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz = struct.unpack_from("<IIQQQQ", data, off)
        if p_type == 1:
            loads.append((p_vaddr, p_filesz, p_offset))
        elif p_type == 2:
            dynamic = (p_offset, p_filesz)
    if not dynamic:
        return [], []

    def vaddr_to_off(addr: int) -> int | None:
        for vaddr, size, offset in loads:
            if vaddr <= addr < vaddr + size:
                return offset + (addr - vaddr)
        return None

    entries = []
    off, end = dynamic[0], dynamic[0] + dynamic[1]
    while off + 16 <= end:
        tag, val = struct.unpack_from("<qQ", data, off)
        if tag == 0:
            break
        entries.append((tag, val))
        off += 16
    strtab = next((v for t, v in entries if t == 5), None)
    stroff = vaddr_to_off(strtab) if strtab is not None else None
    if stroff is None:
        return [], []

    def s(i: int) -> str:
        start = stroff + i
        return data[start:data.index(b"\0", start)].decode("utf-8", "replace")

    needed = [s(v) for t, v in entries if t == 1]
    paths = [s(v) for t, v in entries if t in (15, 29)]
    return needed, paths


def macho_dylibs(data: bytes) -> tuple[list[str], list[str]]:
    """(LC_LOAD_DYLIB-family names, LC_RPATH entries) of a thin 64-bit Mach-O."""
    if data[:4] != b"\xcf\xfa\xed\xfe":
        return [], []
    ncmds = struct.unpack_from("<I", data, 16)[0]
    off = 32
    dylibs, rpaths = [], []
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, off)
        if cmd in (0xC, 0x80000018, 0x8000001F, 0x80000023):
            name_off = struct.unpack_from("<I", data, off + 8)[0]
            raw = data[off + name_off:off + size]
            dylibs.append(raw.split(b"\0", 1)[0].decode("utf-8", "replace"))
        elif cmd == 0x8000001C:
            path_off = struct.unpack_from("<I", data, off + 8)[0]
            raw = data[off + path_off:off + size]
            rpaths.append(raw.split(b"\0", 1)[0].decode("utf-8", "replace"))
        off += size
    return dylibs, rpaths


# ─── Archive model ───────────────────────────────────────────────────────────
class Archive:
    """Members by name with the top-level folder stripped; links resolved in-archive."""

    def __init__(self, path: Path) -> None:
        self.files: dict[str, bytes] = {}
        self.links: dict[str, str] = {}
        if path.name.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                raw = {i.filename: i for i in zf.infolist() if not i.is_dir()}
                prefix = self._prefix(list(raw))
                for name, info in raw.items():
                    key = name[len(prefix):]
                    mode = info.external_attr >> 16
                    if (mode & 0o170000) == 0o120000:
                        self.links[key] = zf.read(info).decode()
                    else:
                        self.files[key] = zf.read(info)
        else:
            with tarfile.open(path) as tf:
                members = [m for m in tf.getmembers() if not m.isdir()]
                prefix = self._prefix([m.name for m in members])
                for m in members:
                    key = m.name[len(prefix):]
                    if m.issym():
                        self.links[key] = m.linkname
                    elif m.islnk():
                        self.links[key] = "/" + m.linkname[len(prefix):]
                    elif m.isfile():
                        fh = tf.extractfile(m)
                        self.files[key] = fh.read() if fh else b""

    @staticmethod
    def _prefix(names: list[str]) -> str:
        parts = [n.strip("/").split("/") for n in names]
        firsts = {p[0] for p in parts}
        if len(firsts) == 1 and any(len(p) > 1 for p in parts):
            return next(iter(firsts)) + "/"
        return ""

    def resolve(self, name: str) -> str | None:
        seen = 0
        while name in self.links and seen < 8:
            target = self.links[name]
            if target.startswith("/"):
                name = target.lstrip("/")
            else:
                base = str(PurePosixPath(name).parent)
                name = str(PurePosixPath(base, target)) if base != "." else target
                name = str(PurePosixPath(*[p for p in PurePosixPath(name).parts]))
            seen += 1
        return name if name in self.files else None

    def by_basename(self, basename: str) -> str | None:
        for name in list(self.files) + list(self.links):
            if name.rsplit("/", 1)[-1] == basename:
                return name
        return None

    def deps(self, name: str) -> tuple[list[str], list[str]]:
        data = self.files[self.resolve(name)]
        if data[:2] == b"MZ":
            return pe_imports(data), []
        if data[:4] == b"\x7fELF":
            return elf_needed(data)
        return macho_dylibs(data)


def is_backend(name: str) -> bool:
    base = name.rsplit("/", 1)[-1].lower()
    stem = base[3:] if base.startswith("lib") else base
    if not stem.startswith("ggml-"):
        return False
    if stem.startswith("ggml-base") or stem.startswith("ggml-rpc"):
        return False
    return any(stem.startswith(f"ggml-{k}") for k in ("cpu", "vulkan", "metal", "blas"))


def runtime_closure(archive: Archive, entry: str) -> tuple[dict[str, str], list[str], list[str]]:
    """Return ({name: sha256}, system_deps, search_paths) for what llama-server needs."""
    if archive.resolve(entry) is None:
        raise SystemExit(f"entry {entry!r} not in archive")
    # Backends loaded at runtime from the executable's folder. Only real files,
    # and on Linux/macOS only the unversioned name ggml looks for.
    start = [entry] + sorted(
        n for n in list(archive.files) + list(archive.links)
        if is_backend(n) and "/" not in n and (n.endswith(".dll") or n.endswith(".so") or n.endswith(".dylib"))
    )
    wanted: dict[str, str] = {}
    system: set[str] = set()
    paths: set[str] = set()
    queue = list(start)
    while queue:
        name = queue.pop()
        if name in wanted:
            continue
        real = archive.resolve(name)
        if real is None:
            continue
        wanted[name] = hashlib.sha256(archive.files[real]).hexdigest()
        deps, search = archive.deps(name)
        paths.update(search)
        for dep in deps:
            base = dep.rsplit("/", 1)[-1]
            found = archive.by_basename(base)
            if found is not None:
                queue.append(found)
            elif not dep.startswith("/usr/lib/") and not dep.startswith("/System/"):
                system.add(base)
    return dict(sorted(wanted.items())), sorted(system, key=str.lower), sorted(paths)


# ─── Catalogue ───────────────────────────────────────────────────────────────
def model_section(revision: str, mirror_tag: str) -> dict:
    tree = http_json(f"https://huggingface.co/api/models/{MODEL_REPO}/tree/{revision}")
    info = {e["path"]: e for e in tree if isinstance(e, dict) and e.get("type") == "file"}
    meta = http_json(f"https://huggingface.co/api/models/{MODEL_REPO}/revision/{revision}")
    last_modified = str(meta.get("lastModified", ""))[:10].replace("-", ".")

    def file_entry(name: str) -> dict:
        e = info.get(name)
        if not e or not e.get("lfs"):
            raise SystemExit(f"{name} not found as an LFS file in {MODEL_REPO}@{revision}")
        return {
            "name": name,
            "size": int(e["lfs"]["size"]),
            "sha256": e["lfs"]["oid"],
            "sources": [
                {"kind": "upstream", "url": f"https://huggingface.co/{MODEL_REPO}/resolve/{revision}/{name}"},
                {"kind": "mirror", "url": f"{MIRROR_BASE}/{mirror_tag}/{name}"},
            ],
        }

    variants = {}
    for vid, spec in MODEL_VARIANTS.items():
        variants[vid] = {
            "label": spec["label"],
            "model_file": spec["model_file"],
            "mmproj_file": spec["mmproj_file"],
            "files": [file_entry(spec["model_file"]), file_entry(spec["mmproj_file"])],
        }
    return {
        "version": f"{last_modified}-{revision[:8]}",
        "license": "MIT",
        "source_repo": MODEL_REPO,
        "source_revision": revision,
        "base_model": BASE_MODEL,
        "default_variant": "q8",
        "variants": variants,
    }


def runtime_section(build: str, mirror_tag: str, cache: Path) -> dict:
    release = http_json(f"https://api.github.com/repos/{LLAMA_REPO}/releases/tags/{build}")
    digests = {
        a["name"]: (a.get("digest") or "").removeprefix("sha256:")
        for a in release.get("assets", [])
    }
    variants = {}
    for vid, (suffix, plat, accel, archive_fmt) in RUNTIME_VARIANTS.items():
        asset = f"llama-{build}-bin-{suffix}"
        upstream = f"https://github.com/{LLAMA_REPO}/releases/download/{build}/{asset}"
        path = cache / build / asset
        if path.is_file():
            sha = sha256_file(path)
        else:
            print(f"downloading {asset} ...", flush=True)
            sha = download(upstream, path)
        if digests.get(asset) and digests[asset] != sha:
            raise SystemExit(f"{asset}: sha256 {sha} does not match GitHub's digest {digests[asset]}")
        archive = Archive(path)
        entry = "llama-server.exe" if suffix.startswith("win") else "llama-server"
        files, system, search = runtime_closure(archive, entry)
        print(f"  {vid}: {len(files)} files; system deps {system}; search paths {search}")
        variants[vid] = {
            "platform": plat,
            "accel": accel,
            "archive": archive_fmt,
            "size": path.stat().st_size,
            "sha256": sha,
            "entry": entry,
            "sha256_files": files,
            "system_deps": system,
            "sources": [
                {"kind": "upstream", "url": upstream},
                {"kind": "mirror", "url": f"{MIRROR_BASE}/{mirror_tag}/{asset}"},
            ],
        }
    return {"version": build, "license": "MIT", "source_repo": LLAMA_REPO, "variants": variants}


COMMENT = [
    "GLM-OCR download catalogue. Generated by scripts/build_glm_ocr_catalogue.py from",
    "pinned upstream versions; do not edit hashes or sizes by hand.",
    "",
    "Every file lists its sources in order: upstream first (Hugging Face for the model,",
    "the llama.cpp GitHub releases for the runtime), then InkDoc's own mirror release.",
    "A failure of any kind on upstream (blocked, 4xx/5xx, rate limit, wrong size or",
    "hash) moves on to the mirror; a file is only ever accepted with the hash below.",
    "",
    "sha256_files is the exact set of runtime files extracted and run, re-hashed",
    "before every launch. Nothing else in the archive is ever written to disk.",
    "",
    "A newer signed catalogue (catalogue_version higher than this one) may replace",
    "these pins at runtime without an app release; see app/core/glm_ocr_catalogue.py.",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--llama-build", required=True)
    ap.add_argument("--model-revision", required=True)
    ap.add_argument("--mirror-tag", required=True)
    ap.add_argument("--catalogue-version", type=int, required=True)
    ap.add_argument("--min-app-version", default="1.1.0")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "app" / "core" / "glm_ocr_catalogue.json")
    ap.add_argument("--cache-dir", type=Path, default=REPO_ROOT / "scratch" / "glm-ocr-cache")
    ap.add_argument("--payload", action="store_true", help="omit _comment (unsigned payload for signing)")
    args = ap.parse_args()

    catalogue = {
        "catalogue_version": args.catalogue_version,
        "min_app_version": args.min_app_version,
        "model": model_section(args.model_revision, args.mirror_tag),
        "runtime": runtime_section(args.llama_build, args.mirror_tag, args.cache_dir),
        "selftest": SELFTEST,
    }
    if not args.payload:
        catalogue = {"_comment": COMMENT, **catalogue}

    from app.core.glm_ocr_catalogue import CatalogueState, validate_glm_catalogue

    state, defects = validate_glm_catalogue(catalogue)
    if state is not CatalogueState.PUBLISHED:
        print("catalogue failed validation:", *defects, sep="\n  ", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalogue, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out} (catalogue_version {args.catalogue_version}, {state.value})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
