"""
tests/test_start_opportunity_analysis.py
-----------------------------------------
Unit tests for start_opportunity_analysis() in app/routers/analysis.py.

The pipeline is never actually executed here — we patch:
  * core.opportunity_resolver.resolve_opportunity_files   → fake OpportunityFiles
  * app.routers.analysis._run_pipeline                   → no-op coroutine
  * app.job_store.get_output_dir                         → tmp_path
  * app.config.get_upload_dir                            → tmp_path / "uploads"

Tests cover:
  - Normal happy path: new job created, pipeline scheduled, opp_id set.
  - Empty file list: HTTPException 400.
  - Missing opportunity directory: HTTPException 404.
  - Deduplication: returns existing RUNNING job without creating a new one.
  - Deduplication: returns existing COMPLETE job without creating a new one.
  - force=True bypasses deduplication and creates a fresh job.
  - Files are copied to uploads/{job_id}/ directory.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.job_store import AnalysisJob, JobStatus, JobStore
from core.opportunity_resolver import OpportunityFiles


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_opp_files(
    opp_id: str,
    opp_dir: Path,
    files: list[tuple[str, str]],  # [(filename, role), ...]
) -> OpportunityFiles:
    """Build an OpportunityFiles with real on-disk stub files."""
    file_paths: list[Path] = []
    file_roles: dict[str, str] = {}
    for name, role in files:
        p = opp_dir / name
        p.write_bytes(b"stub")
        file_paths.append(p)
        file_roles[name] = role
    primary = next((p for p in file_paths if file_roles[p.name] == "primary"), None)
    if primary is None and file_paths:
        primary = file_paths[0]
        file_roles[primary.name] = "primary"
    return OpportunityFiles(
        opp_id=opp_id,
        opp_dir=opp_dir,
        file_paths=file_paths,
        primary_file_path=primary,
        file_roles=file_roles,
    )


@pytest.fixture()
def tmp_store(tmp_path: Path) -> tuple[JobStore, Path, Path]:
    """Return (store, output_dir, upload_dir) all rooted under tmp_path."""
    output_dir = tmp_path / "outputs"
    upload_dir = tmp_path / "uploads"
    output_dir.mkdir()
    upload_dir.mkdir()
    store = JobStore()
    return store, output_dir, upload_dir


async def _run_start(
    opp_id: str,
    opp_files: OpportunityFiles | None,
    store: JobStore,
    output_dir: Path,
    upload_dir: Path,
    force: bool = False,
) -> AnalysisJob:
    """Drive start_opportunity_analysis with mocked dependencies."""
    from app.routers.analysis import start_opportunity_analysis

    bt = BackgroundTasks()
    pipeline_called: list[tuple] = []

    async def _fake_pipeline(*args, **kwargs):
        pipeline_called.append(args)

    def _fake_resolve(opp_id_arg, base_dir=None):
        if opp_files is None:
            raise FileNotFoundError(f"No directory found for opportunity '{opp_id_arg}'")
        return opp_files

    with (
        patch("app.routers.analysis._run_pipeline", side_effect=_fake_pipeline),
        patch("core.opportunity_resolver.resolve_opportunity_files", side_effect=_fake_resolve),
        patch("app.config.get_output_dir", return_value=output_dir),
        patch("app.config.get_upload_dir", return_value=upload_dir),
        patch("app.routers.analysis.get_upload_dir", return_value=upload_dir),
        patch("app.routers.analysis.get_job_store", return_value=store),
    ):
        return await start_opportunity_analysis(opp_id, bt, force=force)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestStartOpportunityAnalysisHappyPath:
    @pytest.mark.asyncio
    async def test_returns_analysis_job(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store
        opp_dir = tmp_path / "raw" / "OPP-001"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-001", opp_dir, [("rfp.pdf", "primary")])

        job = await _run_start("OPP-001", opp_files, store, output_dir, upload_dir)

        assert isinstance(job, AnalysisJob)
        assert job.opp_id == "OPP-001"

    @pytest.mark.asyncio
    async def test_job_has_correct_file_names(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store
        opp_dir = tmp_path / "raw" / "OPP-002"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files(
            "OPP-002", opp_dir,
            [("rfp.pdf", "primary"), ("annex.docx", "annexure")]
        )

        job = await _run_start("OPP-002", opp_files, store, output_dir, upload_dir)

        assert set(job.file_names) == {"rfp.pdf", "annex.docx"}
        assert job.primary_file_name == "rfp.pdf"

    @pytest.mark.asyncio
    async def test_files_copied_to_upload_dir(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store
        opp_dir = tmp_path / "raw" / "OPP-003"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-003", opp_dir, [("rfp.pdf", "primary")])

        job = await _run_start("OPP-003", opp_files, store, output_dir, upload_dir)

        # All file_paths must sit inside uploads/{job_id}/
        expected_dir = upload_dir / job.job_id
        assert expected_dir.is_dir(), "Upload dir for job should have been created"
        for fp in job.file_paths:
            assert Path(fp).parent == expected_dir

    @pytest.mark.asyncio
    async def test_job_registered_in_store(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store
        opp_dir = tmp_path / "raw" / "OPP-004"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-004", opp_dir, [("rfp.pdf", "primary")])

        job = await _run_start("OPP-004", opp_files, store, output_dir, upload_dir)

        assert store.get_job(job.job_id) is job


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestStartOpportunityAnalysisErrors:
    @pytest.mark.asyncio
    async def test_missing_opportunity_raises_404(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store

        with pytest.raises(HTTPException) as exc_info:
            await _run_start("OPP-MISSING", None, store, output_dir, upload_dir)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_file_list_raises_400(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store
        opp_dir = tmp_path / "raw" / "OPP-EMPTY"
        opp_dir.mkdir(parents=True)
        # OpportunityFiles with no files
        empty_opp = OpportunityFiles(
            opp_id="OPP-EMPTY",
            opp_dir=opp_dir,
            file_paths=[],
            primary_file_path=None,
            file_roles={},
        )

        with pytest.raises(HTTPException) as exc_info:
            await _run_start("OPP-EMPTY", empty_opp, store, output_dir, upload_dir)

        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Deduplication logic
# ---------------------------------------------------------------------------

class TestStartOpportunityAnalysisDedup:
    def _seed_job(
        self,
        store: JobStore,
        output_dir: Path,
        opp_id: str,
        status: JobStatus,
    ) -> AnalysisJob:
        with patch("app.config.get_output_dir", return_value=output_dir):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/fake/rfp.pdf",
                opp_id=opp_id,
            )
        job.status = status
        return job

    @pytest.mark.asyncio
    async def test_reuses_running_job(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store
        existing = self._seed_job(store, output_dir, "OPP-R", JobStatus.RUNNING)
        opp_dir = tmp_path / "raw" / "OPP-R"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-R", opp_dir, [("rfp.pdf", "primary")])

        result = await _run_start("OPP-R", opp_files, store, output_dir, upload_dir)

        assert result.job_id == existing.job_id

    @pytest.mark.asyncio
    async def test_reuses_complete_job(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store
        existing = self._seed_job(store, output_dir, "OPP-C", JobStatus.COMPLETE)
        opp_dir = tmp_path / "raw" / "OPP-C"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-C", opp_dir, [("rfp.pdf", "primary")])

        result = await _run_start("OPP-C", opp_files, store, output_dir, upload_dir)

        assert result.job_id == existing.job_id

    @pytest.mark.asyncio
    async def test_does_not_reuse_failed_job(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store
        failed = self._seed_job(store, output_dir, "OPP-F", JobStatus.FAILED)
        opp_dir = tmp_path / "raw" / "OPP-F"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-F", opp_dir, [("rfp.pdf", "primary")])

        result = await _run_start("OPP-F", opp_files, store, output_dir, upload_dir)

        assert result.job_id != failed.job_id

    @pytest.mark.asyncio
    async def test_does_not_reuse_pending_job(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store
        pending = self._seed_job(store, output_dir, "OPP-P", JobStatus.PENDING)
        opp_dir = tmp_path / "raw" / "OPP-P"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-P", opp_dir, [("rfp.pdf", "primary")])

        result = await _run_start("OPP-P", opp_files, store, output_dir, upload_dir)

        assert result.job_id != pending.job_id

    @pytest.mark.asyncio
    async def test_force_bypasses_running_dedup(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store
        existing = self._seed_job(store, output_dir, "OPP-FR", JobStatus.RUNNING)
        opp_dir = tmp_path / "raw" / "OPP-FR"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-FR", opp_dir, [("rfp.pdf", "primary")])

        result = await _run_start("OPP-FR", opp_files, store, output_dir, upload_dir, force=True)

        assert result.job_id != existing.job_id

    @pytest.mark.asyncio
    async def test_force_bypasses_complete_dedup(self, tmp_path: Path, tmp_store):
        store, output_dir, upload_dir = tmp_store
        existing = self._seed_job(store, output_dir, "OPP-FC", JobStatus.COMPLETE)
        opp_dir = tmp_path / "raw" / "OPP-FC"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-FC", opp_dir, [("rfp.pdf", "primary")])

        result = await _run_start("OPP-FC", opp_files, store, output_dir, upload_dir, force=True)

        assert result.job_id != existing.job_id
