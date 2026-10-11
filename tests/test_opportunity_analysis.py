"""
tests/test_opportunity_analysis.py
------------------------------------
End-to-end flow tests for Sub-Task 5: End-to-End Verification & Testing.

Each test scenario exercises a full vertical slice across the four integrated
sub-tasks:

  1. Opportunity storage setup  → core/opportunity_resolver.py
  2. Web URL redirection         → GET /?opp_id=...   (app/main.py)
  3. Status polling              → GET /jobs/{id}
                                   GET /jobs/opportunity/{opp_id}
  4. MCP tool access             → analyze_opportunity_rfp
                                   get_opportunity_analysis

The pipeline is always mocked so these tests run without real LLM calls.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.job_store import AnalysisJob, JobStatus, JobStore
from core.opportunity_resolver import OpportunityFiles, resolve_opportunity_files
import mcp_server


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_raw_opp(raw_dir: Path, opp_id: str, filenames: list[str]) -> Path:
    """Create a raw opportunity directory with stub files."""
    opp_dir = raw_dir / opp_id
    opp_dir.mkdir(parents=True, exist_ok=True)
    for name in filenames:
        (opp_dir / name).write_bytes(b"stub content")
    return opp_dir


def _make_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, JobStore, Path, Path, Path]:
    """Return (client, store, raw_dir, output_dir, upload_dir)."""
    raw_dir = tmp_path / "raw"
    output_dir = tmp_path / "outputs"
    upload_dir = tmp_path / "uploads"
    for d in (raw_dir, output_dir, upload_dir):
        d.mkdir()
    monkeypatch.setenv("RAW_STORAGE_BASE_DIR", str(raw_dir))
    monkeypatch.setenv("OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("RFP_ANALYZER_BASE_URL", "http://localhost:8001/")

    store = JobStore()
    monkeypatch.setattr("app.job_store._store", store)

    pipeline = AsyncMock()
    monkeypatch.setattr("app.routers.analysis._run_pipeline", pipeline)

    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)
    return client, store, raw_dir, output_dir, upload_dir


def _finish_job(
    store: JobStore,
    job_id: str,
    result: dict | None = None,
    md: str = "# Analysis Report",
) -> None:
    """Simulate pipeline completion: write outputs, flip status."""
    if result is None:
        result = {
            "skill_outputs": [
                {"skill_slug": "ams", "items": [{"id": "A1"}]},
            ],
            "spine": {
                "compliance": [{"id": "C1"}],
                "ambiguity": [],
                "risk": [{"id": "R1", "title": "Risk One"}],
                "commercial_structure": [],
            },
            "fired_skills": ["ams"],
            "metadata": {"duration_seconds": 2.0},
        }
    job = store.get_job(job_id)
    assert job is not None and job.output_dir
    out = Path(job.output_dir)
    (out / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (out / "result.md").write_text(md, encoding="utf-8")
    store.set_complete(job_id, result)


async def _mcp_call(name: str, **kwargs):
    """Invoke an MCP tool and return the parsed dict payload."""
    result = await mcp_server.mcp.call_tool(name, kwargs)
    if isinstance(result, tuple):
        content, structured = result
        if structured is not None:
            return structured.get("result", structured) if set(structured) == {"result"} else structured
        result = content
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)


# ===========================================================================
# Scenario 1: Full happy-path web flow
#   setup storage → GET /?opp_id= redirects → GET /jobs/{id} shows opp_id
#   → GET /jobs/opportunity/{opp_id} returns job detail
# ===========================================================================

class TestWebFlowHappyPath:
    """Full slice: resolver → web redirect → REST status checks."""

    def test_opp_dir_setup_and_file_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """resolver finds the directory and classifies files."""
        _, _, raw_dir, *_ = _make_client(tmp_path, monkeypatch)
        _make_raw_opp(raw_dir, "E2E-001", ["RFP_main.pdf", "Annex_A.pdf", "Q&A_Clarifications.docx"])

        opp = resolve_opportunity_files("E2E-001", base_dir=raw_dir)

        assert opp.opp_id == "E2E-001"
        assert len(opp.file_paths) == 3
        assert opp.primary_file_path is not None
        assert opp.primary_file_path.name == "RFP_main.pdf"
        assert opp.file_roles["Annex_A.pdf"] == "annexure"
        assert opp.file_roles["Q&A_Clarifications.docx"] == "qna"

    def test_web_redirect_creates_job_and_redirects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """GET /?opp_id=E2E-002 → 303 redirect to /review/{job_id}."""
        client, store, raw_dir, *_ = _make_client(tmp_path, monkeypatch)
        _make_raw_opp(raw_dir, "E2E-002", ["RFP_main.pdf"])

        resp = client.get("/?opp_id=E2E-002", follow_redirects=False)

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/review/")
        job_id = location.split("/review/")[1]
        uuid.UUID(job_id)  # must be a valid UUID

    def test_get_job_by_id_includes_opp_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """After redirect, GET /jobs/{job_id} shows opp_id in response."""
        client, store, raw_dir, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        _make_raw_opp(raw_dir, "E2E-003", ["RFP_main.pdf"])

        redir = client.get("/?opp_id=E2E-003", follow_redirects=False)
        assert redir.status_code == 303
        job_id = redir.headers["location"].split("/review/")[1]

        resp = client.get(f"/jobs/{job_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == job_id
        assert body["opp_id"] == "E2E-003"
        assert body["status"] in ("pending", "running")

    def test_get_job_by_opp_id_returns_created_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """GET /jobs/opportunity/{opp_id} finds the job created by the web redirect."""
        client, store, raw_dir, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        _make_raw_opp(raw_dir, "E2E-004", ["RFP_main.pdf"])

        redir = client.get("/?opp_id=E2E-004", follow_redirects=False)
        assert redir.status_code == 303
        job_id = redir.headers["location"].split("/review/")[1]

        resp = client.get("/jobs/opportunity/E2E-004")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == job_id
        assert body["opp_id"] == "E2E-004"

    def test_complete_state_visible_via_both_endpoints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Finishing the pipeline is reflected in both /jobs/{id} and /jobs/opportunity/{opp_id}."""
        client, store, raw_dir, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        _make_raw_opp(raw_dir, "E2E-005", ["RFP_main.pdf"])

        redir = client.get("/?opp_id=E2E-005", follow_redirects=False)
        job_id = redir.headers["location"].split("/review/")[1]

        # Simulate pipeline completion
        _finish_job(store, job_id)

        r1 = client.get(f"/jobs/{job_id}")
        r2 = client.get("/jobs/opportunity/E2E-005")

        assert r1.json()["status"] == "complete"
        assert r2.json()["status"] == "complete"
        assert r2.json()["job_id"] == job_id


# ===========================================================================
# Scenario 2: Full happy-path REST API flow
#   POST /analyze/opportunity → status check → verify opp_id round-trip
# ===========================================================================

class TestRestApiFlow:
    """Full slice: POST /analyze/opportunity → status polling."""

    def test_post_analyze_returns_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """POST /analyze/opportunity?opp_id=... creates job and returns JSON."""
        client, store, raw_dir, *_ = _make_client(tmp_path, monkeypatch)
        _make_raw_opp(raw_dir, "API-001", ["RFP.pdf", "Annex.xlsx"])

        resp = client.post("/analyze/opportunity?opp_id=API-001")

        assert resp.status_code == 200
        body = resp.json()
        assert body["opp_id"] == "API-001"
        assert uuid.UUID(body["job_id"])  # valid UUID
        assert body["status"] == "pending"

    def test_dedup_reuses_running_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A second POST while the first job is RUNNING returns the same job_id."""
        client, store, raw_dir, output_dir, *_ = _make_client(tmp_path, monkeypatch)
        _make_raw_opp(raw_dir, "API-002", ["RFP.pdf"])

        r1 = client.post("/analyze/opportunity?opp_id=API-002")
        job_id = r1.json()["job_id"]

        # Flip to RUNNING
        store.set_running(job_id)

        r2 = client.post("/analyze/opportunity?opp_id=API-002")
        assert r2.json()["job_id"] == job_id

    def test_force_creates_new_job_over_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """force=true bypasses dedup even when a COMPLETE job exists."""
        client, store, raw_dir, output_dir, *_ = _make_client(tmp_path, monkeypatch)
        _make_raw_opp(raw_dir, "API-003", ["RFP.pdf"])

        r1 = client.post("/analyze/opportunity?opp_id=API-003")
        job_id_1 = r1.json()["job_id"]
        _finish_job(store, job_id_1)

        r2 = client.post("/analyze/opportunity?opp_id=API-003&force=true")
        assert r2.json()["job_id"] != job_id_1

    def test_unknown_opp_returns_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, *_ = _make_client(tmp_path, monkeypatch)
        resp = client.post("/analyze/opportunity?opp_id=GHOST-999")
        assert resp.status_code == 404

    def test_status_transitions_reflected_in_get_jobs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """pending → running → complete state is visible via GET /jobs/{id}."""
        client, store, raw_dir, output_dir, *_ = _make_client(tmp_path, monkeypatch)
        _make_raw_opp(raw_dir, "API-004", ["RFP.pdf"])

        job_id = client.post("/analyze/opportunity?opp_id=API-004").json()["job_id"]
        assert client.get(f"/jobs/{job_id}").json()["status"] == "pending"

        store.set_running(job_id)
        assert client.get(f"/jobs/{job_id}").json()["status"] == "running"

        _finish_job(store, job_id)
        assert client.get(f"/jobs/{job_id}").json()["status"] == "complete"


# ===========================================================================
# Scenario 3: Full MCP tool flow
#   list_opportunities → analyze_opportunity_rfp → get_opportunity_analysis
# ===========================================================================

class TestMcpToolFlow:
    """Full slice: MCP list → analyze → poll → complete."""

    @pytest.fixture(autouse=True)
    def _env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        raw = tmp_path / "raw"
        out = tmp_path / "outputs"
        up = tmp_path / "uploads"
        for d in (raw, out, up):
            d.mkdir()
        monkeypatch.setenv("RAW_STORAGE_BASE_DIR", str(raw))
        monkeypatch.setenv("OUTPUT_DIR", str(out))
        monkeypatch.setenv("UPLOAD_DIR", str(up))
        monkeypatch.setenv("RFP_ANALYZER_BASE_URL", "http://localhost:8001/")
        monkeypatch.setattr("app.job_store._store", JobStore())
        pipeline = AsyncMock()
        monkeypatch.setattr("app.routers.analysis._run_pipeline", pipeline)
        self.raw = raw
        self.out = out
        self.pipeline = pipeline

    @pytest.mark.asyncio
    async def test_list_discovers_storage_opportunities(self):
        """list_opportunities returns all opp dirs and counts supported files."""
        _make_raw_opp(self.raw, "MCP-001", ["RFP_main.pdf", "Annex.docx"])
        _make_raw_opp(self.raw, "MCP-002", ["rfp.pdf"])

        data = await _mcp_call("list_opportunities")

        by_id = {o["opp_id"]: o for o in data["opportunities"]}
        assert set(by_id) >= {"MCP-001", "MCP-002"}
        assert by_id["MCP-001"]["file_count"] == 2

    @pytest.mark.asyncio
    async def test_analyze_starts_job_returns_pending(self):
        """analyze_opportunity_rfp creates a job and returns pending/running."""
        _make_raw_opp(self.raw, "MCP-003", ["RFP_main.pdf"])

        data = await _mcp_call("analyze_opportunity_rfp", opp_id="MCP-003")

        assert data["opp_id"] == "MCP-003"
        assert data["status"] in ("pending", "running")
        assert data["reused_existing_job"] is False
        assert "/review/" in data["review_url"]

    @pytest.mark.asyncio
    async def test_get_analysis_not_started_before_analyze(self):
        """get_opportunity_analysis returns not_started when no job exists."""
        data = await _mcp_call("get_opportunity_analysis", opp_id="MCP-UNKNOWN")
        assert data["status"] == "not_started"

    @pytest.mark.asyncio
    async def test_full_analyze_then_get_flow(self):
        """analyze → simulate complete → get_opportunity_analysis returns results."""
        from app.job_store import get_job_store

        _make_raw_opp(self.raw, "MCP-004", ["RFP_main.pdf", "Annex.xlsx"])

        started = await _mcp_call("analyze_opportunity_rfp", opp_id="MCP-004")
        job_id = started["job_id"]

        # Before completion
        in_progress = await _mcp_call("get_opportunity_analysis", opp_id="MCP-004")
        assert in_progress["status"] in ("pending", "running")
        assert "summary" not in in_progress

        # Simulate pipeline completion
        store = get_job_store()
        _finish_job(store, job_id)

        # After completion
        done = await _mcp_call("get_opportunity_analysis", opp_id="MCP-004")

        assert done["status"] == "complete"
        assert done["job_id"] == job_id
        assert done["primary_file_name"] == "RFP_main.pdf"
        assert "summary" in done
        assert "markdown" in done
        assert done["markdown"] == "# Analysis Report"
        assert done["summary"]["counts"]["risk"] == 1
        assert [r["id"] for r in done["risks"]] == ["R1"]

    @pytest.mark.asyncio
    async def test_mcp_list_shows_analysis_started(self):
        """list_opportunities shows analysis_status=pending/running/complete after analyze."""
        from app.job_store import get_job_store

        _make_raw_opp(self.raw, "MCP-005", ["RFP.pdf"])

        # Before analyze: not_started
        data_before = await _mcp_call("list_opportunities")
        by_id = {o["opp_id"]: o for o in data_before["opportunities"]}
        assert by_id["MCP-005"]["analysis_status"] == "not_started"

        # After analyze
        started = await _mcp_call("analyze_opportunity_rfp", opp_id="MCP-005")
        job_id = started["job_id"]
        _finish_job(get_job_store(), job_id)

        data_after = await _mcp_call("list_opportunities")
        by_id_after = {o["opp_id"]: o for o in data_after["opportunities"]}
        assert by_id_after["MCP-005"]["analysis_status"] == "complete"

    @pytest.mark.asyncio
    async def test_analyze_reuse_then_force(self):
        """analyze → reuse running → force creates new job."""
        from app.job_store import get_job_store

        _make_raw_opp(self.raw, "MCP-006", ["RFP.pdf"])
        first = await _mcp_call("analyze_opportunity_rfp", opp_id="MCP-006")
        get_job_store().set_running(first["job_id"])

        reused = await _mcp_call("analyze_opportunity_rfp", opp_id="MCP-006")
        assert reused["reused_existing_job"] is True
        assert reused["job_id"] == first["job_id"]

        forced = await _mcp_call("analyze_opportunity_rfp", opp_id="MCP-006", force=True)
        assert forced["reused_existing_job"] is False
        assert forced["job_id"] != first["job_id"]


# ===========================================================================
# Scenario 4: Cross-layer consistency
#   Job created via web also visible in MCP (shared store), and vice versa
# ===========================================================================

class TestCrossLayerConsistency:
    """Verify that the web layer and MCP layer share the same job store."""

    @pytest.fixture(autouse=True)
    def _env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        raw = tmp_path / "raw"
        out = tmp_path / "outputs"
        up = tmp_path / "uploads"
        for d in (raw, out, up):
            d.mkdir()
        monkeypatch.setenv("RAW_STORAGE_BASE_DIR", str(raw))
        monkeypatch.setenv("OUTPUT_DIR", str(out))
        monkeypatch.setenv("UPLOAD_DIR", str(up))
        monkeypatch.setenv("RFP_ANALYZER_BASE_URL", "http://localhost:8001/")
        store = JobStore()
        monkeypatch.setattr("app.job_store._store", store)
        pipeline = AsyncMock()
        monkeypatch.setattr("app.routers.analysis._run_pipeline", pipeline)
        self.raw = raw
        self.store = store
        from app.main import app
        self.client = TestClient(app, raise_server_exceptions=False)

    @pytest.mark.asyncio
    async def test_job_created_via_web_is_visible_in_mcp(self):
        """A job started through the web URL is retrievable via MCP."""
        _make_raw_opp(self.raw, "CROSS-001", ["RFP.pdf"])

        redir = self.client.get("/?opp_id=CROSS-001", follow_redirects=False)
        assert redir.status_code == 303
        job_id = redir.headers["location"].split("/review/")[1]

        data = await _mcp_call("get_opportunity_analysis", opp_id="CROSS-001")

        assert data["job_id"] == job_id
        assert data["status"] in ("pending", "running")

    @pytest.mark.asyncio
    async def test_job_completed_via_mcp_visible_in_rest_api(self):
        """A job finished through the MCP path is visible via REST /jobs/opportunity/{opp_id}."""
        _make_raw_opp(self.raw, "CROSS-002", ["RFP.pdf"])

        started = await _mcp_call("analyze_opportunity_rfp", opp_id="CROSS-002")
        job_id = started["job_id"]
        _finish_job(self.store, job_id)

        resp = self.client.get("/jobs/opportunity/CROSS-002")
        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == job_id
        assert body["status"] == "complete"
