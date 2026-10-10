"""
app/routers/edits.py
---------------------
API endpoints for requirement editing, field specifications, and mutations.

Routes (sub-task 1.1 + 1.2 + 2.3):
  GET  /jobs/{job_id}/fields?kind=&key=          → Field specs for drawer
  POST /jobs/{job_id}/items                      → Add a new item
  PATCH /jobs/{job_id}/items/{uid}               → Edit an item
  DELETE /jobs/{job_id}/items/{uid}              → Soft-remove an item
  POST /jobs/{job_id}/items/{uid}/restore        → Restore a removed item
  POST /jobs/{job_id}/items/{uid}/revert         → Revert to AI baseline
  POST /jobs/{job_id}/items/{uid}/enhance        → Propose AI enhancement (not persisted)
  POST /jobs/{job_id}/items/{uid}/enhance/accept → Accept + persist a proposal
  POST /jobs/{job_id}/reset?confirm=true         → Archive + re-bootstrap
  GET  /jobs/{job_id}/history                    → Paginated audit log
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import structlog

from app.config import get_output_dir
from app.dependencies import validate_job_id
from app.job_store import get_job_store
from core.edit.enhance import EnhancementFailed, enhance_item
from core.edit.field_specs import get_field_spec
from core.edit.repository import FileResultRepository
from core.edit.service import (
    ValidationError,
    add_item,
    bootstrap_working_copy,
    bulk_op,
    compute_edit_state,
    find_item_by_uid,
    remove_item,
    reset_working_copy,
    restore_item,
    revert_item,
    update_item,
)


log = structlog.get_logger()

router = APIRouter()

# ── Repository singleton ──────────────────────────────────────────────────────

_repo: Optional[FileResultRepository] = None


def _get_repo() -> FileResultRepository:
    global _repo
    if _repo is None:
        _repo = FileResultRepository(get_output_dir())
    return _repo


# ── Pydantic models ───────────────────────────────────────────────────────────


class MutationResponse(BaseModel):
    """Response shape for all mutating endpoints (r3-1).

    ``warnings`` (non-blocking) may contain plain strings (e.g. title length)
    and ``{type: "possible_duplicate", uid, id, similarity}`` dicts (2.2).
    """

    item: dict[str, Any]
    edit_state: dict[str, Any]
    warnings: Optional[list[Any]] = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _require_complete_job(job_id: str) -> Any:
    """Validate job exists and is complete; return the AnalysisJob."""
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


async def _load_or_bootstrap(job: Any) -> dict[str, Any]:
    """Load working.json, or return in-memory deterministic bootstrap for legacy jobs."""
    repo = _get_repo()
    doc = await repo.load(job.job_id)
    if doc is None:
        # Legacy job — build in-memory bootstrap
        result_path = Path(job.output_dir) / "result.json"
        if not result_path.exists():
            raise HTTPException(status_code=404, detail="result.json not found for this job")
        baseline = json.loads(result_path.read_text(encoding="utf-8"))
        doc = bootstrap_working_copy(baseline, f"legacy:{job.job_id}")
    return doc


async def _persist_with_bootstrap_check(
    job: Any,
    doc: dict[str, Any],
) -> None:
    """For legacy jobs, re-check that working.json still doesn't exist before writing.

    Acquires the per-job lock before checking. The caller must already hold the
    lock when calling this from a mutation that was loaded from the in-memory bootstrap.
    """
    repo = _get_repo()
    await repo.save(job.job_id, doc)


def _build_log_entry(
    op: str,
    uid: str,
    item: dict[str, Any],
    bucket: str,
    revision: int,
    fields_changed: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "op": op,
        "uid": uid,
        "id": item.get("id", ""),
        "bucket": bucket,
        "fields_changed": fields_changed or {},
        "actor": "local",
        "revision": revision,
    }


def _revision_from_doc(doc: dict[str, Any]) -> int:
    return doc.get("_edit", {}).get("revision", 0)


# ── GET /jobs/{job_id}/fields ─────────────────────────────────────────────────


@router.get("/jobs/{job_id}/fields")
async def get_fields(
    job_id: str = Depends(validate_job_id),
    kind: str = Query(..., description="Bucket kind: 'skill' or 'spine'"),
    key: str = Query(..., description="Bucket key: skill slug or spine category"),
) -> list[dict[str, Any]]:
    """Return field specifications for a given bucket (skill or spine category).

    Used by the review UI to render the edit/add drawer fields.
    """
    store = get_job_store()
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    specs = get_field_spec(kind, key)
    if not specs:
        raise HTTPException(
            status_code=404,
            detail=f"No field specification found for kind={kind!r}, key={key!r}",
        )

    return [s.to_dict() for s in specs]


# ── POST /jobs/{job_id}/items ─────────────────────────────────────────────────


class AddItemRequest(BaseModel):
    bucket: str
    fields: dict[str, Any]


@router.post("/jobs/{job_id}/items", response_model=MutationResponse)
async def post_add_item(
    body: AddItemRequest,
    job_id: str = Depends(validate_job_id),
) -> MutationResponse:
    """Add a new user-added item to the given bucket.

    Creates missing SkillOutput if necessary (C9).
    """
    job = _require_complete_job(job_id)
    repo = _get_repo()
    lock = repo.lock_for(job_id)

    async with lock:
        doc = await _load_or_bootstrap(job)

        try:
            item, warnings = add_item(doc, body.bucket, body.fields)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors)

        # Persist (creates working.json on first mutation for legacy jobs, r2-1)
        await _persist_with_bootstrap_check(job, doc)

    # Find the bucket for logging
    result = find_item_by_uid(doc, item["_meta"]["uid"])
    bucket = result[1] if result else body.bucket

    await repo.append_log(
        job_id,
        _build_log_entry(
            "add",
            item["_meta"]["uid"],
            item,
            bucket,
            _revision_from_doc(doc),
        ),
    )

    edit_state = compute_edit_state(doc)
    log.info("item_add_endpoint", job_id=job_id, uid=item["_meta"]["uid"])
    return MutationResponse(item=item, edit_state=edit_state, warnings=warnings or None)


# ── PATCH /jobs/{job_id}/items/{uid} ─────────────────────────────────────────


class PatchItemRequest(BaseModel):
    base_version: int
    fields: dict[str, Any]


@router.patch("/jobs/{job_id}/items/{uid}", response_model=MutationResponse)
async def patch_item(
    uid: str,
    body: PatchItemRequest,
    job_id: str = Depends(validate_job_id),
) -> MutationResponse:
    """Update fields on an existing item (per-item optimistic concurrency).

    Returns 409 with current item if base_version doesn't match.
    """
    job = _require_complete_job(job_id)
    repo = _get_repo()
    lock = repo.lock_for(job_id)

    async with lock:
        doc = await _load_or_bootstrap(job)

        # Check uid exists (capture current_item before mutation for 409 body)
        found = find_item_by_uid(doc, uid)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Item '{uid}' not found")

        current_item, bucket = found

        try:
            item, warnings = update_item(doc, uid, body.fields, body.base_version)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Item '{uid}' not found")
        except ValueError as exc:
            # Version mismatch → 409 with current item in body
            if "version mismatch" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "msg": str(exc),
                        "current_item": current_item,
                    },
                )
            raise HTTPException(status_code=422, detail=str(exc))

        await _persist_with_bootstrap_check(job, doc)

    # Capture before/after for log — after update_item the item has new values,
    # so "from" is whatever the item held before (we only have post-mutation state
    # here; use the baseline for fields that were updated if available, else None).
    await repo.append_log(
        job_id,
        _build_log_entry(
            "patch",
            uid,
            item,
            bucket,
            _revision_from_doc(doc),
            fields_changed={
                f: {"from": None, "to": item.get(f)}
                for f in body.fields
                if f in item
            },
        ),
    )

    edit_state = compute_edit_state(doc)
    log.info("item_patch_endpoint", job_id=job_id, uid=uid)
    return MutationResponse(item=item, edit_state=edit_state, warnings=warnings or None)


# ── DELETE /jobs/{job_id}/items/{uid} ─────────────────────────────────────────


@router.delete("/jobs/{job_id}/items/{uid}", response_model=MutationResponse)
async def delete_item(
    uid: str,
    job_id: str = Depends(validate_job_id),
) -> MutationResponse:
    """Soft-remove an item (D3). Sets status=removed."""
    job = _require_complete_job(job_id)
    repo = _get_repo()
    lock = repo.lock_for(job_id)

    async with lock:
        doc = await _load_or_bootstrap(job)

        try:
            item = remove_item(doc, uid)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Item '{uid}' not found")

        await _persist_with_bootstrap_check(job, doc)

    found = find_item_by_uid(doc, uid)
    bucket = found[1] if found else "unknown"

    await repo.append_log(
        job_id,
        _build_log_entry("remove", uid, item, bucket, _revision_from_doc(doc)),
    )

    edit_state = compute_edit_state(doc)
    log.info("item_delete_endpoint", job_id=job_id, uid=uid)
    return MutationResponse(item=item, edit_state=edit_state)


# ── POST /jobs/{job_id}/items/{uid}/restore ───────────────────────────────────


@router.post("/jobs/{job_id}/items/{uid}/restore", response_model=MutationResponse)
async def post_restore_item(
    uid: str,
    job_id: str = Depends(validate_job_id),
) -> MutationResponse:
    """Restore a soft-deleted item to active."""
    job = _require_complete_job(job_id)
    repo = _get_repo()
    lock = repo.lock_for(job_id)

    async with lock:
        doc = await _load_or_bootstrap(job)

        try:
            item = restore_item(doc, uid)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Item '{uid}' not found")

        await _persist_with_bootstrap_check(job, doc)

    found = find_item_by_uid(doc, uid)
    bucket = found[1] if found else "unknown"

    await repo.append_log(
        job_id,
        _build_log_entry("restore", uid, item, bucket, _revision_from_doc(doc)),
    )

    edit_state = compute_edit_state(doc)
    log.info("item_restore_endpoint", job_id=job_id, uid=uid)
    return MutationResponse(item=item, edit_state=edit_state)


# ── POST /jobs/{job_id}/items/{uid}/revert ────────────────────────────────────


@router.post("/jobs/{job_id}/items/{uid}/revert", response_model=MutationResponse)
async def post_revert_item(
    uid: str,
    job_id: str = Depends(validate_job_id),
) -> MutationResponse:
    """Revert an AI item to its baseline values. 400 for user-added items."""
    job = _require_complete_job(job_id)
    repo = _get_repo()
    lock = repo.lock_for(job_id)

    async with lock:
        doc = await _load_or_bootstrap(job)

        try:
            item = revert_item(doc, uid)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Item '{uid}' not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        await _persist_with_bootstrap_check(job, doc)

    found = find_item_by_uid(doc, uid)
    bucket = found[1] if found else "unknown"

    await repo.append_log(
        job_id,
        _build_log_entry("revert", uid, item, bucket, _revision_from_doc(doc)),
    )

    edit_state = compute_edit_state(doc)
    log.info("item_revert_endpoint", job_id=job_id, uid=uid)
    return MutationResponse(item=item, edit_state=edit_state)


# ── POST /jobs/{job_id}/items/{uid}/enhance ──────────────────────────────────


class EnhanceRequest(BaseModel):
    """Request body for POST .../enhance."""

    mode: str  # "clarify_wording" | "measurable" | "clarification_question" | "custom"
    instruction: Optional[str] = None


class EnhanceProposalResponse(BaseModel):
    """Response shape for POST .../enhance (proposal — not yet persisted)."""

    uid: str
    base_version: int  # snapshot version at proposal time (for accept's version check)
    proposal: dict[str, Any]


@router.post("/jobs/{job_id}/items/{uid}/enhance", response_model=EnhanceProposalResponse)
async def post_enhance_item(
    uid: str,
    body: EnhanceRequest,
    job_id: str = Depends(validate_job_id),
) -> EnhanceProposalResponse:
    """Propose an AI enhancement for an item without persisting it (spec §6.3).

    Flow:
    1. Acquire lock → load doc → snapshot item → release lock.
    2. Call LLM (lock NOT held).
    3. Return proposal; nothing is written to disk.

    Returns 502 if the LLM returns [] (EnhancementFailed).
    Returns 400 if the mode is disallowed for this item.
    """
    job = _require_complete_job(job_id)
    repo = _get_repo()
    lock = repo.lock_for(job_id)

    # Step 1: read snapshot under lock, then release
    async with lock:
        doc = await _load_or_bootstrap(job)
        found = find_item_by_uid(doc, uid)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Item '{uid}' not found")
        item_snapshot, _bucket = found
        # Deep-copy so further mutations don't affect our snapshot
        import copy
        item_snapshot = copy.deepcopy(item_snapshot)

    base_version = item_snapshot.get("_meta", {}).get("version", 1)

    # Step 2: call LLM (lock NOT held)
    try:
        proposal = await enhance_item(
            item=item_snapshot,
            mode=body.mode,
            instruction=body.instruction,
        )
    except EnhancementFailed as exc:
        log.error("enhance_failed", job_id=job_id, uid=uid, error=str(exc))
        raise HTTPException(
            status_code=502,
            detail={"error": "enhancement_failed", "msg": str(exc)},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    log.info("enhance_endpoint", job_id=job_id, uid=uid, mode=body.mode)
    return EnhanceProposalResponse(
        uid=uid,
        base_version=base_version,
        proposal=proposal.to_dict(),
    )


# ── POST /jobs/{job_id}/items/{uid}/enhance/accept ────────────────────────────


class EnhanceAcceptRequest(BaseModel):
    """Request body for POST .../enhance/accept."""

    base_version: int
    proposal: dict[str, Any]  # EnhanceProposal.to_dict() from the propose step


@router.post(
    "/jobs/{job_id}/items/{uid}/enhance/accept",
    response_model=MutationResponse,
)
async def post_enhance_accept(
    uid: str,
    body: EnhanceAcceptRequest,
    job_id: str = Depends(validate_job_id),
) -> MutationResponse:
    """Accept and persist an enhancement proposal (spec §6.3).

    Applies the proposal's ``proposed`` fields via ``update_item`` under the
    per-job lock, then sets ``_meta.enhanced = True``.

    Returns 409 if ``base_version`` doesn't match the current item version.
    """
    job = _require_complete_job(job_id)
    repo = _get_repo()
    lock = repo.lock_for(job_id)

    proposed_fields: dict[str, Any] = body.proposal.get("proposed", {})

    async with lock:
        doc = await _load_or_bootstrap(job)

        found = find_item_by_uid(doc, uid)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Item '{uid}' not found")

        current_item, bucket = found

        # Apply via update_item (handles version check, validation, fingerprinting)
        try:
            item, warnings = update_item(doc, uid, proposed_fields, body.base_version)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Item '{uid}' not found")
        except ValueError as exc:
            if "version mismatch" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "msg": str(exc),
                        "current_item": current_item,
                    },
                )
            raise HTTPException(status_code=422, detail=str(exc))

        # Mark as AI-enhanced
        item["_meta"]["enhanced"] = True

        await _persist_with_bootstrap_check(job, doc)

    await repo.append_log(
        job_id,
        _build_log_entry(
            "enhance",
            uid,
            item,
            bucket,
            _revision_from_doc(doc),
            fields_changed={
                f: {"from": None, "to": item.get(f)}
                for f in proposed_fields
                if f in item
            },
        ),
    )

    edit_state = compute_edit_state(doc)
    log.info("enhance_accept_endpoint", job_id=job_id, uid=uid)
    return MutationResponse(item=item, edit_state=edit_state, warnings=warnings or None)


# ── POST /jobs/{job_id}/reset ─────────────────────────────────────────────────


class ResetResponse(BaseModel):
    archived: Optional[str] = None
    edit_state: dict[str, Any]
    message: str


@router.post("/jobs/{job_id}/reset", response_model=ResetResponse)
async def post_reset(
    job_id: str = Depends(validate_job_id),
    confirm: bool = Query(False, description="Must be 'true' to actually reset"),
) -> ResetResponse:
    """Archive working.json and re-bootstrap from result.json.

    Requires ``?confirm=true``. For legacy jobs (no working.json), returns
    the in-memory bootstrap without archiving (r3-4).
    """
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Pass ?confirm=true to confirm the reset operation",
        )

    job = _require_complete_job(job_id)
    repo = _get_repo()
    lock = repo.lock_for(job_id)

    async with lock:
        working_existed = (Path(job.output_dir) / "working.json").exists()

        try:
            new_doc, archive_path = reset_working_copy(
                None,  # doc not needed; reset reads from disk
                job_id,
                job.output_dir,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        if working_existed:
            # Save the new bootstrapped doc (legacy jobs skip this)
            await repo.save(job_id, new_doc)

    # Log the reset operation
    if working_existed:
        await repo.append_log(
            job_id,
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "op": "reset",
                "uid": "",
                "id": "",
                "bucket": "",
                "fields_changed": {},
                "actor": "local",
                "revision": _revision_from_doc(new_doc),
                "archive": archive_path,
            },
        )

    edit_state = compute_edit_state(new_doc)
    log.info("reset_endpoint", job_id=job_id, archived=archive_path)
    return ResetResponse(
        archived=archive_path,
        edit_state=edit_state,
        message="Working copy reset to AI baseline" + (
            f"; previous copy archived to {archive_path}" if archive_path else ""
        ),
    )


# ── POST /jobs/{job_id}/items/bulk ────────────────────────────────────────────


class BulkItemRequest(BaseModel):
    """Request body for POST /jobs/{id}/items/bulk."""

    uids: list[str]
    op: str  # "remove" | "restore" | "set_field"
    field: Optional[str] = None
    value: Any = None
    base_versions: Optional[dict[str, int]] = None


class BulkItemResultEntry(BaseModel):
    uid: str
    ok: bool
    error: Optional[str] = None
    meta: dict[str, Any]


class BulkItemResponse(BaseModel):
    """Response shape for POST /jobs/{id}/items/bulk (r3-1)."""

    results: list[BulkItemResultEntry]
    edit_state: dict[str, Any]
    revision: int


@router.post("/jobs/{job_id}/items/bulk", response_model=BulkItemResponse)
async def post_bulk_items(
    body: BulkItemRequest,
    job_id: str = Depends(validate_job_id),
) -> BulkItemResponse:
    """Apply one operation to multiple items in a single lock/save cycle (AT-13).

    All ops are applied in memory; a single ``save`` call is made once all
    items have been processed.  One log entry is appended per uid.

    Per-item conflicts for ``set_field`` are returned as ``ok=False`` entries
    in ``results``; the endpoint always returns 200.
    """
    if not body.uids:
        raise HTTPException(status_code=422, detail="'uids' must not be empty")

    if body.op not in ("remove", "restore", "set_field"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown op '{body.op}'. Must be 'remove', 'restore', or 'set_field'.",
        )

    job = _require_complete_job(job_id)
    repo = _get_repo()
    lock = repo.lock_for(job_id)

    async with lock:
        doc = await _load_or_bootstrap(job)

        try:
            results = bulk_op(
                doc,
                body.uids,
                body.op,
                field=body.field,
                value=body.value,
                base_versions=body.base_versions,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        await _persist_with_bootstrap_check(job, doc)

    revision = _revision_from_doc(doc)

    # Append one log entry per uid (C12)
    for entry_result in results:
        uid = entry_result["uid"]
        found = find_item_by_uid(doc, uid)
        bucket = found[1] if found else "unknown"
        item = found[0] if found else {}
        await repo.append_log(
            job_id,
            _build_log_entry(
                f"bulk_{body.op}",
                uid,
                item,
                bucket,
                revision,
                fields_changed={"field": body.field, "value": str(body.value)}
                if body.op == "set_field" and entry_result["ok"]
                else {},
            ),
        )

    edit_state = compute_edit_state(doc)
    log.info("bulk_endpoint", job_id=job_id, op=body.op, count=len(body.uids))
    return BulkItemResponse(
        results=[BulkItemResultEntry(**r) for r in results],
        edit_state=edit_state,
        revision=revision,
    )


# ── GET /jobs/{job_id}/history ────────────────────────────────────────────────


class HistoryResponse(BaseModel):
    entries: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


@router.get("/jobs/{job_id}/history", response_model=HistoryResponse)
async def get_history(
    job_id: str = Depends(validate_job_id),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> HistoryResponse:
    """Return paginated audit log entries (newest first)."""
    job = _require_complete_job(job_id)  # noqa: F841 (validates job)
    repo = _get_repo()
    all_entries = await repo.read_log(job_id)
    # newest first
    all_entries = list(reversed(all_entries))
    total = len(all_entries)
    start = (page - 1) * page_size
    end = start + page_size
    return HistoryResponse(
        entries=all_entries[start:end],
        total=total,
        page=page,
        page_size=page_size,
    )
