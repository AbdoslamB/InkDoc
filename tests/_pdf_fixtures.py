"""Minimal PDFs written by hand, for the probe and missing-text check tests.

No PDF library is needed: each document is a handful of objects with a correct
xref table, which pdfium reads without complaint. Image streams are tiny
(8x8 pixels) and placed to cover the page; only their dictionaries matter to the
probe, which never decodes an image.

    write_pdf(path, [text_page(["Hello world"]), image_page("ccitt")])
"""
from __future__ import annotations

import io
import zlib
from pathlib import Path

PAGE_W, PAGE_H = 612, 792

# name -> (bits per component, colour space, filter, stream bytes)
_IMAGES = {
    "rgb": (8, "/DeviceRGB", "FlateDecode", zlib.compress(bytes(8 * 8 * 3))),
    "gray": (8, "/DeviceGray", "FlateDecode", zlib.compress(bytes(8 * 8))),
    "bilevel": (1, "/DeviceGray", "FlateDecode", zlib.compress(bytes(8))),
    "ccitt": (1, "/DeviceGray", "CCITTFaxDecode", b"\x00\x01\x02\x03"),
    "jbig2": (1, "/DeviceGray", "JBIG2Decode", b"\x00\x01\x02\x03"),
    # A colour JPEG photo. The stream is not a decodable JPEG, which is fine:
    # the probe reads the dictionary only.
    "dct": (8, "/DeviceRGB", "DCTDecode", b"\xff\xd8\xff\xd9"),
}

_PROSE = (
    "the annual revenue grew across every region while operating costs fell "
    "because the company invested in automation logistics and training programs "
    "customers reported higher satisfaction with delivery times product quality "
    "and support quality throughout the reporting period"
)
WORDS = _PROSE.split()


def words(n: int, offset: int = 0) -> str:
    """n deterministic words, varied by `offset` so pages differ."""
    return " ".join(WORDS[(offset + i) % len(WORDS)] for i in range(n))


def text_page(lines: list[str]) -> dict:
    return {"lines": list(lines)}


def prose_page(n_words: int = 80, offset: int = 0, extra_lines: list[str] | None = None) -> dict:
    """A page of prose split into lines of ten words, plus optional extra lines."""
    text = words(n_words, offset).split()
    lines = [" ".join(text[i:i + 10]) for i in range(0, len(text), 10)]
    return {"lines": lines + list(extra_lines or [])}


def image_page(kind: str = "rgb", *, lines: list[str] | None = None, in_form: bool = False,
               width_fraction: float = 1.0) -> dict:
    return {"image": kind, "lines": list(lines or []), "in_form": in_form, "wf": width_fraction}


def _escape(text: str) -> bytes:
    esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return esc.encode("cp1252")


def build_pdf(pages: list[dict], *, producer: str | None = None, encrypt: bool = False) -> bytes:
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    add(b"")  # obj 2: the Pages tree, written once the kids are known
    kids = []
    for spec in pages:
        xobjects = []
        content = b""
        kind = spec.get("image")
        if kind:
            bpc, space, flt, data = _IMAGES[kind]
            image = add(
                f"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /BitsPerComponent {bpc} "
                f"/ColorSpace {space} /Filter /{flt} /Length {len(data)} >>\nstream\n".encode()
                + data + b"\nendstream"
            )
            w = PAGE_W * spec.get("wf", 1.0)
            if spec.get("in_form"):
                # The image drawn at full size inside a form scaled by 0.5, the
                # form drawn at 2x: page coverage is still the whole page.
                form_content = f"q {w} 0 0 {PAGE_H} 0 0 cm /Im0 Do Q".encode()
                form = add(
                    f"<< /Type /XObject /Subtype /Form /BBox [0 0 {PAGE_W} {PAGE_H}] "
                    f"/Matrix [0.5 0 0 0.5 0 0] /Resources << /XObject << /Im0 {image} 0 R >> >> "
                    f"/Length {len(form_content)} >>\nstream\n".encode() + form_content + b"\nendstream"
                )
                xobjects.append(f"/Fm0 {form} 0 R")
                content += b"q 2 0 0 2 0 0 cm /Fm0 Do Q\n"
            else:
                xobjects.append(f"/Im0 {image} 0 R")
                content += f"q {w} 0 0 {PAGE_H} 0 0 cm /Im0 Do Q\n".encode()
        if spec.get("lines"):
            content += b"BT /F1 9 Tf 11 TL 40 760 Td\n"
            for line in spec["lines"]:
                content += b"(" + _escape(line) + b") '\n"
            content += b"ET\n"
        stream = add(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
        xo = f"/XObject << {' '.join(xobjects)} >>" if xobjects else ""
        kids.append(add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Resources << /Font << /F1 {font} 0 R >> {xo} >> /Contents {stream} 0 R >>".encode()
        ))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    catalog = add(b"<< /Type /Catalog /Pages 2 0 R >>")
    info = add(f"<< /Producer ({producer}) >>".encode()) if producer else None
    encrypt_ref = (
        add(b"<< /Filter /Standard /V 1 /R 2 /O (0123456789abcdef0123456789abcdef) "
            b"/U (0123456789abcdef0123456789abcdef) /P -4 >>")
        if encrypt
        else None
    )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    trailer = f"/Size {len(objs) + 1} /Root {catalog} 0 R"
    if info:
        trailer += f" /Info {info} 0 R"
    if encrypt_ref:
        trailer += (f" /Encrypt {encrypt_ref} 0 R "
                    "/ID [<00112233445566778899aabbccddeeff><00112233445566778899aabbccddeeff>]")
    out.write(f"trailer\n<< {trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def write_pdf(path: Path, pages: list[dict], **kwargs) -> Path:
    path = Path(path)
    path.write_bytes(build_pdf(pages, **kwargs))
    return path
