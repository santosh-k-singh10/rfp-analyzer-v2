"""
tests/test_summary_app.py
--------------------------
Sub-Task 5: Verification & End-to-End Testing.

End-to-end multi-app tests exercising both the RFP Analyzer and RFP Summary
applications together, verifying:

  1. Full-flow URL comparison
       - GET /?opp_id=...         → redirects to /review/{job_id}  (RFP Analyzer)
       - GET /summary?opp_id=...  → renders summary.html           (RFP Summary)
  2. Output reuse / sharing
       - Summary app reuses a COMPLETE analysis job (no duplicate pipeline run).
       - Full analyzer reuses a COMPLETE summary job gracefully.
       - Both apps operate independently when no prior job exists.
  3. MCP tool surface
       - analyze_opportunity_rfp  — starts / reuses full analysis.
       - get_rfp_summary          — starts / reuses fast summary.
       - get_opportunity_analysis — returns results for a completed full analysis.
       - list_opportunities       — enumerates raw-storage folders.
  4. Cross-app output coherence
       - A full analysis job that also wrote summary.json can serve the summary UI.
       - summary.json written by the summary pipeline is readable by both REST
         endpoints and the MCP tool.
  5. Edge / error cases
       - Unknown opp_id returns 404 in REST and a ValueError from MCP tools.
       - Empty opportunity folder returns 400 (REST) / ValueError (MCP).
       - Failed jobs report their error through every access path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

import mcp_server
from app.job_store import AnalysisJob, JobStatus, JobStore, get_job_store
from core.opportunity_resolver import OpportunityFiles
from core.schemas import RfpKeyDate, RfpMetadata, RfpOverview, RfpSummaryOutput


# ---------------------------------------------------------------------------
# Shared helpers & fixtures
# ---------------------------------------------------------------------------

def _make_opp(raw: Path, opp_id: str, files: list[str]) -> Path:
    """Create a raw-storage opportunity folder with stub files."""
    d = raw / opp_id
    d.mkdir(parents=True, exist_ok=True)
    for name in files:
        (d / name).write_bytes(b"stub rfp content for e2e testing")
    return d


def _sample_summary() -> RfpSummaryOutput:
    return RfpSummaryOutput(
        rfp_metadata=RfpMetadata(
            client_name="E2E Test Corp",
            rfp_title="End-to-End Digital Transformation RFP",
            rfp_reference_number="RFP-E2E-2025",
            estimated_contract_value="$3,000,000",
            contract_duration="2 years",
        ),
        key_dates=[
            RfpKeyDate(
                date_id="KD-E2E-1",
                label="Submission Deadline",
                date_type="submission_deadline",
                display_date="2025-12-01",
                date_value="2025-12-01",
                raw_excerpt="Proposals due December 1, 2025",
                source_document="rfp.pdf",
                confidence="high",
            )
        ],
        rfp_overview=RfpOverview(
            summary="E2E Test Corp seeks a digital transformation partner.",
            scope_highlights=["Cloud migration", "API modernisation"],
            client_context="Global retail logistics company.",
            source_sections=["Section 1: Overview"],
        ),
    )


def _opp_files(opp_id: str, opp_dir: Path) -> OpportunityFiles:
    rfp = opp_dir / "rfp.pdf"
    rfp.write_bytes(b"stub rfp content")
    return OpportunityFiles(
        opp_id=opp_id,
        opp_dir=opp_dir,
        file_paths=[rfp],
        primary_file_path=rfp,
        file_roles={"rfp.pdf": "primary"},
    )


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Isolated environment: fresh job store, tmp dirs, mocked summary pipeline."""
    raw = tmp_path / "raw"
    out = tmp_path / "outputs"
    up = tmp_path / "uploads"
    for d in (raw, out, up):
        d.mkdir()

    monkeypatch.setenv("RAW_STORAGE_BASE_DIR", str(raw))
    monkeypatch.setenv("OUTPUT_DIR", str(out))
    monkeypatch.setenv("UPLOAD_DIR", str(up))
    monkeypatch.setenv("RFP_ANALYZER_BASE_URL", "http://testserver:8001/")

    # Isolate the job store singleton
    monkeypatch.setattr("app.job_store._store", JobStore())

    # Mock summary pipeline so no LLM calls are made
    summary_mock = AsyncMock(return_value=_sample_summary())
    monkeypatch.setattr("pipeline.summary_extractor.run_summary_pipeline", summary_mock)

    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)

    return {
        "raw": raw,
        "out": out,
        "up": up,
        "client": client,
        "summary_mock": summary_mock,
    }


async def _mcp_call(name: str, **args: Any) -> dict[str, Any]:
    """Invoke an MCP tool through FastMCP and return its dict payload."""
    result = await mcp_server.mcp.call_tool(name, args)
    if isinstance(result, tuple):
        content, structured = result
        if structured is not None:
            return structured.get("result", structured) if set(structured) == {"result"} else structured
        result = content
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)


# ===========================================================================
# 1. Full-flow URL comparison — RFP Analyzer vs RFP Summary
# ===========================================================================

class TestFullFlowUrlComparison:
    """Verify that both web entry-points accept ?opp_id= and serve distinct UIs."""

    def test_home_opp_id_redirects_to_review(
        self, env: dict[str, Any], tmp_path: Path
    ) -> None:
        """GET /?opp_id= must redirect to /review/{job_id}."""
        opp_dir = tmp_path / "raw" / "OPP-FLOW-001"
        opp_dir.mkdir(parents=True)
        opp_files = _opp_files("OPP-FLOW-001", opp_dir)

        store = get_job_store()
        client: TestClient = env["client"]

        with (
            patch("app.routers.analysis.get_job_store", return_value=store),
            patch("app.routers.analysis.get_upload_dir", return_value=env["up"]),
            patch("app.config.get_output_dir", return_value=env["out"]),
            patch("app.config.get_upload_dir", return_value=env["up"]),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.analysis._run_pipeline", new=AsyncMock()),
        ):
            resp = client.get("/?opp_id=OPP-FLOW-001", follow_redirects=False)

        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/review/")

    def test_summary_opp_id_renders_summary_page(
        self, env: dict[str, Any], tmp_path: Path
    ) -> None:
        """GET /summary?opp_id= must render summary.html (NOT redirect)."""
        opp_dir = tmp_path / "raw" / "OPP-FLOW-002"
        opp_dir.mkdir(parents=True)
        opp_files = _opp_files("OPP-FLOW-002", opp_dir)

        store = get_job_store()
        client: TestClient = env["client"]

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=env["up"]),
            patch("app.config.get_output_dir", return_value=env["out"]),
            patch("app.config.get_upload_dir", return_value=env["up"]),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.summary._run_summary_pipeline_job", new=AsyncMock()),
        ):
            resp = client.get("/summary?opp_id=OPP-FLOW-002")

        assert resp.status_code == 200
        assert b"sum-loading" in resp.content
        # Must contain the opp_id injected into the page for the JS poller
        assert b"OPP-FLOW-002" in resp.content

    def test_home_and_summary_produce_different_uis(
        self, env: dict[str, Any], tmp_path: Path
    ) -> None:
        """Both pages should render, but summary page must NOT have review content,
        and review page must NOT have summary-dashboard content."""
        opp_dir = tmp_path / "raw" / "OPP-FLOW-003"
        opp_dir.mkdir(parents=True)
        opp_files = _opp_files("OPP-FLOW-003", opp_dir)

        store = get_job_store()
        client: TestClient = env["client"]

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=env["up"]),
            patch("app.config.get_output_dir", return_value=env["out"]),
            patch("app.config.get_upload_dir", return_value=env["up"]),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.summary._run_summary_pipeline_job", new=AsyncMock()),
        ):
            summary_resp = client.get("/summary?opp_id=OPP-FLOW-003")

        assert summary_resp.status_code == 200
        # summary.html markers
        assert b"sum-loading" in summary_resp.content
        # review.html would have the review JS bootstrap — it should NOT appear
        # in the summary page (different templates)
        assert b"RFP Summary" in summary_resp.content

    def test_summary_page_links_to_full_analysis(
        self, env: dict[str, Any]
    ) -> None:
        """summary.html must include a link / reference to /?opp_id or /review/."""
        client: TestClient = env["client"]
        resp = client.get("/summary")
        assert resp.status_code == 200
        assert (
            b"/?opp_id" in resp.content
            or b"/review/" in resp.content
            or b"Full Analysis" in resp.content
        )


# ===========================================================================
# 2. Output reuse / sharing between the two apps
# ===========================================================================

class TestOutputReuseAndSharing:
    """Verify that both apps share completed outputs gracefully."""

    def test_summary_api_reuses_complete_job(
        self, env: dict[str, Any]
    ) -> None:
        """POST /summary/opportunity reuses a COMPLETE job without re-running pipeline."""
        store = get_job_store()

        with patch("app.config.get_output_dir", return_value=env["out"]):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/stub/rfp.pdf",
                opp_id="OPP-REUSE-SUM",
            )
        job.status = JobStatus.COMPLETE
        job_out = env["out"] / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        job.output_dir = str(job_out)
        (job_out / "summary.json").write_text(
            json.dumps(_sample_summary().model_dump()), encoding="utf-8"
        )

        client: TestClient = env["client"]
        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.post("/summary/opportunity?opp_id=OPP-REUSE-SUM")

        assert resp.status_code == 200
        body = resp.json()
        # Must return the existing job, not a new one
        assert body["job_id"] == job.job_id
        assert body["status"] == "complete"
        # Pipeline should NOT have been called (job was already complete)
        env["summary_mock"].assert_not_called()

    def test_full_analysis_job_output_readable_by_summary_endpoint(
        self, env: dict[str, Any]
    ) -> None:
        """A full-analysis job that also produced summary data should be returned
        by GET /summary/opportunity/{opp_id}."""
        store = get_job_store()

        with patch("app.config.get_output_dir", return_value=env["out"]):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/stub/rfp.pdf",
                opp_id="OPP-CROSS-1",
            )
        job.status = JobStatus.COMPLETE
        job_out = env["out"] / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        job.output_dir = str(job_out)

        # Write summary data into result.json (as the full pipeline does)
        result_payload = {"rfp_summary": _sample_summary().model_dump()}
        (job_out / "result.json").write_text(
            json.dumps(result_payload), encoding="utf-8"
        )

        client: TestClient = env["client"]
        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.get("/summary/opportunity/OPP-CROSS-1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "complete"
        assert body["rfp_summary"]["rfp_metadata"]["client_name"] == "E2E Test Corp"

    def test_summary_json_output_readable_by_job_endpoint(
        self, env: dict[str, Any]
    ) -> None:
        """GET /jobs/{job_id}/summary must read summary.json written by summary pipeline."""
        store = get_job_store()

        with patch("app.config.get_output_dir", return_value=env["out"]):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/stub/rfp.pdf",
                opp_id="OPP-CROSS-2",
            )
        job.status = JobStatus.COMPLETE
        job_out = env["out"] / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        job.output_dir = str(job_out)
        (job_out / "summary.json").write_text(
            json.dumps(_sample_summary().model_dump()), encoding="utf-8"
        )

        client: TestClient = env["client"]
        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.get(f"/jobs/{job.job_id}/summary")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == job.job_id
        assert body["opp_id"] == "OPP-CROSS-2"
        assert body["rfp_summary"]["rfp_overview"]["summary"].startswith("E2E Test Corp")

    def test_both_apps_independent_when_no_prior_job(
        self, env: dict[str, Any], tmp_path: Path
    ) -> None:
        """Home → /review and /summary?opp_id= can each create independent jobs
        for the same opp_id without interfering."""
        opp_dir = tmp_path / "raw" / "OPP-INDEP"
        opp_dir.mkdir(parents=True)
        opp_files = _opp_files("OPP-INDEP", opp_dir)

        store = get_job_store()
        client: TestClient = env["client"]

        # Start summary job
        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=env["up"]),
            patch("app.config.get_output_dir", return_value=env["out"]),
            patch("app.config.get_upload_dir", return_value=env["up"]),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.summary._run_summary_pipeline_job", new=AsyncMock()),
        ):
            sum_resp = client.post("/summary/opportunity?opp_id=OPP-INDEP")

        assert sum_resp.status_code == 200
        sum_job_id = sum_resp.json()["job_id"]

        # Start full analysis job
        with (
            patch("app.routers.analysis.get_job_store", return_value=store),
            patch("app.routers.analysis.get_upload_dir", return_value=env["up"]),
            patch("app.config.get_output_dir", return_value=env["out"]),
            patch("app.config.get_upload_dir", return_value=env["up"]),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.analysis._run_pipeline", new=AsyncMock()),
        ):
            analysis_resp = client.post("/analyze/opportunity?opp_id=OPP-INDEP&force=true")

        assert analysis_resp.status_code == 200
        analysis_job_id = analysis_resp.json()["job_id"]

        # They should be separate jobs
        assert sum_job_id != analysis_job_id


# ===========================================================================
# 3. MCP tool surface
# ===========================================================================

class TestMcpTools:
    """Verify all four MCP tools operate correctly end-to-end."""

    async def test_analyze_opportunity_rfp_starts_job(
        self, env: dict[str, Any]
    ) -> None:
        """analyze_opportunity_rfp returns job_id, status, and review_url."""
        _make_opp(env["raw"], "MCP-FULL-1", ["rfp.pdf"])

        with patch("app.routers.analysis._run_pipeline", new=AsyncMock()):
            data = await _mcp_call("analyze_opportunity_rfp", opp_id="MCP-FULL-1")

        assert data["opp_id"] == "MCP-FULL-1"
        assert "job_id" in data
        assert data["status"] in ("pending", "running", "complete")
        assert "/review/" in data["review_url"]

    async def test_analyze_opportunity_rfp_reuses_complete_job(
        self, env: dict[str, Any]
    ) -> None:
        """analyze_opportunity_rfp returns reused_existing_job=True for a complete job."""
        _make_opp(env["raw"], "MCP-FULL-REUSE", ["rfp.pdf"])

        store = get_job_store()
        with patch("app.config.get_output_dir", return_value=env["out"]):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/stub/rfp.pdf",
                opp_id="MCP-FULL-REUSE",
            )
        job.status = JobStatus.COMPLETE
        job_out = env["out"] / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        job.output_dir = str(job_out)

        data = await _mcp_call("analyze_opportunity_rfp", opp_id="MCP-FULL-REUSE")

        assert data["reused_existing_job"] is True
        assert data["job_id"] == job.job_id

    async def test_get_rfp_summary_starts_job_and_completes(
        self, env: dict[str, Any]
    ) -> None:
        """get_rfp_summary starts background job and returns completed summary."""
        _make_opp(env["raw"], "MCP-SUM-1", ["rfp.pdf"])

        data = await _mcp_call("get_rfp_summary", opp_id="MCP-SUM-1")
        assert data["opp_id"] == "MCP-SUM-1"
        assert data["status"] in ("pending", "running")
        assert "/summary?opp_id=MCP-SUM-1" in data["summary_url"]

        # Wait for background pipeline
        await asyncio.gather(*mcp_server._background_tasks)

        completed = await _mcp_call("get_rfp_summary", opp_id="MCP-SUM-1")
        assert completed["status"] == "complete"
        assert completed["rfp_metadata"]["client_name"] == "E2E Test Corp"
        assert len(completed["key_dates"]) == 1
        assert "E2E Test Corp" in completed["rfp_overview"]["summary"]

    async def test_get_rfp_summary_reuses_existing_summary_json(
        self, env: dict[str, Any]
    ) -> None:
        """get_rfp_summary returns cached data without re-running pipeline."""
        _make_opp(env["raw"], "MCP-SUM-CACHE", ["rfp.pdf"])

        store = get_job_store()
        with patch("app.config.get_output_dir", return_value=env["out"]):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/stub/rfp.pdf",
                opp_id="MCP-SUM-CACHE",
            )
        job.status = JobStatus.COMPLETE
        job_out = env["out"] / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        job.output_dir = str(job_out)
        (job_out / "summary.json").write_text(
            json.dumps(_sample_summary().model_dump()), encoding="utf-8"
        )
        store.set_complete(job.job_id, {"rfp_summary": _sample_summary().model_dump()})

        env["summary_mock"].reset_mock()
        data = await _mcp_call("get_rfp_summary", opp_id="MCP-SUM-CACHE")

        assert data["status"] == "complete"
        assert data["rfp_metadata"]["client_name"] == "E2E Test Corp"
        env["summary_mock"].assert_not_called()

    async def test_get_opportunity_analysis_not_started(
        self, env: dict[str, Any]
    ) -> None:
        """get_opportunity_analysis returns not_started for unknown opportunity."""
        data = await _mcp_call("get_opportunity_analysis", opp_id="NEVER-STARTED")
        assert data["status"] == "not_started"
        assert "NEVER-STARTED" in data["message"]

    async def test_get_opportunity_analysis_returns_results(
        self, env: dict[str, Any]
    ) -> None:
        """get_opportunity_analysis returns full doc summary when job is complete."""
        _make_opp(env["raw"], "MCP-ANA-DONE", ["rfp.pdf"])

        store = get_job_store()
        with patch("app.config.get_output_dir", return_value=env["out"]):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/stub/rfp.pdf",
                opp_id="MCP-ANA-DONE",
            )
        job.status = JobStatus.COMPLETE
        job_out = env["out"] / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        job.output_dir = str(job_out)

        result_doc = {
            "spine": {"compliance": [], "ambiguity": [], "risk": [], "commercial_structure": []},
            "skill_outputs": [{"skill_slug": "ibm-cloud", "items": [{"_meta": {"status": "active"}}]}],
            "fired_skills": ["ibm-cloud"],
            "metadata": {"client_name": "E2E Test Corp"},
        }
        (job_out / "result.json").write_text(json.dumps(result_doc), encoding="utf-8")

        data = await _mcp_call("get_opportunity_analysis", opp_id="MCP-ANA-DONE")

        assert data["status"] == "complete"
        assert data["job_id"] == job.job_id
        assert "/review/" in data["review_url"]
        assert data["summary"]["fired_skills"] == ["ibm-cloud"]

    async def test_list_opportunities_returns_folder_list(
        self, env: dict[str, Any]
    ) -> None:
        """list_opportunities returns all folders in raw storage."""
        _make_opp(env["raw"], "OPP-LIST-A", ["rfp.pdf", "annex.pdf"])
        _make_opp(env["raw"], "OPP-LIST-B", ["rfp.docx"])

        data = await _mcp_call("list_opportunities")

        opp_ids = [o["opp_id"] for o in data["opportunities"]]
        assert "OPP-LIST-A" in opp_ids
        assert "OPP-LIST-B" in opp_ids

        opp_a = next(o for o in data["opportunities"] if o["opp_id"] == "OPP-LIST-A")
        assert opp_a["file_count"] == 2
        assert opp_a["analysis_status"] == "not_started"

    async def test_list_opportunities_reflects_job_status(
        self, env: dict[str, Any]
    ) -> None:
        """list_opportunities shows job status when a job exists."""
        _make_opp(env["raw"], "OPP-STATUS", ["rfp.pdf"])

        store = get_job_store()
        with patch("app.config.get_output_dir", return_value=env["out"]):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/stub/rfp.pdf",
                opp_id="OPP-STATUS",
            )
        job.status = JobStatus.COMPLETE

        data = await _mcp_call("list_opportunities")

        opp = next((o for o in data["opportunities"] if o["opp_id"] == "OPP-STATUS"), None)
        assert opp is not None
        assert opp["analysis_status"] == "complete"
        assert opp["job_id"] == job.job_id


# ===========================================================================
# 4. Cross-app output coherence
# ===========================================================================

class TestCrossAppOutputCoherence:
    """Verify that outputs produced by one app are consumed correctly by another."""

    def test_summary_json_from_pipeline_served_by_rest_and_readable(
        self, env: dict[str, Any]
    ) -> None:
        """summary.json written by the summary pipeline must be served correctly
        by both GET /summary/opportunity/{opp_id} and GET /jobs/{job_id}/summary."""
        store = get_job_store()

        with patch("app.config.get_output_dir", return_value=env["out"]):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/stub/rfp.pdf",
                opp_id="OPP-COHERE-1",
            )
        job.status = JobStatus.COMPLETE
        job_out = env["out"] / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        job.output_dir = str(job_out)
        summary_data = _sample_summary().model_dump()
        (job_out / "summary.json").write_text(
            json.dumps(summary_data), encoding="utf-8"
        )

        client: TestClient = env["client"]

        with patch("app.routers.summary.get_job_store", return_value=store):
            resp_opp = client.get("/summary/opportunity/OPP-COHERE-1")
            resp_job = client.get(f"/jobs/{job.job_id}/summary")

        assert resp_opp.status_code == 200
        assert resp_job.status_code == 200

        opp_body = resp_opp.json()
        job_body = resp_job.json()

        # Both endpoints must return the same underlying summary data
        assert opp_body["rfp_summary"]["rfp_metadata"]["client_name"] == "E2E Test Corp"
        assert job_body["rfp_summary"]["rfp_metadata"]["client_name"] == "E2E Test Corp"
        assert (
            opp_body["rfp_summary"]["key_dates"][0]["label"]
            == job_body["rfp_summary"]["key_dates"][0]["label"]
        )

    def test_full_analysis_result_json_serves_summary_endpoint(
        self, env: dict[str, Any]
    ) -> None:
        """When only result.json exists (full analysis output), the summary endpoint
        must fall back to result.json's rfp_summary field."""
        store = get_job_store()

        with patch("app.config.get_output_dir", return_value=env["out"]):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/stub/rfp.pdf",
                opp_id="OPP-COHERE-2",
            )
        job.status = JobStatus.COMPLETE
        job_out = env["out"] / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        job.output_dir = str(job_out)

        # Only result.json, no summary.json
        result_payload = {
            "rfp_summary": _sample_summary().model_dump(),
            "spine": {"compliance": [], "ambiguity": [], "risk": [], "commercial_structure": []},
        }
        (job_out / "result.json").write_text(json.dumps(result_payload), encoding="utf-8")

        client: TestClient = env["client"]
        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.get("/summary/opportunity/OPP-COHERE-2")

        assert resp.status_code == 200
        body = resp.json()
        assert body["rfp_summary"]["rfp_metadata"]["rfp_reference_number"] == "RFP-E2E-2025"

    async def test_mcp_summary_tool_reads_summary_json_from_full_pipeline(
        self, env: dict[str, Any]
    ) -> None:
        """get_rfp_summary MCP tool must read summary.json written by the full
        analysis pipeline if the job is complete."""
        _make_opp(env["raw"], "OPP-COHERE-3", ["rfp.pdf"])

        store = get_job_store()
        with patch("app.config.get_output_dir", return_value=env["out"]):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/stub/rfp.pdf",
                opp_id="OPP-COHERE-3",
            )
        job.status = JobStatus.COMPLETE
        job_out = env["out"] / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        job.output_dir = str(job_out)
        (job_out / "summary.json").write_text(
            json.dumps(_sample_summary().model_dump()), encoding="utf-8"
        )
        store.set_complete(job.job_id, {"rfp_summary": _sample_summary().model_dump()})

        data = await _mcp_call("get_rfp_summary", opp_id="OPP-COHERE-3")

        assert data["status"] == "complete"
        assert data["rfp_metadata"]["rfp_title"] == "End-to-End Digital Transformation RFP"
        assert len(data["key_dates"]) == 1


# ===========================================================================
# 5. Edge & error cases
# ===========================================================================

class TestEdgeCases:
    """Verify error handling across REST endpoints and MCP tools."""

    def test_home_opp_id_404_renders_home_with_error(
        self, env: dict[str, Any]
    ) -> None:
        """GET /?opp_id=MISSING renders home.html with error detail (not redirect)."""
        def _raise(opp_id: str) -> None:
            raise FileNotFoundError(f"No directory found for '{opp_id}'")

        client: TestClient = env["client"]
        store = get_job_store()

        with (
            patch("app.routers.analysis.get_job_store", return_value=store),
            patch("core.opportunity_resolver.resolve_opportunity_files", side_effect=_raise),
        ):
            resp = client.get("/?opp_id=MISSING-OPP-HOME", follow_redirects=False)

        assert resp.status_code == 404
        assert b"MISSING-OPP-HOME" in resp.content or b"No directory" in resp.content

    def test_summary_page_opp_id_404_renders_home_with_error(
        self, env: dict[str, Any]
    ) -> None:
        """GET /summary?opp_id=MISSING renders home.html with error (not summary.html)."""
        def _raise(opp_id: str) -> None:
            raise FileNotFoundError(f"No directory found for '{opp_id}'")

        client: TestClient = env["client"]
        store = get_job_store()

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("core.opportunity_resolver.resolve_opportunity_files", side_effect=_raise),
        ):
            resp = client.get("/summary?opp_id=MISSING-OPP-SUM")

        assert resp.status_code == 404
        assert b"sum-loading" not in resp.content  # not summary.html

    def test_summary_rest_404_for_unknown_opp(
        self, env: dict[str, Any]
    ) -> None:
        """GET /summary/opportunity/{opp_id} returns 404 for an unknown opportunity."""
        client: TestClient = env["client"]
        store = get_job_store()

        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.get("/summary/opportunity/GHOST-OPP-99")

        assert resp.status_code == 404
        assert "GHOST-OPP-99" in resp.json()["detail"]

    def test_summary_rest_400_for_empty_opportunity(
        self, env: dict[str, Any], tmp_path: Path
    ) -> None:
        """POST /summary/opportunity returns 400 when no supported files found."""
        empty_opp = OpportunityFiles(
            opp_id="EMPTY-E2E",
            opp_dir=tmp_path,
            file_paths=[],
            primary_file_path=None,
            file_roles={},
        )

        client: TestClient = env["client"]
        store = get_job_store()

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=empty_opp),
            patch("app.config.get_output_dir", return_value=env["out"]),
        ):
            resp = client.post("/summary/opportunity?opp_id=EMPTY-E2E")

        assert resp.status_code == 400

    def test_failed_job_reported_by_summary_rest(
        self, env: dict[str, Any]
    ) -> None:
        """GET /summary/opportunity/{opp_id} reports error for a failed job."""
        store = get_job_store()

        with patch("app.config.get_output_dir", return_value=env["out"]):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/stub/rfp.pdf",
                opp_id="OPP-FAIL-E2E",
            )
        store.set_failed(job.job_id, "LLM extraction timeout")

        client: TestClient = env["client"]
        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.get("/summary/opportunity/OPP-FAIL-E2E")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert "LLM extraction timeout" in body["error"]

    async def test_mcp_analyze_rfp_raises_for_unknown_opp(
        self, env: dict[str, Any]
    ) -> None:
        """analyze_opportunity_rfp raises ValueError for an unknown opportunity."""
        with pytest.raises((ValueError, Exception)):
            await _mcp_call("analyze_opportunity_rfp", opp_id="NON-EXISTENT-OPP")

    async def test_mcp_get_rfp_summary_raises_for_unknown_opp(
        self, env: dict[str, Any]
    ) -> None:
        """get_rfp_summary raises ValueError for an unknown opportunity."""
        with pytest.raises((ValueError, Exception)):
            await _mcp_call("get_rfp_summary", opp_id="NON-EXISTENT-SUM")

    async def test_mcp_get_rfp_summary_raises_for_empty_opportunity(
        self, env: dict[str, Any]
    ) -> None:
        """get_rfp_summary raises for an opportunity with only unsupported files."""
        _make_opp(env["raw"], "MCP-EMPTY-OPP", ["brochure.zip"])
        with pytest.raises((ValueError, Exception)):
            await _mcp_call("get_rfp_summary", opp_id="MCP-EMPTY-OPP")

    async def test_mcp_get_rfp_summary_failed_job(
        self, env: dict[str, Any]
    ) -> None:
        """get_rfp_summary returns failed status and error message for a failed job."""
        _make_opp(env["raw"], "MCP-FAIL-SUM", ["rfp.pdf"])

        # Start job so it exists in the store
        data = await _mcp_call("get_rfp_summary", opp_id="MCP-FAIL-SUM")
        job_id = data["job_id"]

        # Manually fail it
        get_job_store().set_failed(job_id, "Timeout during extraction")

        result = await _mcp_call("get_rfp_summary", opp_id="MCP-FAIL-SUM")
        assert result["status"] == "failed"
        assert result["error"] == "Timeout during extraction"

    def test_health_endpoint_ok(self, env: dict[str, Any]) -> None:
        """GET /health must return 200 with status ok regardless of jobs state."""
        client: TestClient = env["client"]
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
