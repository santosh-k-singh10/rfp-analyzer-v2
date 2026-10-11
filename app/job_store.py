"""
app/job_store.py
-----------------
In-memory (and file-based) job store for analysis jobs.

Each job has:
- job_id: UUID string
- status: pending | running | complete | failed
- result: SynthesizedOutput (when complete)
- error: str (when failed)
- file_name: original uploaded filename
- created_at: ISO timestamp

The store is process-local (in-memory dict). Jobs are persisted to
``{OUTPUT_DIR}/{job_id}/job.json`` at every status transition so they
survive process restarts.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import structlog
from pydantic import BaseModel

from core.utils import atomic_write

log = structlog.get_logger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class AnalysisJob(BaseModel):
    """Represents a single analysis job."""

    job_id: str
    status: JobStatus = JobStatus.PENDING
    # Single-file compat fields (primary document)
    file_name: str = ""
    file_path: str = ""
    # Multi-file metadata (multi-doc addendum)
    file_names: list[str] = []         # all uploaded filenames (display)
    file_paths: list[str] = []         # all uploaded file paths
    file_roles: dict[str, str] = {}    # basename → role tag
    primary_file_name: str = ""        # basename of primary document
    primary_file_path: str = ""        # full path of primary document
    created_at: str = ""
    updated_at: str = ""
    result: Optional[dict[str, Any]] = None  # no longer populated after 0.4 (amendment 14)
    error: Optional[str] = None
    output_dir: Optional[str] = None          # path to {OUTPUT_DIR}/{job_id}/
    opp_id: Optional[str] = None              # opportunity ID (if triggered via opp flow)


class JobStore:
    """In-memory job store with file-based persistence."""

    def __init__(self) -> None:
        self._jobs: dict[str, AnalysisJob] = {}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _write_job_json(self, job: AnalysisJob) -> None:
        """Write ``job.json`` sidecar for *job* using atomic_write."""
        if not job.output_dir:
            return
        out_dir = Path(job.output_dir)
        payload = {
            "job_id": job.job_id,
            "status": job.status.value,
            "file_name": job.file_name,
            "file_names": job.file_names,
            "primary_file_name": job.primary_file_name,
            "primary_file_path": job.primary_file_path,
            "file_roles": job.file_roles,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "error": job.error,
            "opp_id": job.opp_id,
        }
        atomic_write(out_dir / "job.json", json.dumps(payload, indent=2))

    # ── Public API ────────────────────────────────────────────────────────────

    def create_job(
        self,
        file_name: str,
        file_path: str,
        file_names: list[str] | None = None,
        file_paths: list[str] | None = None,
        file_roles: dict[str, str] | None = None,
        primary_file_name: str = "",
        primary_file_path: str = "",
        opp_id: Optional[str] = None,
    ) -> AnalysisJob:
        """Create a new job, write its output directory and initial job.json.

        Accepts both single-file (file_name/file_path) and multi-file
        (file_names/file_paths/file_roles) arguments. Single-file params
        are kept for backward compatibility with the single-upload code path.
        """
        from app.config import get_output_dir

        now = datetime.utcnow().isoformat()
        job_id = str(uuid.uuid4())
        output_dir = get_output_dir() / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        job = AnalysisJob(
            job_id=job_id,
            status=JobStatus.PENDING,
            file_name=file_name,
            file_path=file_path,
            file_names=file_names or ([file_name] if file_name else []),
            file_paths=file_paths or ([file_path] if file_path else []),
            file_roles=file_roles or {},
            primary_file_name=primary_file_name or file_name,
            primary_file_path=primary_file_path or file_path,
            created_at=now,
            updated_at=now,
            output_dir=str(output_dir),
            opp_id=opp_id,
        )
        self._jobs[job.job_id] = job
        try:
            self._write_job_json(job)
        except Exception as exc:
            log.warning("job_json_write_failed_on_create", job_id=job_id, error=str(exc))
        return job

    def get_job(self, job_id: str) -> Optional[AnalysisJob]:
        """Return the job with the given ID, or None."""
        return self._jobs.get(job_id)

    def set_running(self, job_id: str) -> None:
        """Mark a job as running and persist job.json."""
        if job := self._jobs.get(job_id):
            job.status = JobStatus.RUNNING
            job.updated_at = datetime.utcnow().isoformat()
            try:
                self._write_job_json(job)
            except Exception as exc:
                log.warning("job_json_write_failed_set_running", job_id=job_id, error=str(exc))

    def set_complete(self, job_id: str, result: dict[str, Any]) -> None:
        """Mark a job as complete.

        Write order (spec F12, §4.6, r2-3):
        1. Write job.json with status:complete atomically.
        2. Only on success flip in-memory status to COMPLETE.

        If the job.json write fails the exception propagates and the in-memory
        status stays RUNNING (the caller's exception handler will call
        set_failed).
        """
        if job := self._jobs.get(job_id):
            job.updated_at = datetime.utcnow().isoformat()
            # Temporarily stage the new status for the JSON payload
            original_status = job.status
            job.status = JobStatus.COMPLETE
            try:
                self._write_job_json(job)
            except Exception:
                # Restore in-memory status — do not flip
                job.status = original_status
                raise
            # json written — safe to commit in-memory
            # result is no longer stored in-memory (amendment 14); reads go through repository

    def set_failed(self, job_id: str, error: str) -> None:
        """Mark a job as failed (r3-4).

        In-memory status is flipped to FAILED *first*; then job.json is
        written.  If the write fails it is logged and execution continues
        (the in-memory state is already FAILED so nothing is stuck).
        """
        if job := self._jobs.get(job_id):
            job.status = JobStatus.FAILED
            job.error = error
            job.updated_at = datetime.utcnow().isoformat()
            try:
                self._write_job_json(job)
            except Exception as exc:
                log.error(
                    "job_json_write_failed_set_failed",
                    job_id=job_id,
                    error=str(exc),
                )

    def list_jobs(self) -> list[AnalysisJob]:
        """Return all jobs ordered by creation time (newest first)."""
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get_job_by_opp_id(self, opp_id: str) -> Optional[AnalysisJob]:
        """Return the most recent active/complete job for *opp_id*, or None.

        Preference order (descending priority):
        1. RUNNING  — an in-flight job should be reused immediately.
        2. COMPLETE — a finished job is returned if no active job exists.
        3. PENDING  — a queued but not-yet-started job.
        4. FAILED   — only returned if no other status is available.

        Within the same priority tier the most recently *created* job wins.
        """
        candidates = [j for j in self._jobs.values() if j.opp_id == opp_id]
        if not candidates:
            return None

        _priority = {
            JobStatus.RUNNING: 0,
            JobStatus.COMPLETE: 1,
            JobStatus.PENDING: 2,
            JobStatus.FAILED: 3,
        }
        # Find the highest-priority tier present, then return the most recently
        # created job within that tier.
        best_tier = min(_priority.get(j.status, 9) for j in candidates)
        tier_jobs = [j for j in candidates if _priority.get(j.status, 9) == best_tier]
        return max(tier_jobs, key=lambda j: j.created_at)

    # ── Disk persistence ──────────────────────────────────────────────────────

    def load_from_disk(self, output_dir: Path) -> None:
        """Scan ``output_dir/*/job.json`` and rehydrate jobs into memory.

        Called once at startup (lifespan hook).  Results are **not** loaded
        into memory — only job metadata is restored.

        Transition rules:
        - ``running``  → ``failed: "interrupted by restart"``
        - ``complete`` + no result.json → ``failed: "result file missing"``
        - ``complete`` + result.json present → restored as complete
        - ``pending``  → restored as-is (user may retry)
        - ``failed``   → restored as-is
        """
        if not output_dir.is_dir():
            return

        for job_json_path in output_dir.glob("*/job.json"):
            try:
                data = json.loads(job_json_path.read_text(encoding="utf-8"))
                job_id = data.get("job_id", "")
                if not job_id:
                    continue

                job_output_dir = job_json_path.parent
                status_str = data.get("status", "failed")

                # Determine effective status
                if status_str == "running":
                    status_str = "failed"
                    data["error"] = "interrupted by restart"
                elif status_str == "complete":
                    if not (job_output_dir / "result.json").exists():
                        status_str = "failed"
                        data["error"] = "result file missing after restart"

                job = AnalysisJob(
                    job_id=job_id,
                    status=JobStatus(status_str),
                    file_name=data.get("file_name", ""),
                    file_path="",
                    file_names=data.get("file_names", []),
                    file_paths=[],
                    file_roles=data.get("file_roles", {}),
                    primary_file_name=data.get("primary_file_name", ""),
                    primary_file_path=data.get("primary_file_path", ""),
                    created_at=data.get("created_at", ""),
                    updated_at=data.get("updated_at", ""),
                    error=data.get("error"),
                    output_dir=str(job_output_dir),
                    opp_id=data.get("opp_id"),
                )
                self._jobs[job_id] = job
                log.info("job_restored_from_disk", job_id=job_id, status=status_str)
            except Exception as exc:
                log.warning(
                    "job_restore_failed",
                    path=str(job_json_path),
                    error=str(exc),
                )

        # Legacy jobs (pre-job.json): folders with only result.json → complete
        for result_path in output_dir.glob("*/result.json"):
            job_output_dir = result_path.parent
            job_id = job_output_dir.name
            if job_id in self._jobs or (job_output_dir / "job.json").exists():
                continue
            try:
                uuid.UUID(job_id)
            except ValueError:
                continue
            ts = datetime.utcfromtimestamp(result_path.stat().st_mtime).isoformat()
            self._jobs[job_id] = AnalysisJob(
                job_id=job_id,
                status=JobStatus.COMPLETE,
                created_at=ts,
                updated_at=ts,
                output_dir=str(job_output_dir),
            )
            log.info("legacy_job_restored_from_disk", job_id=job_id)


# Process-level singleton
_store = JobStore()


def get_job_store() -> JobStore:
    """Return the process-level job store singleton."""
    return _store
