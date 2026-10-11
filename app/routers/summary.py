"""
app/routers/summary.py
-----------------------
Summary extraction endpoints and helpers:
  POST /summary/opportunity?opp_id=&force=  — trigger fast summary pipeline for an opportunity
  GET  /summary/opportunity/{opp_id}        — return summary results by opportunity ID
  GET  /jobs/{job_id}/summary              — return summary section by job ID
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.config import get_output_dir, get_upload_dir
from app.dependencies import validate_job_id
from app.job_store import AnalysisJob, JobStatus, get_job_store
from core.schemas import RfpSummaryOutput

log = structlog.get_logger()
router = APIRouter()


def _get_job_summary(job: AnalysisJob) -> dict[str, Any] | None:
    """Retrieve rfp_summary dict for a job from summary.json or result.json."""
    if not job.output_dir:
        return None
    output_dir = Path(job.output_dir)
    summary_path = output_dir / "summary.json"
    if summary_path.exists():
        try:
            return json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("summary_json_read_failed", job_id=job.job_id, error=str(exc))
    result_path = output_dir / "result.json"
    if result_path.exists():
        try:
            res = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(res, dict) and "rfp_summary" in res:
                return res["rfp_summary"]
        except Exception as exc:
            log.warning("result_json_read_failed", job_id=job.job_id, error=str(exc))
    return None


async def _run_summary_pipeline_job(
    job_id: str,
    file_paths: list[str],
    primary_file_path: str,
    file_roles: dict[str, str],
) -> None:
    """Background task: run the summary pipeline and update job store."""
    from pipeline.summary_extractor import run_summary_pipeline

    store = get_job_store()
    store.set_running(job_id)
    log.info("summary_pipeline_job_start", job_id=job_id, files=len(file_paths))

    try:
        summary_output: RfpSummaryOutput = await run_summary_pipeline(
            file_paths=file_paths,
            primary_file_path=primary_file_path,
            file_roles=file_roles,
        )
        summary_dict = summary_output.model_dump()
        log.info("summary_pipeline_job_complete", job_id=job_id)

        # Persist summary.json to disk
        job_output_dir = get_output_dir() / job_id
        job_output_dir.mkdir(parents=True, exist_ok=True)

        summary_file = job_output_dir / "summary.json"
        summary_file.write_text(
            json.dumps(summary_dict, indent=2), encoding="utf-8"
        )
        log.info("summary_json_written", job_id=job_id, path=str(summary_file))

        if job := store.get_job(job_id):
            job.output_dir = str(job_output_dir)
        store.set_complete(job_id, {"rfp_summary": summary_dict})

    except Exception as exc:
        log.error("summary_pipeline_job_failed", job_id=job_id, error=str(exc))
        store.set_failed(job_id, str(exc))


async def start_summary_analysis(
    opp_id: str,
    background_tasks: BackgroundTasks,
    force: bool = False,
) -> AnalysisJob:
    """Resolve files for *opp_id* and schedule a summary extraction pipeline run.

    Args:
        opp_id: Opportunity identifier (e.g. ``"OPP-2024-001"``).
        background_tasks: FastAPI ``BackgroundTasks`` to schedule the pipeline.
        force: When ``True``, always create a new job even if a RUNNING or
            COMPLETE job already exists for this opportunity.

    Returns:
        The :class:`~app.job_store.AnalysisJob` (new or existing).

    Raises:
        HTTPException 404: When no directory / files are found for *opp_id*.
        HTTPException 400: When the resolved directory contains no supported files.
    """
    from core.opportunity_resolver import resolve_opportunity_files

    store = get_job_store()

    # ── De-duplicate: return existing active/complete job unless forced ───────
    if not force:
        existing = store.get_job_by_opp_id(opp_id)
        if existing and existing.status in (JobStatus.RUNNING, JobStatus.COMPLETE, "running", "complete"):
            log.info(
                "summary_analysis_reuse",
                opp_id=opp_id,
                job_id=existing.job_id,
                status=existing.status,
            )
            return existing

    # ── Resolve files from storage ────────────────────────────────────────────
    try:
        opp_files = resolve_opportunity_files(opp_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not opp_files.file_paths:
        raise HTTPException(
            status_code=400,
            detail=f"No supported documents found for opportunity '{opp_id}'.",
        )

    # ── Copy files into an isolated upload directory for this job ─────────────
    upload_base = get_upload_dir()
    upload_base.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(dir=upload_base, prefix=f"opp_sum_{opp_id}_"))

    saved_paths: list[str] = []
    saved_names: list[str] = []
    final_roles: dict[str, str] = {}

    for src in opp_files.file_paths:
        dest = stage_dir / src.name
        shutil.copy2(src, dest)
        saved_paths.append(str(dest))
        saved_names.append(src.name)
        final_roles[src.name] = opp_files.file_roles.get(src.name, "primary")
        log.info("opp_sum_file_staged", opp_id=opp_id, filename=src.name, dest=str(dest))

    primary_src = opp_files.primary_file_path
    primary_name = primary_src.name if primary_src else saved_names[0]
    primary_path = str(stage_dir / primary_name)
    final_roles[primary_name] = "primary"

    # ── Create job ────────────────────────────────────────────────────────────
    job = store.create_job(
        file_name=primary_name,
        file_path=primary_path,
        file_names=saved_names,
        file_paths=saved_paths,
        file_roles=final_roles,
        primary_file_name=primary_name,
        primary_file_path=primary_path,
        opp_id=opp_id,
    )

    # Rename staging dir to match job_id for clear traceability
    final_upload_dir = upload_base / job.job_id
    stage_dir.rename(final_upload_dir)

    # Update paths to reflect the renamed directory
    job.file_paths = [str(final_upload_dir / Path(p).name) for p in saved_paths]
    job.primary_file_path = str(final_upload_dir / primary_name)
    job.file_path = job.primary_file_path

    final_file_paths = job.file_paths
    final_primary_path = job.primary_file_path

    # ── Schedule pipeline ─────────────────────────────────────────────────────
    background_tasks.add_task(
        _run_summary_pipeline_job,
        job.job_id,
        final_file_paths,
        final_primary_path,
        final_roles,
    )

    log.info(
        "summary_analysis_started",
        opp_id=opp_id,
        job_id=job.job_id,
        file_count=len(final_file_paths),
    )
    return job


@router.post("/summary/opportunity")
async def analyze_opportunity_summary(
    background_tasks: BackgroundTasks,
    opp_id: str,
    force: bool = False,
) -> dict[str, Any]:
    """Trigger summary extraction for an opportunity ID.

    Query parameters
    ----------------
    opp_id : str
        Opportunity identifier (e.g. ``"OPP-2024-001"``).
    force : bool, optional
        When ``True``, always creates a new job even if one is already running
        or complete for this opportunity. Default ``False``.

    Returns immediately with the job ID and current status.
    """
    job = await start_summary_analysis(
        opp_id=opp_id,
        background_tasks=background_tasks,
        force=force,
    )
    status_str = job.status.value if hasattr(job.status, "value") else str(job.status)
    return {
        "job_id": job.job_id,
        "status": status_str,
        "opp_id": opp_id,
    }


@router.get("/summary/opportunity/{opp_id}")
async def get_summary_by_opp_id(opp_id: str) -> dict[str, Any]:
    """Return the summary output for the given opportunity ID.

    Returns 404 when no job has been started for this opportunity.
    """
    store = get_job_store()
    job = store.get_job_by_opp_id(opp_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"No job found for opportunity '{opp_id}'",
        )

    status_str = job.status.value if hasattr(job.status, "value") else str(job.status)
    summary_data = _get_job_summary(job)

    response: dict[str, Any] = {
        "job_id": job.job_id,
        "opp_id": job.opp_id or opp_id,
        "status": status_str,
        "rfp_summary": summary_data,
    }
    if job.error:
        response["error"] = job.error
    return response


@router.get("/jobs/{job_id}/summary")
async def get_job_summary(job_id: str = Depends(validate_job_id)) -> dict[str, Any]:
    """Return the summary section for a given job ID.

    Returns 404 when the job does not exist.
    """
    store = get_job_store()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    status_str = job.status.value if hasattr(job.status, "value") else str(job.status)
    summary_data = _get_job_summary(job)

    response: dict[str, Any] = {
        "job_id": job.job_id,
        "status": status_str,
        "rfp_summary": summary_data,
    }
    if job.opp_id:
        response["opp_id"] = job.opp_id
    if job.error:
        response["error"] = job.error
    return response
