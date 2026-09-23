"""Standalone Docling Worker Entry Point.

Runs in an isolated Python environment (-I -s -S) outside the main frozen app.
Uses file-based RPC with C-level stdout redirection to prevent protocol corruption.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path

# Configure logging to write strictly to stderr
logging.basicConfig(
    level=logging.INFO,
    format="[DoclingWorker %(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("worker")

# Prevent OpenBLAS memory allocation failures on Windows
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("DOCLING_ARTIFACTS_PATH", str(Path(__file__).resolve().parent / "models"))
# Pin inference to CPU. Docling's accelerator device defaults to "auto", which selects
# the Metal (MPS) backend on Apple Silicon. The pack deliberately installs CPU-only
# torch, and MPS cannot be exercised on a CI runner at all -- the macOS pack build fails
# loading the layout model with "MPS backend out of memory" while trying to allocate
# 3.5 KiB. Pinning CPU makes inference identical on Windows, Linux and macOS, and makes
# the post-build smoke test validate the same code path users run. set via setdefault so
# the launcher can still override it deliberately.
os.environ.setdefault("DOCLING_DEVICE", "cpu")
os.environ.setdefault("DO_NOT_TRACK", "1")
os.environ.setdefault("SCARF_NO_ANALYTICS", "1")

# Redirect C-level stdout (fd 1) to stderr (fd 2) so C extensions (PyTorch, ONNX, OpenCV)
# cannot pollute the IPC channel. Only ipc_stream writes to the parent process.
try:
    _ipc_fd = os.dup(1)
    os.dup2(2, 1)
    ipc_stream = open(_ipc_fd, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stderr
except Exception as _exc:
    logger.warning("Could not duplicate fd 1 for IPC: %s", _exc)
    ipc_stream = sys.stdout

# Global converter instance cached in worker memory
_CACHED_CONVERTER = None
_CACHED_OPTIONS = None
_CONVERT_LOCK = threading.Lock()


def _patch_omegaconf() -> None:
    try:
        from pathlib import PurePath

        import omegaconf._utils

        orig = omegaconf._utils.is_primitive_type

        def patched(type_):
            t = omegaconf._utils.get_type_of(type_)
            if isinstance(t, type) and issubclass(t, PurePath):
                return True
            return orig(type_)

        omegaconf._utils.is_primitive_type = patched
    except Exception:
        pass


def _get_converter(
    ocr: bool = True,
    table_structure: bool = True,
    code_enrichment: bool = False,
    formula_enrichment: bool = False,
):
    global _CACHED_CONVERTER, _CACHED_OPTIONS
    _patch_omegaconf()

    # The enrichment flags belong in the cache key, not just the options. Docling
    # builds the code/formula stage at pipeline construction and loads its model
    # eagerly, so a converter created with enrichment on holds ~640 MB for as
    # long as it is cached. Keying on the flags means switching them off builds a
    # converter without the stage and drops the old one, releasing the weights.
    curr_opts = (ocr, table_structure, code_enrichment, formula_enrichment)
    if _CACHED_CONVERTER is not None and curr_opts == _CACHED_OPTIONS:
        return _CACHED_CONVERTER

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption, WordFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = ocr
    pipeline_options.do_table_structure = table_structure
    pipeline_options.do_code_enrichment = code_enrichment
    pipeline_options.do_formula_enrichment = formula_enrichment
    pipeline_options.do_picture_description = False
    pipeline_options.do_picture_classification = False

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            InputFormat.DOCX: WordFormatOption(pipeline_options=pipeline_options),
        }
    )
    _CACHED_CONVERTER = converter
    _CACHED_OPTIONS = curr_opts
    return _CACHED_CONVERTER


# ─── Code language fences ────────────────────────────────────────────────────
# Docling detects the language of every code block it transcribes but the
# Markdown serializer drops it, emitting a bare fence. This puts it back.
#
# This is a deliberate copy of app/core/engines/code_language.py. That module
# cannot be imported here: this worker runs under the engine pack's own
# interpreter, which has no access to the application package, and the pack
# ships this single file. tests/test_code_language.py parses this literal and
# fails if the two ever disagree.
_CODE_LANGUAGE_SLUGS = {
    "Ada": "ada",
    "Awk": "awk",
    "Bash": "bash",
    "bc": "bc",
    "C": "c",
    "C#": "csharp",
    "C++": "cpp",
    "CMake": "cmake",
    "COBOL": "cobol",
    "CSS": "css",
    "Ceylon": "ceylon",
    "Clojure": "clojure",
    "Crystal": "crystal",
    "Cuda": "cuda",
    "Cython": "cython",
    "D": "d",
    "Dart": "dart",
    "dc": "dc",
    "Dockerfile": "dockerfile",
    "Elixir": "elixir",
    "Erlang": "erlang",
    "FORTRAN": "fortran",
    "Forth": "forth",
    "Go": "go",
    "HTML": "html",
    "Haskell": "haskell",
    "Haxe": "haxe",
    "Java": "java",
    "JavaScript": "javascript",
    "JSON": "json",
    "Julia": "julia",
    "Kotlin": "kotlin",
    "Latex": "latex",
    "Lisp": "lisp",
    "Lua": "lua",
    "Matlab": "matlab",
    "MoonScript": "moonscript",
    "Nim": "nim",
    "OCaml": "ocaml",
    "ObjectiveC": "objectivec",
    "Octave": "octave",
    "PHP": "php",
    "Pascal": "pascal",
    "Perl": "perl",
    "Prolog": "prolog",
    "Python": "python",
    "Racket": "racket",
    "Ruby": "ruby",
    "Rust": "rust",
    "SML": "sml",
    "SQL": "sql",
    "Scala": "scala",
    "Scheme": "scheme",
    "Swift": "swift",
    "Tikz": "tikz",
    "TypeScript": "typescript",
    "VisualBasic": "vbnet",
    "XML": "xml",
    "YAML": "yaml",
}

def _code_language_slug(label) -> str:
    """Map a CodeLanguageLabel (or its value) onto a Markdown fence identifier."""
    if label is None:
        return ""
    value = getattr(label, "value", label)
    if not isinstance(value, str):
        return ""
    return _CODE_LANGUAGE_SLUGS.get(value, "")


def _apply_code_languages(doc, markdown: str) -> str:
    """Rewrite bare code fences so they carry the detected language.

    Driven by the document's own CodeItems walked in order and matched against a
    moving cursor, so repeated identical blocks still line up with their own
    languages. Anything unexpected is skipped: a missing language is cosmetic
    and must never cost the conversion.
    """
    if not markdown or doc is None:
        return markdown
    try:
        from docling_core.types.doc import CodeItem
    except Exception:
        return markdown
    try:
        items = [item for item, _level in doc.iterate_items(with_groups=False)]
    except Exception:
        try:
            items = list(getattr(doc, "texts", []) or [])
        except Exception:
            return markdown

    cursor = 0
    for item in items:
        if not isinstance(item, CodeItem):
            continue
        slug = _code_language_slug(getattr(item, "code_language", None))
        text = getattr(item, "text", None)
        if not slug or not text:
            continue
        nl = chr(10)
        block = "```" + nl + text + nl + "```"
        found = markdown.find(block, cursor)
        if found == -1:
            continue
        markdown = markdown[:found] + "```" + slug + markdown[found + 3:]
        cursor = found + len(block) + len(slug)
    return markdown


def _handle_convert(payload: dict) -> dict:
    job_id = payload.get("job_id", "")
    source_file = payload.get("source_file", "")
    output_file = payload.get("output_file", "")
    ocr = bool(payload.get("ocr", True))
    table_structure = bool(payload.get("table_structure", True))
    code_enrichment = bool(payload.get("code_enrichment", False))
    formula_enrichment = bool(payload.get("formula_enrichment", False))

    if not source_file or not os.path.exists(source_file):
        return {"status": "error", "job_id": job_id, "error": f"Source file does not exist: {source_file}"}

    if not output_file:
        return {"status": "error", "job_id": job_id, "error": "Output file path missing from request"}

    with _CONVERT_LOCK:
        try:
            converter = _get_converter(
                ocr=ocr,
                table_structure=table_structure,
                code_enrichment=code_enrichment,
                formula_enrichment=formula_enrichment,
            )
            conv_res = converter.convert(Path(source_file))
            markdown_text = conv_res.document.export_to_markdown()
            markdown_text = _apply_code_languages(conv_res.document, markdown_text)

            # Write result directly to the requested output file
            out_path = Path(output_file)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(markdown_text, encoding="utf-8")

            return {
                "status": "ok",
                "job_id": job_id,
                "output_file": str(out_path),
                "chars": len(markdown_text),
            }
        except Exception as exc:
            logger.exception("Conversion failed in worker: %s", exc)
            return {"status": "error", "job_id": job_id, "error": str(exc)}


def main() -> int:
    logger.info("Docling isolated worker started (PID %d)", os.getpid())
    # Send ready signal to parent
    ipc_stream.write(json.dumps({"status": "ready", "pid": os.getpid()}) + "\n")
    ipc_stream.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except Exception as err:
            ipc_stream.write(json.dumps({"status": "error", "error": f"Malformed JSON request: {err}"}) + "\n")
            ipc_stream.flush()
            continue

        action = req.get("action", "convert")

        if action == "ping":
            ipc_stream.write(json.dumps({"status": "pong", "pid": os.getpid()}) + "\n")
            ipc_stream.flush()

        elif action == "shutdown":
            logger.info("Worker received shutdown signal. Exiting.")
            ipc_stream.write(json.dumps({"status": "bye"}) + "\n")
            ipc_stream.flush()
            break

        elif action == "convert":
            resp = _handle_convert(req)
            ipc_stream.write(json.dumps(resp) + "\n")
            ipc_stream.flush()

        else:
            ipc_stream.write(json.dumps({"status": "error", "error": f"Unknown action: {action}"}) + "\n")
            ipc_stream.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
