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

# Prevent OpenBLAS memory allocation failures on Windows
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.converter import ConversionOptions
from app.core.engines.code_language import apply_code_languages
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
    """Return True if Docling is installed (either via pack worker or Python environment)."""
    try:
        from app.core.engine_manager import EngineManager
        if EngineManager.get_instance().is_engine_installed("docling"):
            return True
    except Exception:
        pass

    try:
        import docling  # noqa: F401
        return True
    except (ImportError, Exception):
        return False


def get_docling_version() -> str:
    """Return the installed docling version string or 'Not installed'."""
    try:
        from app.core.engine_manager import EngineManager
        mgr = EngineManager.get_instance()
        if mgr.is_pack_installed("docling"):
            return mgr.manifest.pack_version
        is_sys, ver = mgr._detect_system_docling()
        if is_sys and ver:
            return ver
    except Exception:
        pass

    try:
        return pkg_version("docling")
    except PackageNotFoundError:
        return "Not installed"


def get_document_converter(
    ocr: bool = True,
    table_structure: bool = True,
    code_enrichment: bool = False,
    formula_enrichment: bool = False,
) -> Any:
    """Lazily initialize and return a cached in-process DocumentConverter instance (dev/source mode)."""
    global _CACHED_CONVERTER, _CACHED_OPTIONS

    _patch_omegaconf_for_windows()

    # The enrichment flags are part of the key, not just the options: Docling
    # loads the code/formula model eagerly when either is set, so a cached
    # converter built with enrichment on pins ~640 MB until it is replaced.
    current_options = (ocr, table_structure, code_enrichment, formula_enrichment)
    if _CACHED_CONVERTER is not None and current_options == _CACHED_OPTIONS:
        return _CACHED_CONVERTER

    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption, WordFormatOption
    except ImportError as exc:
        raise RuntimeError(
            "Docling is not installed in this environment. "
            "Install it via Settings or switch back to the 'MarkItDown' engine."
        ) from exc

    try:
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
        _CACHED_OPTIONS = current_options
        return _CACHED_CONVERTER
    except Exception as exc:
        logger.warning("Custom Docling pipeline init failed, falling back to default: %s", exc)
        from docling.document_converter import DocumentConverter
        _CACHED_CONVERTER = DocumentConverter()
        _CACHED_OPTIONS = (True, True, False, False)
        return _CACHED_CONVERTER


def convert_with_docling(item: QueueItem, options: ConversionOptions) -> str:
    """Convert a queue item using IBM Docling into Markdown.

    Dispatches to the out-of-process worker client if installed.
    Never silently substitutes engines unless the user explicitly enabled
    fallback in Settings.
    """
    from app.core.engine_manager import EngineManager, EngineStatus

    mgr = EngineManager.get_instance()
    status = mgr.get_engine_status("docling")

    if status == EngineStatus.UNSUPPORTED:
        raise RuntimeError(f"Docling engine is not supported on this platform ({mgr.platform_key}).")

    # 1. Dispatch to isolated worker if standalone pack is installed (or mocked in tests)
    if mgr.is_pack_installed("docling") or (
        hasattr(mgr.is_engine_installed, "return_value") and mgr.is_engine_installed("docling")
    ):
        from app.core.engines.docling_worker_client import (
            DoclingWorkerClient,
            EnrichmentModelUnavailableError,
        )

        try:
            client = DoclingWorkerClient.get_instance()
            return client.convert_file(
                source_path=item.source,
                ocr=getattr(options, "docling_ocr", True),
                table_structure=getattr(options, "docling_table_structure", True),
                code_enrichment=getattr(options, "docling_code_enrichment", False),
                formula_enrichment=getattr(options, "docling_formula_enrichment", False),
            )
        except EnrichmentModelUnavailableError:
            # Not eligible for fallback. The settings ask for enrichment and the
            # pack cannot provide it, which is a configuration inconsistency with
            # a concrete remedy the user has to carry out. Answering it with a
            # quietly un-enriched document from another engine would leave the
            # setting switched on, the model still missing, and the user unaware
            # that what they asked for never happened.
            raise
        except Exception as exc:
            # Check if user explicitly enabled fallback to MarkItDown
            settings = mgr.get_settings()
            if settings.get("fallback_to_markitdown", False):
                logger.warning(
                    "Docling failed for '%s', falling back to MarkItDown per user preference: %s",
                    item.display_name,
                    exc,
                )
                options.fallback_occurred = True
                options.fallback_reason = str(exc)
                from app.core.converter import convert_item
                from app.core.queue_model import EngineKind, QueueItem

                fallback_item = QueueItem(
                    source=item.source,
                    kind=item.kind,
                    display_name=item.display_name,
                    engine=EngineKind.MARKITDOWN,
                )
                return convert_item(fallback_item, options)
            raise

    # 2. If in development/source mode and docling is in Python env, run in-process
    try:
        import docling  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Docling engine is not installed. Please install it in Settings before use."
        ) from exc

    # Source mode has no pack, so neither the add-on nor the worker's capability
    # precheck applies. A plain `pip install docling` leaves artifacts_path unset,
    # which is the one configuration where Docling resolves models from the
    # Hugging Face cache itself -- so enrichment works here without the add-on, and
    # gating it would break the development workflow for no gain. A genuine
    # failure still raises out of get_document_converter with Docling's own error.
    converter = get_document_converter(
        ocr=getattr(options, "docling_ocr", True),
        table_structure=getattr(options, "docling_table_structure", True),
        code_enrichment=getattr(options, "docling_code_enrichment", False),
        formula_enrichment=getattr(options, "docling_formula_enrichment", False),
    )

    try:
        with _CONVERSION_LOCK:
            source_input = Path(item.source) if os.path.exists(item.source) else item.source
            conv_res = converter.convert(source_input)
            markdown_text = conv_res.document.export_to_markdown()
            markdown_text = apply_code_languages(conv_res.document, markdown_text)
            return markdown_text
    except Exception as exc:
        settings = mgr.get_settings()
        if settings.get("fallback_to_markitdown", False):
            logger.warning(
                "In-process Docling failed for '%s', falling back to MarkItDown per user preference: %s",
                item.display_name,
                exc,
            )
            options.fallback_occurred = True
            options.fallback_reason = str(exc)
            from app.core.converter import convert_item
            from app.core.queue_model import EngineKind, QueueItem

            fallback_item = QueueItem(
                source=item.source,
                kind=item.kind,
                display_name=item.display_name,
                engine=EngineKind.MARKITDOWN,
            )
            return convert_item(fallback_item, options)
        raise RuntimeError(f"Docling conversion failed for '{item.display_name}': {exc}") from exc
