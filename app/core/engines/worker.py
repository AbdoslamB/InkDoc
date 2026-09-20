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


def _get_converter(ocr: bool = True, table_structure: bool = True):
    global _CACHED_CONVERTER, _CACHED_OPTIONS
    _patch_omegaconf()

    curr_opts = (ocr, table_structure)
    if _CACHED_CONVERTER is not None and curr_opts == _CACHED_OPTIONS:
        return _CACHED_CONVERTER

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption, WordFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = ocr
    pipeline_options.do_table_structure = table_structure
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


def _handle_convert(payload: dict) -> dict:
    job_id = payload.get("job_id", "")
    source_file = payload.get("source_file", "")
    output_file = payload.get("output_file", "")
    ocr = bool(payload.get("ocr", True))
    table_structure = bool(payload.get("table_structure", True))

    if not source_file or not os.path.exists(source_file):
        return {"status": "error", "job_id": job_id, "error": f"Source file does not exist: {source_file}"}

    if not output_file:
        return {"status": "error", "job_id": job_id, "error": "Output file path missing from request"}

    with _CONVERT_LOCK:
        try:
            converter = _get_converter(ocr=ocr, table_structure=table_structure)
            conv_res = converter.convert(Path(source_file))
            markdown_text = conv_res.document.export_to_markdown()

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
