"""
core/state.py
-------------
LangGraph pipeline state for V2.

The canonical state object is PipelineState (defined in core/schemas.py as
a TypedDict). This module re-exports it for convenience and provides a
helper function to build the initial state for a new pipeline run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.schemas import DocumentRole, PipelineState


def initial_state(
    file_paths: list[str],
    primary_file_path: str,
    file_roles: dict[str, DocumentRole],
    job_id: str,
) -> PipelineState:
    """
    Build an empty PipelineState ready for the first pipeline node.

    Parameters
    ----------
    file_paths : list[str]
        All uploaded file paths. Primary document first, then annexures/QnA
        in upload order. Must include primary_file_path.
    primary_file_path : str
        The designated primary RFP document (must be in file_paths).
    file_roles : dict[str, DocumentRole]
        Mapping of filename → role ("primary" / "annexure" / "qna").
        Keys are basenames (Path(p).name), not full paths.
    job_id : str
        Unique job identifier assigned by the job store.

    Returns
    -------
    PipelineState
        A TypedDict ready for the first pipeline node. All other fields
        are absent until pipeline nodes populate them.
    """
    primary_name = Path(primary_file_path).name
    return PipelineState(
        # Multi-document inputs
        file_paths=file_paths,
        primary_file_path=primary_file_path,
        file_roles=file_roles,
        # Backward-compat single-file alias
        file_path=primary_file_path,
        job_id=job_id,
        metadata={"primary_file": primary_name, "job_id": job_id, "file_count": len(file_paths)},
        errors=[],
        skill_outputs=[],
        spine_compliance=[],
        spine_ambiguity=[],
        spine_risk=[],
        spine_commercial_structure=[],
    )


__all__ = ["PipelineState", "initial_state"]
