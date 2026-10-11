"""
tests/test_summary_endpoints.py
---------------------------------
Unit and integration tests for Sub-Task 2: Summary Backend Endpoints & Job Store Support.

Coverage areas:
  1. start_summary_analysis:
     - Opportunity resolution and upload staging
     - Job creation in JobStore
     - Background task scheduling
     - Job reuse (RUNNING / COMPLETE) unless force=True
     - 404 on missing opportunity
     - 400 on no supported files
  2. _run_summary_pipeline_job:
     - Executes run_summary_pipeline, writes summary.json, marks COMPLETE
     - Marks FAILED on exception
  3. POST /summary/opportunity:
     - Triggers summary analysis and returns {job_id, status, opp_id}
     - 404 / 400 error handling
     - force=true flag
  4. GET /summary/opportunity/{opp_id}:
     - Returns summary by opp_id
     - 404 on unknown opp_id
     - Fallback to result.json if summary.json missing
  5. GET /jobs/{job_id}/summary:
     - Returns summary by job_id
     - 404 on unknown job_id
     - 400 on invalid job_id
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

from app.job_store import AnalysisJob, JobStatus, JobStore
from core.opportunity_resolver import OpportunityFiles
from core.schemas import RfpKeyDate, RfpMetadata, RfpOverview, RfpSummaryOutput


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

def _make_opp_files(
    opp_id: str,
    opp_dir: Path,
    files: list[tuple[str, str]],  # [(filename, role), ...]
) -> OpportunityFiles:
    """Build OpportunityFiles with real dummy files on disk."""
    file_paths: list[Path] = []
    file_roles: dict[str, str] = {}
    for name, role in files:
        p = opp_dir / name
        p.write_bytes(b"dummy RFP content for testing summary")
        file_paths.append(p)
        file_roles[name] = role
    primary = next((p for p in file_paths if file_roles[p.name] == "primary"), file_paths[0] if file_paths else None)
    if primary is not None:
        file_roles[primary.name] = "primary"
    return OpportunityFiles(
        opp_id=opp_id,
        opp_dir=opp_dir,
        file_paths=file_paths,
        primary_file_path=primary,
        file_roles=file_roles,
    )


def _sample_summary_output() -> RfpSummaryOutput:
    return RfpSummaryOutput(
        rfp_metadata=RfpMetadata(
            client_name="Test Enterprise Corp",
            rfp_title="Digital Transformation RFP",
            rfp_reference_number="RFP-2025-999",
            estimated_contract_value="$5,000,000",
            contract_duration="3 years",
        ),
        key_dates=[
            RfpKeyDate(
                date_id="KD-001",
                label="Submission Deadline",
                date_type="submission_deadline",
                display_date="2025-09-30",
                date_value="2025-09-30",
                raw_excerpt="Proposals due September 30, 2025",
                source_document="rfp.pdf",
                confidence="high",
            )
        ],
        rfp_overview=RfpOverview(
            summary="Test Enterprise Corp is procuring digital transformation services.",
            scope_highlights=["Cloud migration", "Core modernisation"],
            client_context="Global logistics firm.",
            source_sections=["Section 1: Overview"],
        ),
    )


def _make_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, JobStore, Path, Path]:
    output_dir = tmp_path / "outputs"
    upload_dir = tmp_path / "uploads"
    output_dir.mkdir(parents=True, exist_ok=True)
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))

    store = JobStore()

    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)
    return client, store, output_dir, upload_dir


async def _noop_summary_pipeline(*args, **kwargs) -> None:
    pass


# ---------------------------------------------------------------------------
# 1. start_summary_analysis unit tests
# ---------------------------------------------------------------------------

class TestStartSummaryAnalysis:
    @pytest.mark.asyncio
    async def test_creates_and_schedules_summary_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        output_dir = tmp_path / "outputs"
        upload_dir = tmp_path / "uploads"
        output_dir.mkdir(parents=True, exist_ok=True)
        upload_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("OUTPUT_DIR", str(output_dir))
        monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))

        store = JobStore()
        opp_dir = tmp_path / "raw" / "OPP-SUM-001"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-SUM-001", opp_dir, [("rfp.pdf", "primary"), ("annex.pdf", "annexure")])

        bg = BackgroundTasks()

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
        ):
            from app.routers.summary import start_summary_analysis
            job = await start_summary_analysis("OPP-SUM-001", bg)

        assert job.opp_id == "OPP-SUM-001"
        assert job.status == JobStatus.PENDING
        assert len(job.file_names) == 2
        assert len(bg.tasks) == 1
        assert store.get_job(job.job_id) is not None

    @pytest.mark.asyncio
    async def test_reuses_running_or_complete_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        output_dir = tmp_path / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        store = JobStore()

        with patch("app.config.get_output_dir", return_value=output_dir):
            existing = store.create_job(
                file_name="rfp.pdf",
                file_path="/path/rfp.pdf",
                opp_id="OPP-REUSE",
            )
        existing.status = JobStatus.COMPLETE

        bg = BackgroundTasks()

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.config.get_output_dir", return_value=output_dir),
        ):
            from app.routers.summary import start_summary_analysis
            reused = await start_summary_analysis("OPP-REUSE", bg, force=False)

        assert reused.job_id == existing.job_id
        assert len(bg.tasks) == 0  # No new background task scheduled

    @pytest.mark.asyncio
    async def test_force_creates_new_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        output_dir = tmp_path / "outputs"
        upload_dir = tmp_path / "uploads"
        output_dir.mkdir(parents=True, exist_ok=True)
        upload_dir.mkdir(parents=True, exist_ok=True)
        store = JobStore()

        with patch("app.config.get_output_dir", return_value=output_dir):
            existing = store.create_job(
                file_name="rfp.pdf",
                file_path="/path/rfp.pdf",
                opp_id="OPP-FORCE",
            )
        existing.status = JobStatus.COMPLETE

        opp_dir = tmp_path / "raw" / "OPP-FORCE"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-FORCE", opp_dir, [("rfp.pdf", "primary")])

        bg = BackgroundTasks()

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
        ):
            from app.routers.summary import start_summary_analysis
            new_job = await start_summary_analysis("OPP-FORCE", bg, force=True)

        assert new_job.job_id != existing.job_id
        assert len(bg.tasks) == 1

    @pytest.mark.asyncio
    async def test_raises_404_when_opp_not_found(self):
        store = JobStore()
        bg = BackgroundTasks()

        def _raise_missing(opp_id):
            raise FileNotFoundError(f"No opportunity '{opp_id}'")

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("core.opportunity_resolver.resolve_opportunity_files", side_effect=_raise_missing),
        ):
            from app.routers.summary import start_summary_analysis
            with pytest.raises(HTTPException) as exc_info:
                await start_summary_analysis("UNKNOWN-OPP", bg)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_raises_400_when_no_files(self, tmp_path: Path):
        store = JobStore()
        bg = BackgroundTasks()
        empty_opp = OpportunityFiles(
            opp_id="EMPTY-OPP",
            opp_dir=tmp_path,
            file_paths=[],
            primary_file_path=None,
            file_roles={},
        )

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=empty_opp),
        ):
            from app.routers.summary import start_summary_analysis
            with pytest.raises(HTTPException) as exc_info:
                await start_summary_analysis("EMPTY-OPP", bg)

        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# 2. _run_summary_pipeline_job tests
# ---------------------------------------------------------------------------

class TestRunSummaryPipelineJob:
    @pytest.mark.asyncio
    async def test_runs_pipeline_and_writes_summary_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        output_dir = tmp_path / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("OUTPUT_DIR", str(output_dir))

        store = JobStore()
        with patch("app.config.get_output_dir", return_value=output_dir):
            job = store.create_job(file_name="rfp.pdf", file_path="/x/rfp.pdf")

        summary_output = _sample_summary_output()

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_output_dir", return_value=output_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch(
                "pipeline.summary_extractor.run_summary_pipeline",
                new_callable=AsyncMock,
                return_value=summary_output,
            ),
        ):
            from app.routers.summary import _run_summary_pipeline_job
            await _run_summary_pipeline_job(
                job.job_id,
                ["/x/rfp.pdf"],
                "/x/rfp.pdf",
                {"rfp.pdf": "primary"},
            )

        updated_job = store.get_job(job.job_id)
        assert updated_job.status == JobStatus.COMPLETE
        summary_file = output_dir / job.job_id / "summary.json"
        assert summary_file.exists()
        saved_data = json.loads(summary_file.read_text(encoding="utf-8"))
        assert saved_data["rfp_metadata"]["client_name"] == "Test Enterprise Corp"

    @pytest.mark.asyncio
    async def test_sets_failed_on_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        output_dir = tmp_path / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("OUTPUT_DIR", str(output_dir))

        store = JobStore()
        with patch("app.config.get_output_dir", return_value=output_dir):
            job = store.create_job(file_name="rfp.pdf", file_path="/x/rfp.pdf")

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_output_dir", return_value=output_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch(
                "pipeline.summary_extractor.run_summary_pipeline",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Extraction failed"),
            ),
        ):
            from app.routers.summary import _run_summary_pipeline_job
            await _run_summary_pipeline_job(
                job.job_id,
                ["/x/rfp.pdf"],
                "/x/rfp.pdf",
                {"rfp.pdf": "primary"},
            )

        updated_job = store.get_job(job.job_id)
        assert updated_job.status == JobStatus.FAILED
        assert "Extraction failed" in updated_job.error


# ---------------------------------------------------------------------------
# 3. POST /summary/opportunity endpoint tests
# ---------------------------------------------------------------------------

class TestPostSummaryOpportunityEndpoint:
    def test_triggers_summary_analysis_returns_job_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        opp_dir = tmp_path / "raw" / "OPP-POST-1"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-POST-1", opp_dir, [("rfp.pdf", "primary")])

        with (
            patch("app.routers.summary._run_summary_pipeline_job", side_effect=_noop_summary_pipeline),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
        ):
            resp = client.post("/summary/opportunity?opp_id=OPP-POST-1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["opp_id"] == "OPP-POST-1"
        assert body["status"] == "pending"
        assert "job_id" in body

    def test_404_when_opportunity_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, *_ = _make_client(tmp_path, monkeypatch)

        def _raise(opp_id):
            raise FileNotFoundError(f"No opportunity {opp_id}")

        with (
            patch("core.opportunity_resolver.resolve_opportunity_files", side_effect=_raise),
            patch("app.routers.summary.get_job_store", return_value=store),
        ):
            resp = client.post("/summary/opportunity?opp_id=MISSING-OPP")

        assert resp.status_code == 404

    def test_400_when_no_files_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, _ = _make_client(tmp_path, monkeypatch)
        empty_opp = OpportunityFiles(
            opp_id="EMPTY-1",
            opp_dir=tmp_path,
            file_paths=[],
            primary_file_path=None,
            file_roles={},
        )

        with (
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=empty_opp),
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.config.get_output_dir", return_value=output_dir),
        ):
            resp = client.post("/summary/opportunity?opp_id=EMPTY-1")

        assert resp.status_code == 400

    def test_post_summary_with_json_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        opp_dir = tmp_path / "raw" / "OPP-JSON-1"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-JSON-1", opp_dir, [("rfp.pdf", "primary")])

        with (
            patch("pipeline.summary_extractor.run_summary_pipeline", new=AsyncMock(return_value=_sample_summary_output())),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
        ):
            resp = client.post("/summary", json={"opp_id": "OPP-JSON-1"})

        assert resp.status_code == 200
        data = resp.json()
        assert "rfp_metadata" in data
        assert "key_dates" in data
        assert "rfp_overview" in data
        assert data["rfp_metadata"]["client_name"] == "Test Enterprise Corp"

    def test_post_summary_with_file_upload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        pdf_bytes = b"%PDF-1.4 dummy content"

        with (
            patch("pipeline.summary_extractor.run_summary_pipeline", new=AsyncMock(return_value=_sample_summary_output())),
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
        ):
            resp = client.post(
                "/summary",
                files=[("files", ("test.pdf", pdf_bytes, "application/pdf"))],
                data={"primary_filename": "test.pdf"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "rfp_metadata" in data
        assert "key_dates" in data
        assert "rfp_overview" in data
        assert data["rfp_metadata"]["client_name"] == "Test Enterprise Corp"


# ---------------------------------------------------------------------------
# 4. GET /summary/opportunity/{opp_id} endpoint tests
# ---------------------------------------------------------------------------

class TestGetSummaryOpportunityEndpoint:
    def test_returns_summary_for_completed_opportunity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, _ = _make_client(tmp_path, monkeypatch)

        with patch("app.config.get_output_dir", return_value=output_dir):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/x/rfp.pdf",
                opp_id="OPP-GET-1",
            )
        job.status = JobStatus.COMPLETE
        job_out = output_dir / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        summary_payload = _sample_summary_output().model_dump()
        (job_out / "summary.json").write_text(json.dumps(summary_payload), encoding="utf-8")

        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.get("/summary/opportunity/OPP-GET-1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == job.job_id
        assert body["opp_id"] == "OPP-GET-1"
        assert body["status"] == "complete"
        assert body["rfp_summary"]["rfp_metadata"]["client_name"] == "Test Enterprise Corp"

    def test_returns_404_for_unknown_opportunity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, *_ = _make_client(tmp_path, monkeypatch)

        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.get("/summary/opportunity/UNKNOWN-OPP")

        assert resp.status_code == 404
        assert "UNKNOWN-OPP" in resp.json()["detail"]

    def test_falls_back_to_result_json_if_summary_json_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, _ = _make_client(tmp_path, monkeypatch)

        with patch("app.config.get_output_dir", return_value=output_dir):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/x/rfp.pdf",
                opp_id="OPP-GET-FALLBACK",
            )
        job.status = JobStatus.COMPLETE
        job_out = output_dir / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        result_payload = {"rfp_summary": _sample_summary_output().model_dump()}
        (job_out / "result.json").write_text(json.dumps(result_payload), encoding="utf-8")

        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.get("/summary/opportunity/OPP-GET-FALLBACK")

        assert resp.status_code == 200
        body = resp.json()
        assert body["rfp_summary"]["rfp_metadata"]["client_name"] == "Test Enterprise Corp"


# ---------------------------------------------------------------------------
# 5. GET /jobs/{job_id}/summary endpoint tests
# ---------------------------------------------------------------------------

class TestGetJobSummaryEndpoint:
    def test_returns_summary_by_job_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, _ = _make_client(tmp_path, monkeypatch)

        with patch("app.config.get_output_dir", return_value=output_dir):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/x/rfp.pdf",
                opp_id="OPP-JOB-SUM",
            )
        job.status = JobStatus.COMPLETE
        job_out = output_dir / job.job_id
        job_out.mkdir(parents=True, exist_ok=True)
        summary_payload = _sample_summary_output().model_dump()
        (job_out / "summary.json").write_text(json.dumps(summary_payload), encoding="utf-8")

        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.get(f"/jobs/{job.job_id}/summary")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == job.job_id
        assert body["status"] == "complete"
        assert body["opp_id"] == "OPP-JOB-SUM"
        assert body["rfp_summary"]["rfp_metadata"]["client_name"] == "Test Enterprise Corp"

    def test_returns_404_for_unknown_job_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, *_ = _make_client(tmp_path, monkeypatch)

        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.get("/jobs/00000000-0000-0000-0000-000000000000/summary")

        assert resp.status_code == 404

    def test_returns_400_for_invalid_job_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, *_ = _make_client(tmp_path, monkeypatch)

        with patch("app.routers.summary.get_job_store", return_value=store):
            resp = client.get("/jobs/not-a-valid-uuid/summary")

        assert resp.status_code == 400
