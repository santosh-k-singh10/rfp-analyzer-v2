"""
core/edit/repository.py
------------------------
Repository abstraction for the working-copy JSON documents.

Design (spec §3.1, D6):
- One asyncio.Lock per job_id — serialises concurrent mutations.
- All writes go through atomic_write to avoid partial-write corruption.
- Logs are stored in a separate ``{job_output_dir}/edit_log.jsonl`` file
  (append-only JSON lines).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

import structlog

from core.utils import atomic_write

log = structlog.get_logger(__name__)


@runtime_checkable
class ResultRepository(Protocol):
    """Protocol that all repository implementations must satisfy."""

    async def load(self, job_id: str) -> Optional[dict]:
        """Return the working.json document, or None if it doesn't exist."""
        ...

    async def save(self, job_id: str, doc: dict) -> None:
        """Persist the working.json document atomically."""
        ...

    async def append_log(self, job_id: str, entry: dict) -> None:
        """Append one entry to the job's edit log (edit_log.jsonl)."""
        ...

    async def read_log(self, job_id: str) -> list[dict]:
        """Return all log entries for a job (oldest first)."""
        ...


class FileResultRepository:
    """File-based implementation of ResultRepository.

    One asyncio.Lock is held per job_id so that concurrent mutations within
    the same process are serialised.  Across processes (multi-worker) an
    external locking mechanism is required.
    """

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._locks: dict[str, asyncio.Lock] = {}

    # ── Lock management ───────────────────────────────────────────────────────

    def lock_for(self, job_id: str) -> asyncio.Lock:
        """Return (creating if needed) the per-job asyncio Lock."""
        if job_id not in self._locks:
            self._locks[job_id] = asyncio.Lock()
        return self._locks[job_id]

    # ── Paths ─────────────────────────────────────────────────────────────────

    def _working_path(self, job_id: str) -> Path:
        return self._output_dir / job_id / "working.json"

    def _log_path(self, job_id: str) -> Path:
        return self._output_dir / job_id / "edit_log.jsonl"

    # ── ResultRepository implementation ──────────────────────────────────────

    async def load(self, job_id: str) -> Optional[dict]:
        """Return the working.json document, or None if absent."""
        p = self._working_path(job_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("working_json_load_failed", job_id=job_id, error=str(exc))
            return None

    async def save(self, job_id: str, doc: dict) -> None:
        """Write *doc* to working.json atomically."""
        p = self._working_path(job_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(p, json.dumps(doc, indent=2))

    async def append_log(self, job_id: str, entry: dict) -> None:
        """Append a JSON-lines entry to edit_log.jsonl."""
        p = self._log_path(job_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception as exc:
            log.error("edit_log_append_failed", job_id=job_id, error=str(exc))

    async def read_log(self, job_id: str) -> list[dict]:
        """Return all log entries (oldest first), or [] if the file is absent."""
        p = self._log_path(job_id)
        if not p.exists():
            return []
        entries: list[dict] = []
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        except Exception as exc:
            log.error("edit_log_read_failed", job_id=job_id, error=str(exc))
        return entries
