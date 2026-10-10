# RFP Analyzer V2 — Markdown Output Plan

## Overview

The V2 RFP Analyzer currently holds all analysis results in-memory (via `JobStore`) and
returns them as JSON through the REST API. No files are written to disk at all — the
`OUTPUT_DIR` environment variable exists in `.env.example` but is unused.

**Goal:** When a pipeline job completes, persist both a `.json` file and a `.md` file to
`OUTPUT_DIR/{job_id}/`. The `.md` file must follow the layout shown in the reference sample
(title, generated timestamp, summary table, then sectioned items). Both files must also be
returned / discoverable through the existing REST API.

**Scope boundary:**
- The `SynthesizedOutput` Pydantic model is the single source of truth; the Markdown
  renderer reads from it — no schema changes.
- The pipeline itself is unchanged; the new I/O happens in the post-pipeline step
  (`_run_pipeline` in `app/routers/analysis.py`).
- The existing in-memory `JobStore` continues to work as before; disk persistence is
  additive.

---

## Section Classification Rules

The renderer classifies every item into exactly one Markdown section using this
two-step logic (applied in order, first match wins):

### Step 1 — Spine items (source = `SynthesizedOutput.spine`)

| Spine field               | Markdown section      | ID prefix expected |
|---------------------------|-----------------------|--------------------|
| `spine.compliance`        | **Compliance**        | CR-                |
| `spine.ambiguity`         | **Ambiguity**         | AMB-               |
| `spine.risk`              | **Risk**              | RISK-              |
| `spine.commercial_structure` | **Commercial Structure** | CS-          |

### Step 2 — Skill items (source = `SynthesizedOutput.skill_outputs[*].items`)

Use the item's **`type`** field first; fall back to the **`id`** prefix if `type` is absent.

| `type` value       | Markdown section   | Fallback id prefix |
|--------------------|--------------------|--------------------|
| `functional`       | **Functional**     | FR-                |
| `non_functional`   | **Non Functional** | NFR-               |
| `staffing_mandate` | **Non Functional** | —                  |
| _(absent)_         | derive from `id`   | FR- → Functional, NFR- → Non Functional |
| _(absent, other id)_ | **Functional** (default) | —           |

For skills that do not emit a `type` field at all (e.g., `ams`, `rate-card`, `packaged`),
use the `id` prefix: `FR-` → Functional, `NFR-` → Non Functional, anything else → Functional.

### Ambiguous flag

An item is flagged ⚠ Ambiguous when **any** of these is true:
- `ambiguity_flag == True`
- `clarification_question` is a non-empty string
- `is_inferred == True` (AMS skill only)

### Section ordering in the output file

1. Functional
2. Non Functional
3. Compliance
4. Ambiguity
5. Risk
6. Commercial Structure

---

## Sub-Tasks

---

### Sub-Task 1 — Activate `OUTPUT_DIR` and write JSON to disk on job completion

**Status:** `[x] done`

**Intent**

Before adding Markdown, establish the disk-write infrastructure: read `OUTPUT_DIR` from
the environment, create the per-job output folder, and write the JSON result file. This is
a self-contained change that can be verified before Markdown is added.

**Expected Outcomes**

- After a successful pipeline run, `{OUTPUT_DIR}/{job_id}/result.json` exists on disk.
- `OUTPUT_DIR` defaults to `./outputs` (matching `.env.example`).
- The per-job folder is created if it does not exist.
- All existing API behaviour is unchanged (in-memory store still populated as before).

**Todo List**

1. In [`app/routers/analysis.py`](app/routers/analysis.py), read `OUTPUT_DIR` from the
   environment at module level — same pattern as `UPLOAD_DIR` on line 40:
   ```python
   OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./outputs"))
   ```
2. In `_run_pipeline`, after `store.set_complete(job_id, result_dict)`:
   - Create `OUTPUT_DIR / job_id` directory (`mkdir(parents=True, exist_ok=True)`).
   - Write `result.json` via `Path.write_text(json.dumps(result_dict, indent=2), encoding="utf-8")`.
3. Wrap the file-write in a `try/except` so a disk failure logs a warning but does NOT
   re-raise (job is already marked complete in memory).
4. On pipeline failure (`set_failed`), no output file is written — no change needed.
5. Confirm `outputs/` is listed in `.gitignore`; add it if missing.

**Relevant Context**

- Write location: [`app/routers/analysis.py`](app/routers/analysis.py) — `_run_pipeline`
  function starts at line 44.
- Pattern to follow: `UPLOAD_DIR` at line 40; upload folder creation at lines 127–130.
- `result_dict` is the `SynthesizedOutput.model_dump()` result, already available before
  `store.set_complete` is called.
- `OUTPUT_DIR=./outputs` is declared in [`.env.example`](.env.example) line 19.

---

### Sub-Task 2 — Build the Markdown renderer

**Status:** `[x] done`

**Intent**

Implement a pure `render_markdown(output, primary_filename) -> str` function that
produces the exact Markdown format shown in the reference sample. Keeping it as a
standalone module means it can be unit-tested without running the pipeline.

**Expected Outcomes**

- New file: [`app/markdown_renderer.py`](app/markdown_renderer.py).
- `render_markdown(output: SynthesizedOutput, primary_filename: str) -> str` is the
  single public function.
- The renderer produces the complete file content as described in the Appendix below.
- The renderer is a **pure function** — no file I/O, no network calls, no side effects.
- All field access on item dicts uses `.get(key, default)` to be robust against
  schema variation across skills.

**Todo List**

1. Create `app/markdown_renderer.py`.

2. Write a helper `_classify_items(output: SynthesizedOutput) -> dict[str, list[dict]]`
   that returns a dict with keys:
   `"Functional"`, `"Non Functional"`, `"Compliance"`, `"Ambiguity"`, `"Risk"`,
   `"Commercial Structure"` — each mapped to a list of item dicts.

   Classification logic (see Section Classification Rules above):
   - Append each item in `spine.compliance` → `"Compliance"`.
   - Append each item in `spine.ambiguity` → `"Ambiguity"`.
   - Append each item in `spine.risk` → `"Risk"`.
   - Append each item in `spine.commercial_structure` → `"Commercial Structure"`.
   - For each `SkillOutput` in `skill_outputs`, for each item dict:
     - Read `item.get("type", "")`.
     - If `type == "functional"` → `"Functional"`.
     - If `type in ("non_functional", "staffing_mandate")` → `"Non Functional"`.
     - If `type` is empty/absent → inspect `item.get("id", "")`:
       - Starts with `"NFR-"` → `"Non Functional"`.
       - Starts with `"FR-"` or anything else → `"Functional"`.

3. Write a helper `_is_ambiguous(item: dict) -> bool`:
   - Returns `True` if `item.get("ambiguity_flag")` is truthy, OR
   - `item.get("clarification_question", "")` is non-empty, OR
   - `item.get("is_inferred")` is truthy.

4. Write a helper `_render_item(item: dict) -> str` that produces:
   ```markdown
   ### {id} — {title}  ⚠ Ambiguous        ← ⚠ suffix only when _is_ambiguous
   - **Priority:** {priority.upper()}
   - **Confidence:** {int(confidence*100)}%
   - **Section:** {source_section} (p.{source_page})

   {description}

   > **Clarification needed:** {clarification_question}   ← block-quote, only if non-empty

   _Related: {related_ids}_                               ← only if "related_ids" field present
   ```
   All fields accessed with `.get(key, "")` / `.get(key, 0)` with safe defaults.
   `source_page` rendered as `p.{n}` if present; omit the `(p.n)` part if absent/0.

5. Write a helper `_build_summary_table(classified: dict[str, list]) -> str` that
   produces the summary table.
   - Row order: Functional, Non Functional, Compliance, Ambiguity, Risk, Commercial Structure.
   - **Count** = `len(items)`.
   - **Must** = count of items where `item.get("priority", "").lower() == "must"`.
   - **Ambiguous** = count of items where `_is_ambiguous(item)` is True.
   - Omit rows with Count == 0.
   - Final row: `| **Total** | **{sum of counts}** | | |`.

6. Write the main `render_markdown(output, primary_filename) -> str`:
   - Title: `Path(primary_filename).stem` — replace underscores/hyphens with spaces,
     title-case it.
   - Timestamp: `datetime.now().strftime("%Y-%m-%d %H:%M")`.
   - Call `_classify_items`, `_build_summary_table`.
   - Iterate sections in order: Functional, Non Functional, Compliance, Ambiguity, Risk,
     Commercial Structure. For each non-empty section emit `## {section_name}` then
     call `_render_item` for each item.
   - Append Clarification Questions section (see Appendix).
   - Return joined string.

7. Append a **Clarification Questions for RFP Issuer** section at the end:
   ```markdown
   ---
   ## Clarification Questions for RFP Issuer

   1. **[{id}]** {clarification_question}
   2. ...
   ```
   Collect all items across all sections where `clarification_question` is non-empty,
   in section order. Number them sequentially.

**Relevant Context**

- Reference sample: the large Markdown block provided by the user in the conversation.
- `SkillOutput` schema: [`core/schemas.py`](core/schemas.py) lines 95–105.
  `items` is `list[dict[str, Any]]`; keys vary by skill.
- `SpineOutput` schema: [`core/schemas.py`](core/schemas.py) lines 113–123.
- Confirmed field names from schema files:
  - **design-build** items: `id`, `type` (functional/non_functional/staffing_mandate),
    `title`, `description`, `priority`, `confidence`, `source_section`, `source_page` (absent — use `raw_excerpt` page if needed), `ambiguity_flag`, `clarification_question`.
  - **ams** items: `id`, `title`, `description`, `priority`, `confidence`, `source_section`,
    `source_page`, `is_inferred`, `clarified_by` (no `type` field — use id prefix).
  - **rate-card** items: `id`, `role_title` (use as title), `source_section` (no `type`
    field — use id prefix or default to Functional).
  - **spine** items: `id`, `title`, `description`, `priority`, `confidence`,
    `source_section`, `ambiguity_flag`, `clarification_question`, `archetype_tag`.
  - **commercial_structure** items: `id`, `pricing_dimension` (use as title),
    `cost_type`, `quantified_value`, `mandatory`, `source_section`.

---

### Sub-Task 3 — Write the Markdown file to disk on job completion

**Status:** `[x] done`

**Intent**

Wire the renderer from Sub-Task 2 into `_run_pipeline` so `result.md` is written
alongside `result.json`.

**Expected Outcomes**

- After a successful pipeline run, both `{OUTPUT_DIR}/{job_id}/result.json` and
  `{OUTPUT_DIR}/{job_id}/result.md` exist on disk.
- A disk write failure for the Markdown file logs a warning but does not affect the job
  status (result is already persisted in memory and as JSON).

**Todo List**

1. Import `render_markdown` from `app.markdown_renderer` in
   [`app/routers/analysis.py`](app/routers/analysis.py).
2. In `_run_pipeline`, after writing `result.json`, call:
   ```python
   md_content = render_markdown(result, primary_file_path)
   (OUTPUT_DIR / job_id / "result.md").write_text(md_content, encoding="utf-8")
   ```
   Note: `result` here is the `SynthesizedOutput` **object** (before `.model_dump()`).
   `primary_file_path` is already a parameter of `_run_pipeline`.
3. Wrap the Markdown write in its own `try/except` block, separate from the JSON write,
   so one failure does not suppress the other.

**Relevant Context**

- `_run_pipeline` signature: `async def _run_pipeline(job_id, file_paths, primary_file_path, file_roles)`.
- `result` is a `SynthesizedOutput` returned by `run_pipeline(...)` — the Pydantic object
  is available before `.model_dump()` is called on the next line.

---

### Sub-Task 4 — Expose output file paths via the API

**Status:** `[x] done`

**Intent**

Let API consumers discover the output files and retrieve the Markdown directly, without
breaking any existing API contract.

**Expected Outcomes**

- `GET /jobs/{job_id}` response gains an `output_files` object when the job is complete:
  ```json
  {
    "output_files": {
      "json": "outputs/{job_id}/result.json",
      "markdown": "outputs/{job_id}/result.md"
    }
  }
  ```
- New endpoint `GET /jobs/{job_id}/markdown` returns the raw Markdown as `text/plain`.
  Returns HTTP 404 if the file does not exist.
- All existing response fields are unchanged.

**Todo List**

1. In [`app/job_store.py`](app/job_store.py), add `output_dir: str | None = None` to
   `AnalysisJob` (optional field with default `None`).
2. In [`app/routers/analysis.py`](app/routers/analysis.py), after writing the output
   files successfully, call:
   ```python
   store.get_job(job_id).output_dir = str(OUTPUT_DIR / job_id)
   ```
   (or pass it through `set_complete` if preferred — minimal change is fine).
3. In `get_job`, append to the response dict:
   ```python
   if job.output_dir:
       response["output_files"] = {
           "json": f"{job.output_dir}/result.json",
           "markdown": f"{job.output_dir}/result.md",
       }
   ```
4. Add a new FastAPI endpoint:
   ```python
   @router.get("/jobs/{job_id}/markdown", response_class=PlainTextResponse)
   async def get_job_markdown(job_id: str) -> str:
       ...
   ```
   - Retrieve job from store; 404 if not found or not complete.
   - Read and return `{job.output_dir}/result.md`; 404 if file missing.
5. Update `list_jobs` to include `output_files` per job in the same manner as step 3.

**Relevant Context**

- `AnalysisJob` model: [`app/job_store.py`](app/job_store.py) lines 36–55.
- `get_job` endpoint: [`app/routers/analysis.py`](app/routers/analysis.py) lines 179–206.
- `list_jobs` endpoint: [`app/routers/analysis.py`](app/routers/analysis.py) lines 210–226.
- FastAPI `PlainTextResponse` is in `fastapi.responses`.

---

## Appendix — Reference Markdown Format

### File-level header
```markdown
# {Document Name}

_Generated: {YYYY-MM-DD HH:MM}_
```
Document name = `Path(primary_filename).stem`, underscores/hyphens replaced with spaces.

### Summary table
```markdown
## Summary

| Category | Count | Must | Ambiguous |
|---|---|---|---|
| Functional           | N | N | N |
| Non Functional       | N | N | N |
| Compliance           | N | N | N |
| Ambiguity            | N | N | N |
| Risk                 | N | N | N |
| Commercial Structure | N | N | N |
| **Total**            | **N** | | |
```

### Item block (all categories)
```markdown
### {id} — {title}  ⚠ Ambiguous
- **Priority:** MUST
- **Confidence:** 95%
- **Section:** 2. Assignment Objectives (p.2)

Full description text here.

> **Clarification needed:** Clarification question text here.

_Related: FR-001, NFR-002_
```
- `⚠ Ambiguous` suffix only when `ambiguity_flag` is True or `clarification_question`
  is non-empty or `is_inferred` is True.
- Clarification block-quote only when `clarification_question` is non-empty.
- Related line only when `related_ids` field is present and non-empty.
- For **commercial_structure** items, use `pricing_dimension` as the title if `title`
  is absent, and show `cost_type` and `quantified_value` as additional bullet points.

### Clarification Questions appendix
```markdown
---
## Clarification Questions for RFP Issuer

1. **[CR-001]** What specific MOHAP requirements apply?
2. **[AMB-003]** What is the planned project start date?
```
Only items with non-empty `clarification_question` field appear here.

---

## Implementation Notes for Agent Mode

- All changes are confined to `integrated apps/V2/`.
- Sub-Tasks execute in order: 1 → 2 → 3 → 4.
- Sub-Task 2 (renderer) is a pure function and should be written first so it can be
  manually verified before it is wired into the pipeline in Sub-Task 3.
- No schema changes to `SynthesizedOutput`, `SkillOutput`, or `SpineOutput` are needed.
- Use `structlog` (already used in the project via `logging_config.py`) for any new log
  lines — do not use `print`.
- The renderer must be robust to missing fields: every `item[key]` access must use
  `.get(key, safe_default)` because item dicts are untyped `dict[str, Any]` and field
  presence varies across skills.
