"""The save folder: where auto-save writes converted Markdown.

Downloads by default. A different folder can be chosen in the desktop app's
Settings, through the system's folder dialog only: the choice is stored in
settings.json under "save_dir" and is deliberately absent from the HTTP
settings payload, so no web page or API request can make InkDoc write
somewhere else.

A chosen folder that is missing or cannot be written to never loses a file:
the Markdown goes to Downloads and the caller gets a sentence saying so. The
choice itself is kept, so a folder on a drive that was unplugged works again
once it is back.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from app.core import converter
from app.core.engine_manager import EngineManager
from app.core.queue_model import QueueItem

logger = logging.getLogger(__name__)

SAVE_DIR_KEY = "save_dir"


def configured_save_dir() -> Path | None:
    """The folder chosen in Settings, or None for the default (Downloads).

    Only an absolute path counts; anything else can only come from a
    hand-edited settings.json and is treated as no choice.
    """
    value = EngineManager.get_instance().get_settings().get(SAVE_DIR_KEY)
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        return None
    return Path(value)


def _redirected(folder: Path) -> bool:
    """True when the folder no longer resolves to itself.

    The dialog stores the resolved path, so a difference means the folder, or a
    folder above it, was replaced by a link or junction after it was chosen.
    is_symlink() alone would miss both a junction and a replaced parent.
    """
    try:
        real = folder.resolve()
    except Exception:
        # Unsure (a link loop raises RuntimeError on Python 3.10-3.12): treat it
        # as redirected, so the file goes to Downloads rather than somewhere unknown.
        return True
    return os.path.normcase(str(real)) != os.path.normcase(str(folder))


def folder_state() -> dict[str, Any]:
    """What Settings shows: the folder files go to now, and why if it is not the chosen one.

    Cheap checks only (two stats); nothing is written. The real test is the
    write itself, in save_markdown().
    """
    downloads = converter.get_downloads_dir()
    chosen = configured_save_dir()
    if chosen is None:
        return {"path": str(downloads), "chosen": "", "custom": False, "problem": ""}
    if not chosen.is_dir():
        problem = "This folder was not found. Files are saved to Downloads until it is back."
    elif _redirected(chosen):
        problem = "This folder is now a link to another place. Files are saved to Downloads instead."
    elif not os.access(chosen, os.W_OK):
        problem = "InkDoc can't write to this folder. Files are saved to Downloads instead."
    else:
        problem = ""
    return {
        "path": str(downloads if problem else chosen),
        "chosen": str(chosen),
        "custom": True,
        "problem": problem,
    }


def check_usable(folder: Path) -> str:
    """Why `folder` cannot be the save folder, or "" when it can.

    Tries an actual write: permission bits say little on Windows (ACLs) and
    nothing about macOS privacy prompts or read-only mounts.
    """
    if not folder.is_absolute():
        return "Choose a full folder path."
    if not folder.is_dir():
        return "That folder doesn't exist."
    try:
        fd, probe = tempfile.mkstemp(prefix=".inkdoc-write-test-", dir=str(folder))
        os.close(fd)
        os.unlink(probe)
    except OSError:
        return "InkDoc can't write to that folder."
    return ""


def store_save_dir(value: str) -> bool:
    """Store the save folder ("" for Downloads). False when it could not be saved for next time.

    update_settings() keeps the value in memory even when the file write fails,
    so the file is read back to tell the user the truth.
    """
    mgr = EngineManager.get_instance()
    mgr.update_settings({SAVE_DIR_KEY: value})
    try:
        stored = json.loads(mgr.get_settings_file_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(stored, dict) and stored.get(SAVE_DIR_KEY, "") == value


def save_markdown(item: QueueItem, markdown_text: str) -> tuple[Path, str | None]:
    """Auto-save to the save folder; fall back to Downloads, with a sentence saying why.

    A missing folder is never written to: auto_save_markdown() would re-create
    it, which would quietly bring back a deleted folder or a path on an
    unplugged drive. If Downloads fails too, that error is raised as before.
    """
    chosen = configured_save_dir()
    if chosen is None:
        return converter.auto_save_markdown(item, markdown_text), None

    if not chosen.is_dir():
        reason = "was not found"
    elif _redirected(chosen):
        reason = "is a link to another place"
    else:
        try:
            return converter.auto_save_markdown(item, markdown_text, chosen), None
        except (OSError, ValueError) as exc:
            logger.warning("Could not write to the save folder (%s); saved to Downloads.", type(exc).__name__)
            logger.debug("Save folder %s: %s", chosen, exc)
            reason = "can't be written to"

    path = converter.auto_save_markdown(item, markdown_text)
    return path, f"Saved to Downloads because the save folder {chosen} {reason}."
