# RFP Summary & Dual-App Architecture Plan

## Top-Level Overview
Enable RFP Analyzer V2 to host two first-class applications side-by-side:
1. **RFP Analyzer**: Full archetype classification, spine analysis (compliance, ambiguity, risk, commercial structure), skills extraction, and review/edit dashboard at `http://localhost:8001/?opp_id=...`.
2. **RFP Summary**: Dedicated fast-extraction summary app at `http://localhost:8001/summary?opp_id=...` and MCP tool `get_rfp_summary(opp_id)` focusing on client metadata, key milestone dates, narrative overview, and scope highlights.

---

## Sub-Tasks

### Sub-Task 1: Summary Pipeline Integration & Schema Enrichment
- **Intent**: Connect the existing extraction functions in `pipeline/summary_extractor.py` to the core pipeline and data schemas so summary data is automatically generated and persisted.
- **Expected Outcomes**:
  - `SynthesizedOutput` in `core/schemas.py` includes `rfp_summary: RfpSummaryOutput`.
  - Main LangGraph pipeline (`pipeline/graph.py`) incorporates summary extraction concurrently with spine/skill nodes.
  - Summary results are persisted to `outputs/{job_id}/summary.json` and bundled in `result.json`.
  - A fast standalone extractor service `run_summary_pipeline(file_paths, primary_path)` is available for summary-only requests.
- **Todo List**:
  - [x] Update `core/schemas.py` to add `rfp_summary` to `SynthesizedOutput` and `PipelineState`.
  - [x] Add `summary_node` to `pipeline/graph.py` running metadata and overview extraction concurrently.
  - [x] Implement fast `run_summary_pipeline()` in `pipeline/summary_extractor.py` for summary-only jobs.
  - [x] Persist `summary.json` in job output directory.
- **Relevant Context**:
  - `pipeline/summary_extractor.py`
  - `core/schemas.py`
  - `pipeline/graph.py`
- **Status**: `[x] done`

---

### Sub-Task 2: Summary Backend Endpoints & Job Store Support
- **Intent**: Provide REST API routes to trigger, check, and retrieve RFP Summary outputs by Opportunity ID or Job ID.
- **Expected Outcomes**:
  - `POST /summary/opportunity?opp_id=...` kicks off summary extraction for an opportunity.
  - `GET /summary/opportunity/{opp_id}` returns the structured summary JSON for the given opportunity.
  - `GET /jobs/{job_id}/summary` returns the summary section of any existing job.
- **Todo List**:
  - [x] Create `app/routers/summary.py` with summary endpoints.
  - [x] Include router in `app/main.py`.
  - [x] Support opportunity resolution and caching for summary jobs.
- **Relevant Context**:
  - `app/routers/analysis.py`
  - `app/job_store.py`
  - `core/opportunity_resolver.py`
- **Status**: `[x] done`

---

### Sub-Task 3: Dedicated Summary Web UI
- **Intent**: Provide a clean, executive-facing UI page for RFP Summary accessible directly via `http://localhost:8001/summary?opp_id=...`.
- **Expected Outcomes**:
  - `GET /summary` in `app/main.py` detects `opp_id`, initiates/retrieves summary analysis, and displays the summary dashboard.
  - Template `app/templates/summary.html` rendering:
    - Executive Header (Client Name, RFP Title, Reference Number, Estimated Value, Duration).
    - Key Dates / Milestones schedule table with confidence tags.
    - Narrative Executive Overview & Scope Highlights.
    - Action button linking to "Open Full RFP Analysis" (`/?opp_id=...`).
  - Summary tab also added to the full review view (`review.html`) for consistency.
- **Todo List**:
  - [x] Create `app/templates/summary.html` with clean typography and layout matching the design system.
  - [x] Implement `GET /summary` route in `app/main.py` with `opp_id` resolution.
  - [x] Add summary tab to `app/templates/review.html` and `app/static/js/review.js`.
- **Relevant Context**:
  - `app/templates/home.html`
  - `app/templates/review.html`
  - `app/static/css/app.css`
- **Status**: `[x] done`

---

### Sub-Task 4: MCP Tools for RFP Summary
- **Intent**: Expose RFP Summary capabilities to external AI agents over Model Context Protocol.
- **Expected Outcomes**:
  - New MCP tool `get_rfp_summary(opp_id: str, force: bool = False)` in `mcp_server.py`:
    - Returns structured metadata, key milestone dates, executive overview, and link to the summary UI.
  - MCP tool `analyze_opportunity_rfp` updated to reference both Full Analysis and Summary capabilities.
  - Documentation updated in `MCP_INTEGRATION_GUIDE.md` and `README.md`.
- **Todo List**:
  - [x] Add `get_rfp_summary` tool in `mcp_server.py`.
  - [x] Update MCP documentation in `MCP_INTEGRATION_GUIDE.md` and `README.md`.
  - [x] Add tests in `tests/test_mcp_summary.py` and verify.
- **Relevant Context**:
  - `mcp_server.py`
  - `MCP_INTEGRATION_GUIDE.md`
  - `tests/test_mcp_summary.py`
- **Status**: `[x] done`

---

### Sub-Task 5: Verification & End-to-End Testing
- **Intent**: Ensure both RFP Analyzer and RFP Summary apps work independently and together.
- **Expected Outcomes**:
  - Unit tests verifying summary extraction and schemas.
  - Integration tests for `/summary?opp_id=...` web flow and REST endpoints.
  - MCP test verifying `get_rfp_summary` tool.
  - Full test suite passing with 0 regressions.
- **Todo List**:
  - [x] Create `tests/test_summary_app.py`.
  - [x] Run full test suite with `pytest`.
- **Status**: `[x] done`
