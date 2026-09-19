"""Markit (Broad Format Support) conversion engine wrapper.

Inspired by mitdown.ca's broad format support backend powered by @shiftlabs/markit.
Provides broad format support covering EPUB ebooks, YAML, Jupyter Notebooks (.ipynb),
XML/RSS feeds, data formats, and URLs.

Provides cross-platform execution:
- Invokes native `markit` CLI / `npx @shiftlabs/markit` on macOS, Linux, or WSL.
- Provides robust, zero-dependency Python broad-format extractors for Windows
  and environments where native Rust bindings are not available.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

if TYPE_CHECKING:
    from app.core.converter import ConversionOptions
    from app.core.queue_model import QueueItem

logger = logging.getLogger(__name__)

MARKIT_VERSION = "0.6.1"


def is_markit_available() -> bool:
    """Return True if Markit CLI (or Python broad-format support) is available."""
    # Native markit command, npx, or Python broad-format handlers are always accessible
    if shutil.which("markit"):
        return True
    if shutil.which("npx"):
        return True
    return True


def get_markit_version() -> str:
    """Return the Markit version string."""
    return MARKIT_VERSION


def convert_with_markit(item: QueueItem, options: ConversionOptions) -> str:
    """Convert a queue item using Markit broad format engine into Markdown.

    Handles local files and remote URLs with fallback for cross-platform reliability.
    """
    source = item.source.strip()
    is_url = source.startswith("http://") or source.startswith("https://")

    # 1. Try native markit / npx execution if on macOS / Linux
    if sys.platform != "win32":
        cmd = None
        if shutil.which("markit"):
            cmd = ["markit", source, "-q"]
        elif shutil.which("npx"):
            cmd = ["npx", "--yes", "@shiftlabs/markit", source, "-q"]

        if cmd:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout
            except Exception as exc:
                logger.warning("Native markit invocation failed, falling back: %s", exc)

    # 2. On Windows or fallback: handle broad formats via high-speed Python extractors
    if is_url:
        return _convert_url_fallback(source)

    file_path = Path(source)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {source}")

    suffix = file_path.suffix.lower()

    # Jupyter Notebook (.ipynb)
    if suffix == ".ipynb":
        return _convert_jupyter_notebook(file_path)

    # YAML (.yaml, .yml)
    if suffix in (".yaml", ".yml"):
        return _convert_yaml(file_path)

    # EPUB (.epub)
    if suffix == ".epub":
        return _convert_epub(file_path)

    # XML / RSS / Atom
    if suffix in (".xml", ".rss", ".atom", ".svg"):
        return _convert_xml(file_path)

    # JSON (.json)
    if suffix == ".json":
        return _convert_json(file_path)

    # CSV / TSV
    if suffix in (".csv", ".tsv"):
        return _convert_tabular(file_path, delimiter="\t" if suffix == ".tsv" else ",")

    # Plain Text / Code / Markdown
    text_exts = {
        ".txt", ".md", ".markdown", ".rst", ".log", ".py", ".js", ".ts",
        ".jsx", ".tsx", ".html", ".htm", ".css", ".scss", ".sh", ".bash",
        ".bat", ".cmd", ".ps1", ".rs", ".go", ".c", ".cpp", ".h", ".java",
        ".kt", ".swift", ".sql", ".ini", ".cfg", ".conf", ".toml", ".env",
    }
    if suffix in text_exts:
        return _convert_plaintext(file_path)

    # For standard Office / PDF documents on Windows when Markit route is chosen,
    # invoke WSL if available, or fall back to MarkItDown core
    if sys.platform == "win32" and shutil.which("wsl"):
        wsl_path = _to_wsl_path(file_path)
        if wsl_path:
            try:
                res = subprocess.run(
                    ["wsl", "npx", "--yes", "@shiftlabs/markit", wsl_path, "-q"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout
            except Exception as exc:
                logger.debug("WSL markit attempt failed: %s", exc)

    # Standard fallback to MarkItDown programmatic conversion
    from app.core.converter import convert_item
    from app.core.queue_model import EngineKind, QueueItem
    fallback_item = QueueItem(
        source=item.source,
        kind=item.kind,
        display_name=item.display_name,
        engine=EngineKind.MARKITDOWN,
    )
    return convert_item(fallback_item, options)


def _convert_jupyter_notebook(file_path: Path) -> str:
    """Convert a Jupyter Notebook (.ipynb) to structured Markdown."""
    try:
        data = json.loads(file_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        raise RuntimeError(f"Failed to read Jupyter notebook: {exc}") from exc

    lines = [f"# {file_path.stem}\n"]
    cells = data.get("cells", [])
    for idx, cell in enumerate(cells, 1):
        cell_type = cell.get("cell_type", "")
        source_content = "".join(cell.get("source", [])).strip()
        if not source_content:
            continue

        if cell_type == "markdown":
            lines.append(source_content)
            lines.append("\n")
        elif cell_type == "code":
            lines.append(f"```python\n{source_content}\n```")
            # Include text outputs if present
            outputs = cell.get("outputs", [])
            output_texts = []
            for out in outputs:
                if "text" in out:
                    output_texts.append("".join(out["text"]).strip())
                elif "data" in out and "text/plain" in out["data"]:
                    output_texts.append("".join(out["data"]["text/plain"]).strip())
            if output_texts:
                lines.append(f"```output\n{chr(10).join(output_texts)}\n```")
            lines.append("\n")

    return "\n".join(lines).strip() + "\n"


def _convert_yaml(file_path: Path) -> str:
    """Convert a YAML file into readable Markdown."""
    content = file_path.read_text(encoding="utf-8", errors="replace")
    title = f"# {file_path.name}\n\n"
    return f"{title}```yaml\n{content.strip()}\n```\n"


def _convert_epub(file_path: Path) -> str:
    """Extract and convert text from an EPUB ebook archive."""
    lines = [f"# {file_path.stem}\n"]
    try:
        with zipfile.ZipFile(file_path, "r") as z:
            # Look for XHTML / HTML content files in the EPUB container
            html_files = [
                n for n in z.namelist()
                if n.lower().endswith((".xhtml", ".html", ".htm"))
                and not n.lower().endswith(("toc.ncx", "nav.xhtml"))
            ]
            html_files.sort()

            for name in html_files:
                raw_bytes = z.read(name)
                text = raw_bytes.decode("utf-8", errors="replace")
                # Strip basic XML/HTML tags
                try:
                    root = ET.fromstring(text)
                    plain = "".join(root.itertext()).strip()
                    if plain:
                        lines.append(plain)
                        lines.append("\n---\n")
                except Exception:
                    # Fallback simple regex or tag strip
                    import re
                    clean = re.sub(r"<[^>]+>", " ", text)
                    clean = re.sub(r"\s+", " ", clean).strip()
                    if clean:
                        lines.append(clean)
                        lines.append("\n---\n")

    except Exception as exc:
        raise RuntimeError(f"Failed to parse EPUB file '{file_path.name}': {exc}") from exc

    return "\n".join(lines).strip() + "\n"


def _convert_xml(file_path: Path) -> str:
    """Convert XML / RSS file to Markdown."""
    content = file_path.read_text(encoding="utf-8", errors="replace")
    try:
        root = ET.fromstring(content)
        # Check if RSS
        channel = root.find("channel")
        if channel is not None:
            title = channel.findtext("title", file_path.name)
            lines = [f"# {title}\n"]
            desc = channel.findtext("description", "")
            if desc:
                lines.append(f"*{desc}*\n")
            items = channel.findall("item")
            for it in items:
                it_title = it.findtext("title", "Untitled Item")
                it_link = it.findtext("link", "")
                it_desc = it.findtext("description", "")
                if it_link:
                    lines.append(f"### [{it_title}]({it_link})")
                else:
                    lines.append(f"### {it_title}")
                if it_desc:
                    lines.append(it_desc)
                lines.append("")
            return "\n".join(lines).strip() + "\n"
    except Exception:
        pass

    return f"# {file_path.name}\n\n```xml\n{content.strip()}\n```\n"


def _convert_json(file_path: Path) -> str:
    """Convert JSON file to formatted Markdown."""
    try:
        obj = json.loads(file_path.read_text(encoding="utf-8", errors="replace"))
        formatted = json.dumps(obj, indent=2, ensure_ascii=False)
        return f"# {file_path.name}\n\n```json\n{formatted}\n```\n"
    except Exception:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return f"# {file_path.name}\n\n```json\n{content.strip()}\n```\n"


def _convert_tabular(file_path: Path, delimiter: str = ",") -> str:
    """Convert CSV or TSV to a Markdown table."""
    import csv
    lines = [f"# {file_path.name}\n"]
    with file_path.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f, delimiter=delimiter)
        rows = list(reader)

    if not rows:
        return f"# {file_path.name}\n\n*(Empty file)*\n"

    header = rows[0]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * len(header)) + " |")
    for r in rows[1:]:
        # Pad or trim row to match header length
        cells = r[:len(header)] + [""] * max(0, len(header) - len(r))
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")

    return "\n".join(lines) + "\n"


def _convert_plaintext(file_path: Path) -> str:
    """Wrap plain text or code file into Markdown."""
    content = file_path.read_text(encoding="utf-8", errors="replace")
    ext = file_path.suffix.lstrip(".").lower()
    lang_map = {
        "py": "python", "js": "javascript", "ts": "typescript", "rs": "rust",
        "go": "go", "html": "html", "css": "css", "sh": "bash", "ps1": "powershell",
        "sql": "sql", "json": "json", "yaml": "yaml", "yml": "yaml",
    }
    lang = lang_map.get(ext, "")
    if ext in ("md", "markdown", "txt"):
        return content
    return f"# {file_path.name}\n\n```{lang}\n{content.strip()}\n```\n"


def _convert_url_fallback(url: str) -> str:
    """Fetch and convert a URL using MarkItDown fallback."""
    from app.core.converter import convert_item
    from app.core.queue_model import EngineKind, QueueItem, SourceKind
    item = QueueItem(
        source=url,
        kind=SourceKind.URL,
        display_name=url,
        engine=EngineKind.MARKITDOWN,
    )
    from app.core.converter import ConversionOptions
    return convert_item(item, ConversionOptions(engine=EngineKind.MARKITDOWN))


def _to_wsl_path(path: Path) -> str | None:
    """Convert a Windows path (C:\\path\\file) to WSL path (/mnt/c/path/file)."""
    try:
        resolved = path.resolve()
        drive = resolved.drive.rstrip(":").lower()
        if not drive:
            return None
        rel = str(resolved)[len(resolved.drive):].replace("\\", "/")
        return f"/mnt/{drive}{rel}"
    except Exception:
        return None
