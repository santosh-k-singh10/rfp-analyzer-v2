"""
tests/test_opportunity_web_and_api.py
---------------------------------------
Integration tests for Sub-Task 3: Web Route & API Endpoint for opp_id.

Covers:
  GET  /?opp_id=   — redirect to /review/{job_id} on success
  GET  /?opp_id=   — renders home.html with error on missing opportunity (404)
  GET  /?opp_id=   — renders home.html with error when no files found (400)
  GET  /           — normal home render (no opp_id) is unaffected
  POST /analyze/opportunity — creates job, returns {job_id, status, opp_id}
  POST /analyze/opportunity — 404 when opportunity not found
  POST /analyze/opportunity — 400 when no files found
  POST /analyze/opportunity — force=True creates new job despite existing one
  GET  /jobs/opportunity/{opp_id} — returns job detail for known opp_id
  GET  /jobs/opportunity/{opp_id} — 404 for unknown opp_id
  GET  /jobs/{id}  — now includes opp_id field when present
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.job_store import AnalysisJob, JobStatus, JobStore
from core.opportunity_resolver import OpportunityFiles


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_opp_files(
    opp_id: str,
    opp_dir: Path,
    files: list[tuple[str, str]],  # [(filename, role), ...]
) -> OpportunityFiles:
    """Build a real OpportunityFiles with on-disk stub files."""
    file_paths: list[Path] = []
    file_roles: dict[str, str] = {}
    for name, role in files:
        p = opp_dir / name
        p.write_bytes(b"stub content")
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


def _make_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, JobStore, Path, Path]:
    """Return (client, store, output_dir, upload_dir) all wired together."""
    output_dir = tmp_path / "outputs"
    upload_dir = tmp_path / "uploads"
    output_dir.mkdir()
    upload_dir.mkdir()
    monkeypatch.setenv("OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))

    store = JobStore()

    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)
    return client, store, output_dir, upload_dir


# ─── no-op pipeline ──────────────────────────────────────────────────────────

async def _noop_pipeline(*args, **kwargs) -> None:
    pass


# ---------------------------------------------------------------------------
# GET / (home page — no opp_id)
# ---------------------------------------------------------------------------

class TestHomeNoOppId:
    def test_home_renders_200(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        client, *_ = _make_client(tmp_path, monkeypatch)
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Analyse an RFP" in resp.content

    def test_home_no_error_banner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        client, *_ = _make_client(tmp_path, monkeypatch)
        resp = client.get("/")
        # No error div should be present on a normal home load
        assert b"fef2f2" not in resp.content  # error banner background colour


# ---------------------------------------------------------------------------
# GET /?opp_id= — happy path (redirect)
# ---------------------------------------------------------------------------

class TestHomeWithOppId:
    def test_redirect_to_review_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        opp_dir = tmp_path / "raw" / "OPP-WEB-1"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-WEB-1", opp_dir, [("rfp.pdf", "primary")])

        with (
            patch("app.routers.analysis._run_pipeline", side_effect=_noop_pipeline),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.analysis.get_job_store", return_value=store),
            patch("app.routers.analysis.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
        ):
            resp = client.get("/?opp_id=OPP-WEB-1", follow_redirects=False)

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/review/")

    def test_redirect_location_contains_valid_job_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import uuid
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        opp_dir = tmp_path / "raw" / "OPP-WEB-2"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-WEB-2", opp_dir, [("rfp.pdf", "primary")])

        with (
            patch("app.routers.analysis._run_pipeline", side_effect=_noop_pipeline),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.analysis.get_job_store", return_value=store),
            patch("app.routers.analysis.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
        ):
            resp = client.get("/?opp_id=OPP-WEB-2", follow_redirects=False)

        job_id = resp.headers["location"].split("/review/")[1]
        uuid.UUID(job_id)  # must be a valid UUID

    def test_reuses_existing_running_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)

        # Pre-seed a RUNNING job
        with patch("app.config.get_output_dir", return_value=output_dir):
            existing = store.create_job(file_name="rfp.pdf", file_path="/x/rfp.pdf", opp_id="OPP-REUSE")
        existing.status = JobStatus.RUNNING

        opp_dir = tmp_path / "raw" / "OPP-REUSE"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-REUSE", opp_dir, [("rfp.pdf", "primary")])

        with (
            patch("app.routers.analysis._run_pipeline", side_effect=_noop_pipeline),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.analysis.get_job_store", return_value=store),
            patch("app.routers.analysis.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
        ):
            resp = client.get("/?opp_id=OPP-REUSE", follow_redirects=False)

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location == f"/review/{existing.job_id}"

    def test_missing_opp_renders_home_with_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)

        def _raise_not_found(opp_id_arg, **kwargs):
            raise FileNotFoundError(f"No directory found for opportunity '{opp_id_arg}'")

        with (
            patch("core.opportunity_resolver.resolve_opportunity_files", side_effect=_raise_not_found),
            patch("app.routers.analysis.get_job_store", return_value=store),
        ):
            resp = client.get("/?opp_id=OPP-MISSING", follow_redirects=False)

        # Should render home (not redirect) with error detail
        assert resp.status_code == 404
        assert b"OPP-MISSING" in resp.content

    def test_no_files_renders_home_with_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        opp_dir = tmp_path / "raw" / "OPP-EMPTY"
        opp_dir.mkdir(parents=True)
        empty_opp = OpportunityFiles(
            opp_id="OPP-EMPTY",
            opp_dir=opp_dir,
            file_paths=[],
            primary_file_path=None,
            file_roles={},
        )

        with (
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=empty_opp),
            patch("app.routers.analysis.get_job_store", return_value=store),
        ):
            resp = client.get("/?opp_id=OPP-EMPTY", follow_redirects=False)

        assert resp.status_code == 400
        assert b"OPP-EMPTY" in resp.content


# ---------------------------------------------------------------------------
# POST /analyze/opportunity
# ---------------------------------------------------------------------------

class TestAnalyzeOpportunityEndpoint:
    def test_returns_job_id_status_opp_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        opp_dir = tmp_path / "raw" / "OPP-API-1"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-API-1", opp_dir, [("rfp.pdf", "primary")])

        with (
            patch("app.routers.analysis._run_pipeline", side_effect=_noop_pipeline),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.analysis.get_job_store", return_value=store),
            patch("app.routers.analysis.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
        ):
            resp = client.post("/analyze/opportunity?opp_id=OPP-API-1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["opp_id"] == "OPP-API-1"
        assert "job_id" in body
        assert body["status"] == "pending"

    def test_404_when_opportunity_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, *_ = _make_client(tmp_path, monkeypatch)

        def _raise(opp_id_arg, **kwargs):
            raise FileNotFoundError(f"No directory for '{opp_id_arg}'")

        with (
            patch("core.opportunity_resolver.resolve_opportunity_files", side_effect=_raise),
            patch("app.routers.analysis.get_job_store", return_value=store),
        ):
            resp = client.post("/analyze/opportunity?opp_id=GHOST")

        assert resp.status_code == 404

    def test_400_when_no_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, *_ = _make_client(tmp_path, monkeypatch)
        opp_dir = tmp_path / "raw" / "OPP-EMPTY2"
        opp_dir.mkdir(parents=True)
        empty_opp = OpportunityFiles(
            opp_id="OPP-EMPTY2",
            opp_dir=opp_dir,
            file_paths=[],
            primary_file_path=None,
            file_roles={},
        )

        with (
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=empty_opp),
            patch("app.routers.analysis.get_job_store", return_value=store),
            patch("app.config.get_output_dir", return_value=output_dir),
        ):
            resp = client.post("/analyze/opportunity?opp_id=OPP-EMPTY2")

        assert resp.status_code == 400

    def test_force_creates_new_job_over_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)

        with patch("app.config.get_output_dir", return_value=output_dir):
            existing = store.create_job(file_name="rfp.pdf", file_path="/x/rfp.pdf", opp_id="OPP-FORCE")
        existing.status = JobStatus.COMPLETE

        opp_dir = tmp_path / "raw" / "OPP-FORCE"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-FORCE", opp_dir, [("rfp.pdf", "primary")])

        with (
            patch("app.routers.analysis._run_pipeline", side_effect=_noop_pipeline),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.analysis.get_job_store", return_value=store),
            patch("app.routers.analysis.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
        ):
            resp = client.post("/analyze/opportunity?opp_id=OPP-FORCE&force=true")

        assert resp.status_code == 200
        assert resp.json()["job_id"] != existing.job_id


# ---------------------------------------------------------------------------
# GET /jobs/opportunity/{opp_id}
# ---------------------------------------------------------------------------

class TestGetJobByOppId:
    def _seed(self, store: JobStore, output_dir: Path, opp_id: str) -> AnalysisJob:
        with patch("app.config.get_output_dir", return_value=output_dir):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/x/rfp.pdf",
                file_names=["rfp.pdf"],
                file_paths=["/x/rfp.pdf"],
                file_roles={"rfp.pdf": "primary"},
                primary_file_name="rfp.pdf",
                primary_file_path="/x/rfp.pdf",
                opp_id=opp_id,
            )
        job.status = JobStatus.COMPLETE
        return job

    def test_returns_job_detail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        client, store, output_dir, _ = _make_client(tmp_path, monkeypatch)
        job = self._seed(store, output_dir, "OPP-LOOKUP")

        with patch("app.routers.analysis.get_job_store", return_value=store):
            resp = client.get("/jobs/opportunity/OPP-LOOKUP")

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == job.job_id
        assert body["opp_id"] == "OPP-LOOKUP"
        assert body["status"] == "complete"

    def test_returns_404_for_unknown_opp_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, *_ = _make_client(tmp_path, monkeypatch)

        with patch("app.routers.analysis.get_job_store", return_value=store):
            resp = client.get("/jobs/opportunity/UNKNOWN-OPP")

        assert resp.status_code == 404
        assert "UNKNOWN-OPP" in resp.json()["detail"]

    def test_includes_file_metadata(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        client, store, output_dir, _ = _make_client(tmp_path, monkeypatch)
        job = self._seed(store, output_dir, "OPP-META")

        with patch("app.routers.analysis.get_job_store", return_value=store):
            resp = client.get("/jobs/opportunity/OPP-META")

        body = resp.json()
        assert body["file_name"] == "rfp.pdf"
        assert body["file_count"] == 1


# ---------------------------------------------------------------------------
# GET /jobs/{id} — opp_id now included when set
# ---------------------------------------------------------------------------

class TestGetJobIncludesOppId:
    def test_opp_id_in_job_response(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        client, store, output_dir, _ = _make_client(tmp_path, monkeypatch)

        with patch("app.config.get_output_dir", return_value=output_dir):
            job = store.create_job(
                file_name="rfp.pdf",
                file_path="/x/rfp.pdf",
                opp_id="OPP-JOB-FIELD",
            )

        with patch("app.routers.analysis.get_job_store", return_value=store):
            resp = client.get(f"/jobs/{job.job_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("opp_id") == "OPP-JOB-FIELD"

    def test_opp_id_absent_for_regular_jobs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        client, store, output_dir, _ = _make_client(tmp_path, monkeypatch)

        with patch("app.config.get_output_dir", return_value=output_dir):
            job = store.create_job(file_name="rfp.pdf", file_path="/x/rfp.pdf")

        with patch("app.routers.analysis.get_job_store", return_value=store):
            resp = client.get(f"/jobs/{job.job_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert "opp_id" not in body
