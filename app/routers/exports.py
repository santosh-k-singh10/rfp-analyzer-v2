"""
app/routers/exports.py
-----------------------
Export endpoint: GET /jobs/{id}/export

Query parameters:
  format          xlsx | md | json   (default: xlsx)
  include_removed false | true        (default: false)
  view            edited | original   (default: edited)
  strip_meta      false | true        (default: false) — JSON only

Content-Disposition is RFC 5987-encoded so spaces and non-ASCII filenames
work correctly in all modern browsers.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from app.config import get_output_dir
from app.dependencies import validate_job_id
from app.job_store import get_job_store
from core.edit.repository import FileResultRepository
from core.edit.service import bootstrap_working_copy

log = structlog.get_logger(__name__)

router = APIRouter()

# ── Repository singleton (mirrors edits.py pattern) ──────────────────────────

_repo: Optional[FileResultRepository] = None


def _get_repo() -> FileResultRepository:
    global _repo
    if _repo is None:
        _repo = FileResultRepository(get_output_dir())
    return _repo


# ── Helpers ───────────────────────────────────────────────────────────────────


def _rfc5987_encode(filename: str) -> str:
    """Return a Content-Disposition value with ASCII fallback + RFC 5987 encoded name."""
    ascii_name = filename.encode("ascii", errors="replace").decode("ascii")
    # Replace any non-printable replacement chars with underscore
    ascii_name = ascii_name.replace("?", "_")
    encoded = quote(filename, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'


def _export_filename(primary_file_name: str, ext: str) -> str:
    """Build the download filename from the primary file stem."""
    stem = Path(primary_file_name).stem
    date_str = datetime.now().strftime("%Y%m%d")
    return f"{stem}_requirements_{date_str}.{ext}"


async def _load_working_doc(job_id: str, job: Any, view: str) -> dict[str, Any]:
    """Return the document to export based on *view*.

    - ``"original"`` → raw baseline from result.json (no _meta).
    - ``"edited"``   → working.json or in-memory bootstrap for legacy jobs.
    """
    output_dir = Path(job.output_dir)
    result_path = output_dir / "result.json"
    working_path = output_dir / "working.json"

    if view == "original":
        if not result_path.exists():
            raise HTTPException(status_code=404, detail="result.json not found")
        return json.loads(result_path.read_text(encoding="utf-8"))

    # Edited view
    if working_path.exists():
        return json.loads(working_path.read_text(encoding="utf-8"))

    # Legacy job — build in-memory bootstrap
    if not result_path.exists():
        raise HTTPException(status_code=404, detail="result.json not found")
    baseline = json.loads(result_path.read_text(encoding="utf-8"))
    return bootstrap_working_copy(baseline, f"legacy:{job_id}")


def _require_complete_job(job_id: str) -> Any:
    store = get_job_store()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job.status != "complete":
        raise HTTPException(
            status_code=400,
            detail=f"Job '{job_id}' is not complete (status={job.status})",
        )
    if not job.output_dir:
        raise HTTPException(status_code=400, detail=f"No output directory for job '{job_id}'")
    return job


# ── GET /jobs/{job_id}/export ─────────────────────────────────────────────────


@router.get("/jobs/{job_id}/export")
async def export_job(
    job_id: str = Depends(validate_job_id),
    format: str = Query("xlsx", description="Export format: xlsx | md | json"),
    include_removed: bool = Query(False, description="Include removed items"),
    view: str = Query("edited", description="edited | original"),
    strip_meta: bool = Query(False, description="JSON only: strip _meta, _edit, _baselines"),
) -> Response:
    """Download the job requirements in the requested format.

    - **xlsx**: Excel workbook with sheets grouped by bucket (C2).
    - **md**: Markdown report with ✎ Edited / ＋ Added markers.
    - **json**: Working document (with or without metadata).

    For ``view=original`` the baseline ``result.json`` is used; Change Log and
    edit markers are omitted.
    """
    if format not in ("xlsx", "md", "json"):
        raise HTTPException(status_code=422, detail=f"Unsupported format: {format!r}")
    if view not in ("edited", "original"):
        raise HTTPException(status_code=422, detail=f"Unsupported view: {view!r}")

    job = _require_complete_job(job_id)
    primary_file_name = job.primary_file_name or job.file_name or "requirements"

    doc = await _load_working_doc(job_id, job, view)

    # ── Excel ─────────────────────────────────────────────────────────────────
    if format == "xlsx":
        from app.exporters.xlsx import build_workbook

        log_entries: list[dict[str, Any]] = []
        if view != "original":
            repo = _get_repo()
            log_entries = await repo.read_log(job_id)

        wb = build_workbook(
            doc,
            primary_file_name=primary_file_name,
            log_entries=log_entries,
            include_removed=include_removed,
            view=view,
            job_id=job_id,
        )

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        filename = _export_filename(primary_file_name, "xlsx")
        content_disp = _rfc5987_encode(filename)

        return Response(
            content=buf.read(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": content_disp},
        )

    # ── Markdown ──────────────────────────────────────────────────────────────
    if format == "md":
        from app.markdown_renderer import render_markdown_from_doc

        if view == "original":
            # For original view, render directly without markers
            from core.schemas import SynthesizedOutput
            from app.markdown_renderer import render_markdown
            output_model = SynthesizedOutput.model_validate(doc)
            rendered = render_markdown(output_model, primary_file_name)
        else:
            rendered = render_markdown_from_doc(
                doc,
                primary_file_name,
                include_removed=include_removed,
            )

        filename = _export_filename(primary_file_name, "md")
        content_disp = _rfc5987_encode(filename)

        return Response(
            content=rendered.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": content_disp},
        )

    # ── JSON ──────────────────────────────────────────────────────────────────
    if format == "json":
        if view == "original":
            # Return bare baseline as-is (no _meta exists)
            output_doc = doc
        else:
            output_doc = dict(doc)
            if strip_meta:
                # Remove per-item _meta recursively and top-level working-copy keys
                output_doc = _strip_meta_from_doc(output_doc)

        filename = _export_filename(primary_file_name, "json")
        content_disp = _rfc5987_encode(filename)

        return Response(
            content=json.dumps(output_doc, indent=2).encode("utf-8"),
            media_type="application/json",
            headers={"Content-Disposition": content_disp},
        )

    # Should not reach here
    raise HTTPException(status_code=422, detail="Unsupported format")  # pragma: no cover


def _strip_meta_from_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *doc* with _meta, _edit, _baselines removed."""
    import copy
    cleaned = copy.deepcopy(doc)

    # Top-level working-copy keys
    for key in ("_edit", "_baselines"):
        cleaned.pop(key, None)

    # Per-item _meta in skill_outputs
    for so in cleaned.get("skill_outputs", []):
        for item in so.get("items", []):
            item.pop("_meta", None)

    # Per-item _meta in spine
    spine = cleaned.get("spine", {})
    for key in ("compliance", "ambiguity", "risk", "commercial_structure"):
        for item in spine.get(key, []):
            item.pop("_meta", None)

    return cleaned
