"""
tests/test_export_endpoints.py
-------------------------------
Tests for app/routers/exports.py — Sub-task 1.5.

Covers:
  AT-8   Excel: opens in openpyxl; AMS / rate-card / design-build on own sheets,
         not all under Functional; formula guard (=, +, -, @, \\t, \\r);
         no Change Log sheet on view=original.
  AT-9   Markdown export: markers present; removed items omitted;
         baseline result.md byte-identical (regression).
  AT-22  Export format, view, include_removed query params produce different downloads;
         non-ASCII filenames encoded correctly.
  AT-23  JSON export: as-is, strip_meta, view=original each correct.
  AT-5   include_removed=true includes removed items with Status=removed.
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from core.edit.service import bootstrap_working_copy


# ── Seed helpers ──────────────────────────────────────────────────────────────


def _seed_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden: dict,
    *,
    with_working: bool = True,
    primary_file_name: str = "My RFP.pdf",
) -> tuple[str, TestClient]:
    """Create a complete job on disk and return (job_id, TestClient)."""
    from app.job_store import JobStore

    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))

    job_id = str(uuid.uuid4())
    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True)

    (job_dir / "result.json").write_text(json.dumps(golden), encoding="utf-8")

    if with_working:
        doc = bootstrap_working_copy(golden, job_id)
        (job_dir / "working.json").write_text(json.dumps(doc), encoding="utf-8")

    job_json = {
        "job_id": job_id,
        "status": "complete",
        "file_name": primary_file_name,
        "file_names": [primary_file_name],
        "primary_file_name": primary_file_name,
        "primary_file_path": str(job_dir / primary_file_name),
        "file_roles": {},
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
        "error": None,
    }
    (job_dir / "job.json").write_text(json.dumps(job_json), encoding="utf-8")

    # Patch repo singletons
    from core.edit.repository import FileResultRepository
    repo = FileResultRepository(tmp_path)
    import app.routers.edits as edits_mod
    import app.routers.exports as exports_mod
    monkeypatch.setattr(edits_mod, "_repo", repo)
    monkeypatch.setattr(exports_mod, "_repo", repo)

    store = JobStore()
    store.load_from_disk(tmp_path)
    monkeypatch.setattr("app.job_store._store", store)

    from app.main import app
    client = TestClient(app, raise_server_exceptions=True)
    return job_id, client


def _wb_from_response(resp) -> openpyxl.Workbook:
    """Parse an openpyxl Workbook from a test response."""
    assert resp.status_code == 200, resp.text
    return openpyxl.load_workbook(io.BytesIO(resp.content))


# ── AT-8: Excel export ────────────────────────────────────────────────────────


class TestExcelExport:
    """AT-8 — Excel workbook structure and formula guard."""

    def test_workbook_opens(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Workbook is parseable by openpyxl."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        wb = _wb_from_response(resp)
        assert "Summary" in wb.sheetnames

    def test_summary_sheet_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        wb = _wb_from_response(resp)
        assert wb.sheetnames[0] == "Summary"

    def test_ams_on_own_sheet_not_functional(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AMS items must appear on their own sheet, not under Functional (C2)."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        wb = _wb_from_response(resp)

        sheet_names = wb.sheetnames
        assert "ams" in sheet_names, f"Expected 'ams' sheet, got: {sheet_names}"

        # AMS items must NOT appear in Functional/Non Functional/Staffing Mandates
        ams_ids = {"AMS-001", "AMS-002", "AMS-003", "AMS-004", "AMS-005", "AMS-006"}
        for sname in ("Functional", "Non Functional", "Staffing Mandates"):
            if sname not in wb.sheetnames:
                continue
            ws = wb[sname]
            ids_in_sheet = {ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)}
            leaked = ams_ids & ids_in_sheet
            assert not leaked, f"AMS ids {leaked!r} found in sheet '{sname}'"

    def test_design_build_split_by_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Functional, Non Functional, Staffing Mandates sheets created from design-build."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        wb = _wb_from_response(resp)
        names = wb.sheetnames
        # golden fixture has all three types in design-build
        assert "Functional" in names
        assert "Non Functional" in names
        assert "Staffing Mandates" in names

    def test_rate_card_on_own_sheet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """rate-card skill items appear on their own sheet."""
        # Inject a rate-card item so the sheet isn't empty
        golden = json.loads(json.dumps(golden_result))
        for so in golden["skill_outputs"]:
            if so["skill_slug"] == "rate-card":
                so["items"] = [{
                    "id": "RC-001",
                    "role_title": "Senior Developer",
                    "seniority": "Senior",
                    "priority": "must",
                    "confidence": 0.9,
                }]
                so["no_roles_stated"] = False
        job_id, client = _seed_job(tmp_path, monkeypatch, golden)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        wb = _wb_from_response(resp)
        assert "rate-card" in wb.sheetnames

    def test_formula_injection_guard_equals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Cells starting with '=' must be stored as text, not evaluated.

        openpyxl reads cells with quotePrefix=True as data_type='f' when the
        cell value starts with '=', but the value is still the literal string,
        not an evaluated formula result.  The correct guard is quotePrefix=True
        (which tells Excel to treat the leading '=' as literal text).
        """
        golden = json.loads(json.dumps(golden_result))
        for so in golden["skill_outputs"]:
            if so["skill_slug"] == "design-build":
                so["items"][0]["title"] = "=SUM(A1)"
        job_id, client = _seed_job(tmp_path, monkeypatch, golden)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        wb = _wb_from_response(resp)
        ws = wb["Functional"]
        # Find the cell with =SUM(A1) — should be stored as literal text
        found = False
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if cell.value == "=SUM(A1)":
                    found = True
                    # quotePrefix=True is the formula-injection guard mechanism
                    assert cell.quotePrefix is True, (
                        f"Cell with '=SUM(A1)' should have quotePrefix=True; "
                        f"data_type={cell.data_type!r}"
                    )
        assert found, "Cell with injection value not found in sheet"

    def test_formula_guard_plus_minus_at(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Cells starting with +, -, @ are stored as literal text with quotePrefix=True."""
        for prefix in ("+CMD", "-1+1", "@SUM"):
            golden = json.loads(json.dumps(golden_result))
            golden["spine"]["compliance"][0]["title"] = prefix
            job_id, client = _seed_job(tmp_path, monkeypatch, golden)
            resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
            wb = _wb_from_response(resp)
            ws = wb["Compliance"]
            titles = [ws.cell(row=r, column=2).value for r in range(2, ws.max_row + 1)]
            assert prefix in titles, f"Expected literal '{prefix}' in titles"
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if cell.value == prefix:
                        assert cell.quotePrefix is True, (
                            f"Cell {prefix!r} should have quotePrefix=True"
                        )

    def test_formula_guard_tab_cr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Cells starting with tab or carriage-return are guarded with quotePrefix=True."""
        for prefix in ("\tTAB", "\rCR"):
            golden = json.loads(json.dumps(golden_result))
            golden["spine"]["risk"][0]["title"] = prefix
            job_id, client = _seed_job(tmp_path, monkeypatch, golden)
            resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
            wb = _wb_from_response(resp)
            ws = wb["Risk"]
            titles = [ws.cell(row=r, column=2).value for r in range(2, ws.max_row + 1)]
            assert prefix in titles, f"Expected literal {prefix!r} in titles"
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if cell.value == prefix:
                        assert cell.quotePrefix is True, (
                            f"Cell {prefix!r} should have quotePrefix=True"
                        )

    def test_no_change_log_on_original_view(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """view=original must omit the Change Log sheet."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx&view=original")
        wb = _wb_from_response(resp)
        assert "Change Log" not in wb.sheetnames, (
            f"Change Log sheet should not exist for view=original. Sheets: {wb.sheetnames}"
        )

    def test_change_log_present_for_edited_view_with_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Change Log sheet present when log entries exist in edited view."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)

        # Write a mutation to generate a log entry
        resp = client.get(f"/jobs/{job_id}")
        first_uid = resp.json()["result"]["skill_outputs"][0]["items"][0]["_meta"]["uid"]
        client.patch(
            f"/jobs/{job_id}/items/{first_uid}",
            json={"base_version": 1, "fields": {"title": "Patched title"}},
        )

        resp = client.get(f"/jobs/{job_id}/export?format=xlsx&view=edited")
        wb = _wb_from_response(resp)
        assert "Change Log" in wb.sheetnames

    def test_include_removed_adds_grey_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """include_removed=true shows removed items; false hides them."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)

        # Remove first compliance item
        resp = client.get(f"/jobs/{job_id}")
        uid = resp.json()["result"]["spine"]["compliance"][0]["_meta"]["uid"]
        client.delete(f"/jobs/{job_id}/items/{uid}")

        # Without include_removed: item absent
        resp_no = client.get(f"/jobs/{job_id}/export?format=xlsx&include_removed=false")
        wb_no = _wb_from_response(resp_no)
        ws_no = wb_no["Compliance"]
        ids_no = {ws_no.cell(row=r, column=1).value for r in range(2, ws_no.max_row + 1)}
        assert "COMP-001" not in ids_no

        # With include_removed: item present
        resp_yes = client.get(f"/jobs/{job_id}/export?format=xlsx&include_removed=true")
        wb_yes = _wb_from_response(resp_yes)
        ws_yes = wb_yes["Compliance"]
        ids_yes = {ws_yes.cell(row=r, column=1).value for r in range(2, ws_yes.max_row + 1)}
        assert "COMP-001" in ids_yes

    def test_sheet_name_max_31_chars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """All sheet names must be ≤ 31 characters."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        wb = _wb_from_response(resp)
        for name in wb.sheetnames:
            assert len(name) <= 31, f"Sheet name too long: {name!r}"

    def test_ams_artifact_type_column(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AMS sheet must have an 'Artifact Type' column (O6)."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        wb = _wb_from_response(resp)
        ws = wb["ams"]
        headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
        assert "Artifact Type" in headers, f"Headers: {headers}"

    def test_view_original_no_meta_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """view=original: Origin defaults to AI, Edited defaults to No."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx&view=original")
        wb = _wb_from_response(resp)
        ws = wb["Compliance"]
        headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
        if "Origin" in headers:
            origin_col = headers.index("Origin") + 1
            for row in range(2, ws.max_row + 1):
                val = ws.cell(row=row, column=origin_col).value
                if val is not None and val != "":
                    assert val == "AI", f"Expected AI origin, got {val!r}"
        if "Edited" in headers:
            edited_col = headers.index("Edited") + 1
            for row in range(2, ws.max_row + 1):
                val = ws.cell(row=row, column=edited_col).value
                if val is not None and val != "":
                    assert val == "No", f"Expected No, got {val!r}"


# ── AT-9: Markdown export ─────────────────────────────────────────────────────


class TestMarkdownExport:
    """AT-9 — Markdown markers and baseline byte-identity."""

    def test_edited_marker_in_export(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """✎ Edited marker appears in export for patched items."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)

        # Patch first compliance item
        resp = client.get(f"/jobs/{job_id}")
        uid = resp.json()["result"]["spine"]["compliance"][0]["_meta"]["uid"]
        client.patch(
            f"/jobs/{job_id}/items/{uid}",
            json={"base_version": 1, "fields": {"title": "Edited Compliance"}},
        )

        resp = client.get(f"/jobs/{job_id}/export?format=md")
        assert resp.status_code == 200
        content = resp.content.decode("utf-8")
        assert "✎ Edited" in content, "Expected ✎ Edited marker in markdown export"

    def test_added_marker_in_export(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """＋ Added marker appears for user-added items."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        add_resp = client.post(
            f"/jobs/{job_id}/items",
            json={
                "bucket": "design-build",
                "fields": {
                    "type": "functional",
                    "title": "Brand new requirement",
                    "description": "Test description",
                    "priority": "must",
                    "source_section": "Section 99",
                    "confidence": 0.9,
                    "ambiguity_flag": False,
                },
            },
        )
        assert add_resp.status_code == 200, add_resp.text
        resp = client.get(f"/jobs/{job_id}/export?format=md")
        assert resp.status_code == 200
        content = resp.content.decode("utf-8")
        assert "＋ Added" in content, "Expected ＋ Added marker in markdown export"

    def test_removed_items_excluded_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Removed items do not appear in Markdown export by default."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)

        resp = client.get(f"/jobs/{job_id}")
        uid = resp.json()["result"]["spine"]["compliance"][0]["_meta"]["uid"]
        client.delete(f"/jobs/{job_id}/items/{uid}")

        resp = client.get(f"/jobs/{job_id}/export?format=md")
        content = resp.content.decode("utf-8")
        assert "Data Residency Compliance" not in content

    def test_export_header_line_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Export markdown contains the 'Edited version — exported' header."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=md&view=edited")
        content = resp.content.decode("utf-8")
        assert "Edited version" in content

    def test_baseline_result_md_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Calling render_markdown_from_doc must not modify result.md on disk (AT-9)."""
        from app.markdown_renderer import render_markdown

        # Write a result.md (simulates baseline created at job completion)
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        from core.schemas import SynthesizedOutput
        output = SynthesizedOutput.model_validate(golden_result)
        expected_md = render_markdown(output, "RFP_main.pdf")
        result_md_path = tmp_path / job_id / "result.md"
        result_md_path.write_text(expected_md, encoding="utf-8")

        # Export via the export endpoint (triggers render_markdown_from_doc)
        client.get(f"/jobs/{job_id}/export?format=md")

        # result.md must be byte-identical
        actual_md = result_md_path.read_text(encoding="utf-8")
        assert actual_md == expected_md, "result.md was modified by the export path"


# ── AT-22: Export menu options + non-ASCII filenames ─────────────────────────


class TestExportMenuAndFilenames:
    """AT-22 — format/view/include_removed params; non-ASCII filenames."""

    def test_format_md_returns_markdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=md")
        assert resp.status_code == 200
        assert "markdown" in resp.headers["content-type"]

    def test_format_json_returns_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=json")
        assert resp.status_code == 200
        assert "json" in resp.headers["content-type"]

    def test_format_xlsx_returns_spreadsheet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        assert resp.status_code == 200
        assert "spreadsheet" in resp.headers["content-type"] or "xlsx" in resp.headers["content-type"]

    def test_invalid_format_422(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=csv")
        assert resp.status_code == 422

    def test_view_original_vs_edited_differ(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """view=original and view=edited produce different JSON outputs after an edit."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)

        # Patch an item in the working copy
        resp = client.get(f"/jobs/{job_id}")
        uid = resp.json()["result"]["spine"]["compliance"][0]["_meta"]["uid"]
        client.patch(
            f"/jobs/{job_id}/items/{uid}",
            json={"base_version": 1, "fields": {"title": "Changed in working copy"}},
        )

        resp_edited = client.get(f"/jobs/{job_id}/export?format=json&strip_meta=true&view=edited")
        resp_original = client.get(f"/jobs/{job_id}/export?format=json&strip_meta=true&view=original")
        assert resp_edited.content != resp_original.content

    def test_non_ascii_filename_content_disposition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Non-ASCII primary filename encoded correctly in Content-Disposition."""
        job_id, client = _seed_job(
            tmp_path, monkeypatch, golden_result, primary_file_name="プロジェクト要件.pdf"
        )
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        assert resp.status_code == 200
        cd = resp.headers.get("content-disposition", "")
        assert "filename*=UTF-8''" in cd
        # The UTF-8 percent-encoded form should contain the stem
        assert "requirements" in cd

    def test_ascii_filename_content_disposition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """ASCII filename has a plain filename= field in Content-Disposition."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result, primary_file_name="test.pdf")
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        cd = resp.headers.get("content-disposition", "")
        assert "filename=" in cd
        assert "requirements" in cd

    def test_include_removed_false_hides_removed_from_md(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """include_removed=false keeps removed items out of markdown."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}")
        uid = resp.json()["result"]["spine"]["risk"][0]["_meta"]["uid"]
        client.delete(f"/jobs/{job_id}/items/{uid}")

        resp_md = client.get(f"/jobs/{job_id}/export?format=md&include_removed=false")
        content = resp_md.content.decode("utf-8")
        assert "Single-Vendor Lock-in" not in content

    def test_spine_items_on_own_sheets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Spine categories appear as separate sheets."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=xlsx")
        wb = _wb_from_response(resp)
        for expected in ("Compliance", "Risk", "Ambiguity", "Commercial Structure"):
            assert expected in wb.sheetnames, f"Missing sheet: {expected}"


# ── AT-23: JSON export ────────────────────────────────────────────────────────


class TestJsonExport:
    """AT-23 — JSON export: as-is, strip_meta, view=original."""

    def test_json_as_is_includes_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Default JSON export includes _meta on items."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=json")
        assert resp.status_code == 200
        data = resp.json()
        # Working copy items have _meta
        item = data["skill_outputs"][0]["items"][0]
        assert "_meta" in item

    def test_json_includes_edit_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Default JSON export includes the _edit block."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=json")
        data = resp.json()
        assert "_edit" in data

    def test_json_strip_meta_removes_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """strip_meta=true removes _meta from items and _edit/_baselines from top level."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=json&strip_meta=true")
        assert resp.status_code == 200
        data = resp.json()
        assert "_edit" not in data
        assert "_baselines" not in data
        item = data["skill_outputs"][0]["items"][0]
        assert "_meta" not in item

    def test_json_view_original_returns_baseline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """view=original returns baseline result.json without _meta."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)

        # Patch an item to distinguish working copy from baseline
        resp = client.get(f"/jobs/{job_id}")
        uid = resp.json()["result"]["spine"]["compliance"][0]["_meta"]["uid"]
        client.patch(
            f"/jobs/{job_id}/items/{uid}",
            json={"base_version": 1, "fields": {"title": "Working copy title"}},
        )

        resp_orig = client.get(f"/jobs/{job_id}/export?format=json&view=original")
        data = resp_orig.json()
        titles = [i.get("title") for i in data["spine"]["compliance"]]
        assert "Working copy title" not in titles
        assert "Data Residency Compliance" in titles

    def test_json_view_original_no_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """view=original baseline items have no _meta."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}/export?format=json&view=original")
        data = resp.json()
        item = data["skill_outputs"][0]["items"][0]
        assert "_meta" not in item


# ── AT-5: include_removed export ─────────────────────────────────────────────


class TestIncludeRemovedExport:
    """AT-5 partial: include_removed=true includes removed items in exports."""

    def test_removed_item_in_json_export_with_include_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """JSON export returns working copy as-is (removed items kept with _meta.status=removed).

        strip_meta removes _meta keys but does NOT filter out removed items —
        that would make the export lossy.  AT-5 for JSON export verifies that
        the removal is recorded in working.json and visible to consumers who
        read _meta.status.
        """
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}")
        uid = resp.json()["result"]["spine"]["compliance"][0]["_meta"]["uid"]
        client.delete(f"/jobs/{job_id}/items/{uid}")

        # Default JSON export includes removed items (it returns working copy as-is)
        resp_full = client.get(f"/jobs/{job_id}/export?format=json")
        data_full = resp_full.json()
        ids_full = [i["id"] for i in data_full["spine"]["compliance"]]
        assert "COMP-001" in ids_full

        # Verify the removed item is marked with status=removed in its _meta
        for item in data_full["spine"]["compliance"]:
            if item["id"] == "COMP-001":
                assert item["_meta"]["status"] == "removed"
                break
        else:
            pytest.fail("COMP-001 not found in JSON export")

    def test_removed_item_status_in_excel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """include_removed=true shows Status=removed for removed items in Excel."""
        job_id, client = _seed_job(tmp_path, monkeypatch, golden_result)
        resp = client.get(f"/jobs/{job_id}")
        uid = resp.json()["result"]["spine"]["compliance"][0]["_meta"]["uid"]
        client.delete(f"/jobs/{job_id}/items/{uid}")

        resp = client.get(f"/jobs/{job_id}/export?format=xlsx&include_removed=true")
        wb = _wb_from_response(resp)
        ws = wb["Compliance"]
        headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
        assert "Status" in headers
        status_col = headers.index("Status") + 1
        for row in range(2, ws.max_row + 1):
            id_val = ws.cell(row=row, column=1).value
            status_val = ws.cell(row=row, column=status_col).value
            if id_val == "COMP-001":
                assert status_val == "removed"
                break
        else:
            pytest.fail("COMP-001 not found in Compliance sheet with include_removed=true")


# ── classify.py unit tests ────────────────────────────────────────────────────


class TestGroupByBucket:
    """Unit tests for app/exporters/classify.group_by_bucket."""

    def test_design_build_split(self, golden_result: dict) -> None:
        from app.exporters.classify import group_by_bucket
        groups = group_by_bucket(golden_result)
        assert "Functional" in groups
        assert "Non Functional" in groups
        assert "Staffing Mandates" in groups
        # DB-001 and DB-002 are functional
        func_ids = [i["id"] for i in groups["Functional"]]
        assert "DB-001" in func_ids
        assert "DB-002" in func_ids
        # DB-003 is non_functional
        nf_ids = [i["id"] for i in groups["Non Functional"]]
        assert "DB-003" in nf_ids
        # DB-004 is staffing_mandate
        sm_ids = [i["id"] for i in groups["Staffing Mandates"]]
        assert "DB-004" in sm_ids

    def test_ams_own_key(self, golden_result: dict) -> None:
        from app.exporters.classify import group_by_bucket
        groups = group_by_bucket(golden_result)
        assert "ams" in groups
        ams_ids = [i["id"] for i in groups["ams"]]
        assert "AMS-001" in ams_ids

    def test_spine_keys_present(self, golden_result: dict) -> None:
        from app.exporters.classify import group_by_bucket
        groups = group_by_bucket(golden_result)
        for label in ("Compliance", "Ambiguity", "Risk", "Commercial Structure"):
            assert label in groups

    def test_packaged_own_key(self, golden_result: dict) -> None:
        from app.exporters.classify import group_by_bucket
        groups = group_by_bucket(golden_result)
        assert "packaged" in groups
        ids = [i["id"] for i in groups["packaged"]]
        assert "PKG-001" in ids

    def test_ams_not_in_functional(self, golden_result: dict) -> None:
        """AMS items must NOT appear in Functional (C2 fix)."""
        from app.exporters.classify import group_by_bucket
        groups = group_by_bucket(golden_result)
        func_ids = {i["id"] for i in groups.get("Functional", [])}
        ams_ids = {i["id"] for i in groups.get("ams", [])}
        assert not (func_ids & ams_ids), f"AMS ids leaked into Functional: {func_ids & ams_ids}"


# ── Sheet-name sanitisation unit tests ───────────────────────────────────────


class TestSheetNaming:
    """Unit tests for app/exporters/xlsx sheet-name helpers."""

    def test_sanitise_strips_forbidden_chars(self) -> None:
        from app.exporters.xlsx import _sanitise_sheet_name
        assert _sanitise_sheet_name("Sheet[1]") == "Sheet1"
        assert _sanitise_sheet_name("A:B*C?D/E\\F") == "ABCDEF"

    def test_sanitise_truncates_to_31(self) -> None:
        from app.exporters.xlsx import _sanitise_sheet_name
        long_name = "A" * 40
        assert len(_sanitise_sheet_name(long_name)) == 31

    def test_unique_suffix_on_collision(self) -> None:
        from app.exporters.xlsx import _unique_sheet_name
        used: set[str] = set()
        name1 = _unique_sheet_name("Sheet", used)
        name2 = _unique_sheet_name("Sheet", used)
        name3 = _unique_sheet_name("Sheet", used)
        assert name1 == "Sheet"
        assert name2 == "Sheet 2"
        assert name3 == "Sheet 3"
