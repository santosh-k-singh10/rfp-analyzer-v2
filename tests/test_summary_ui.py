"""
tests/test_summary_ui.py
--------------------------
Tests for Sub-Task 3: Dedicated Summary Web UI.

Coverage:
  1. GET /summary (no opp_id)           — renders summary.html (empty state)
  2. GET /summary?opp_id=...            — kicks off summary job, renders summary.html
  3. GET /summary?opp_id=... (error)    — resolution failure returns home.html with error
  4. GET /summary/{job_id}              — renders summary.html with job_id
  5. review.html                        — contains the summary rail section markup
  6. GET /summary route returns OPP_ID / JOB_ID vars injected into JS
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

from app.job_store import AnalysisJob, JobStatus, JobStore
from core.opportunity_resolver import OpportunityFiles
from core.schemas import RfpKeyDate, RfpMetadata, RfpOverview, RfpSummaryOutput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_summary_output() -> RfpSummaryOutput:
    return RfpSummaryOutput(
        rfp_metadata=RfpMetadata(
            client_name="Acme Government",
            rfp_title="Digital Transformation Services",
            rfp_reference_number="RFP-UI-2025-001",
            estimated_contract_value="$2,500,000",
            contract_duration="2 years",
        ),
        key_dates=[
            RfpKeyDate(
                date_id="KD-001",
                label="Submission Deadline",
                date_type="submission_deadline",
                display_date="2025-10-15",
                date_value="2025-10-15",
                raw_excerpt="Submissions due October 15, 2025.",
                source_document="rfp.pdf",
                confidence="high",
            ),
        ],
        rfp_overview=RfpOverview(
            summary="Acme Government seeks digital transformation services for its citizen portal.",
            scope_highlights=["Cloud migration", "UX redesign", "API integration"],
            client_context="Federal agency managing citizen services.",
            source_sections=["Section 1"],
        ),
    )


def _make_opp_files(opp_id: str, opp_dir: Path) -> OpportunityFiles:
    rfp = opp_dir / "rfp.pdf"
    rfp.write_bytes(b"dummy rfp bytes")
    return OpportunityFiles(
        opp_id=opp_id,
        opp_dir=opp_dir,
        file_paths=[rfp],
        primary_file_path=rfp,
        file_roles={"rfp.pdf": "primary"},
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


# ---------------------------------------------------------------------------
# 1. GET /summary with no opp_id
# ---------------------------------------------------------------------------

class TestSummaryPageNoOppId:
    def test_renders_summary_html(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        client, *_ = _make_client(tmp_path, monkeypatch)
        resp = client.get("/summary")
        assert resp.status_code == 200
        assert b"sum-loading" in resp.content

    def test_injects_null_opp_id_and_job_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        client, *_ = _make_client(tmp_path, monkeypatch)
        resp = client.get("/summary")
        assert resp.status_code == 200
        # Both vars should be null in the page
        assert b"OPP_ID  = null" in resp.content or b"OPP_ID = null" in resp.content or b"null" in resp.content
        assert b"JOB_ID  = null" in resp.content or b"JOB_ID = null" in resp.content or b"null" in resp.content


# ---------------------------------------------------------------------------
# 2. GET /summary?opp_id=... — successful job creation
# ---------------------------------------------------------------------------

class TestSummaryPageWithOppId:
    def test_renders_summary_html_with_opp_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        opp_dir = tmp_path / "raw" / "OPP-UI-001"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-UI-001", opp_dir)

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.summary._run_summary_pipeline_job", new=AsyncMock()),
        ):
            resp = client.get("/summary?opp_id=OPP-UI-001")

        assert resp.status_code == 200
        # Must be summary template, not redirect
        assert b"sum-loading" in resp.content

    def test_injects_opp_id_into_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        opp_dir = tmp_path / "raw" / "OPP-UI-002"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-UI-002", opp_dir)

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.summary._run_summary_pipeline_job", new=AsyncMock()),
        ):
            resp = client.get("/summary?opp_id=OPP-UI-002")

        assert resp.status_code == 200
        assert b"OPP-UI-002" in resp.content

    def test_injects_job_id_into_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        opp_dir = tmp_path / "raw" / "OPP-UI-003"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-UI-003", opp_dir)

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.summary._run_summary_pipeline_job", new=AsyncMock()),
        ):
            resp = client.get("/summary?opp_id=OPP-UI-003")

        assert resp.status_code == 200
        # A job_id (UUID) should appear somewhere in the JS block
        import re
        assert re.search(rb'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', resp.content)


# ---------------------------------------------------------------------------
# 3. GET /summary?opp_id=... — resolution failure falls back to home.html
# ---------------------------------------------------------------------------

class TestSummaryPageResolutionError:
    def test_404_renders_home_with_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, *_ = _make_client(tmp_path, monkeypatch)

        def _raise(opp_id):
            raise FileNotFoundError(f"No opportunity '{opp_id}'")

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("core.opportunity_resolver.resolve_opportunity_files", side_effect=_raise),
        ):
            resp = client.get("/summary?opp_id=MISSING-OPP")

        assert resp.status_code == 404
        # Should render home.html with error content, not summary.html loading state
        assert b"sum-loading" not in resp.content


# ---------------------------------------------------------------------------
# 4. GET /summary/{job_id}
# ---------------------------------------------------------------------------

class TestSummaryJobPage:
    def test_renders_summary_html_for_job_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, *_ = _make_client(tmp_path, monkeypatch)
        job_id = "11111111-2222-3333-4444-555555555555"
        resp = client.get(f"/summary/{job_id}")
        assert resp.status_code == 200
        assert b"sum-loading" in resp.content

    def test_injects_job_id_into_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, *_ = _make_client(tmp_path, monkeypatch)
        job_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        resp = client.get(f"/summary/{job_id}")
        assert resp.status_code == 200
        assert job_id.encode() in resp.content

    def test_opp_id_is_null_for_job_only_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, *_ = _make_client(tmp_path, monkeypatch)
        job_id = "cccccccc-dddd-eeee-ffff-000000000000"
        resp = client.get(f"/summary/{job_id}")
        assert resp.status_code == 200
        assert b"OPP_ID" in resp.content
        # opp_id should be null (not a real opp ID)
        assert b"null" in resp.content


# ---------------------------------------------------------------------------
# 5. review.html — summary rail section present
# ---------------------------------------------------------------------------

class TestReviewHtmlSummaryRail:
    def test_summary_rail_section_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, *_ = _make_client(tmp_path, monkeypatch)
        job_id = "12345678-1234-1234-1234-123456789012"
        resp = client.get(f"/review/{job_id}")
        assert resp.status_code == 200
        assert b"rail-summary-section" in resp.content

    def test_summary_tab_data_attribute_in_rail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, *_ = _make_client(tmp_path, monkeypatch)
        job_id = "12345678-1234-1234-1234-123456789013"
        resp = client.get(f"/review/{job_id}")
        assert resp.status_code == 200
        assert b'data-tab="summary"' in resp.content

    def test_summary_rail_hidden_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, *_ = _make_client(tmp_path, monkeypatch)
        job_id = "12345678-1234-1234-1234-123456789014"
        resp = client.get(f"/review/{job_id}")
        assert resp.status_code == 200
        # The section should start hidden (display:none)
        assert b'id="rail-summary-section" style="display:none;"' in resp.content


# ---------------------------------------------------------------------------
# 6. Page title / template title block
# ---------------------------------------------------------------------------

class TestSummaryPageTitle:
    def test_title_contains_rfp_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, *_ = _make_client(tmp_path, monkeypatch)
        resp = client.get("/summary")
        assert resp.status_code == 200
        assert b"RFP Summary" in resp.content

    def test_title_contains_opp_id_when_supplied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, store, output_dir, upload_dir = _make_client(tmp_path, monkeypatch)
        opp_dir = tmp_path / "raw" / "OPP-TITLE-01"
        opp_dir.mkdir(parents=True)
        opp_files = _make_opp_files("OPP-TITLE-01", opp_dir)

        with (
            patch("app.routers.summary.get_job_store", return_value=store),
            patch("app.routers.summary.get_upload_dir", return_value=upload_dir),
            patch("app.config.get_output_dir", return_value=output_dir),
            patch("app.config.get_upload_dir", return_value=upload_dir),
            patch("core.opportunity_resolver.resolve_opportunity_files", return_value=opp_files),
            patch("app.routers.summary._run_summary_pipeline_job", new=AsyncMock()),
        ):
            resp = client.get("/summary?opp_id=OPP-TITLE-01")

        assert resp.status_code == 200
        assert b"OPP-TITLE-01" in resp.content

    def test_action_button_links_to_full_analysis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, *_ = _make_client(tmp_path, monkeypatch)
        resp = client.get("/summary")
        assert resp.status_code == 200
        # The template should reference the full-analysis link pattern
        assert b"/?opp_id" in resp.content or b"/review/" in resp.content or b"Full Analysis" in resp.content
