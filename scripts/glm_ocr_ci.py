#!/usr/bin/env python3
"""Helpers for .github/workflows/glm-ocr-track.yml ("latest tested").

    detect        Compare the bundled pins with upstream; write pins.json.
    smoke         Install GLM-OCR from a catalogue (real downloads), self-test it
                  with the exact production command line, OCR the fixture pages,
                  check token recall, write a JSON report.
    mirror-files  Download every file of a catalogue from upstream, verify each
                  hash, and stage them (plus the licenses) for the mirror release.
    report        Turn smoke reports into the maintainer's issue text.

Upstream facts this relies on (checked 2026-10-01):
- llama.cpp tags every build "b<number>" and marks each one a prerelease;
  GitHub's /releases/latest points at a semver release with no binaries. The
  newest build is the highest b-number whose own release page (the list
  endpoint truncates asset lists) carries every needed asset.
- Hugging Face's model API returns the repository head revision as "sha".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

RECALL_BAR = 0.95


def http_json(url: str) -> object:
    headers = {"User-Agent": "InkDoc-glm-ocr-track", "Accept": "application/json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ─── detect ──────────────────────────────────────────────────────────────────
def newest_llama_build(needed_suffixes: list[str], candidates: int = 8) -> str | None:
    """Highest b-number build that carries every asset we pin.

    llama.cpp marks every build a prerelease, so prereleases are accepted here,
    and the releases list truncates each release's asset list, so each
    candidate's full list comes from its own /releases/tags/<tag> endpoint.
    """
    releases = http_json("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=40")
    tags = sorted(
        (rel["tag_name"] for rel in releases
         if re.fullmatch(r"b\d+", rel.get("tag_name", "")) and not rel.get("draft")),
        key=lambda t: int(t[1:]), reverse=True,
    )
    for tag in tags[:candidates]:
        full = http_json(f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{tag}")
        names = {a["name"] for a in full.get("assets", [])}
        if all(f"llama-{tag}-bin-{s}" in names for s in needed_suffixes):
            return tag
    return None


def cmd_detect(args) -> int:
    from build_glm_ocr_catalogue import RUNTIME_VARIANTS

    current = json.loads((REPO_ROOT / "app" / "core" / "glm_ocr_catalogue.json").read_text(encoding="utf-8"))
    model_sha = http_json("https://huggingface.co/api/models/ggml-org/GLM-OCR-GGUF")["sha"]
    build = args.llama_build or newest_llama_build([v[0] for v in RUNTIME_VARIANTS.values()])
    revision = args.model_revision or model_sha
    if not build:
        print("::error::no llama.cpp build carries every needed asset")
        return 1
    changed = (revision != current["model"]["source_revision"]) or (build != current["runtime"]["version"])
    next_version = int(current["catalogue_version"]) + 1
    pins = {
        "changed": bool(changed or args.force),
        "model_revision": revision,
        "llama_build": build,
        "current_model_revision": current["model"]["source_revision"],
        "current_llama_build": current["runtime"]["version"],
        "catalogue_version": next_version,
        "mirror_tag": f"glm-ocr-v{next_version}",
    }
    Path(args.out).write_text(json.dumps(pins, indent=2), encoding="utf-8")
    print(json.dumps(pins, indent=2))
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            for key in ("changed", "model_revision", "llama_build", "catalogue_version", "mirror_tag"):
                value = pins[key]
                fh.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    return 0


# ─── smoke ───────────────────────────────────────────────────────────────────
def fixtures(out: Path) -> list[tuple[Path, str]]:
    """Synthetic, redistributable fixture pages with known text: prose, a table, a
    noisy fax-like scan and a phone photo stored sideways (EXIF orientation)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    out.mkdir(parents=True, exist_ok=True)
    pages = {
        "prose": ["Annual review 2026", "The research team measured growth in every region.",
                  "Average costs fell by 7 percent while revenue increased.",
                  "Customers reported faster service and fewer delays."],
        "table": ["Results by region", "Region Q1 Q2 Q3", "North 120 135 150", "South 98 101 117",
                  "East 77 80 95", "Total 295 316 362"],
        "fax": ["Invoice number 48213", "Payment due within 30 days.", "Amount 1250.00 dollars",
                "Thank you for your business."],
        "photo": ["Meeting notes", "Ship the release on Friday.", "Review the open issues first.",
                  "Next meeting on Monday at 10."],
    }
    made = []
    for name, lines in pages.items():
        img = Image.new("L", (1240, 1754), 255)
        draw = ImageDraw.Draw(img)
        y = 140
        for i, line in enumerate(lines):
            draw.text((110, y), line, fill=0, font=ImageFont.load_default(size=46 if i == 0 else 36))
            y += 80
        if name == "fax":
            import random

            rnd = random.Random(1)
            px = img.load()
            for _ in range(25000):
                px[rnd.randrange(1240), rnd.randrange(1754)] = 0 if rnd.random() < 0.5 else 255
            img = img.filter(ImageFilter.GaussianBlur(0.8)).point(lambda v: 0 if v < 150 else 255)
        path = out / f"{name}.png"
        if name == "photo":
            exif = Image.Exif()
            exif[0x0112] = 8                      # displayed rotated 90°; stored sideways
            path = out / "photo.jpg"
            img.rotate(-90, expand=True).convert("RGB").save(path, exif=exif, quality=90)
        else:
            img.save(path)
        made.append((path, "\n".join(lines)))
    return made


def recall(reference: str, output: str) -> float:
    def toks(t):
        return Counter(x for x in re.findall(r"[^\W_]+", t.lower()) if len(x) >= 2 or x.isdigit())

    ref, out = toks(reference), toks(output)
    total = sum(ref.values())
    return sum(min(c, out[t]) for t, c in ref.items()) / total if total else 1.0


def cmd_smoke(args) -> int:
    os.environ["INKDOC_GLM_OCR_CATALOGUE"] = str(Path(args.catalogue).resolve())
    os.environ.setdefault("INKDOC_ENGINES_DIR", str(Path(args.engines_dir).resolve()))
    from app.core.converter import ConversionOptions, convert_item
    from app.core.engines.glm_ocr_server import GlmOcrServer
    from app.core.glm_ocr_manager import GlmOcrManager
    from app.core.queue_model import EngineKind, QueueItem, SourceKind

    report: dict = {"platform": sys.platform, "engines_dir": os.environ["INKDOC_ENGINES_DIR"], "pages": []}
    mgr = GlmOcrManager.get_instance()
    mgr.provider.refresh_remote = lambda force=False: mgr.provider.current()   # test the given pins only
    started = time.time()
    try:
        mgr.install(gpu=False)
    except Exception as exc:
        report["error"] = f"install failed: {exc}"
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"::error::{report['error']}")
        return 1
    st = mgr.get_status()
    report.update(install_seconds=round(time.time() - started, 1), selftest=st["selftest"],
                  usable=st["usable"], seconds_per_page=st["seconds_per_page"])
    ok = bool(st["usable"])
    for path, reference in fixtures(Path(args.fixtures_dir)):
        item = QueueItem(source=str(path), kind=SourceKind.FILE, display_name=path.name, engine=EngineKind.GLM_OCR)
        t0 = time.time()
        md = convert_item(item, ConversionOptions(engine=EngineKind.GLM_OCR))
        r = recall(reference, md)
        report["pages"].append({"file": path.name, "recall": round(r, 3), "seconds": round(time.time() - t0, 1)})
        print(f"{path.name}: recall {r:.3f}")
        ok = ok and r >= RECALL_BAR
    GlmOcrServer.get_instance().shutdown()
    report["passed"] = ok
    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not ok:
        print(f"::error::GLM-OCR smoke test failed (recall bar {RECALL_BAR})")
    return 0 if ok else 1


# ─── mirror-files ────────────────────────────────────────────────────────────
def cmd_mirror_files(args) -> int:
    from app.core.download_utils import (
        MODEL_DOWNLOAD_HOST_SUFFIXES,
        MODEL_DOWNLOAD_HOSTS,
        stream_download,
    )

    catalogue = json.loads(Path(args.catalogue).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wanted: dict[str, tuple[str, int, str]] = {}
    for v in catalogue["model"]["variants"].values():
        for f in v["files"]:
            up = next(s["url"] for s in f["sources"] if s["kind"] == "upstream")
            wanted[f["name"]] = (up, f["size"], f["sha256"])
    for v in catalogue["runtime"]["variants"].values():
        up = next(s["url"] for s in v["sources"] if s["kind"] == "upstream")
        wanted[up.rsplit("/", 1)[-1]] = (up, v["size"], v["sha256"])
    for name, (url, size, sha) in wanted.items():
        print(f"fetching {name} ...", flush=True)
        got = stream_download(url, out / name, expected_size=size, allowed_hosts=MODEL_DOWNLOAD_HOSTS,
                              allowed_suffixes=MODEL_DOWNLOAD_HOST_SUFFIXES, user_agent="InkDoc-glm-ocr-track")
        if got != sha:
            print(f"::error::{name} hashes to {got}, catalogue says {sha}")
            return 1
    for lic in ("LICENSE-GLM-OCR.txt", "LICENSE-llama.cpp.txt"):
        (out / lic).write_bytes((REPO_ROOT / "app" / "core" / "assets" / "licenses" / lic).read_bytes())
    # Mirror filenames must match the mirror URLs in the catalogue exactly.
    for v in list(catalogue["model"]["variants"].values()):
        for f in v["files"]:
            mirror = next(s["url"] for s in f["sources"] if s["kind"] == "mirror")
            assert mirror.rsplit("/", 1)[-1] == f["name"], mirror
    print(f"staged {len(wanted)} files in {out}")
    return 0


# ─── report ──────────────────────────────────────────────────────────────────
def cmd_report(args) -> int:
    pins = json.loads(Path(args.pins).read_text(encoding="utf-8"))
    lines = [
        f"## GLM-OCR update ready to sign: {pins['mirror_tag']}",
        "",
        f"- Model: `{pins['current_model_revision'][:8]}` → `{pins['model_revision'][:8]}`",
        f"- llama.cpp: `{pins['current_llama_build']}` → `{pins['llama_build']}`",
        f"- catalogue_version: {pins['catalogue_version']}",
        "",
        "| Platform | Self-test s/page | Fixture recall | Passed |",
        "|---|---|---|---|",
    ]
    for path in sorted(Path(args.reports).glob("*.json")):
        r = json.loads(path.read_text(encoding="utf-8"))
        rec = ", ".join(f"{p['file']} {p['recall']}" for p in r.get("pages", []))
        lines.append(f"| {path.stem} | {r.get('seconds_per_page', '?')} | {rec or r.get('error', '')} | {r.get('passed')} |")
    lines += [
        "",
        "The mirror release is a **draft**, and a PR updates the bundled catalogue. To make the",
        "new pins reach installed apps without an app release, sign offline:",
        "",
        "```",
        f"python scripts/sign_glm_ocr_catalogue.py --tag {pins['mirror_tag']} --publish",
        "```",
    ]
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect")
    d.add_argument("--out", default="pins.json")
    d.add_argument("--llama-build", default="")
    d.add_argument("--model-revision", default="")
    d.add_argument("--force", action="store_true")
    s = sub.add_parser("smoke")
    s.add_argument("--catalogue", required=True)
    s.add_argument("--engines-dir", required=True)
    s.add_argument("--fixtures-dir", default="glm-ocr-fixtures")
    s.add_argument("--report", required=True)
    m = sub.add_parser("mirror-files")
    m.add_argument("--catalogue", required=True)
    m.add_argument("--out", required=True)
    r = sub.add_parser("report")
    r.add_argument("--pins", required=True)
    r.add_argument("--reports", required=True)
    r.add_argument("--out", required=True)
    args = ap.parse_args()
    return {"detect": cmd_detect, "smoke": cmd_smoke, "mirror-files": cmd_mirror_files, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
