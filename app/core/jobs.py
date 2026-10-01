"""Live progress and cancellation for conversion jobs.

A conversion can take more than one pass (Auto may read with MarkItDown, check,
then retry with Docling), and a bare "Converting…" hides why it is slow. The UI
sends an optional `job_id` with a conversion request, polls
GET /convert/progress/{job_id} for the current phase, and may call
POST /convert/cancel/{job_id}.

Engine-neutral by design: any engine can report phases or pages through the
same JobHandle (the GLM-OCR plan reports per-page progress here). Cancellation
is cooperative: work stops at the next phase boundary, and nothing is saved.

State lives in memory only, for at most JOB_TTL_S, and holds no document
content, just phase labels and timestamps.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

JOB_TTL_S = 3600.0
MAX_JOBS = 2000
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,100}$")


class ConversionCancelledError(Exception):
    """The job was cancelled by its caller; the endpoint answers 409 conversion_cancelled."""


def is_valid_job_id(job_id: str | None) -> bool:
    return bool(job_id) and bool(_JOB_ID_RE.match(job_id or ""))


@dataclass
class _JobState:
    job_id: str
    status: str = "running"          # running | done | error | cancelled
    phase: str = "queued"            # machine-readable phase
    message: str = "Waiting…"        # what the queue row shows
    engine: str = ""
    page: int | None = None
    pages: int | None = None
    # When page counting began: the estimate measures pages per second from
    # here, not from `started`, which also covers queueing and model loading.
    pages_started: float | None = None
    cancel_requested: bool = False
    started: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "engine": self.engine,
            "seconds_elapsed": round(now - self.started, 1),
            "cancel_requested": self.cancel_requested,
        }
        if self.page is not None and self.pages:
            data["page"] = self.page
            data["pages"] = self.pages
            # `page` counts pages already finished. No estimate before the first
            # one: the first page also pays for loading the model.
            elapsed = now - (self.pages_started or self.started)
            if self.page > 0:
                data["seconds_remaining_est"] = round(elapsed / self.page * (self.pages - self.page), 1)
        return data


class JobRegistry:
    """Thread-safe registry: written from worker threads, read by the event loop."""

    def __init__(self) -> None:
        self._jobs: dict[str, _JobState] = {}
        self._lock = threading.Lock()

    def _purge_locked(self) -> None:
        cutoff = time.time() - JOB_TTL_S
        for key in [k for k, v in self._jobs.items() if v.updated < cutoff]:
            del self._jobs[key]
        if len(self._jobs) > MAX_JOBS:
            for key, _ in sorted(self._jobs.items(), key=lambda kv: kv[1].updated)[: len(self._jobs) - MAX_JOBS]:
                del self._jobs[key]

    def start(self, job_id: str) -> JobHandle:
        """Register a job. A cancel that arrived first (a race with the request) is kept."""
        with self._lock:
            self._purge_locked()
            state = self._jobs.get(job_id)
            # A running entry is either a cancel that overtook this request (keep
            # its flag) or a client reusing an id; a finished one is replaced.
            if state is None or state.status != "running":
                self._jobs[job_id] = _JobState(job_id=job_id)
            return JobHandle(self, job_id)

    def update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return
            for key, value in changes.items():
                setattr(state, key, value)
            state.updated = time.time()

    def cancel(self, job_id: str) -> bool:
        """Request cancellation. Returns True when the job was known and still running.

        An unknown id is remembered as cancelled for a while, so a cancel that
        overtakes its own request still takes effect when the request lands.
        """
        with self._lock:
            self._purge_locked()
            state = self._jobs.get(job_id)
            if state is None:
                self._jobs[job_id] = _JobState(job_id=job_id, cancel_requested=True)
                return False
            if state.status != "running":
                return False
            state.cancel_requested = True
            state.updated = time.time()
            return True

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            state = self._jobs.get(job_id)
            return bool(state and state.cancel_requested)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._purge_locked()
            state = self._jobs.get(job_id)
            return state.to_dict() if state is not None else None

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()


class JobHandle:
    """What a conversion holds: report the phase, check for cancellation."""

    def __init__(self, registry: JobRegistry | None, job_id: str | None) -> None:
        self._registry = registry
        self.job_id = job_id

    def set_phase(self, phase: str, message: str, *, engine: str | None = None) -> None:
        if self._registry and self.job_id:
            changes: dict[str, Any] = {"phase": phase, "message": message}
            if engine is not None:
                changes["engine"] = engine
            self._registry.update(self.job_id, **changes)

    def set_pages(self, page: int, pages: int) -> None:
        """Report `page` of `pages` finished. The first call starts the page clock."""
        if self._registry and self.job_id:
            changes: dict[str, Any] = {"page": page, "pages": pages}
            if page == 0:
                changes["pages_started"] = time.time()
            self._registry.update(self.job_id, **changes)

    def cancelled(self) -> bool:
        return bool(self._registry and self.job_id and self._registry.is_cancelled(self.job_id))

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise ConversionCancelledError("conversion_cancelled")

    def finish(self, status: str) -> None:
        if self._registry and self.job_id:
            message = {"done": "Done", "error": "Failed", "cancelled": "Cancelled"}.get(status, status)
            self._registry.update(self.job_id, status=status, phase=status, message=message)


NULL_JOB = JobHandle(None, None)

# The process-wide registry the server uses.
JOBS = JobRegistry()
