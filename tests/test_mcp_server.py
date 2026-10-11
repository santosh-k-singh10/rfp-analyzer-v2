"""
tests/test_mcp_server.py
------------------------
Tests for the MCP server (mcp_server.py).

Covers:
  - tool registration
  - list_opportunities: folder discovery, file counts/types, analysis status
  - analyze_opportunity_rfp: starts a job, reuse, force, unknown opportunity
  - get_opportunity_analysis: not_started, running, complete (summary/risks/markdown)
  - CLI argument parsing
  - end-to-end over the real stdio transport (subprocess + MCP client)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import mcp_server
from app.job_store import JobStore


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolated storage/output/upload dirs, fresh job store, mocked pipeline."""
    raw = tmp_path / "raw"
    out = tmp_path / "outputs"
    up = tmp_path / "uploads"
    for d in (raw, out, up):
        d.mkdir()
    monkeypatch.setenv("RAW_STORAGE_BASE_DIR", str(raw))
    monkeypatch.setenv("OUTPUT_DIR", str(out))
    monkeypatch.setenv("UPLOAD_DIR", str(up))
    monkeypatch.setenv("RFP_ANALYZER_BASE_URL", "http://example.test:9000/")

    monkeypatch.setattr("app.job_store._store", JobStore())
    pipeline = AsyncMock()
    monkeypatch.setattr("app.routers.analysis._run_pipeline", pipeline)
    return {"raw": raw, "out": out, "up": up, "pipeline": pipeline}  # type: ignore[dict-item]


def _make_opp(raw: Path, opp_id: str, files: list[str]) -> Path:
    d = raw / opp_id
    d.mkdir()
    for name in files:
        (d / name).write_bytes(b"stub")
    return d


async def _call(name: str, **args: Any) -> dict[str, Any]:
    """Invoke a tool through the FastMCP layer and return its dict payload."""
    result = await mcp_server.mcp.call_tool(name, args)
    if isinstance(result, tuple):  # (content blocks, structured content)
        content, structured = result
        if structured is not None:
            return structured.get("result", structured) if set(structured) == {"result"} else structured
        result = content
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)


def _finish_job(env: dict[str, Path], job_id: str, result: dict[str, Any], md: str = "# Report") -> None:
    """Simulate a completed pipeline run by writing outputs and flipping status."""
    from app.job_store import get_job_store

    store = get_job_store()
    job = store.get_job(job_id)
    assert job is not None and job.output_dir
    out = Path(job.output_dir)
    (out / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (out / "result.md").write_text(md, encoding="utf-8")
    store.set_complete(job_id, result)


# ---------------------------------------------------------------------------
# Registration / CLI
# ---------------------------------------------------------------------------

async def test_tools_registered() -> None:
    tools = {t.name for t in await mcp_server.mcp.list_tools()}
    assert tools == {"analyze_opportunity_rfp", "get_opportunity_analysis", "list_opportunities"}


def test_cli_defaults() -> None:
    args = mcp_server._parse_args([])
    assert args.transport == "stdio"
    assert args.port == 8002


def test_cli_sse_port() -> None:
    args = mcp_server._parse_args(["--transport", "sse", "--port", "9100"])
    assert (args.transport, args.port) == ("sse", 9100)


def test_cli_rejects_unknown_transport() -> None:
    with pytest.raises(SystemExit):
        mcp_server._parse_args(["--transport", "carrier-pigeon"])


# ---------------------------------------------------------------------------
# list_opportunities
# ---------------------------------------------------------------------------

async def test_list_opportunities(env: dict[str, Path]) -> None:
    _make_opp(env["raw"], "OPP-1", ["RFP_main.pdf", "Annex_A.pdf", "QnA.xlsx", "notes.md"])
    _make_opp(env["raw"], "OPP-2", ["rfp.docx"])
    (env["raw"] / "stray.txt").write_text("not a folder")

    data = await _call("list_opportunities")

    assert data["storage_dir"] == str(env["raw"])
    by_id = {o["opp_id"]: o for o in data["opportunities"]}
    assert set(by_id) == {"OPP-1", "OPP-2"}
    assert by_id["OPP-1"]["file_count"] == 3  # notes.md is unsupported
    assert by_id["OPP-1"]["file_types"] == {".pdf": 2, ".xlsx": 1}
    assert by_id["OPP-1"]["analysis_status"] == "not_started"
    assert by_id["OPP-2"]["file_types"] == {".docx": 1}


async def test_list_opportunities_missing_storage(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAW_STORAGE_BASE_DIR", str(env["raw"] / "does-not-exist"))
    data = await _call("list_opportunities")
    assert data["opportunities"] == []
    assert "error" in data


# ---------------------------------------------------------------------------
# analyze_opportunity_rfp
# ---------------------------------------------------------------------------

async def test_analyze_starts_job_and_runs_pipeline(env: dict[str, Path]) -> None:
    _make_opp(env["raw"], "OPP-1", ["RFP_main.pdf", "Annex_A.pdf"])

    data = await _call("analyze_opportunity_rfp", opp_id="OPP-1")

    assert data["opp_id"] == "OPP-1"
    assert data["status"] in ("pending", "running")
    assert data["reused_existing_job"] is False
    assert data["review_url"] == f"http://example.test:9000/review/{data['job_id']}"

    # Let the scheduled background task run.
    import asyncio
    await asyncio.gather(*mcp_server._background_tasks)
    env["pipeline"].assert_awaited_once()
    args = env["pipeline"].await_args.args
    assert args[0] == data["job_id"]
    assert Path(args[2]).name == "RFP_main.pdf"
    assert args[3]["RFP_main.pdf"] == "primary"


async def test_analyze_unknown_opportunity_raises(env: dict[str, Path]) -> None:
    with pytest.raises(Exception, match="No directory found"):
        await _call("analyze_opportunity_rfp", opp_id="NOPE")


async def test_analyze_empty_opportunity_raises(env: dict[str, Path]) -> None:
    _make_opp(env["raw"], "EMPTY", ["readme.md"])
    with pytest.raises(Exception, match="No supported documents"):
        await _call("analyze_opportunity_rfp", opp_id="EMPTY")


async def test_analyze_reuses_active_job_unless_forced(env: dict[str, Path]) -> None:
    _make_opp(env["raw"], "OPP-1", ["RFP_main.pdf"])
    first = await _call("analyze_opportunity_rfp", opp_id="OPP-1")

    from app.job_store import get_job_store
    get_job_store().set_running(first["job_id"])

    again = await _call("analyze_opportunity_rfp", opp_id="OPP-1")
    assert again["job_id"] == first["job_id"]
    assert again["reused_existing_job"] is True
    assert again["status"] == "running"

    forced = await _call("analyze_opportunity_rfp", opp_id="OPP-1", force=True)
    assert forced["job_id"] != first["job_id"]
    assert forced["reused_existing_job"] is False


# ---------------------------------------------------------------------------
# get_opportunity_analysis
# ---------------------------------------------------------------------------

async def test_get_analysis_not_started(env: dict[str, Path]) -> None:
    data = await _call("get_opportunity_analysis", opp_id="OPP-X")
    assert data["status"] == "not_started"


async def test_get_analysis_running_has_no_results(env: dict[str, Path]) -> None:
    _make_opp(env["raw"], "OPP-1", ["RFP_main.pdf"])
    started = await _call("analyze_opportunity_rfp", opp_id="OPP-1")

    data = await _call("get_opportunity_analysis", opp_id="OPP-1")

    assert data["job_id"] == started["job_id"]
    assert data["status"] in ("pending", "running")
    assert data["primary_file_name"] == "RFP_main.pdf"
    assert "summary" not in data and "markdown" not in data


async def test_get_analysis_complete(env: dict[str, Path]) -> None:
    _make_opp(env["raw"], "OPP-1", ["RFP_main.pdf"])
    started = await _call("analyze_opportunity_rfp", opp_id="OPP-1")
    result = {
        "skill_outputs": [
            {"skill_slug": "ams", "items": [{"id": "A1"}, {"id": "A2", "_meta": {"status": "removed"}}]},
        ],
        "spine": {
            "compliance": [{"id": "C1"}],
            "ambiguity": [],
            "risk": [{"id": "R1", "title": "Big risk"}, {"id": "R2", "_meta": {"status": "removed"}}],
            "commercial_structure": [],
        },
        "fired_skills": ["ams"],
        "metadata": {"duration_seconds": 1.5},
    }
    _finish_job(env, started["job_id"], result, md="# Analysis Report")

    data = await _call("get_opportunity_analysis", opp_id="OPP-1")

    assert data["status"] == "complete"
    assert data["markdown"] == "# Analysis Report"
    assert data["summary"]["archetypes"] == [{"archetype": "ams", "item_count": 1}]
    assert data["summary"]["fired_skills"] == ["ams"]
    assert data["summary"]["counts"] == {
        "skill_items": 1, "compliance": 1, "ambiguity": 0, "risk": 1, "commercial_structure": 0,
    }
    assert [r["id"] for r in data["risks"]] == ["R1"]


async def test_get_analysis_failed_reports_error(env: dict[str, Path]) -> None:
    _make_opp(env["raw"], "OPP-1", ["RFP_main.pdf"])
    started = await _call("analyze_opportunity_rfp", opp_id="OPP-1")
    from app.job_store import get_job_store
    get_job_store().set_failed(started["job_id"], "boom")

    data = await _call("get_opportunity_analysis", opp_id="OPP-1")
    assert data["status"] == "failed"
    assert data["error"] == "boom"


async def test_job_persisted_on_disk_is_visible_to_fresh_store(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job written by another process (e.g. the web app) is found via job.json."""
    _make_opp(env["raw"], "OPP-1", ["RFP_main.pdf"])
    started = await _call("analyze_opportunity_rfp", opp_id="OPP-1")
    _finish_job(env, started["job_id"], {"spine": {}, "skill_outputs": []})

    monkeypatch.setattr("app.job_store._store", JobStore())  # simulate new process
    data = await _call("get_opportunity_analysis", opp_id="OPP-1")
    assert data["job_id"] == started["job_id"]
    assert data["status"] == "complete"


# ---------------------------------------------------------------------------
# Real stdio transport
# ---------------------------------------------------------------------------

async def test_stdio_transport_end_to_end(tmp_path: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    raw = tmp_path / "raw"
    (raw / "OPP-9").mkdir(parents=True)
    (raw / "OPP-9" / "RFP.pdf").write_bytes(b"stub")

    import os
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(mcp_server.__file__))],
        env={
            **os.environ,
            "RAW_STORAGE_BASE_DIR": str(raw),
            "OUTPUT_DIR": str(tmp_path / "outputs"),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                "analyze_opportunity_rfp", "get_opportunity_analysis", "list_opportunities",
            }
            res = await session.call_tool("list_opportunities", {})
            assert not res.isError
            payload = res.structuredContent or json.loads(res.content[0].text)
            assert [o["opp_id"] for o in payload["opportunities"]] == ["OPP-9"]
