"""
tests/test_opportunity_resolver.py
------------------------------------
Unit tests for core/opportunity_resolver.py.

All tests use ``tmp_path`` (pytest built-in) to build an in-memory-equivalent
directory structure so no real storage is required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.opportunity_resolver import (
    VALID_EXTENSIONS,
    OpportunityFiles,
    _classify_filename,
    _collect_valid_files,
    _find_opp_directory,
    _pick_primary,
    resolve_opportunity_files,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _touch(path: Path, size: int = 0) -> Path:
    """Create *path* (and parents) with *size* bytes of dummy content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# ---------------------------------------------------------------------------
# _classify_filename
# ---------------------------------------------------------------------------

class TestClassifyFilename:
    def test_rfp_keyword(self):
        assert _classify_filename("RFP_2024_Procurement.pdf") == "primary"

    def test_request_for_proposal(self):
        assert _classify_filename("Request_for_Proposal_IT.docx") == "primary"

    def test_tender_document(self):
        assert _classify_filename("Tender Document v2.pdf") == "primary"

    def test_main_keyword(self):
        assert _classify_filename("Main_Specification.pdf") == "primary"

    def test_qna_ampersand(self):
        assert _classify_filename("Q&A_Round1.pdf") == "qna"

    def test_questions(self):
        assert _classify_filename("Questions_and_Clarifications.docx") == "qna"

    def test_addendum(self):
        assert _classify_filename("Addendum_1.pdf") == "qna"

    def test_amendment(self):
        assert _classify_filename("Amendment_Final.docx") == "qna"

    def test_annex(self):
        assert _classify_filename("Annex_A_Pricing.xlsx") == "annexure"

    def test_exhibit(self):
        assert _classify_filename("Exhibit_B_SOW.docx") == "annexure"

    def test_pricing_sheet(self):
        assert _classify_filename("Price_Sheet_v1.xlsx") == "annexure"

    def test_sow(self):
        assert _classify_filename("SOW_Details.docx") == "annexure"

    def test_unknown(self):
        assert _classify_filename("misc_notes.txt") == "unknown"

    def test_case_insensitive(self):
        assert _classify_filename("rfp_main.PDF") == "primary"


# ---------------------------------------------------------------------------
# _collect_valid_files
# ---------------------------------------------------------------------------

class TestCollectValidFiles:
    def test_returns_only_valid_extensions(self, tmp_path: Path):
        _touch(tmp_path / "doc.pdf")
        _touch(tmp_path / "doc.docx")
        _touch(tmp_path / "img.png")   # should be excluded
        _touch(tmp_path / "data.xlsx")
        files = _collect_valid_files(tmp_path)
        names = {f.name for f in files}
        assert names == {"doc.pdf", "doc.docx", "data.xlsx"}

    def test_empty_directory(self, tmp_path: Path):
        assert _collect_valid_files(tmp_path) == []

    def test_does_not_recurse(self, tmp_path: Path):
        _touch(tmp_path / "top.pdf")
        sub = tmp_path / "subdir"
        _touch(sub / "nested.pdf")
        files = _collect_valid_files(tmp_path)
        assert all(f.parent == tmp_path for f in files)

    def test_all_valid_extensions_accepted(self, tmp_path: Path):
        for ext in VALID_EXTENSIONS:
            _touch(tmp_path / f"file{ext}")
        files = _collect_valid_files(tmp_path)
        assert len(files) == len(VALID_EXTENSIONS)


# ---------------------------------------------------------------------------
# _find_opp_directory
# ---------------------------------------------------------------------------

class TestFindOppDirectory:
    def test_exact_match(self, tmp_path: Path):
        opp_dir = tmp_path / "OPP-001"
        opp_dir.mkdir()
        assert _find_opp_directory(tmp_path, "OPP-001") == opp_dir

    def test_substring_match(self, tmp_path: Path):
        opp_dir = tmp_path / "OPP-001_Procurement_2024"
        opp_dir.mkdir()
        assert _find_opp_directory(tmp_path, "OPP-001") == opp_dir

    def test_case_insensitive_substring(self, tmp_path: Path):
        opp_dir = tmp_path / "opp-001_details"
        opp_dir.mkdir()
        assert _find_opp_directory(tmp_path, "OPP-001") == opp_dir

    def test_uuid_wrapper(self, tmp_path: Path):
        wrapper = tmp_path / "some-uuid-1234"
        nested = wrapper / "OPP-001"
        nested.mkdir(parents=True)
        assert _find_opp_directory(tmp_path, "OPP-001") == nested

    def test_not_found_returns_none(self, tmp_path: Path):
        assert _find_opp_directory(tmp_path, "NONEXISTENT") is None

    def test_base_not_a_directory(self, tmp_path: Path):
        not_a_dir = tmp_path / "file.txt"
        _touch(not_a_dir)
        assert _find_opp_directory(not_a_dir, "OPP-001") is None

    def test_exact_takes_priority_over_substring(self, tmp_path: Path):
        exact = tmp_path / "OPP-001"
        exact.mkdir()
        partial = tmp_path / "OPP-001_extra"
        partial.mkdir()
        assert _find_opp_directory(tmp_path, "OPP-001") == exact


# ---------------------------------------------------------------------------
# _pick_primary
# ---------------------------------------------------------------------------

class TestPickPrimary:
    def test_empty_returns_none(self):
        assert _pick_primary([], {}) is None

    def test_prefers_primary_role(self, tmp_path: Path):
        rfp = _touch(tmp_path / "rfp.pdf", size=100)
        annex = _touch(tmp_path / "annex.docx", size=9999)
        roles = {"rfp.pdf": "primary", "annex.docx": "annexure"}
        assert _pick_primary([rfp, annex], roles) == rfp

    def test_largest_primary_wins(self, tmp_path: Path):
        small_rfp = _touch(tmp_path / "rfp_small.pdf", size=10)
        large_rfp = _touch(tmp_path / "rfp_large.pdf", size=500)
        roles = {"rfp_small.pdf": "primary", "rfp_large.pdf": "primary"}
        assert _pick_primary([small_rfp, large_rfp], roles) == large_rfp

    def test_falls_back_to_largest_pdf(self, tmp_path: Path):
        small_pdf = _touch(tmp_path / "a.pdf", size=50)
        large_pdf = _touch(tmp_path / "b.pdf", size=200)
        txt = _touch(tmp_path / "notes.txt", size=9999)
        roles = {f.name: "unknown" for f in [small_pdf, large_pdf, txt]}
        assert _pick_primary([small_pdf, large_pdf, txt], roles) == large_pdf

    def test_falls_back_to_largest_overall(self, tmp_path: Path):
        docx = _touch(tmp_path / "doc.docx", size=300)
        txt = _touch(tmp_path / "notes.txt", size=100)
        roles = {"doc.docx": "unknown", "notes.txt": "unknown"}
        assert _pick_primary([docx, txt], roles) == docx


# ---------------------------------------------------------------------------
# resolve_opportunity_files  (integration-style, fully mocked FS)
# ---------------------------------------------------------------------------

class TestResolveOpportunityFiles:
    def _make_opp(self, base: Path, opp_id: str, files: dict[str, int]) -> Path:
        """Create an opportunity directory with named files of given sizes."""
        opp_dir = base / opp_id
        opp_dir.mkdir(parents=True, exist_ok=True)
        for name, size in files.items():
            _touch(opp_dir / name, size)
        return opp_dir

    def test_basic_resolution(self, tmp_path: Path):
        self._make_opp(tmp_path, "OPP-001", {
            "RFP_Main.pdf": 500,
            "Annex_A.docx": 100,
            "Q&A.pdf": 80,
        })
        result = resolve_opportunity_files("OPP-001", base_dir=tmp_path)

        assert isinstance(result, OpportunityFiles)
        assert result.opp_id == "OPP-001"
        assert len(result.file_paths) == 3

    def test_primary_file_identified(self, tmp_path: Path):
        self._make_opp(tmp_path, "OPP-002", {
            "RFP_Procurement.pdf": 800,
            "Exhibit_B.docx": 200,
        })
        result = resolve_opportunity_files("OPP-002", base_dir=tmp_path)
        assert result.primary_file_path is not None
        assert result.primary_file_path.name == "RFP_Procurement.pdf"

    def test_roles_assigned(self, tmp_path: Path):
        self._make_opp(tmp_path, "OPP-003", {
            "RFP.pdf": 400,
            "Annex_1.xlsx": 50,
            "QnA_Clarifications.docx": 60,
        })
        result = resolve_opportunity_files("OPP-003", base_dir=tmp_path)
        assert result.file_roles["RFP.pdf"] == "primary"
        assert result.file_roles["Annex_1.xlsx"] == "annexure"
        assert result.file_roles["QnA_Clarifications.docx"] == "qna"

    def test_unsupported_files_excluded(self, tmp_path: Path):
        opp_dir = tmp_path / "OPP-004"
        opp_dir.mkdir()
        _touch(opp_dir / "rfp.pdf", size=100)
        _touch(opp_dir / "image.png", size=999)
        _touch(opp_dir / "archive.zip", size=999)
        result = resolve_opportunity_files("OPP-004", base_dir=tmp_path)
        assert len(result.file_paths) == 1
        assert result.file_paths[0].name == "rfp.pdf"

    def test_base_dir_does_not_exist_raises(self, tmp_path: Path):
        missing = tmp_path / "nonexistent"
        with pytest.raises(FileNotFoundError, match="Raw storage directory"):
            resolve_opportunity_files("OPP-X", base_dir=missing)

    def test_opp_id_not_found_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="No directory found"):
            resolve_opportunity_files("OPP-MISSING", base_dir=tmp_path)

    def test_substring_folder_name(self, tmp_path: Path):
        # Directory named differently but contains opp_id as substring
        opp_dir = tmp_path / "OPP-005_SpecialProject"
        opp_dir.mkdir()
        _touch(opp_dir / "rfp.pdf", size=100)
        result = resolve_opportunity_files("OPP-005", base_dir=tmp_path)
        assert result.opp_dir.name == "OPP-005_SpecialProject"

    def test_all_unknown_roles_still_picks_primary(self, tmp_path: Path):
        self._make_opp(tmp_path, "OPP-006", {
            "document_a.pdf": 300,
            "document_b.pdf": 150,
        })
        result = resolve_opportunity_files("OPP-006", base_dir=tmp_path)
        assert result.primary_file_path is not None
        # Largest PDF should be picked
        assert result.primary_file_path.name == "document_a.pdf"

    def test_empty_directory_no_primary(self, tmp_path: Path):
        (tmp_path / "OPP-007").mkdir()
        result = resolve_opportunity_files("OPP-007", base_dir=tmp_path)
        assert result.file_paths == []
        assert result.primary_file_path is None
        assert result.file_roles == {}
