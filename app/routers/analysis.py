"""
app/routers/analysis.py
------------------------
Analysis endpoints:
  POST /analyze   — upload one or more files, kick off pipeline, return job_id
  GET  /jobs/{id} — return job status + results when complete
  GET  /jobs      — list all jobs

Multi-doc addendum (§1):
  POST /analyze now accepts:
    - files          : list[UploadFile]   — all documents (required)
    - primary_filename : Form field        — basename of the primary RFP doc
    - roles          : Form field         — JSON object basename→role
                       e.g. '{"RFP_main.pdf":"primary","Annex_A.pdf":"annexure"}'

  Single-file compat is preserved: if only one file is sent and primary_filename
  is omitted, that file is treated as the primary document.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, List, Optional

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import get_output_dir, get_upload_dir
from app.dependencies import validate_job_id
from app.job_store import AnalysisJob, get_job_store
from core.schemas import SynthesizedOutput

log = structlog.get_logger()
router = APIRouter()


async def _run_pipeline(
    job_id: str,
    file_paths: list[str],
    primary_file_path: str,
    file_roles: dict[str, str],
) -> None:
    """Background task: run the pipeline and update job store."""
    from pipeline.graph import run_pipeline

    store = get_job_store()
    store.set_running(job_id)
    log.info("analysis_pipeline_start", job_id=job_id, files=len(file_paths))

    try:
        result: SynthesizedOutput = await run_pipeline(
            file_paths=file_paths,
            primary_file_path=primary_file_path,
            file_roles=file_roles,
            job_id=job_id,
        )
        result_dict = result.model_dump()
        log.info("analysis_pipeline_complete", job_id=job_id)

        # ── Persist output files to disk ─────────────────────────────────────
        job_output_dir = get_output_dir() / job_id
        job_output_dir.mkdir(parents=True, exist_ok=True)

        # Write JSON (fatal — propagates if it fails)
        (job_output_dir / "result.json").write_text(
            json.dumps(result_dict, indent=2), encoding="utf-8"
        )
        log.info("output_json_written", job_id=job_id, path=str(job_output_dir / "result.json"))

        # Write Markdown (non-fatal)
        try:
            from app.markdown_renderer import render_markdown
            md_content = render_markdown(result, primary_file_path)
            (job_output_dir / "result.md").write_text(md_content, encoding="utf-8")
            log.info("output_md_written", job_id=job_id, path=str(job_output_dir / "result.md"))
        except Exception as md_exc:
            log.warning("output_md_write_failed", job_id=job_id, error=str(md_exc))

        # [0.4 HOOK: bootstrap working copy here]
        try:
            from core.edit.service import bootstrap_working_copy
            from core.utils import atomic_write
            working_doc = bootstrap_working_copy(result_dict, job_id)
            atomic_write(job_output_dir / "working.json", json.dumps(working_doc, indent=2))
            log.info("working_json_written", job_id=job_id)
        except Exception as boot_exc:
            # Non-fatal: the lazy path in GET /jobs/{id} covers the gap
            log.warning("working_copy_bootstrap_failed", job_id=job_id, error=str(boot_exc))

        # Record output dir on the job and mark complete — must be last
        if job := store.get_job(job_id):
            job.output_dir = str(job_output_dir)
        store.set_complete(job_id, result_dict)

    except Exception as exc:
        log.error("analysis_pipeline_failed", job_id=job_id, error=str(exc))
        store.set_failed(job_id, str(exc))


@router.post("/analyze")
async def analyze(
    background_tasks: BackgroundTasks,
    files: List[UploadFile],
    primary_filename: Optional[str] = Form(default=None),
    roles: Optional[str] = Form(default=None),
) -> dict[str, Any]:
    """
    Upload one or more RFP documents and kick off the analysis pipeline.

    Accepts PDF, DOCX, TXT, XLSX, or XLS files.
    Returns immediately with a job_id; poll GET /jobs/{job_id} for results.

    Form fields
    -----------
    files : list[UploadFile]
        One or more documents. For a single-file upload this works exactly
        as before (no breaking change).
    primary_filename : str, optional
        Basename of the primary RFP document within the uploaded set.
        Defaults to the first file when omitted.
    roles : str (JSON), optional
        JSON object mapping each file's basename to a role string:
        "primary" | "annexure" | "qna".
        Example: '{"RFP_main.pdf":"primary","QnA.xlsx":"qna"}'
        Defaults all files to "primary" when omitted.
    """
    allowed_extensions = {".pdf", ".docx", ".doc", ".txt", ".xlsx", ".xls"}

    if not files:
        raise HTTPException(status_code=400, detail="At least one file must be uploaded.")

    # Parse role map from JSON form field (if provided)
    role_map: dict[str, str] = {}
    if roles:
        try:
            role_map = json.loads(roles)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="'roles' must be a valid JSON object.")

    # Create a shared upload folder for this job
    job_folder = get_upload_dir() / str(uuid.uuid4())
    job_folder.mkdir(parents=True, exist_ok=True)

    saved_paths: list[str] = []
    saved_names: list[str] = []

    for upload in files:
        fname = upload.filename or f"upload_{len(saved_paths)}"
        file_ext = Path(fname).suffix.lower()
        if file_ext not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '{file_ext}' in '{fname}'. "
                       f"Supported: {', '.join(allowed_extensions)}",
            )
        dest = job_folder / fname
        with open(dest, "wb") as f:
            shutil.copyfileobj(upload.file, f)
        saved_paths.append(str(dest))
        saved_names.append(fname)
        log.info("file_uploaded", filename=fname, path=str(dest))

    # Resolve primary file
    if primary_filename and primary_filename in saved_names:
        primary_name = primary_filename
    else:
        primary_name = saved_names[0]

    primary_path = str(job_folder / primary_name)

    # Build final role map — default everything to "primary" unless overridden
    final_roles: dict[str, str] = {name: role_map.get(name, "primary") for name in saved_names}
    # Ensure the designated primary is always "primary"
    final_roles[primary_name] = "primary"

    # Create job
    store = get_job_store()
    job = store.create_job(
        file_name=primary_name,
        file_path=primary_path,
        file_names=saved_names,
        file_paths=saved_paths,
        file_roles=final_roles,
        primary_file_name=primary_name,
        primary_file_path=primary_path,
    )

    # Schedule pipeline in background
    background_tasks.add_task(
        _run_pipeline,
        job.job_id,
        saved_paths,
        primary_path,
        final_roles,
    )

    return {
        "job_id": job.job_id,
        "status": job.status,
        "primary_file": primary_name,
        "file_count": len(saved_paths),
        "message": "Analysis started. Poll GET /jobs/{job_id} for results.",
    }


@router.get("/jobs/{job_id}")
async def get_job(job_id: str = Depends(validate_job_id), view: Optional[str] = None) -> dict[str, Any]:
    """
    Return the status and results of an analysis job.

    Query parameters:
    - ``view=original``: return the bare baseline from result.json directly.

    When status is 'complete' and working.json exists, returns the working copy
    with ``edit_state`` included.  For legacy jobs (no working.json), returns the
    in-memory deterministic bootstrap (uids present, ``_baselines`` stripped,
    ``has_edits=false``) — never the bare baseline, never writes to disk (r2-1).
    """
    from core.edit.service import bootstrap_working_copy, compute_edit_state

    store = get_job_store()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    response: dict[str, Any] = {
        "job_id": job.job_id,
        "status": job.status,
        "file_name": job.file_name,
        "file_names": job.file_names,
        "primary_file_name": job.primary_file_name,
        "file_count": len(job.file_names),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    if job.error:
        response["error"] = job.error
    if job.output_dir:
        response["output_files"] = {
            "json": f"{job.output_dir}/result.json",
            "markdown": f"{job.output_dir}/result.md",
        }

    # Only add result/edit_state for complete jobs
    if job.status == "complete" and job.output_dir:
        output_dir = Path(job.output_dir)
        result_path = output_dir / "result.json"
        working_path = output_dir / "working.json"

        if view == "original":
            # Return bare baseline from result.json (no edit_state)
            if result_path.exists():
                try:
                    response["result"] = json.loads(result_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    log.warning("result_json_read_failed", job_id=job_id, error=str(exc))
        else:
            # Return working copy if available, else in-memory deterministic bootstrap
            if working_path.exists():
                try:
                    working_doc = json.loads(working_path.read_text(encoding="utf-8"))
                    # Strip _baselines before sending to client (amendment 13)
                    client_doc = {k: v for k, v in working_doc.items() if k != "_baselines"}
                    response["result"] = client_doc
                    response["edit_state"] = compute_edit_state(working_doc)
                except Exception as exc:
                    log.warning("working_json_read_failed", job_id=job_id, error=str(exc))
                    # Fall through to legacy path
            elif result_path.exists():
                # Legacy job — no working.json: return deterministic in-memory bootstrap
                # Never writes to disk (r2-1, C4)
                try:
                    baseline = json.loads(result_path.read_text(encoding="utf-8"))
                    bootstrap = bootstrap_working_copy(baseline, f"legacy:{job_id}")
                    # Strip _baselines before sending to client
                    client_doc = {k: v for k, v in bootstrap.items() if k != "_baselines"}
                    response["result"] = client_doc
                    response["edit_state"] = compute_edit_state(bootstrap)
                except Exception as exc:
                    log.warning("legacy_bootstrap_failed", job_id=job_id, error=str(exc))
    elif job.result:
        # In-memory result (old code path, fallback)
        response["result"] = job.result

    return response


@router.get("/jobs/{job_id}/markdown", response_class=PlainTextResponse)
async def get_job_markdown(job_id: str = Depends(validate_job_id)) -> str:
    """Return the Markdown analysis report for a completed job."""
    store = get_job_store()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job.status != "complete":
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' is not complete yet")
    if not job.output_dir:
        raise HTTPException(status_code=404, detail=f"No output files for job '{job_id}'")
    md_path = Path(job.output_dir) / "result.md"
    if not md_path.exists():
        raise HTTPException(status_code=404, detail="Markdown file not found for this job")
    return md_path.read_text(encoding="utf-8")


@router.get("/jobs/{job_id}/items/{uid}/baseline")
async def get_item_baseline(
    job_id: str = Depends(validate_job_id),
    uid: str = "",
) -> dict[str, Any]:
    """Return the AI baseline for a specific item from working.json's ``_baselines`` map.

    Returns 404 for user-added items (no baseline) or if the uid is absent.
    For legacy jobs without working.json, uses the in-memory deterministic bootstrap.
    """
    from core.edit.service import bootstrap_working_copy

    store = get_job_store()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job.status != "complete":
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' is not complete yet")
    if not job.output_dir:
        raise HTTPException(status_code=404, detail=f"No output files for job '{job_id}'")

    output_dir = Path(job.output_dir)
    working_path = output_dir / "working.json"

    baselines: dict[str, Any] = {}

    if working_path.exists():
        try:
            working_doc = json.loads(working_path.read_text(encoding="utf-8"))
            baselines = working_doc.get("_baselines", {})
        except Exception as exc:
            log.warning("working_json_read_failed", job_id=job_id, error=str(exc))
    else:
        result_path = output_dir / "result.json"
        if result_path.exists():
            try:
                baseline_dict = json.loads(result_path.read_text(encoding="utf-8"))
                bootstrap = bootstrap_working_copy(baseline_dict, f"legacy:{job_id}")
                baselines = bootstrap.get("_baselines", {})
            except Exception as exc:
                log.warning("legacy_bootstrap_failed", job_id=job_id, error=str(exc))

    if uid not in baselines:
        raise HTTPException(
            status_code=404,
            detail=f"No baseline found for uid '{uid}' (may be a user-added item)",
        )

    return {"uid": uid, "baseline": baselines[uid]}


@router.get("/jobs")
async def list_jobs() -> dict[str, Any]:
    """Return a summary list of all analysis jobs (newest first)."""
    store = get_job_store()
    jobs = store.list_jobs()
    job_list = []
    for j in jobs:
        entry: dict[str, Any] = {
            "job_id": j.job_id,
            "status": j.status,
            "file_name": j.file_name,
            "file_count": len(j.file_names),
            "created_at": j.created_at,
        }
        if j.output_dir:
            entry["output_files"] = {
                "json": f"{j.output_dir}/result.json",
                "markdown": f"{j.output_dir}/result.md",
            }
        job_list.append(entry)
    return {"jobs": job_list}
