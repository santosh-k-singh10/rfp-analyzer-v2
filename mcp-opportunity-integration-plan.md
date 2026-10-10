# RFP Analyzer V2 - MCP & Opportunity ID Integration Plan

## Top-Level Overview
Enable RFP Analyzer V2 to automatically ingest RFP documents for a specific Opportunity ID (`opp_id`) directly from the raw storage folder without manual file upload, accessible via:
1. **Web URL Query Parameter**: Visiting `http://localhost:8001/?opp_id=...` resolves the files from disk, triggers the analysis pipeline, and navigates straight to the review/progress page.
2. **REST API**: Direct endpoints to trigger and poll opportunity-based analysis.
3. **Model Context Protocol (MCP) Server**: A standard MCP server exposing tools (`analyze_opportunity_rfp`, `get_opportunity_analysis`, `list_opportunities`) for external AI agents and workflows.

---

## Sub-Tasks

### Sub-Task 1: Opportunity Storage Resolver & Configuration
- **Intent**: Provide a robust utility to locate, validate, and retrieve RFP documents given an `opp_id` from the configured raw storage directory.
- **Expected Outcomes**:
  - Configuration setting `RAW_STORAGE_BASE_DIR` added to `app/config.py` with default fallback to `C:\Agents\StarterKit-v0.1\Framework\backend\data\storage\inputs\raw`.
  - Helper `resolve_opportunity_files(opp_id: str)` that resolves directory path (e.g., `{RAW_STORAGE_BASE_DIR}/{opp_id}` or scans directory structures) and returns valid document paths with role assignments (`primary`, `annexure`, `qna`).
- **Todo List**:
  - [ ] Add `get_raw_storage_dir()` in `app/config.py` supporting `RAW_STORAGE_BASE_DIR` environment variable.
  - [ ] Implement `core/opportunity_resolver.py` to discover and classify files in the opportunity folder.
- **Relevant Context**:
  - `app/config.py`
  - Allowed file types: `.pdf`, `.docx`, `.doc`, `.txt`, `.xlsx`, `.xls`
- **Status**: `[ ] pending`

---

### Sub-Task 2: Job Store & Background Analysis for Opportunities
- **Intent**: Enhance the job store and analysis service to track `opp_id` associations, avoid redundant duplicate runs, and execute analysis directly from local disk paths.
- **Expected Outcomes**:
  - `AnalysisJob` schema in `app/job_store.py` updated to include optional `opp_id`.
  - Job store supports lookup by `opp_id` (`get_job_by_opp_id(opp_id)`).
  - Background pipeline runner supports copying or linking files from the resolved opportunity folder into the job's upload directory.
- **Todo List**:
  - [ ] Add `opp_id` field to `AnalysisJob` and `job.json` serialization/deserialization.
  - [ ] Add `get_job_by_opp_id` and indexing in `JobStore`.
  - [ ] Create `start_opportunity_analysis(opp_id, background_tasks)` service function in `app/routers/analysis.py`.
- **Relevant Context**:
  - `app/job_store.py`
  - `app/routers/analysis.py`
- **Status**: `[ ] pending`

---

### Sub-Task 3: Web Route & API Endpoint for `opp_id`
- **Intent**: Allow browser and HTTP client requests with `?opp_id=...` to automatically launch analysis and display results.
- **Expected Outcomes**:
  - `GET /` endpoint in `app/main.py` detects `opp_id` query parameter; if provided, launches the analysis (if not already running/complete) and redirects the user to `/review/{job_id}`.
  - `POST /analyze/opportunity?opp_id=...` endpoint added for direct REST invocation.
  - `GET /jobs/opportunity/{opp_id}` endpoint added for querying status by opportunity ID.
- **Todo List**:
  - [ ] Update `GET /` in `app/main.py` to handle `opp_id` query parameter with immediate job creation and redirect.
  - [ ] Add `POST /analyze/opportunity` and `GET /jobs/opportunity/{opp_id}` in `app/routers/analysis.py`.
  - [ ] Update review page UI breadcrumbs/header to display opportunity ID when available.
- **Relevant Context**:
  - `app/main.py`
  - `app/routers/analysis.py`
  - `app/templates/review.html`
- **Status**: `[ ] pending`

---

### Sub-Task 4: MCP Server Implementation
- **Intent**: Expose RFP Analyzer V2 as a Model Context Protocol (MCP) server so external AI tools can inspect opportunities and run RFP analysis.
- **Expected Outcomes**:
  - `mcp_server.py` implements an MCP server using the official Python MCP protocol (or FastMCP standard).
  - Exposes tools:
    - `analyze_opportunity_rfp(opp_id: str)`: Triggers analysis for the opportunity and returns status & job ID.
    - `get_opportunity_analysis(opp_id: str)`: Retrieves synthesized findings, executive summary, scores, risks, and compliance results.
    - `list_opportunities()`: Lists available opportunity folders in storage.
  - Command-line entry point to launch MCP server via stdio or SSE.
- **Todo List**:
  - [ ] Add MCP dependencies (e.g. `mcp`) if needed or create standalone stdio/JSON-RPC MCP server.
  - [ ] Create `mcp_server.py` with tools `analyze_opportunity_rfp`, `get_opportunity_analysis`, `list_opportunities`.
  - [ ] Add instructions for configuring MCP client in `README.md`.
- **Relevant Context**:
  - `pipeline/graph.py`
  - `app/job_store.py`
  - `core/opportunity_resolver.py`
- **Status**: `[ ] pending`

---

### Sub-Task 5: End-to-End Verification & Testing
- **Intent**: Verify opportunity resolution, web redirect, REST API, and MCP tools with test opportunity data.
- **Expected Outcomes**:
  - Automated test verifying folder discovery with mock/sample opportunity directory.
  - End-to-end verification of `/?opp_id=...` flow and MCP tool invocations.
- **Todo List**:
  - [ ] Create test case in `tests/test_opportunity_analysis.py`.
  - [ ] Verify execution with sample test files.
- **Status**: `[ ] pending`
