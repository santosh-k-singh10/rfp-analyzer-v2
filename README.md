# RFP Analyzer V2

Skill-based, archetype-routed pipeline for extracting structured intelligence from RFP documents.

## What V2 Is

V2 moves from a fixed extraction pipeline to a **registry-driven skill architecture**:

```
Ingestion → Vision Pass → Archetype Classifier → [Skill Module(s)] → Shared Spine → Synthesizer → Output/UI
```

1. **Archetype Classifier** reads all skills from the `/skills/` directory and asks the LLM which archetypes fire (e.g. `design-build`, `ams`, `rate-card`, `packaged-sap`).
2. **Skill Modules** extract domain-specific items using each skill's `SKILL.md` framing and `schema.yaml` field definitions.
3. **Shared Spine** runs four cross-cutting agents (Compliance, Ambiguity, Risk, Commercial Structure) regardless of archetype.
4. **Synthesizer** deduplicates and merges outputs from all skill modules.

Adding a new skill requires only creating a folder under `/skills/` with a `SKILL.md` and `schema.yaml` — no code changes required.

## Installation

```bash
pip install -e .
```

Copy `.env.example` to `.env` and fill in your API credentials.

## Running the App

```bash
uvicorn app.main:app --reload
```

The app will be available at `http://localhost:8000`.

## Opportunity ID Analysis

Documents for an opportunity live in `{RAW_STORAGE_BASE_DIR}/{opp_id}/` (set `RAW_STORAGE_BASE_DIR` in `.env`). They can be analyzed without uploading:

- Browser: `http://localhost:8001/?opp_id=OPP-2024-001` (starts analysis and redirects to the review page)
- REST: `POST /analyze/opportunity?opp_id=...[&force=true]` and `GET /jobs/opportunity/{opp_id}`
- MCP: see below

## MCP Server

`mcp_server.py` exposes the analyzer as a [Model Context Protocol](https://modelcontextprotocol.io) server (built on the `mcp` package, installed with `pip install -e .`).

| Tool | Description |
|------|-------------|
| `analyze_opportunity_rfp(opp_id, force=False)` | Resolves the opportunity's files, starts (or reuses) the background pipeline, returns `job_id`, `status`, and a browser `review_url`. |
| `get_opportunity_analysis(opp_id)` | Returns status; once complete also a summary (archetypes, item counts, metadata), risks, and the Markdown report. Returns `not_started` if no analysis exists. |
| `list_opportunities()` | Lists opportunity folders in `RAW_STORAGE_BASE_DIR` with file counts, file types, and analysis status. |

### Running

```bash
python mcp_server.py                              # stdio (default)
python mcp_server.py --transport sse --port 8002  # SSE at http://127.0.0.1:8002/sse
python -m mcp_server --transport stdio
```

Options: `--transport {stdio,sse}`, `--host` (SSE bind address, default `127.0.0.1`), `--port` (SSE port, default `8002`).

The `review_url` host is taken from the `RFP_ANALYZER_BASE_URL` environment variable (default `http://localhost:8001`). The analysis runs inside the MCP server process; jobs are persisted under `OUTPUT_DIR`, so results are shared with the web app after it restarts.

### Configuring MCP clients (stdio)

Bob, Claude Desktop, Cursor, and other clients use the same JSON shape. Use absolute paths:

```json
{
  "mcpServers": {
    "rfp-analyzer-v2": {
      "command": "python",
      "args": ["C:/Agents/V2/mcp_server.py"],
      "env": {
        "RAW_STORAGE_BASE_DIR": "C:/path/to/storage/inputs/raw",
        "RFP_ANALYZER_BASE_URL": "http://localhost:8001"
      }
    }
  }
}
```

- Bob: add this to `.bob/mcp.json` (project) or `~/.bob/mcp.json` (global), or use Settings → MCP → Edit Project/Global MCP.
- Claude Desktop: `claude_desktop_config.json`.
- Cursor: `.cursor/mcp.json`.

For SSE, start the server yourself and point the client at `http://127.0.0.1:8002/sse` (client key is typically `url`). The SSE transport has no authentication; keep it bound to localhost.

LLM credentials are read from the project's `.env` (see Installation).

## How to Add a New Skill

1. Create a folder under `skills/` (e.g. `skills/my-new-skill/`)
2. Add `SKILL.md` with YAML frontmatter:
   ```yaml
   ---
   name: my-new-skill
   display_name: My New Skill
   description: One-sentence description used by the classifier.
   parent: null
   ---
   ## Extraction framing
   ...
   ```
3. Add `schema.yaml` defining the output fields.
4. Add at least one test case in `test_cases/case_001.yaml`.
5. Restart the app — the registry auto-discovers the skill.

## Running Skill Promotion

Before promoting a skill to production, run its test harness:

```bash
python -m promotion.runner --skill ams
```

This runs all test cases for the skill and reports pass/fail results.

## Security Note

The write, reset, and bulk endpoints (`POST /jobs/{id}/items`, `PATCH /jobs/{id}/items/{uid}`, `DELETE /jobs/{id}/items/{uid}`, `POST /jobs/{id}/reset`, `POST /jobs/{id}/items/bulk`) are **unauthenticated**. They must not be exposed beyond `localhost` without an authentication layer in front (e.g. a reverse proxy with auth). Run the server behind a firewall or VPN for any multi-user or internet-facing deployment.

## Project Structure

```
V2/
  skills/          # Skill definitions (SKILL.md + schema.yaml + test_cases/)
  core/            # Shared infrastructure (LLM client, ingestor, schemas, registry)
  classifier/      # Archetype classifier
  skills_engine/   # Per-skill extraction agents
  spine/           # Cross-cutting spine agents (compliance, risk, ambiguity, commercial)
  synthesizer/     # Dedup and merge
  pipeline/        # LangGraph pipeline wiring
  promotion/       # Skill promotion workflow (test harness)
  app/             # FastAPI application, templates, static assets
  mcp_server.py    # MCP server (stdio / SSE)
```
