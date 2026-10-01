#!/usr/bin/env python3
"""Phase 0 calibration for GLM-OCR (dev-only; never writes to ~/Downloads).

Measures, on this machine, for each render size (long side in pixels):
  - image tokens, prompt (vision + prefill) time, generation time and speed,
  - total seconds per page,
  - token recall against a reference text, when one is given.

Usage (GLM-OCR must already be installed, e.g. from Settings):

    python scripts/calibrate_glm_ocr.py page1.png page2.pdf ... \
        [--sizes 1024,1280,1600,2048] [--threads 4,8] [--gpu] \
        [--reference-dir refs/]   # refs/<stem>.txt holds the hand-checked text

The documents stay local; only the printed numbers belong in the plan, next to
RENDER_LONG_SIDE in app/core/glm_ocr_manager.py and the speed bar in 12.3.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def tokens(text: str) -> Counter:
    return Counter(t for t in re.findall(r"[^\W_]+", text.lower()) if len(t) >= 2 or t.isdigit())


def recall(reference: str, output: str) -> float:
    ref, out = tokens(reference), tokens(output)
    total = sum(ref.values())
    return sum(min(c, out[t]) for t, c in ref.items()) / total if total else 1.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("--sizes", default="1024,1280,1600")
    ap.add_argument("--threads", default="")
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--reference-dir", type=Path)
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--json", type=Path, help="also write the raw rows here")
    args = ap.parse_args()

    from app.core.engines.glm_ocr_engine import (
        PROMPT_TEXT,
        _png_bytes,
        estimate_image_tokens,
        open_pages,
        output_budget,
    )
    from app.core.engines.glm_ocr_server import (
        DEFAULT_CTX_SIZE,
        LlamaServerProcess,
        image_to_data_uri,
        ocr_payload,
        parse_ocr_response,
    )
    from app.core.glm_ocr_manager import GlmOcrManager

    mgr = GlmOcrManager.get_instance()
    if not mgr.is_usable():
        print("GLM-OCR is not installed and self-tested; download it in Settings first.", file=sys.stderr)
        return 1
    sizes = [int(s) for s in args.sizes.split(",") if s]
    thread_options = [int(t) for t in args.threads.split(",") if t] or [None]
    rows = []
    for threads in thread_options:
        spec = mgr.launch_spec(args.gpu)
        if threads:
            spec.threads = threads
        proc = LlamaServerProcess(spec)
        proc.start()
        print(f"# server: accel={spec.accel} threads={spec.threads} load={proc.load_seconds:.1f}s")
        try:
            for path in args.inputs:
                ref = None
                if args.reference_dir and (args.reference_dir / f"{path.stem}.txt").is_file():
                    ref = (args.reference_dir / f"{path.stem}.txt").read_text(encoding="utf-8")
                pages = open_pages(str(path))
                try:
                    count = min(pages.count, args.max_pages)
                    for size in sizes:
                        texts, secs = [], []
                        for i in range(count):
                            img = pages.render(i, size)
                            est = estimate_image_tokens(img.width, img.height)
                            budget = output_budget(DEFAULT_CTX_SIZE, est) or 1024
                            uri = image_to_data_uri(_png_bytes(img))
                            t0 = time.time()
                            raw = proc.chat(ocr_payload(uri, PROMPT_TEXT, budget), timeout=900)
                            res = parse_ocr_response(raw, time.time() - t0)
                            timings = raw.get("timings") or {}
                            row = {
                                "file": path.name, "page": i + 1, "size": size, "threads": spec.threads,
                                "accel": spec.accel, "px": f"{img.width}x{img.height}",
                                "image_tokens_est": est, "prompt_tokens": res.prompt_tokens,
                                "prompt_s": round((timings.get("prompt_ms") or 0) / 1000, 2),
                                "gen_tokens": res.completion_tokens,
                                "gen_s": round((timings.get("predicted_ms") or 0) / 1000, 2),
                                "gen_tps": round(res.predicted_per_second, 1),
                                "total_s": round(res.seconds, 2),
                            }
                            rows.append(row)
                            texts.append(res.text)
                            secs.append(res.seconds)
                            print(json.dumps(row))
                        summary = f"{path.name} @ {size}px: {sum(secs) / max(1, len(secs)):.1f} s/page"
                        if ref is not None:
                            summary += f", token recall {recall(ref, chr(10).join(texts)):.3f}"
                        print("SUMMARY", summary, flush=True)
                finally:
                    pages.close()
        finally:
            proc.stop()
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
