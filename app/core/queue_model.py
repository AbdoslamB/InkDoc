"""Queue data model backing document conversion."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ItemStatus(Enum):
    """Conversion item lifecycle status."""

    PENDING = "status.pending"
    CONVERTING = "status.converting"
    DONE = "status.done"
    ERROR = "status.error"


class SourceKind(Enum):
    """Origin kind of conversion source."""

    FILE = "File"
    URL = "URL"


class EngineKind(str, Enum):
    """Supported conversion engines.

    AUTO is a routing mode, never a converter: app/core/auto_engine.py resolves it
    to one of the concrete engines per file, and the engine that actually produced
    the output is always reported as a concrete value (`engine_used`).
    """

    MARKITDOWN = "markitdown"
    DOCLING = "docling"
    MARKIT = "markit"
    GLM_OCR = "glm_ocr"
    AUTO = "auto"


@dataclass
class QueueItem:
    """A single entry in the conversion queue.

    For FILE items, `source` is an absolute local path.
    For URL items, `source` is the raw URL string.
    """

    source: str
    kind: SourceKind
    display_name: str
    detected_type: str = ""
    status: ItemStatus = ItemStatus.PENDING
    error_message: str = ""
    markdown_result: str | None = None
    engine: EngineKind = EngineKind.MARKITDOWN
    # Per-item conversion overrides (extension/mimetype/charset hints)
    extension_override: str = ""
    mimetype_override: str = ""
    charset_override: str = ""
    # Result of the automatic save-to-Downloads step (set after conversion).
    saved_path: str = ""
    save_error: str = ""

    def display_source(self) -> str:
        return self.display_name
