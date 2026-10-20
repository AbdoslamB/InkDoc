"""A text file MarkItDown decodes with the wrong charset is retried as UTF-8.

MarkItDown guesses a text file's charset from its first 64 KB. A CSV export that
is plain ASCII there and has one accented name further on is guessed as ASCII
and fails with UnicodeDecodeError, in 0.1.7 and 0.1.8 alike. run_markitdown()
retries such a file once as UTF-8, only when the whole file is valid UTF-8 and
no charset was given.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core import converter
from app.core.converter import ConversionOptions, run_markitdown


def _late_text(tmp_path: Path, tail: bytes, name: str = "export.csv") -> Path:
    """A CSV whose first ~90 KB are ASCII, ending with `tail`."""
    rows = ["id,name"] + [f"{i},customer number {i} plain ascii" for i in range(3000)]
    path = tmp_path / name
    path.write_bytes(("\n".join(rows) + "\n").encode("ascii") + tail)
    assert path.stat().st_size > 65536
    return path


def test_a_csv_with_utf8_after_the_first_64_kb_converts(tmp_path):
    src = _late_text(tmp_path, "3001,Zoë Café – “quoted”\n".encode())
    out = run_markitdown(str(src), ConversionOptions(), {}, None)
    assert "Zoë Café – “quoted”" in out
    assert "customer number 2999" in out


def test_a_text_file_with_utf8_after_the_first_64_kb_converts(tmp_path):
    src = _late_text(tmp_path, "naïve résumé\n".encode(), name="notes.txt")
    assert "naïve résumé" in run_markitdown(str(src), ConversionOptions(), {}, None)


def test_a_file_that_is_not_utf8_still_fails(tmp_path):
    # 0xE9 is "é" in Windows-1252, and invalid on its own in UTF-8: no retry.
    src = _late_text(tmp_path, b"3001,Caf\xe9\n")
    with pytest.raises(Exception) as caught:
        run_markitdown(str(src), ConversionOptions(), {}, None)
    assert converter._failed_on_decoding(caught.value)


def test_a_charset_given_explicitly_is_not_second_guessed(tmp_path):
    src = _late_text(tmp_path, "3001,Café\n".encode())
    with pytest.raises(Exception) as caught:
        run_markitdown(str(src), ConversionOptions(), {"charset": "ascii"}, None)
    assert converter._failed_on_decoding(caught.value)


def test_utf8_check_reads_across_piece_boundaries(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes("ab€cd".encode())  # "€" is three bytes, split by 2-byte pieces
    assert converter._is_utf8_file(str(path), chunk_size=2)
    path.write_bytes("ab€".encode()[:-1])  # cut inside the last character
    assert not converter._is_utf8_file(str(path), chunk_size=2)
    assert not converter._is_utf8_file(str(tmp_path / "missing.txt"))


def test_decoding_failures_are_recognised_inside_markitdowns_exception():
    class Attempt:
        def __init__(self, exc):
            self.exc_info = (type(exc), exc, None)

    class Failure(Exception):
        def __init__(self, attempts):
            super().__init__("failed")
            self.attempts = attempts

    decode = UnicodeDecodeError("ascii", b"\xc3", 0, 1, "ordinal not in range(128)")
    assert converter._failed_on_decoding(Failure([Attempt(ValueError("x")), Attempt(decode)]))
    assert converter._failed_on_decoding(decode)
    assert not converter._failed_on_decoding(Failure([Attempt(ValueError("x"))]))
    assert not converter._failed_on_decoding(Failure(None))
    assert not converter._failed_on_decoding(RuntimeError("other"))
