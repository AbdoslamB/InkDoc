#!/usr/bin/env python3
"""Render app/core/assets/glm_ocr_selftest.png, the image GLM-OCR must read after install.

The GLM-OCR install is not complete until the model actually reads this image
and its output contains every token in the catalogue's selftest.expect_all
("InkDoc", "2026", "self-test"). Kept small and high-contrast so the check
takes a few seconds on CPU and failing it means the runtime or model is
broken, not that the text was hard to read.

Regenerate with:  python scripts/make_glm_ocr_selftest_image.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "app" / "core" / "assets" / "glm_ocr_selftest.png"
LINES = [
    ("InkDoc self-test", 54),
    ("Reading check 2026", 40),
    ("Local OCR works on this computer.", 30),
]


def main() -> None:
    img = Image.new("L", (900, 300), 255)
    draw = ImageDraw.Draw(img)
    y = 40
    for text, size in LINES:
        font = ImageFont.load_default(size=size)
        draw.text((48, y), text, fill=0, font=font)
        y += int(size * 1.6)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
