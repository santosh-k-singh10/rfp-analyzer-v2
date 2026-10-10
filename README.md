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
```
