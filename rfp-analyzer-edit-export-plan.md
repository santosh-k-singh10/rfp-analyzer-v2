# RFP Analyzer V2 — Edit, Enhance, Save & Export — Implementation Plan

> **Source spec:** `rfp-analyzer-v2-edit-export-spec(1).md` (v1.1) + amendments r1 + r2 + r3 applied
> **Codebase root:** `c:\Agents\V2`

---

## Top-Level Overview

This plan implements the full requirement-editing, AI-enhancement, persistence, and export feature set in three build phases. Each sub-task is scoped to be implemented independently, one at a time, and ships with its own tests.

**Guiding principle:** The AI baseline (`result.json`, `result.md`) is **never overwritten**. All human edits live in `working.json`. Autosave per operation (no explicit Save button) is the persistence model.

**Key design decisions:**
- Working copy is created **at job completion** (`_run_pipeline`), never on a `GET` (C4). For legacy jobs without `working.json`, GET returns the in-memory deterministic bootstrap (uids present, `_baselines` stripped, `has_edits=false`) — never the bare baseline, never writes. The first mutation re-checks that `working.json` still doesn't exist, then persists under the job lock (r2-1).
- uids are **12 hex chars**: `itm_<12hex>` for both random (new jobs) and deterministic (legacy) paths (r2-5).
- Baselines are stored in a **separate top-level `_baselines: {uid: item}` map** in `working.json`; GET drops that key entirely instead of stripping every item (amendment 13).
- After 0.4, `AnalysisJob.result` is **no longer populated**; all reads go through the repository (amendment 14).
- Optimistic concurrency is **per-item `_meta.version`**, not document revision (C6/D7).
- Spine agents are **not refactored**; spine edit field specs are a separate static dict; consistency test uses `_sanitise` probe calls, not whitelist imports (C7, amendment 8).
- AMS prefix mapping extracted from `skills_engine/ams.py` (C8).
- Excel groups by **bucket**, not by the Markdown classifier (C2).
- Duplicate check uses a new single-item O(n) helper (C3).
- `working.json` is **archived** on reset, never deleted (C11).
- Row mutations: field edits replace only the `[data-uid]` row; add/remove/restore/AMS `artifact_type` changes re-render the affected **section** only; scroll preserved in both cases (amendment 2).
- `RV.state.result` is updated **before** any DOM change after every mutation; `mutationSuccess(bucket, uid, item, editState)` takes the bucket (r2-5/amendment 1).
- POST/PATCH responses return `{item (with _meta.version), edit_state, warnings?}` (r2-5).
- `review.js` keeps an IIFE that assigns `window.RV`; exposes `RV.boot()` called after both JS files load; `window.RV` includes `renderStats` and a defined `findItemByUid` (r2-2/amendment 6).
- `renderAMSArtifacts` is split into per-artifact section renderers; `rerenderSection` calls these and refreshes KPI cards (r2-2).
- Write/export endpoints are unauthenticated — README must document this (amendment 15).

---

## Phase 0 — Foundations

### Sub-Task 0.1 — UUID Path-Safety Dependency, `app/config.py`, and Test Infrastructure

**Intent:**
Three bootstrapping concerns grouped together: (a) `job_id` must be validated as a canonical UUID on every `/jobs/{id}…` route — existing and new; (b) `OUTPUT_DIR`/`UPLOAD_DIR` must be read from env at call time (not at import), with no `mkdir` at import, via a new `app/config.py`; (c) test infrastructure needs `asyncio_mode = "auto"`, a golden fixture, and credential-guarded connectivity tests.

**Expected Outcomes:**
- `app/dependencies.py` with `validate_job_id(job_id: str) -> str`:
  - Parses input with `uuid.UUID(x)`; raises `HTTPException(400)` if it throws.
  - Returns `str(uuid.UUID(x))` (lowercase, hyphenated canonical form).
  - Rejects non-canonical inputs: braces, `urn:` prefix, no hyphens, uppercase — anything that differs from its own canonical form.
- Dependency applied to all `/jobs/{id}…` routes in `app/routers/analysis.py` **and** all new routers.
- `app/config.py` with lazily evaluated `get_output_dir() -> Path` and `get_upload_dir() -> Path` (read `OUTPUT_DIR`/`UPLOAD_DIR` env vars at call time; no `mkdir` at import). `app/routers/analysis.py` and `app/job_store.py` import these getters instead of module-level constants.
- `load_from_disk` is an **instance method** on the `JobStore` singleton (not a classmethod), called via the lifespan hook (amendment 5).
- `pyproject.toml`: add `asyncio_mode = "auto"` to `[tool.pytest.ini_options]`.
- `tests/conftest.py`: FastAPI `TestClient` fixture; tmp `OUTPUT_DIR` fixture; note that tests must not call `configure_logging()`.
- `tests/test_ica_connectivity.py`: add `pytest.mark.skip` (or `skipif`) guard so it is skipped when credentials are absent.
- `tests/fixtures/result.json`: golden fixture covering design-build (functional, non-functional, staffing_mandate items), AMS (several artifact_types), rate-card with `no_roles_stated=true`, one packaged sub-skill, all four spine categories, and a QnA-resolved ambiguity item.
- Test: `job_id="../x"` → `4xx` on every `/jobs/{id}…` route, including existing ones (AT-10).

**Todo List:**
- [ ] Create `app/config.py` with `get_output_dir()` and `get_upload_dir()`.
- [ ] Update `app/routers/analysis.py` and `app/job_store.py` to use `get_output_dir()`/`get_upload_dir()` instead of module-level constants.
- [ ] Create `app/dependencies.py` with `validate_job_id` (canonical UUID check).
- [ ] Apply `validate_job_id` to existing routes in `app/routers/analysis.py`.
- [ ] Add `asyncio_mode = "auto"` to `pyproject.toml` `[tool.pytest.ini_options]`.
- [ ] Create `tests/conftest.py` with `TestClient` and tmp dir fixtures.
- [ ] Add credential skip guard to `tests/test_ica_connectivity.py`.
- [ ] Create `tests/fixtures/result.json` golden fixture.
- [ ] Write parametrised path-safety test for AT-10.

**Relevant Context:**
- `app/routers/analysis.py` — `get_job` (lines 211–244), `get_job_markdown` (lines 247–261); module-level `OUTPUT_DIR`/`UPLOAD_DIR` constants to remove.
- `app/job_store.py` — references to `OUTPUT_DIR`.
- `app/main.py` — `logging_config` import (line 27); routers registered (lines 49–51).
- `tests/test_ica_connectivity.py` — existing connectivity test.
- Spec §4.2, AT-10; amendments 4, 5, 11.

**Status:** `[x] done`

---

### Sub-Task 0.2 — Job Persistence (`job.json`) and `load_from_disk`

**Intent:**
`JobStore` is process-local; jobs 404 after a restart. Fix: (a) `output_dir` set deterministically at job creation; (b) `job.json` sidecar written at every status transition; (c) `_run_pipeline` write order fixed — a crash must never leave an in-memory `complete` job with no result file; (d) startup scan rehydrates jobs from disk.

Correct write order (spec F12, §4.6): `result.json` → `result.md` → **[marked step for working-copy bootstrap, filled in 0.4]** → `set_complete()` (which writes `job.json` with `status:complete` then flips in-memory status) → `_run_pipeline` returns. `result.json` write failure is **fatal** (pipeline fails); `result.md` write failure stays **non-fatal** as today; bootstrap failure is **non-fatal** (log it; the lazy path in 0.4 covers the gap).

**`set_complete` (r2-3, r3-4):** writes `job.json` with `status:complete` atomically first, then flips in-memory status. If the `job.json` write fails, the exception propagates — the in-memory status stays `RUNNING`. **`set_failed` (r3-4):** must update the in-memory status even if its own `job.json` write fails — log the write error but never leave the job stuck at `RUNNING` on a failure path.

**Expected Outcomes:**
- `output_dir = get_output_dir() / job_id` — set in `create_job()`; directory created there.
- `create_job()` writes initial `job.json` (`status: pending`).
- `set_running()` updates `job.json` atomically.
- `set_complete`: write `job.json` (`status:complete`) atomically; only on success flip in-memory `job.status` to `COMPLETE`; on write failure propagate exception (in-memory stays `RUNNING`).
- `set_failed`: update in-memory status to `FAILED` first; then attempt `job.json` write; if write fails, log error and continue (in-memory is already `FAILED`) (r3-4).
- `_run_pipeline`: `result.json` write failure is fatal (propagates); `result.md` failure logs and continues; `# [0.4 HOOK]` comment left for 0.4; `store.set_complete()` called last.
- All file writes use `atomic_write(path, content, retries=3)` from `core/utils.py`.
- `job_store.load_from_disk(get_output_dir())` (instance method): scans `outputs/*/job.json`; `running` → `failed: "interrupted by restart"`; `complete` + no `result.json` → `failed`; results **not** loaded into memory.
- `app/main.py` lifespan event calls `job_store.load_from_disk(get_output_dir())`.
- Tests: AT-6 (restart simulation), ordering fix (complete not set before files written), `job.json` write failure in `set_complete` → job not marked complete; `set_failed` `job.json` write failure → job still marked failed in memory.

**Todo List:**
- [ ] Add `atomic_write(path, content, retries=3)` to `core/utils.py`.
- [ ] In `app/job_store.py`: use `get_output_dir()` in `create_job()`; create directory; add `_write_job_json(job)` using `atomic_write`; call it from `create_job()` and `set_running()`; change `load_from_disk` to an instance method.
- [ ] Update `set_complete`: write `job.json` first; flip in-memory only on success (r2-3).
- [ ] Update `set_failed`: flip in-memory to `FAILED` first; attempt `job.json` write; log and continue on write error (r3-4).
- [ ] Fix `_run_pipeline` in `app/routers/analysis.py`: make `result.json` failure fatal; keep `result.md` non-fatal; add `# [0.4 HOOK]` comment; move `store.set_complete()` to last; remove post-hoc `job.output_dir =` assignment.
- [ ] Add lifespan context manager in `app/main.py`.
- [ ] Write tests for AT-6, ordering, `set_complete` failure, and `set_failed` write-error resilience (r3-4).

**Relevant Context:**
- `app/job_store.py` — `AnalysisJob` (lines 36–54), `create_job`, `set_complete`, `set_failed`.
- `app/routers/analysis.py` — `_run_pipeline` write sequence (lines 64–98).
- `app/main.py` — no lifespan yet.
- Spec F12, §4.6, D1; amendments 5, 9; r2-3; r3-4.

**Status:** `[x] done`

---

### Sub-Task 0.3 — JS Extraction and `RV` Namespace Setup

**Intent:**
`review.html` has ~500 lines of JS inside an IIFE (lines 63–575). `JOB_ID` is a Jinja placeholder inside that IIFE (line 64). `review-edit.js` must share state and helpers that are currently IIFE-private. The IIFE is **kept** in `review.js` but refactored to assign `window.RV` with an explicit export surface; `RV.boot()` replaces the trailing `load()` call and is invoked after both JS files have loaded (amendment 6). `esc()` moves to `V2` in `app.js`.

**Expected Outcomes:**
- `review.html` retains only `<script>window.JOB_ID = '{{ job_id }}';</script>` inline; the IIFE is **not** in the HTML.
- `app/static/js/review.js` wraps all logic in an IIFE that ends with `window.RV = { state, boot, render, renderTabPanel, renderSkillPanel, renderSpinePanel, renderAMSArtifacts, makeSkillRow, makeSpineRow, findItemByUid, activeItems, renderStats }` — explicit, narrow export surface (r2-2: `renderStats` and `findItemByUid` are both exported and defined in this file).
- Inside the IIFE: `const JOB_ID = window.JOB_ID;`; `const esc = V2.esc;`; the trailing `load()` call is removed; `function boot() { load(); }` is defined and exported.
- `findItemByUid(uid)` is defined inside the IIFE: walks `RV.state.result.skill_outputs[].items` and all `spine.*` arrays, returns `{item, bucket}` or `null`.
- `RV.state = { result: null, activeTab: null, revision: 0, showRemoved: false, selection: new Set() }`.
- `app/static/js/app.js` gains `V2.esc(str)` and `V2.apiSend(method, url, body)` (JSON, surfaces 409/422 bodies).
- `app/static/js/review-edit.js` scaffolded empty with section comment headings: `// STATE`, `// ROW ACTIONS`, `// EDIT DRAWER`, `// ADD BUTTONS`, `// TOOLBAR`, `// REMOVED TOGGLE`.
- `review.html` script load order: inline `JOB_ID` → `app.js` → `review.js` → `review-edit.js` → inline `<script>RV.boot();</script>`.
- Page loads, tabs switch, all tabs render identically to before (AT-15 exit check).

**Todo List:**
- [ ] Add `V2.esc(str)` and `V2.apiSend(method, url, body)` to `app/static/js/app.js`.
- [ ] Create `app/static/js/review.js`:
  - Copy all IIFE content from `review.html`; keep the IIFE wrapper.
  - Replace `const JOB_ID = '{{ job_id }}';` with `const JOB_ID = window.JOB_ID;`.
  - Replace `function esc(s)` / uses of `esc` with `const esc = V2.esc;` / `V2.esc(...)`.
  - Remove the trailing `load()` call; define `function boot() { load(); }`.
  - Add `function findItemByUid(uid)` that walks `skill_outputs` and `spine` arrays.
  - Replace the IIFE's closing with `window.RV = { state, boot, render, renderTabPanel, renderSkillPanel, renderSpinePanel, renderAMSArtifacts, makeSkillRow, makeSpineRow, findItemByUid, activeItems, renderStats };`.
  - Initialise `state` at top of IIFE.
- [ ] Create empty `app/static/js/review-edit.js` with section comment headings.
- [ ] Update `review.html`: replace inline `<script>` block with four tags + trailing `<script>RV.boot();</script>`.
- [ ] Manual smoke-test: page loads, all tabs render.

**Relevant Context:**
- `app/templates/review.html` — IIFE lines 63–575; `JOB_ID` line 64; `window.switchTab` line 213; `esc` line 564; trailing `load()` call inside IIFE.
- `app/static/js/app.js` — `window.V2` export (lines 99–103).
- Spec §5.0, C1, F6; amendment 6.

**Status:** `[x] done`

---

### Sub-Task 0.4 — `ResultRepository` and Working-Copy Bootstrap

**Intent:**
Introduce the repository abstraction (D6) with `load`, `save`, `append_log`, `read_log`. Implement `bootstrap_working_copy`. Baselines live in a **separate `_baselines` map** at the top level of `working.json` (amendment 13). For new jobs, bootstrap runs inside `_run_pipeline` at the `# [0.4 HOOK]` marker. For legacy jobs (no `working.json`), `GET /jobs/{id}` returns the in-memory deterministic bootstrap (uids present, `_baselines` stripped, `has_edits=false`) — never the bare baseline, never writes. The first mutation re-checks under lock that `working.json` still doesn't exist, persists the bootstrap, then applies the op. `reset` on a legacy job with no `working.json` returns the current in-memory bootstrap and does not archive anything (r3-4). After this sub-task, `AnalysisJob.result` is no longer populated; reads go through the repository (amendment 14).

**Expected Outcomes:**
- `core/edit/__init__.py` (empty).
- `core/edit/repository.py`: `ResultRepository` Protocol + `FileResultRepository` with one `asyncio.Lock` per job, `atomic_write`-based save, `append_log`, `read_log`.
- `core/edit/service.py`:
  - `bootstrap_working_copy(baseline_dict, job_id) -> dict`: assigns uids — `itm_<12hex>` for both paths; for new-job path uses 12 random hex chars (collision-checked per job); for legacy path uses `sha256(job_id+bucket+index+display_id)` truncated to 12 hex chars. Builds `_meta` defaults. Stores baseline copy in top-level `_baselines[uid]` (not inside the item). Adds `_edit` block (revision=0).
  - `compute_edit_state(doc) -> dict` — counts active/removed/user_added/edited.
  - `find_item_by_uid(doc, uid) -> (item, bucket) | None`.
- `_run_pipeline` fills the `# [0.4 HOOK]` marker: calls `bootstrap_working_copy`, writes `working.json` atomically; failure is non-fatal (log, continue).
- `set_complete` in `JobStore` no longer stores `result` in `AnalysisJob.result`; `AnalysisJob.result` field removed or set to `None` permanently.
- `GET /jobs/{id}` updated:
  - Returns in-memory deterministic bootstrap (uids present, `_baselines` stripped, `has_edits=false`) for legacy jobs without `working.json` — **never** returns the bare baseline, **never** writes (r2-1).
  - `?view=original` returns baseline from `result.json` directly.
  - Running jobs behave as today (no working copy, no edit_state).
- First mutation on a legacy job: acquires lock, re-checks `working.json` doesn't exist, persists bootstrap, applies op (r2-1).
- `reset_working_copy` on a legacy job with no `working.json`: returns the current in-memory bootstrap and does not archive anything — there is nothing to archive (r3-4).
- `GET /jobs/{id}/items/{uid}/baseline` reads from `working.json`'s (or in-memory bootstrap's) `_baselines[uid]`; 404 for user items or if key absent.
- Tests: AT-1 (working copy written at completion; running job GET returns no working copy; legacy GET returns in-memory bootstrap with uids, does not write; legacy PATCH succeeds and creates `working.json`; legacy reset returns bootstrap, no archive created).

**Todo List:**
- [ ] Create `core/edit/repository.py` with `ResultRepository` Protocol and `FileResultRepository`.
- [ ] Create `core/edit/service.py` with `bootstrap_working_copy` (12-hex uids, both random and deterministic paths), `compute_edit_state`, `find_item_by_uid`.
- [ ] Fill the `# [0.4 HOOK]` in `_run_pipeline`.
- [ ] Remove `result` storage from `JobStore.set_complete`; remove or nullify `AnalysisJob.result`.
- [ ] Update `GET /jobs/{id}` handler: return in-memory bootstrap for legacy jobs (strip `_baselines`); no write; include `edit_state` (r2-1).
- [ ] Update mutation handler (`service.py`): on first mutation for legacy job, re-check under lock, persist bootstrap, then apply op (r2-1).
- [ ] Update `reset_working_copy` in `service.py`: if `working.json` doesn't exist, skip archive and return current in-memory bootstrap (r3-4).
- [ ] Add `GET /jobs/{id}/items/{uid}/baseline` endpoint.
- [ ] Write tests for AT-1 including: legacy GET → uids present → PATCH persists `working.json`; legacy reset → no archive.

**Relevant Context:**
- `core/schemas.py` — `SynthesizedOutput`, `SkillOutput`, `SpineOutput` (lines 95–144).
- `app/routers/analysis.py` — `get_job` (lines 211–244), `_run_pipeline` `# [0.4 HOOK]` marker (from 0.2).
- `app/job_store.py` — `set_complete`, `AnalysisJob.result`.
- Spec §3.1, §3.2, §4.2, D1, D6, D7, C4, C12; amendments 3, 13, 14; r2-1, r2-5; r3-4.

**Status:** `[x] done`

---

### Sub-Task 0.5 — Filter Removed Items from All Render Paths

**Intent:**
Every place in `review.js` that reads items must skip `status === "removed"` when `RV.state.showRemoved` is false. This is a pure frontend change — no new UI; just correct filtering before Phase 1 editing lands.

**Expected Outcomes:**
- `activeItems(arr)` (exposed on `window.RV`) filters items where `_meta?.status === 'removed'` unless `RV.state.showRemoved`.
- Applied at every `.items` read in `renderSkillPanel`, each section array in `renderAMSArtifacts`, `renderSpinePanel`, and every count in `renderStats`.
- `RV.state.showRemoved` defaults to `false`; toggling wired in Phase 1.

**Todo List:**
- [ ] Add `function activeItems(arr)` inside the IIFE in `review.js`; include in `window.RV` export.
- [ ] Wrap every `.items` read in `renderSkillPanel` with `activeItems(...)`.
- [ ] Wrap every AMS section array in `renderAMSArtifacts` with `activeItems(...)`.
- [ ] Wrap items list in `renderSpinePanel` with `activeItems(...)`.
- [ ] Wrap each bucket count in `renderStats` with `activeItems(...)`.

**Relevant Context:**
- `app/static/js/review.js` (from 0.3) — `renderStats`, `renderSkillPanel`, `renderAMSArtifacts`, `renderSpinePanel`.
- Spec §5.1, F7.

**Status:** `[x] done`

---

## Phase 1 — Core Edit + Export (MVP)

### Sub-Task 1.1 — ID Prefix Extraction and Field Specs

**Intent:**
Two prerequisites for all edit endpoints:
1. Extract the AMS prefix mapping from `skills_engine/ams.py` to `core/edit/id_prefix.py`; `ams.py` imports it; a test proves extraction output is unchanged.
2. `core/edit/field_specs.py` exposes `get_field_spec(kind, key)` for skills (schema.yaml via registry) and spine (separate static dict, spine agents untouched). Consistency test uses **probe calls** to `_sanitise` — not whitelist imports, because `allowed` is a local variable inside each function (amendment 8).

**Expected Outcomes:**
- `core/edit/id_prefix.py` with `get_prefix(kind, key, artifact_type=None, support_level=None) -> str` covering all AMS prefixes: `AMS-L1`, `AMS-L2`, `AMS-L3`, `AMS-SLA`, `AMS-APP`, `AMS-VOL`, `AMS-TOOL`, `AMS-GOV`, `AMS-DEL`, `AMS-RES`, `AMS-TRN`, `PEN`, `LD`, `CQ`, `ASS`, `AMS-REQ` (catch-all); plus all skill and spine prefixes (`DB`, `CR`, `RSK`, `CS`, `RR`, packaged slugs).
- `skills_engine/ams.py` imports its mapping from `core/edit/id_prefix.py`; `_assign_ids` output byte-identical (AT-14).
- `core/edit/field_specs.py`: `FieldSpec` dataclass `{name, label, type, required, enum_values, editable, help}`; `get_field_spec(kind, key)`:
  - Skills: from `skills/<slug>/schema.yaml` via `SkillRegistry`; `id`, `source_document`, `document_role`, `source_type`, `clarified_by` → `editable=False`.
  - Spine: static dicts per spec §4.4 (separate from `_sanitise` whitelists).
  - AMS: `artifact_type` is a required editable enum with help text.
- Spine consistency test: for each agent, call `_sanitise({field: "x", ...})` with a dict containing every field listed in `get_field_spec("spine", key)` where `editable=True`; assert each survives with its value intact.
- `GET /jobs/{id}/fields?kind=&key=` endpoint in new `app/routers/edits.py`; `validate_job_id` applied; UI caches per bucket.
- Tests: AT-14 (AMS output unchanged), spine consistency probe test.

**Todo List:**
- [x] Extract prefix mapping from `skills_engine/ams.py` `_assign_ids` to `core/edit/id_prefix.py`.
- [x] Update `skills_engine/ams.py` to import mapping from `core/edit/id_prefix.py`.
- [x] Create `core/edit/field_specs.py` with `FieldSpec`, static spine dicts, and `get_field_spec`.
- [x] Create `app/routers/edits.py` with `GET /jobs/{id}/fields`; apply `validate_job_id`.
- [x] Register edits router in `app/main.py`.
- [x] Write tests: AT-14 (AMS output unchanged), spine consistency probe test.

**Relevant Context:**
- `skills_engine/ams.py` — `_assign_ids`, counters dict (lines 247–330).
- `core/skill_registry.py` — `SkillMeta.schema_dict`, `get_registry()`.
- `spine/*.py` — `_sanitise` local `allowed` variable (do not import it).
- Spec §3.3, §4.4, C7, C8, F5; amendments 8.

**Status:** `[x] done`

---

### Sub-Task 1.2 — Edit Service: Add, PATCH, Remove, Restore, Revert, Reset

**Intent:**
Core CRUD logic in `core/edit/service.py` and endpoints in `app/routers/edits.py`. Concurrency: per-job lock around read-modify-write; optimistic concurrency is **per-item `_meta.version`** — editing item A never causes a 409 when later editing item B. Reset **archives** working copy to `working.<ts>.bak.json` and re-bootstraps (never deletes). User-added item defaults applied at add time (amendment 7).

**Expected Outcomes:**
- `core/edit/service.py` functions: `add_item`, `update_item`, `remove_item`, `restore_item`, `revert_item`, `reset_working_copy`.
- `add_item` defaults for user items (amendment 7): `origin:"user"`, `source_type="text"`, `document_role="primary"`, `source_document=""`, `source_section` defaults to `"User-added"` if blank, `ambiguity_flag=false`, `confidence=null` (rendered as "—"), `raw_excerpt` optional. Type-required fields (design-build `type`, AMS `artifact_type`) remain required.
- ID generation: `get_prefix` + `_edit.counters` + `U` namespace (`DB-U001`); counters never reused after delete.
- Add into a skill bucket whose `SkillOutput` entry is missing creates it (C9).
- `update_item`: validates (required fields, enum, type coercion, non-editable guard); strips unknown keys; `title > 10 words` → warning only; editing `raw_excerpt` on AI item sets `excerpt_edited=True`.
- `base_version` mismatch on PATCH → `409` with current item; cross-item edits never conflict.
- Each mutation: bumps `_meta.version` on affected item; bumps `_edit.revision`; appends log line after successful save.
- `reset_working_copy`: if `working.json` exists, renames it to `working.<ts>.bak.json`, re-bootstraps, logs; if `working.json` does not exist (legacy job), returns the current in-memory bootstrap and does not archive anything (r3-4).
- **All mutating endpoints** (add, PATCH, remove, restore, revert, enhance/accept) respond with `{item: {…with _meta.version and status…}, edit_state, warnings?}` (r3-1). No endpoint requires a full refetch.
- Endpoints in `app/routers/edits.py`: `POST /items`, `PATCH /items/{uid}`, `DELETE /items/{uid}`, `POST /items/{uid}/restore`, `POST /items/{uid}/revert`, `POST /reset?confirm=true`.
- Status codes: `404` unknown uid; `409` version mismatch + current item in body; `422` validation; `400` disallowed op / job not complete.
- Tests: AT-2, AT-3, AT-4 (cross-item 409), AT-5, AT-11.

**Todo List:**
- [ ] Implement `add_item(doc, bucket, fields)` with user-item defaults in `service.py`.
- [ ] Implement `update_item(doc, uid, fields, base_version)` with per-item version check in `service.py`.
- [ ] Implement `remove_item`, `restore_item`, `revert_item` in `service.py`; each returns the updated item + new `_meta.version`/`status`.
- [ ] Implement `reset_working_copy` with legacy-job no-archive path (r3-4).
- [ ] Implement field validation logic (required, enum, coerce, non-editable guard, `excerpt_edited`).
- [ ] Wire all endpoints in `app/routers/edits.py` through `FileResultRepository` with per-job lock.
- [ ] Add Pydantic response model: `{item (with _meta.version, status), edit_state, warnings?}` — shared by all mutating endpoints (r3-1).
- [ ] Write tests for AT-2, AT-3, AT-4, AT-5, AT-11.

**Relevant Context:**
- `core/edit/service.py`, `core/edit/repository.py` (from 0.4).
- `core/edit/id_prefix.py`, `core/edit/field_specs.py` (from 1.1).
- Spec §4.2, §4.3, §4.5, D3, D7, C6, C9, C11, C13; amendments 7; r3-1, r3-4.

**Status:** `[x] done`

---

### Sub-Task 1.3a — Row Markup, Badges, Action Cluster, and Add Buttons

**Intent:**
The DOM markup changes in `review.js` that Phase 1 editing depends on: `data-uid` on every row, action cluster (Edit · Remove; Restore · Revert when applicable — **no Enhance button in Phase 1**, arrives in 2.4), status badges, AMS action cells, and Add buttons rendered **before** any "No data" early return. `renderAMSArtifacts` is refactored into per-artifact section renderers (r2-2). No JS event logic here — that is 1.3b.

**Expected Outcomes:**
- `makeSkillRow` and `makeSpineRow` emit `data-uid` attribute + action cluster + badges:
  - `User-added` badge for `origin==="user"`.
  - `Edited` badge (tooltip listing `edited_fields`) for `edited===true`.
  - `AI-enhanced` badge for `enhanced===true`.
  - `Removed` styling: dimmed row, strikethrough on title; Restore action shown instead of Remove.
- `renderAMSArtifacts` is **split into named per-artifact section renderers** (e.g. `renderAMSRequirements`, `renderAMSSLAMatrix`, `renderAMSApps`, `renderAMSTransition`, `renderAMSPenalties`, `renderAMSClarifications`). Output verified by snapshot (r3-3, see below). `renderAMSArtifacts` becomes a coordinator calling all section renderers and refreshing KPI cards.
- **`artifact_type → section` mapping** lives in one place in `review.js` — a const dict mapping each `artifact_type` value to its section name and renderer. Every section renderer and Add-button logic reads from this single mapping (r3-2).
- Each section's Add button preselects that section's **primary** `artifact_type` in the drawer, but `artifact_type` remains **editable**, restricted to the `artifact_type` values belonging to that section (r3-2).
- Every AMS section renderer adds a trailing actions cell with `data-uid` to each table row.
- **Snapshot test (r3-3):** Before splitting, load the golden fixture in the page and save the rendered AMS container `innerHTML` to `tests/fixtures/ams_render_snapshot.html`. After the split, the same render must match the snapshot exactly (whitespace-normalised). Snapshot committed with the change.
- `renderSkillPanel` renders `+ Add requirement` button **before** the "No data" / "No items" early returns (C9/F15); similarly for `renderSpinePanel`.
- All item text through `V2.esc` / `textContent` — no raw `innerHTML` with item fields (AT-20).

**Todo List:**
- [ ] Define `AMS_SECTION_MAP` const (artifact_type → {section, primaryType, allowedTypes}) in `review.js` (r3-2).
- [ ] **Before** splitting: load golden fixture, capture AMS container innerHTML, save to `tests/fixtures/ams_render_snapshot.html` (r3-3).
- [ ] Split `renderAMSArtifacts` into per-section renderers using `AMS_SECTION_MAP`; verify output matches snapshot (whitespace-normalised) (r3-3).
- [ ] Each AMS section renderer: `data-uid` and trailing actions cell; Add button presetting primary `artifact_type`; drawer restricts `artifact_type` to section's `allowedTypes` (r3-2).
- [ ] Update `makeSkillRow` in `review.js`: `data-uid`, action cluster (Edit · Remove / Restore · Revert), badges.
- [ ] Update `makeSpineRow` in `review.js`: `data-uid`, action cluster, badges.
- [ ] Update `renderSkillPanel` in `review.js`: Add button before early returns.
- [ ] Update `renderSpinePanel` in `review.js`: Add button before early returns.
- [ ] Audit every `innerHTML` interpolation of item / `_meta` fields; ensure all pass through `V2.esc`.

**Relevant Context:**
- `app/static/js/review.js` (from 0.3) — `makeSkillRow` (lines 437–487), `makeSpineRow` (lines 506–560), `renderAMSArtifacts` (lines 276–435, outerHTML line 363), `renderSkillPanel` early returns (lines 246, 259), `renderSpinePanel` early return (line 496).
- `tests/fixtures/result.json` — golden fixture for AMS section output.
- Spec §5.2, §5.3, C9, F6, F15; amendments 2, 10; r2-2; r3-2, r3-3.

**Status:** `[x] done`

---

### Sub-Task 1.3b — Delegation Handler, Edit Drawer, Save Indicator, and State Sync

**Intent:**
All event logic for editing: a single delegated click handler on `#tab-panels`, the edit drawer, `saveDrawer()`, the save indicator, and — critically — state sync. **Before touching the DOM after any mutation**, `RV.state.result` must be updated: replace the item in its bucket, create the bucket/SkillOutput on first add, update `revision` and `edit_state`. Subsequent edits send the item's current `_meta.version` from state (amendment 1).

Row update rules (amendment 2): field edits → `replaceRow(uid, newHtml)` (know whether `li` or table `tr`); add / remove / restore / AMS `artifact_type` change → re-render the affected **section** only (not the whole tab) and refresh AMS KPI cards if applicable; scroll position preserved in both cases.

**Expected Outcomes:**
- `review-edit.js` implements:
  - `mutationSuccess(bucket, uid, updatedItem, editState)` — takes the bucket explicitly (r2-5); updates `RV.state.result` item in its bucket (creates the `SkillOutput` entry on first add); updates `RV.state.revision` and `edit_state`; then dispatches DOM update.
  - `replaceRow(uid, newHtml)` — locates `[data-uid]`, detects `li` vs `tr`, replaces in place, preserves scroll.
  - `rerenderSection(sections)` — takes a list of section names; for each AMS section calls the relevant per-section renderer from 1.3a and refreshes KPI cards; for skills/spine re-renders that panel's list. When `artifact_type` changes, the caller passes both the old and new section names so both are re-rendered (r3-2).
  - Single delegated click handler on `#tab-panels` dispatching on `closest('[data-action]')`.
  - `openDrawer(uid | null, bucket)` — fetches `/fields` (cached per bucket); renders form; populates with item values from `RV.state`; for AI items shows "Show original" toggle (fetches `…/baseline` lazily).
  - `saveDrawer()` — `POST /items` or `PATCH /items/{uid}` sending current `_meta.version` from state; response shape `{item, edit_state, warnings?}`; on success calls `mutationSuccess(bucket, uid, item, edit_state)` then DOM update; on `409` shows merge prompt; on `422` shows per-field errors; non-blocking duplicate banner scaffolded (Phase 2 wires content).
  - `removeItem(uid)`, `restoreItem(uid)`, `revertItem(uid)` — each calls `mutationSuccess(bucket, uid, item, edit_state)` then `rerenderSection([sectionForBucket(bucket)])`.
  - Save indicator: "Saved · rev N" / "Saving…" / "Save failed — retry".
  - After every mutation, `renderStats(RV.state.result)` is called.
- Save indicator element added to `review.html` toolbar.
- Tests (frontend manual): AT-16 (AMS delegation after re-render; only row/section updates; scroll preserved), AT-17 (add on empty tab), AT-18 (remove updates stats).

**Todo List:**
- [ ] Implement `mutationSuccess(bucket, uid, item, editState)` in `review-edit.js` (r2-5).
- [ ] Implement `replaceRow(uid, html)` (li/tr aware) in `review-edit.js`.
- [ ] Implement `rerenderSection(sections)` accepting a list of section names; for AMS call per-section renderers + refresh KPI cards; handle artifact_type change by passing old+new sections (r3-2).
- [ ] Implement delegated click handler on `#tab-panels` in `review-edit.js`.
- [ ] Implement `openDrawer(uid, bucket)` with cached field-spec fetch in `review-edit.js`.
- [ ] Implement `saveDrawer()` — send `_meta.version` from `RV.state`; handle success/409/422 in `review-edit.js`.
- [ ] Implement `removeItem`, `restoreItem`, `revertItem` in `review-edit.js`.
- [ ] Add save indicator to `review.html` toolbar; wire from `review-edit.js`.
- [ ] Call `renderStats` after every mutation.

**Relevant Context:**
- `app/static/js/review.js` (from 0.3) — `renderStats`, `renderSkillPanel`, `renderAMSArtifacts`.
- `app/static/js/app.js` — `V2.esc`, `V2.apiSend`.
- Spec §5.2, §5.4, §5.6, C10; amendments 1, 2; r3-2.

**Status:** `[x] done`

---

### Sub-Task 1.4 — Removed-Items Toggle, Reset Button, Edit Counts, Rate-Card Notice

**Intent:**
"Show removed (n)" tab footer toggle wires `RV.state.showRemoved` and re-renders the current panel. The toolbar gains a "Reset all edits" button and the "n edited · n added · n removed" edit-state summary line. Rate-card notice text changes when user-added roles are present (r2-4).

**Expected Outcomes:**
- Tab footer "Show removed (n)" toggle; toggling flips `RV.state.showRemoved` and re-renders current panel.
- Toolbar **"Reset all edits"** button: confirm dialog → `POST /jobs/{id}/reset?confirm=true` → on success, re-fetch the whole document (`RV.boot()` re-run or equivalent full reload) and re-render, since uids change after re-bootstrap (r2-4).
- Stats bar gains a second line showing `edit_state` counts: "N edited · N added · N removed" sourced from the `edit_state` returned by each mutation (r2-4).
- Rate-card panel: if any items have `_meta.origin === "user"`, notice reads "This RFP does not specify roles. The roles below were added manually." (§5.8, C13).
- Tests (frontend manual): AT-18 (show/hide removed), AT-21 (rate-card notice), and: Reset triggers full re-render with new uids.

**Todo List:**
- [x] In `review.js`, add tab footer with removed count to `renderSkillPanel` and `renderSpinePanel`.
- [x] In `review-edit.js`, wire toggle to `RV.state.showRemoved` and trigger panel re-render.
- [x] Add "Reset all edits" button to `review.html` toolbar; implement confirm + `POST /reset?confirm=true` + full re-fetch in `review-edit.js` (r2-4).
- [x] Add "n edited · n added · n removed" line to stats bar in `review.js`/`review-edit.js`; populate from `edit_state` (r2-4).
- [x] In `renderSkillPanel` for rate-card slug: check for user-added items and update notice text.

**Relevant Context:**
- `app/static/js/review.js` — `renderSkillPanel`, `renderStats`.
- `app/static/js/review-edit.js` — mutation handlers, toolbar.
- Spec §5.6, §5.7, §5.8, C13; r2-4.

**Status:** `[x] done`

---

### Sub-Task 1.5 — Export Endpoints and Exporters

**Intent:**
Three download formats from `GET /jobs/{id}/export`. Excel groups by **bucket** (amendment 2/C2). Markdown filters then calls `render_markdown` — which takes a `SynthesizedOutput`, so filter the dict first then `model_validate` (amendment 12). `Content-Disposition` RFC 5987-encoded. `view=original` documents have no `_meta` — handle gracefully in Excel (amendment 12). README security note added here (amendment 15).

**Expected Outcomes:**
- `app/exporters/__init__.py`.
- `app/exporters/classify.py` with `group_by_bucket(doc) -> dict[str, list[dict]]` — one key per spine category; design-build split into `Functional`/`Non Functional`/`Staffing Mandates` by `type`; every other skill its own key.
- `app/exporters/xlsx.py` — `build_workbook(doc, primary_file_name, log_entries, include_removed) -> openpyxl.Workbook`:
  - Sheet 1: Summary (file name, job id, export timestamp, view, per-sheet counts).
  - Content sheets per `group_by_bucket`; empty sheets skipped; sheet names ≤ 31 chars, `[]:*?/\` stripped, made unique.
  - AMS sheet has `Artifact Type` column (O6 default).
  - Per-sheet columns: common set + category-specific from field spec.
  - Formula guard: cells starting `=`, `+`, `-`, `@`, tab (`\t`), or carriage return (`\r`) written as literal text.
  - User-origin rows tinted; removed rows grey/strikethrough; header bold+frozen; autofilter; wrap text.
  - `view=original` (no `_meta`): Origin defaults to `AI`, Edited defaults to `N`; Change Log sheet omitted.
  - Sheet 3 (edited view only): Change Log from `read_log`.
- `app/markdown_renderer.py` updated: export path filters removed items from doc dict, calls `SynthesizedOutput.model_validate(filtered_dict)`, passes that to `render_markdown`; adds `✎ Edited` / `＋ Added` markers. Default call (baseline) unchanged — `result.md` stays byte-identical (AT-9).
- `app/routers/exports.py` — `GET /jobs/{id}/export?format=xlsx|md|json&include_removed=false&view=edited|original&strip_meta=false`; RFC 5987 `Content-Disposition`; `validate_job_id` dependency.
- JSON export: working doc as-is (including `_baselines`); `strip_meta=true` removes `_meta`, `_edit`, `_baselines`; `view=original` returns baseline `result.json`.
- Export toolbar menu in `review.html`/`review-edit.js`: format selector, view radio, include-removed checkbox; browser download.
- README updated with security note: write/reset/bulk endpoints are unauthenticated; must not be exposed beyond localhost without auth (amendment 15).
- Tests: AT-8 (Excel opens; AMS/rate-card/DB on own sheets; formula injection; `view=original` has no Change Log), AT-9 (Markdown markers; baseline byte-identical), AT-22 (export menu options; non-ASCII filenames), JSON export test (as-is, `strip_meta`, `view=original`).

**Todo List:**
- [ ] Create `app/exporters/__init__.py`.
- [ ] Create `app/exporters/classify.py` with `group_by_bucket`.
- [ ] Create `app/exporters/xlsx.py` with `build_workbook` (sheet name limits, formula guard including `\t`/`\r`, `view=original` handling).
- [ ] Update `app/markdown_renderer.py`: export wrapper that filters dict, `model_validate`, then renders; add markers. Default signature unchanged.
- [ ] Create `app/routers/exports.py`; apply `validate_job_id`.
- [ ] Register exports router in `app/main.py`.
- [ ] Add Export toolbar menu to `review.html` and `review-edit.js`.
- [ ] Update README with security note.
- [ ] Write tests for AT-8, AT-9, AT-22, and JSON export.

**Relevant Context:**
- `app/markdown_renderer.py` — `render_markdown(output: SynthesizedOutput, primary_filename: str)` signature (lines 1–263); `_classify_items` puts AMS/rate-card/packaged in Functional (F7 — Excel must not reuse it, C2).
- `core/schemas.py` — `SynthesizedOutput.model_validate`.
- `core/edit/repository.py` — `read_log`.
- `pyproject.toml` — `openpyxl>=3.1.2` confirmed.
- Spec §7.1–§7.5, C2; amendments 12, 15.

**Status:** `[x] done`

---

### Sub-Task 1.6 — History Endpoint and Panel

**Intent:**
`GET /jobs/{id}/history` reads `edits.log.jsonl` via `repository.read_log`; returns paginated entries newest-first. Backs the History side panel in the toolbar.

**Expected Outcomes:**
- `GET /jobs/{id}/history?page=1&page_size=50` returns `{entries, total, page, page_size}`.
- Each entry: `{ts, op, uid, id, bucket, fields_changed:{field:{from,to}}, actor, revision}`.
- History button in toolbar opens a side panel; entry text through `V2.esc` (AT-20).

**Todo List:**
- [x] Add `GET /jobs/{id}/history` endpoint to `app/routers/edits.py`.
- [x] Implement pagination over `read_log` (reverse, slice).
- [x] Add History button + side panel to `review.html` and `review-edit.js`.

**Relevant Context:**
- `core/edit/repository.py` — `read_log`.
- Spec §4.2, §5.6.

**Status:** `[x] done`

---

## Phase 2 — Enhance + Scale

### Sub-Task 2.1 — Bulk Operations (Single Save)

**Intent:**
Bulk is prioritised as the practical answer to 1,000+ item volumes. A single HTTP call: one lock acquire, all ops applied in memory, one file write, one revision bump, one log entry per uid (C12, AT-13).

**Expected Outcomes:**
- `POST /jobs/{id}/items/bulk` body `{uids[], op:"remove"|"restore"|"set_field", field?, value?}`.
- One lock acquire → apply all ops → one `save` call → release → append log entries.
- Response: `{results: [{uid, ok, error?, meta: {status, version}}], edit_state, revision}` — client applies these through a bulk-`mutationSuccess` equivalent; no full refetch required (r3-1).
- Frontend: checkbox column on rows; bulk action bar when selection non-empty; Remove and Restore.
- Tests: AT-13 (500 uids → exactly one file write and one revision bump).

**Todo List:**
- [ ] Add `bulk_op(doc, uids, op, field, value) -> list[dict]` to `core/edit/service.py`.
- [ ] Add `POST /jobs/{id}/items/bulk` endpoint to `app/routers/edits.py`.
- [ ] Add Pydantic bulk response model: `{results:[{uid,ok,error?,meta:{status,version}}], edit_state, revision}` (r3-1).
- [ ] Add checkbox rendering to row markup in `review.js`.
- [ ] Add `RV.state.selection` management and bulk action bar to `review-edit.js`.
- [ ] Wire bulk remove/restore in `review-edit.js`; update `RV.state.result` for each uid from `results`; call `rerenderSection` for affected sections (r3-1).
- [ ] Write test for AT-13.

**Relevant Context:**
- `core/edit/service.py` — individual op functions.
- Spec §4.2, C12; r3-1.

**Status:** `[x] done`

---

### Sub-Task 2.2 — Duplicate Warning Helper

**Intent:**
A new O(n) `find_similar_to` in `synthesizer/dedup.py` compares a single item against its bucket's active items. The existing O(n²) `find_duplicate_candidates` is never called from the edit path (C3/F13/AT-12).

**Expected Outcomes:**
- `find_similar_to(item, items, threshold=0.90) -> list[{uid, id, similarity}]` added to `synthesizer/dedup.py`.
- Called in `add_item` and `update_item` after successful write.
- `warnings: list[{type, uid, id, similarity}]` in POST/PATCH response models (non-blocking).
- Dismissable yellow banner in the drawer when `warnings` present.
- Tests: AT-12 (2,000-item bucket completes in reasonable time; assert `find_duplicate_candidates` never called).

**Todo List:**
- [x] Add `find_similar_to` to `synthesizer/dedup.py`.
- [x] Call in `service.add_item` and `service.update_item`.
- [x] Include `warnings` in response models.
- [x] Render duplicate warning banner in drawer (`review-edit.js`).
- [x] Write test for AT-12.

**Relevant Context:**
- `synthesizer/dedup.py` — `_item_fingerprint` (lines 57–82); O(n²) `find_duplicate_candidates` (lines 85–118).
- Spec §4.5, C3, F13.

**Status:** `[x] done`

---

### Sub-Task 2.3 — Enhancement Prompt and Orchestration

**Intent:**
`core/prompts/enhance.py` defines guardrails and `build_enhance_prompt`. `core/edit/enhance.py` calls `call_llm_async`, post-validates, returns `EnhanceProposal`. Lock **not held** during LLM call. `[]` from LLM → `502`.

**Expected Outcomes:**
- `core/prompts/enhance.py` following `QNA_RECONCILIATION_FRAGMENT` pattern: guardrail constant + `build_enhance_prompt(item, mode, instruction) -> (system_prompt, user_content)` for 4 modes.
- `core/edit/enhance.py`: `EnhancementFailed`, `EnhanceProposal`, `enhance_item(item, mode, instruction)`. Post-validates: rejects out-of-scope fields; flags `unverified_numbers` (digit tokens not in item text or instruction). `[]` → raises `EnhancementFailed`.
- `POST /jobs/{id}/items/{uid}/enhance` — reads item under lock, releases, calls LLM, returns proposal (not persisted); `502` on failure.
- `POST /jobs/{id}/items/{uid}/enhance/accept` — `{base_version, proposal}`; acquires lock, applies via `update_item` with `enhanced=True`; `409` on version conflict; response is `{item (with _meta.version, status), edit_state, warnings?}` (r3-1).
- `origin:"user"` items without `raw_excerpt` → only `clarify_wording` allowed (`400` otherwise).
- Tests: AT-7.

**Todo List:**
- [ ] Create `core/prompts/enhance.py`.
- [ ] Create `core/edit/enhance.py`.
- [ ] Add enhance and enhance/accept endpoints to `app/routers/edits.py`.
- [ ] Write tests for AT-7.

**Relevant Context:**
- `core/llm_client.py` — `call_llm_async` returns `[]` on failure (F8, lines 451–605).
- `core/prompts/qna_reconciliation.py` — fragment pattern.
- Spec §6.1–§6.3, §6.5, F8, C12; r3-1.

**Status:** `[x] done`

---

### Sub-Task 2.4 — Enhancement Diff UI

**Intent:**
Per-field before/after diff UI for the enhance flow. Enhance button arrives here (Phase 2, amendment 10). One in-flight enhance per job. Accept → persist; Reject → nothing saved.

**Expected Outcomes:**
- Enhance button added to row action cluster in `makeSkillRow`/`makeSpineRow`/AMS rows (amendment 10: not in Phase 1).
- Mode selector (4 modes; `custom` shows free-text); pending: button disabled + spinner.
- Proposal panel: per-field diff with editable after-text; rationale; `needs_info` list; `unverified_numbers` banner; all text through `V2.esc`.
- Accept → `POST …/enhance/accept`; calls `mutationSuccess(bucket, uid, item, edit_state)` + `replaceRow`.
- Reject → close, nothing saved.
- Tests (frontend manual): AT-20 (XSS in proposal text).

**Todo List:**
- [ ] Add Enhance button to `makeSkillRow`, `makeSpineRow`, AMS rows in `review.js`.
- [ ] Implement `openEnhancePanel(uid)` in `review-edit.js`.
- [ ] Implement proposal rendering and Accept/Edit/Reject handlers.
- [ ] Track in-flight enhance per job; re-enable on completion or error.

**Relevant Context:**
- `review-edit.js` — `mutationSuccess`, `replaceRow` (from 1.3b).
- Spec §6.3, §6.5; amendment 10.

**Status:** `[x] done`

---

## Cross-Cutting Concerns

### Security
- `validate_job_id` on **every** `/jobs/{id}…` route (existing and new); returns canonical lowercase UUID form; rejects braces, `urn:`, no hyphens, uppercase (amendment 4).
- All user-entered text via `V2.esc` / `textContent` (AT-20).
- Excel formula guard: `=`, `+`, `-`, `@`, `\t`, `\r` as literal text (AT-8, amendment 12).
- **Write, reset, and bulk endpoints are unauthenticated.** README documents they must not be exposed beyond localhost without auth in front (amendment 15).

### Concurrency
- One `asyncio.Lock` per job in `FileResultRepository`; never held across an LLM call.
- Bulk ops: single acquire, one write, one release (AT-13).
- `atomic_write` retries on `PermissionError` (Windows, F14).

### Backwards Compatibility
- Legacy jobs (no `working.json`): GET returns in-memory deterministic bootstrap (uids present, `has_edits=false`) — never the bare baseline, never writes; first mutation re-checks, then persists under lock (r2-1).
- Legacy jobs (no `job.json`): `load_from_disk` falls back to folders with only `result.json`.
- `result.json` and `result.md` never modified after first write.
- `render_markdown` default signature unchanged; `result.md` byte-identical (AT-9).

---

## Acceptance Test Coverage

| AT | Phase | Sub-Task |
|---|---|---|
| AT-1 Working copy at completion, not on GET; legacy GET returns in-memory bootstrap with uids, does not write; legacy PATCH persists it | 0 | 0.4 |
| AT-2 User-added IDs; add into empty skill creates SkillOutput | 1 | 1.2 |
| AT-3 PATCH → edited/edited_fields/baseline unchanged/log; revert clears; excerpt_edited | 1 | 1.2 |
| AT-4 Stale base_version → 409; cross-item edit no 409; unknown field → 422 | 1 | 1.2 |
| AT-5 Remove hides; restore returns; include_removed export | 1 | 1.2, 1.5 |
| AT-6 Restart: load_from_disk; running→failed; complete+no result→failed | 0 | 0.2 |
| AT-7 Enhance: unverified numbers, priority rejected, []→502, not persisted until accept, lock not held | 2 | 2.3 |
| AT-8 Excel: own sheets for AMS/rate-card/DB; formula guard (incl. \t \r); no Change Log on original view | 1 | 1.5 |
| AT-9 Markdown markers; baseline byte-identical | 1 | 1.5 |
| AT-10 Path safety on all /jobs/{id}… routes (existing + new); canonical UUID enforcement | 0 | 0.1 |
| AT-11 Reset archives working.json; logs reset | 1 | 1.2 |
| AT-12 Duplicate check O(n); all-pairs never called from edit path | 2 | 2.2 |
| AT-13 Bulk: one file write, one revision bump for 500 uids | 2 | 2.1 |
| AT-14 AMS output unchanged after prefix extraction; spine consistency probe test | 1 | 1.1 |
| AT-15 Page loads after JS extraction; JOB_ID resolves; RV.boot() works; tabs render | 0 | 0.3 |
| AT-16 AMS delegation after re-render; row/section replace, not tab; scroll preserved | 1 | 1.3b |
| AT-17 Add on empty tab; fired skill with no items | 1 | 1.3a, 1.3b |
| AT-18 Remove updates stats; show/hide removed | 1 | 1.3b, 1.4 |
| AT-19 Reload + restart preserve edits | 0 | 0.2, 0.4 |
| AT-20 XSS renders as text in rows, drawer, history, Excel, proposal panel | 0/1/2 | 0.3, 1.3a, 1.5, 1.6, 2.4 |
| AT-21 Rate-card notice changes after user adds role | 1 | 1.4 |
| AT-22 Export menu options; non-ASCII filenames download correctly | 1 | 1.5 |
| AT-23 JSON export: as-is, strip_meta, view=original each correct | 1 | 1.5 |

---

## Open Decisions (defaults from spec §10)

| # | Decision | Default |
|---|---|---|
| O1 | Save model | Autosave per operation |
| O2 | Delete | Soft delete only |
| O3 | Enhance grounding | Excerpt-only (no chunk persistence) |
| O4 | Rate-card flag | Keep `no_roles_stated`; label manual roles User-added |
| O5 | Cross-bucket moves | Not in v1 |
| O6 | Excel AMS grouping | One AMS sheet with `Artifact Type` column |
| O7 | Frontend tests | Manual checklist |
