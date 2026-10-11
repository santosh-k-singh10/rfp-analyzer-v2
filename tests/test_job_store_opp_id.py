"""
tests/test_job_store_opp_id.py
-------------------------------
Unit tests for the opp_id additions to app/job_store.py:

- AnalysisJob.opp_id field (default None, accepts a value)
- JobStore.create_job(opp_id=...) persistence into job.json
- JobStore.load_from_disk() round-trips opp_id from job.json
- JobStore.get_job_by_opp_id() lookup and priority ordering
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app.job_store import AnalysisJob, JobStatus, JobStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _isonow(offset_seconds: int = 0) -> str:
    return (datetime.utcnow() + timedelta(seconds=offset_seconds)).isoformat()


def _make_store(tmp_path: Path) -> JobStore:
    """Return a fresh JobStore whose output dir is rooted at tmp_path."""
    store = JobStore()
    return store


def _make_job(
    store: JobStore,
    tmp_path: Path,
    opp_id: str | None = None,
    status: JobStatus = JobStatus.PENDING,
    created_offset: int = 0,
) -> AnalysisJob:
    """Create a job in *store*, using *tmp_path* as OUTPUT_DIR."""
    with patch("app.config.get_output_dir", return_value=tmp_path):
        job = store.create_job(
            file_name="rfp.pdf",
            file_path="/fake/rfp.pdf",
            opp_id=opp_id,
        )
    if status != JobStatus.PENDING:
        job.status = status
        job.created_at = _isonow(created_offset)
    return job


# ---------------------------------------------------------------------------
# AnalysisJob model field
# ---------------------------------------------------------------------------

class TestAnalysisJobOppIdField:
    def test_default_is_none(self):
        job = AnalysisJob(job_id=str(uuid.uuid4()))
        assert job.opp_id is None

    def test_accepts_string(self):
        job = AnalysisJob(job_id=str(uuid.uuid4()), opp_id="OPP-2024-001")
        assert job.opp_id == "OPP-2024-001"

    def test_accepts_none_explicitly(self):
        job = AnalysisJob(job_id=str(uuid.uuid4()), opp_id=None)
        assert job.opp_id is None


# ---------------------------------------------------------------------------
# create_job persists opp_id into job.json
# ---------------------------------------------------------------------------

class TestCreateJobOppId:
    def test_opp_id_stored_on_job(self, tmp_path: Path):
        store = JobStore()
        with patch("app.config.get_output_dir", return_value=tmp_path):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/fake/rfp.pdf",
                opp_id="OPP-001",
            )
        assert job.opp_id == "OPP-001"

    def test_no_opp_id_defaults_none(self, tmp_path: Path):
        store = JobStore()
        with patch("app.config.get_output_dir", return_value=tmp_path):
            job = store.create_job(file_name="rfp.pdf", file_path="/fake/rfp.pdf")
        assert job.opp_id is None

    def test_job_json_contains_opp_id(self, tmp_path: Path):
        store = JobStore()
        with patch("app.config.get_output_dir", return_value=tmp_path):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/fake/rfp.pdf",
                opp_id="OPP-002",
            )
        job_json_path = tmp_path / job.job_id / "job.json"
        assert job_json_path.exists(), "job.json sidecar should have been written"
        data = json.loads(job_json_path.read_text(encoding="utf-8"))
        assert data.get("opp_id") == "OPP-002"

    def test_job_json_opp_id_null_when_none(self, tmp_path: Path):
        store = JobStore()
        with patch("app.config.get_output_dir", return_value=tmp_path):
            job = store.create_job(file_name="rfp.pdf", file_path="/fake/rfp.pdf")
        job_json_path = tmp_path / job.job_id / "job.json"
        data = json.loads(job_json_path.read_text(encoding="utf-8"))
        # opp_id key must be present (value None → JSON null)
        assert "opp_id" in data
        assert data["opp_id"] is None


# ---------------------------------------------------------------------------
# load_from_disk round-trips opp_id
# ---------------------------------------------------------------------------

class TestLoadFromDiskOppId:
    def _write_job_json(self, output_dir: Path, job_id: str, opp_id: str | None) -> None:
        job_dir = output_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        # Also touch result.json so status=complete passes the check
        (job_dir / "result.json").write_text("{}", encoding="utf-8")
        payload = {
            "job_id": job_id,
            "status": "complete",
            "file_name": "rfp.pdf",
            "file_names": ["rfp.pdf"],
            "primary_file_name": "rfp.pdf",
            "primary_file_path": "/fake/rfp.pdf",
            "file_roles": {},
            "created_at": _isonow(),
            "updated_at": _isonow(),
            "error": None,
            "opp_id": opp_id,
        }
        (job_dir / "job.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_opp_id_restored(self, tmp_path: Path):
        job_id = str(uuid.uuid4())
        self._write_job_json(tmp_path, job_id, opp_id="OPP-RESTORED")
        store = JobStore()
        store.load_from_disk(tmp_path)
        job = store.get_job(job_id)
        assert job is not None
        assert job.opp_id == "OPP-RESTORED"

    def test_missing_opp_id_key_defaults_none(self, tmp_path: Path):
        """Older job.json files without opp_id should load as None (backward compat)."""
        job_id = str(uuid.uuid4())
        job_dir = tmp_path / job_id
        job_dir.mkdir()
        (job_dir / "result.json").write_text("{}", encoding="utf-8")
        payload = {
            "job_id": job_id,
            "status": "complete",
            "file_name": "rfp.pdf",
            "file_names": [],
            "primary_file_name": "",
            "primary_file_path": "",
            "file_roles": {},
            "created_at": _isonow(),
            "updated_at": _isonow(),
            "error": None,
            # opp_id key intentionally absent
        }
        (job_dir / "job.json").write_text(json.dumps(payload), encoding="utf-8")
        store = JobStore()
        store.load_from_disk(tmp_path)
        job = store.get_job(job_id)
        assert job is not None
        assert job.opp_id is None


# ---------------------------------------------------------------------------
# get_job_by_opp_id
# ---------------------------------------------------------------------------

class TestGetJobByOppId:
    def test_returns_none_when_no_jobs(self, tmp_path: Path):
        store = JobStore()
        assert store.get_job_by_opp_id("OPP-X") is None

    def test_returns_none_when_opp_id_not_matched(self, tmp_path: Path):
        store = JobStore()
        _make_job(store, tmp_path, opp_id="OPP-001")
        assert store.get_job_by_opp_id("OPP-999") is None

    def test_returns_single_matching_job(self, tmp_path: Path):
        store = JobStore()
        job = _make_job(store, tmp_path, opp_id="OPP-001")
        found = store.get_job_by_opp_id("OPP-001")
        assert found is not None
        assert found.job_id == job.job_id

    def test_running_preferred_over_complete(self, tmp_path: Path):
        store = JobStore()
        complete_job = _make_job(store, tmp_path, opp_id="OPP-A", status=JobStatus.COMPLETE)
        running_job = _make_job(store, tmp_path, opp_id="OPP-A", status=JobStatus.RUNNING)
        found = store.get_job_by_opp_id("OPP-A")
        assert found.job_id == running_job.job_id

    def test_complete_preferred_over_pending(self, tmp_path: Path):
        store = JobStore()
        pending_job = _make_job(store, tmp_path, opp_id="OPP-B", status=JobStatus.PENDING)
        complete_job = _make_job(store, tmp_path, opp_id="OPP-B", status=JobStatus.COMPLETE)
        found = store.get_job_by_opp_id("OPP-B")
        assert found.job_id == complete_job.job_id

    def test_pending_preferred_over_failed(self, tmp_path: Path):
        store = JobStore()
        failed_job = _make_job(store, tmp_path, opp_id="OPP-C", status=JobStatus.FAILED)
        pending_job = _make_job(store, tmp_path, opp_id="OPP-C", status=JobStatus.PENDING)
        found = store.get_job_by_opp_id("OPP-C")
        assert found.job_id == pending_job.job_id

    def test_most_recent_within_same_status(self, tmp_path: Path):
        store = JobStore()
        older = _make_job(store, tmp_path, opp_id="OPP-D", status=JobStatus.COMPLETE)
        older.created_at = _isonow(-60)  # 60 seconds ago
        newer = _make_job(store, tmp_path, opp_id="OPP-D", status=JobStatus.COMPLETE)
        newer.created_at = _isonow(0)
        found = store.get_job_by_opp_id("OPP-D")
        assert found.job_id == newer.job_id

    def test_ignores_jobs_with_different_opp_id(self, tmp_path: Path):
        store = JobStore()
        _make_job(store, tmp_path, opp_id="OPP-X")
        _make_job(store, tmp_path, opp_id="OPP-Y")
        assert store.get_job_by_opp_id("OPP-Z") is None

    def test_ignores_jobs_with_no_opp_id(self, tmp_path: Path):
        store = JobStore()
        _make_job(store, tmp_path, opp_id=None)
        assert store.get_job_by_opp_id("OPP-001") is None
