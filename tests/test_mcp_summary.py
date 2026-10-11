"""
tests/test_mcp_summary.py
-------------------------
Tests for MCP tool get_rfp_summary in mcp_server.py.

Covers:
  - Tool registration of get_rfp_summary
  - Returns existing summary immediately when job is complete with summary.json
  - Returns existing summary from result.json fallback
  - Starts summary analysis when no job exists
  - force=True bypasses existing completed summary and starts new run
  - URL formatting: http://{base_url}/summary?opp_id={opp_id}
  - Error handling: raises on unknown opportunity or empty folder
  - Failed job status reporting
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import mcp_server
from app.job_store import JobStore
from core.schemas import RfpKeyDate, RfpMetadata, RfpOverview, RfpSummaryOutput


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
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

    summary_mock = AsyncMock(
        return_value=RfpSummaryOutput(
            rfp_metadata=RfpMetadata(
                client_name="Acme Corp",
                rfp_title="Cloud Migration RFP",
                rfp_reference_number="RFP-2025-001",
                estimated_contract_value="$2,500,000",
                contract_duration="24 months",
            ),
            key_dates=[
                RfpKeyDate(
                    date_id="KD-1",
                    label="Submission Deadline",
                    date_type="submission_deadline",
                    display_date="2025-11-15",
                    date_value="2025-11-15",
                    raw_excerpt="Submissions due by November 15, 2025",
                    source_document="rfp.pdf",
                    confidence="high",
                )
            ],
            rfp_overview=RfpOverview(
                summary="Acme Corp seeks cloud migration partner.",
                scope_highlights=["AWS migration", "CI/CD setup"],
                client_context="Retail logistics",
                source_sections=["Section 1"],
            ),
        )
    )
    monkeypatch.setattr("pipeline.summary_extractor.run_summary_pipeline", summary_mock)

    return {"raw": raw, "out": out, "up": up, "summary_mock": summary_mock}


def _make_opp(raw: Path, opp_id: str, files: list[str]) -> Path:
    d = raw / opp_id
    d.mkdir(parents=True, exist_ok=True)
    for name in files:
        (d / name).write_bytes(b"stub file content")
    return d


async def _call(name: str, **args: Any) -> dict[str, Any]:
    """Invoke a tool through FastMCP and return its dict payload."""
    result = await mcp_server.mcp.call_tool(name, args)
    if isinstance(result, tuple):
        content, structured = result
        if structured is not None:
            return structured.get("result", structured) if set(structured) == {"result"} else structured
        result = content
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_get_rfp_summary_tool_registered() -> None:
    tools = {t.name for t in await mcp_server.mcp.list_tools()}
    assert "get_rfp_summary" in tools


async def test_get_rfp_summary_returns_existing_summary_json(env: dict[str, Any]) -> None:
    _make_opp(env["raw"], "OPP-SUM-1", ["rfp.pdf"])

    from app.job_store import get_job_store
    store = get_job_store()
    job = store.create_job(file_name="rfp.pdf", file_path="/raw/rfp.pdf", opp_id="OPP-SUM-1")
    job_out = env["out"] / job.job_id
    job_out.mkdir(parents=True, exist_ok=True)
    job.output_dir = str(job_out)

    summary_data = {
        "rfp_metadata": {
            "client_name": "Test Client Corp",
            "rfp_title": "Enterprise Modernization",
            "rfp_reference_number": "RFP-12345",
            "estimated_contract_value": "$10M",
            "contract_duration": "3 years",
        },
        "key_dates": [
            {
                "date_id": "KD-01",
                "label": "RFP Due Date",
                "date_type": "submission_deadline",
                "display_date": "2025-12-01",
                "date_value": "2025-12-01",
                "raw_excerpt": "Due 2025-12-01",
                "confidence": "high",
            }
        ],
        "rfp_overview": {
            "summary": "Modernization of legacy systems.",
            "scope_highlights": ["Refactoring", "Deployment"],
            "client_context": "Financial services",
        },
    }
    (job_out / "summary.json").write_text(json.dumps(summary_data), encoding="utf-8")
    store.set_complete(job.job_id, {"rfp_summary": summary_data})

    data = await _call("get_rfp_summary", opp_id="OPP-SUM-1")

    assert data["opp_id"] == "OPP-SUM-1"
    assert data["job_id"] == job.job_id
    assert data["status"] == "complete"
    assert data["summary_url"] == "http://example.test:9000/summary?opp_id=OPP-SUM-1"
    assert data["rfp_metadata"]["client_name"] == "Test Client Corp"
    assert len(data["key_dates"]) == 1
    assert data["key_dates"][0]["label"] == "RFP Due Date"
    assert data["rfp_overview"]["summary"] == "Modernization of legacy systems."
    # Ensure pipeline mock was NOT called since completed summary was reused
    env["summary_mock"].assert_not_called()


async def test_get_rfp_summary_falls_back_to_result_json(env: dict[str, Any]) -> None:
    _make_opp(env["raw"], "OPP-SUM-FALLBACK", ["rfp.pdf"])

    from app.job_store import get_job_store
    store = get_job_store()
    job = store.create_job(file_name="rfp.pdf", file_path="/raw/rfp.pdf", opp_id="OPP-SUM-FALLBACK")
    job_out = env["out"] / job.job_id
    job_out.mkdir(parents=True, exist_ok=True)
    job.output_dir = str(job_out)

    summary_data = {
        "rfp_metadata": {"client_name": "Fallback Corp", "rfp_title": "Security Audit"},
        "key_dates": [],
        "rfp_overview": {"summary": "Security analysis"},
    }
    (job_out / "result.json").write_text(json.dumps({"rfp_summary": summary_data}), encoding="utf-8")
    store.set_complete(job.job_id, {"rfp_summary": summary_data})

    data = await _call("get_rfp_summary", opp_id="OPP-SUM-FALLBACK")

    assert data["status"] == "complete"
    assert data["rfp_metadata"]["client_name"] == "Fallback Corp"
    assert data["summary_url"] == "http://example.test:9000/summary?opp_id=OPP-SUM-FALLBACK"


async def test_get_rfp_summary_starts_background_job_when_not_started(env: dict[str, Any]) -> None:
    _make_opp(env["raw"], "OPP-NEW", ["RFP_Main.pdf"])

    data = await _call("get_rfp_summary", opp_id="OPP-NEW")

    assert data["opp_id"] == "OPP-NEW"
    assert data["status"] in ("pending", "running")
    assert data["summary_url"] == "http://example.test:9000/summary?opp_id=OPP-NEW"
    assert "job_id" in data

    # Await background task
    await asyncio.gather(*mcp_server._background_tasks)

    env["summary_mock"].assert_awaited_once()

    # Now call again to read completed summary
    completed_data = await _call("get_rfp_summary", opp_id="OPP-NEW")
    assert completed_data["status"] == "complete"
    assert completed_data["rfp_metadata"]["client_name"] == "Acme Corp"
    assert len(completed_data["key_dates"]) == 1
    assert completed_data["rfp_overview"]["summary"] == "Acme Corp seeks cloud migration partner."


async def test_get_rfp_summary_force_triggers_new_analysis(env: dict[str, Any]) -> None:
    _make_opp(env["raw"], "OPP-FORCE", ["rfp.pdf"])

    # First call starts job
    first = await _call("get_rfp_summary", opp_id="OPP-FORCE")
    await asyncio.gather(*mcp_server._background_tasks)

    # Calling with force=True should create a new job
    forced = await _call("get_rfp_summary", opp_id="OPP-FORCE", force=True)
    assert forced["job_id"] != first["job_id"]
    await asyncio.gather(*mcp_server._background_tasks)


async def test_get_rfp_summary_unknown_opportunity_raises(env: dict[str, Any]) -> None:
    with pytest.raises(Exception, match="No directory found"):
        await _call("get_rfp_summary", opp_id="NON_EXISTENT_OPP")


async def test_get_rfp_summary_empty_opportunity_raises(env: dict[str, Any]) -> None:
    _make_opp(env["raw"], "OPP-EMPTY", ["unsupported.zip"])
    with pytest.raises(Exception, match="No supported documents"):
        await _call("get_rfp_summary", opp_id="OPP-EMPTY")


async def test_get_rfp_summary_failed_job_reports_error(env: dict[str, Any]) -> None:
    _make_opp(env["raw"], "OPP-FAIL", ["rfp.pdf"])
    started = await _call("get_rfp_summary", opp_id="OPP-FAIL")

    from app.job_store import get_job_store
    get_job_store().set_failed(started["job_id"], "LLM extraction error")

    data = await _call("get_rfp_summary", opp_id="OPP-FAIL")
    assert data["status"] == "failed"
    assert data["error"] == "LLM extraction error"
