"""
mcp_server.py
-------------
Model Context Protocol (MCP) server for RFP Analyzer V2.

Exposes three tools so external AI clients (Bob, Claude Desktop, Cursor, ...)
can run and inspect RFP analyses by Opportunity ID:

* ``analyze_opportunity_rfp(opp_id, force=False)``
* ``get_opportunity_analysis(opp_id)``
* ``list_opportunities()``

Run::

    python mcp_server.py                              # stdio (default)
    python mcp_server.py --transport sse --port 8002  # SSE over HTTP
    python -m mcp_server --transport stdio

Notes
-----
* The server runs the analysis pipeline in its own process.  Jobs are
  persisted under ``OUTPUT_DIR`` and are re-read from disk on each tool call,
  so results produced by the web app (or an earlier MCP session) are visible.
* In stdio mode stdout carries the protocol; all logging goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, HTTPException
from mcp.server.fastmcp import FastMCP

from app.config import get_raw_storage_dir
from app.job_store import JobStore, get_job_store
from app.routers.analysis import start_opportunity_analysis
from core.opportunity_resolver import VALID_EXTENSIONS

_PROJECT_ROOT = Path(__file__).resolve().parent

DEFAULT_WEB_BASE_URL = "http://localhost:8001"
DEFAULT_SSE_PORT = 8002

mcp = FastMCP(
    "rfp-analyzer-v2",
    instructions=(
        "Analyze RFP documents stored under an Opportunity ID. "
        "Use list_opportunities to discover IDs, analyze_opportunity_rfp to start "
        "(or reuse) an analysis, and get_opportunity_analysis to poll status and "
        "read results."
    ),
)

# Strong references so fire-and-forget pipeline tasks are not garbage collected.
_background_tasks: set[asyncio.Task[Any]] = set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _web_base_url() -> str:
    return os.getenv("RFP_ANALYZER_BASE_URL", DEFAULT_WEB_BASE_URL).rstrip("/")


def _review_url(job_id: str) -> str:
    return f"{_web_base_url()}/review/{job_id}"


def _refresh_store() -> JobStore:
    """Return the job store, adding jobs persisted on disk that it does not know.

    Jobs already held in memory are never overwritten (``load_from_disk`` would
    mark in-flight jobs as interrupted), so this is safe to call on every tool
    invocation.
    """
    from app.config import get_output_dir

    store = get_job_store()
    scratch = JobStore()
    scratch.load_from_disk(get_output_dir())
    for job_id, job in scratch._jobs.items():
        store._jobs.setdefault(job_id, job)
    return store


def _spawn_background(tasks: BackgroundTasks) -> None:
    """Run the callables queued on a FastAPI ``BackgroundTasks`` as asyncio tasks."""
    for bg in tasks.tasks:
        task = asyncio.ensure_future(bg())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


def _load_result(output_dir: Path) -> Optional[dict[str, Any]]:
    """Load the analysis document (edited working copy preferred)."""
    for name in ("working.json", "result.json"):
        path = output_dir / name
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc.pop("_baselines", None)
            return doc
    return None


def _active(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop items the user removed in the review UI."""
    return [i for i in items if i.get("_meta", {}).get("status", "active") != "removed"]


def _summarize(doc: dict[str, Any]) -> dict[str, Any]:
    spine = doc.get("spine", {})
    archetypes = [
        {"archetype": so.get("skill_slug"), "item_count": len(_active(so.get("items", [])))}
        for so in doc.get("skill_outputs", [])
    ]
    return {
        "archetypes": archetypes,
        "fired_skills": doc.get("fired_skills", []),
        "metadata": doc.get("metadata", {}),
        "counts": {
            "skill_items": sum(a["item_count"] for a in archetypes),
            "compliance": len(_active(spine.get("compliance", []))),
            "ambiguity": len(_active(spine.get("ambiguity", []))),
            "risk": len(_active(spine.get("risk", []))),
            "commercial_structure": len(_active(spine.get("commercial_structure", []))),
        },
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def analyze_opportunity_rfp(opp_id: str, force: bool = False) -> dict[str, Any]:
    """Start (or reuse) an RFP analysis for an Opportunity ID.

    Resolves the opportunity's documents from raw storage and launches the
    analysis pipeline in the background.  Returns immediately with the job ID,
    status, and a browser URL for the review page.  Poll
    ``get_opportunity_analysis`` for results.

    Args:
        opp_id: Opportunity identifier (a folder name under raw storage).
        force: Create a new job even if one is already running or complete.
    """
    store = _refresh_store()
    before = store.get_job_by_opp_id(opp_id)

    tasks = BackgroundTasks()
    try:
        job = await start_opportunity_analysis(opp_id=opp_id, background_tasks=tasks, force=force)
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc
    _spawn_background(tasks)

    reused = bool(before and before.job_id == job.job_id and not tasks.tasks)
    return {
        "opp_id": opp_id,
        "job_id": job.job_id,
        "status": job.status.value,
        "reused_existing_job": reused,
        "review_url": _review_url(job.job_id),
        "message": (
            "Existing analysis reused."
            if reused
            else "Analysis started. Call get_opportunity_analysis to check progress."
        ),
    }


@mcp.tool()
async def get_opportunity_analysis(opp_id: str) -> dict[str, Any]:
    """Get the analysis status and results for an Opportunity ID.

    Returns the job status; when complete also returns a summary (archetypes,
    item counts, metadata), the risks, and the full Markdown report.
    """
    store = _refresh_store()
    job = store.get_job_by_opp_id(opp_id)
    if job is None:
        return {
            "opp_id": opp_id,
            "status": "not_started",
            "message": f"No analysis exists for '{opp_id}'. Call analyze_opportunity_rfp first.",
        }

    response: dict[str, Any] = {
        "opp_id": opp_id,
        "job_id": job.job_id,
        "status": job.status.value,
        "review_url": _review_url(job.job_id),
        "file_names": job.file_names,
        "primary_file_name": job.primary_file_name,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    if job.error:
        response["error"] = job.error

    if job.status.value == "complete" and job.output_dir:
        output_dir = Path(job.output_dir)
        doc = _load_result(output_dir)
        if doc is not None:
            response["summary"] = _summarize(doc)
            response["risks"] = _active(doc.get("spine", {}).get("risk", []))
        md_path = output_dir / "result.md"
        if md_path.exists():
            response["markdown"] = md_path.read_text(encoding="utf-8")
    return response


@mcp.tool()
async def list_opportunities() -> dict[str, Any]:
    """List opportunity folders available in raw storage.

    Each entry has the opportunity ID, count and types of supported documents,
    and the status of any existing analysis.
    """
    store = _refresh_store()
    base = get_raw_storage_dir()
    if not base.is_dir():
        return {"storage_dir": str(base), "opportunities": [], "error": "Storage directory not found."}

    opportunities: list[dict[str, Any]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        types: dict[str, int] = {}
        count = 0
        for f in child.iterdir():
            if f.is_file() and f.suffix.lower() in VALID_EXTENSIONS:
                count += 1
                types[f.suffix.lower()] = types.get(f.suffix.lower(), 0) + 1
        job = store.get_job_by_opp_id(child.name)
        opportunities.append(
            {
                "opp_id": child.name,
                "file_count": count,
                "file_types": types,
                "analysis_status": job.status.value if job else "not_started",
                "job_id": job.job_id if job else None,
            }
        )
    return {"storage_dir": str(base), "opportunities": opportunities}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RFP Analyzer V2 MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="SSE bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_SSE_PORT, help="SSE port (default 8002)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)

    # Relative paths in .env (OUTPUT_DIR=./outputs) must resolve against the
    # project, not whatever directory the MCP client launched us from.
    os.chdir(_PROJECT_ROOT)
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")

    from logging_config import configure_logging

    configure_logging()  # logs go to stderr — stdout is the stdio protocol channel

    from app.config import get_output_dir

    get_job_store().load_from_disk(get_output_dir())

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
