"""IBM Research Zurich Docling conversion engine wrapper.

Provides deep visual document parsing, layout segmentation, reading-order graphs,
and TableFormer table recovery.

Kept lazy-loaded and decoupled so that systems without docling installed
run without errors or overhead.
"""
from __future__ import annotations

import logging
import os
import threading
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.converter import ConversionOptions
    from app.core.queue_model import QueueItem

logger = logging.getLogger(__name__)

# Concurrency lock to prevent multiple heavy AI conversions from exhausting RAM
_CONVERSION_LOCK = threading.Semaphore(1)

# Cached DocumentConverter instance to avoid reloading heavy AI model weights
_CACHED_CONVERTER: Any = None
_CACHED_OPTIONS: tuple[bool, bool] | None = None


def _patch_omegaconf_for_windows() -> None:
    """Fix upstream OmegaConf bug on Windows where pathlib.Path (WindowsPath)
    is rejected as an unsupported primitive type by RapidOCR.
    """
    try:
        from pathlib import PurePath

        import omegaconf._utils
        import omegaconf.base
        import omegaconf.dictconfig

        orig_is_primitive = omegaconf._utils.is_primitive_type

        def patched_is_primitive(type_: Any) -> bool:
            t = omegaconf._utils.get_type_of(type_)
            if isinstance(t, type) and issubclass(t, PurePath):
                return True
            return orig_is_primitive(type_)

        omegaconf._utils.is_primitive_type = patched_is_primitive
        omegaconf.base.is_primitive_type = patched_is_primitive
        omegaconf.dictconfig.is_primitive_type = patched_is_primitive
    except Exception:
        pass


def is_docling_available() -> bool:
    """Return True if docling is installed and can be imported."""
    try:
        import docling  # noqa: F401
        return True
    except (ImportError, Exception):
        return False


def get_docling_version() -> str:
    """Return the installed docling version string or 'Not installed'."""
    try:
        return pkg_version("docling")
    except PackageNotFoundError:
        return "Not installed"


def get_document_converter(ocr: bool = True, table_structure: bool = True) -> Any:
    """Lazily initialize and return a cached DocumentConverter instance."""
    global _CACHED_CONVERTER, _CACHED_OPTIONS

    _patch_omegaconf_for_windows()

    current_options = (ocr, table_structure)
    if _CACHED_CONVERTER is not None and current_options == _CACHED_OPTIONS:
        return _CACHED_CONVERTER

    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        raise RuntimeError(
            "Docling is not installed in this Python environment. "
            "Install it via: pip install docling"
        ) from exc

    try:
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = ocr
        pipeline_options.do_table_structure = table_structure

        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
        _CACHED_CONVERTER = converter
        _CACHED_OPTIONS = current_options
        return _CACHED_CONVERTER
    except Exception as exc:
        # Fallback to default DocumentConverter if custom pipeline options fail
        logger.warning("Custom Docling pipeline init failed, falling back to default: %s", exc)
        from docling.document_converter import DocumentConverter
        _CACHED_CONVERTER = DocumentConverter()
        _CACHED_OPTIONS = (True, True)
        return _CACHED_CONVERTER


def convert_with_docling(item: QueueItem, options: ConversionOptions) -> str:
    """Convert a queue item using IBM Docling into Markdown.

    Handles local files and remote URLs.
    """
    if not is_docling_available():
        raise RuntimeError(
            "Docling engine selected, but 'docling' is not installed in this environment. "
            "Please install it with: pip install docling\n"
            "Or switch back to the 'MarkItDown' engine for instant programmatic conversion."
        )

    converter = get_document_converter(
        ocr=getattr(options, "docling_ocr", True),
        table_structure=getattr(options, "docling_table_structure", True),
    )

    try:
        with _CONVERSION_LOCK:
            source_input = Path(item.source) if os.path.exists(item.source) else item.source
            conv_res = converter.convert(source_input)
            markdown_text = conv_res.document.export_to_markdown()
            return markdown_text
    except Exception as exc:
        raise RuntimeError(f"Docling conversion failed for '{item.display_name}': {exc}") from exc
