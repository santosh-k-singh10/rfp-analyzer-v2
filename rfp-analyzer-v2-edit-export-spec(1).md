# RFP Analyzer V2 — Requirement Editing, Enhancement, Save & Export

**Spec v1.1** for a coding agent. Written against the V2 codebase as uploaded (`V2 - Copy.zip`), amended after review of `rfp-analyzer-edit-export-plan.md`.

### Changes from v1.0 (read first)

| # | Change | Why |
|---|---|---|
| C1 | Template/JS extraction rules added (§5.0): `JOB_ID` handoff, no IIFE, shared namespace, `V2.esc` | Verbatim move would break `{{ job_id }}` and hide state from `review-edit.js` |
| C2 | Excel groups by **bucket**, not by the Markdown classifier (§7.2) | `_classify_items` sends all untyped skill items (AMS, rate-card, packaged) to "Functional" |
| C3 | Duplicate check is **single-item vs bucket**, not all-pairs (§4.5) | `find_duplicate_candidates` is O(n²) difflib |
| C4 | Working copy is created at **job completion**, never on a GET (D1, §4.6) | Reads must not write; polling GET must not race |
| C5 | Job persistence order fixed (§4.6) | `_run_pipeline` marks complete before files exist and sets `output_dir` afterwards |
| C6 | Conflict detection is **per-item `_meta.version`**, not document revision (D7, §4.3) | Document revision causes false 409s |
| C7 | Spine agents are **not refactored**; edit specs are built separately with a consistency test (§4.4) | Extraction whitelist ≠ editable fields; wrong dependency direction |
| C8 | AMS prefix mapping must be extracted from `ams.py` (§3.3) | Mapping is inline there today |
| C9 | Add button renders on empty tabs; missing `SkillOutput` is created on first add (§5.3, §4.2) | Panels return early on empty data |
| C10 | Row-level DOM replace, spec cache (§5.2) | Whole-tab re-render is slow at 1,000+ items |
| C11 | Reset archives instead of deleting (§4.2) | One confirm must not destroy SME work |
| C12 | `read_log` in repository from Phase 0; UUID check is a global dependency; bulk = one save; LLM failure handling; no lock across LLM call; Windows `os.replace` retry; `Content-Disposition` encoding; baseline not sent on every GET | Review items |
| C13 | `excerpt_edited`, rate-card notice wording, export-menu options, and tests are now explicit tasks | Were missing from the plan |

---

## 0. Goal

After an analysis completes, a user can, on the Review page:

1. **Add** a requirement the tool missed.
2. **Edit** any field of a generated requirement.
3. **Remove** a requirement (reversibly).
4. **Enhance** a requirement with AI help, always as a proposal the user accepts or rejects.
5. **Save** all of this to disk so it survives reloads and restarts.
6. **Export** the edited result as Excel, Markdown or JSON.

Non-goals: multi-user permissions/login, re-running extraction, re-running the synthesizer after edits, import of externally edited files (Phase 3).

Guiding principle (consistent with the tool's extraction-only stance): the **AI baseline is never overwritten**. Human edits live in a separate working copy so the extraction stays auditable and any item can be reverted.

---

## 1. Findings from the current code

| # | Finding | Consequence |
|---|---|---|
| F1 | `JobStore` is a process-local dict. `result.json` / `result.md` are written once after the pipeline completes; nothing reloads jobs from `outputs/` on startup. | Jobs must be reloadable; edits must persist to disk. |
| F2 | `get_job` returns `output_files` as **server filesystem paths**; the only content endpoint is `/jobs/{id}/markdown`. No JSON download, no Excel. | Need real download endpoints. `openpyxl` is already a dependency. |
| F3 | Items are untyped dicts in `skill_outputs[].items` and `spine.{compliance,ambiguity,risk,commercial_structure}`. IDs are positional counters per module (`DB-001`, `CR-001`, `RSK-001`, `CS-001`, `RR-001`, `AMS-L1-001`, `AMS-SLA-001`, packaged `{SLUG}-001`, …). | No stable key. Add `uid`. Never renumber display IDs. |
| F4 | `SynthesizedOutput` items are `dict[str, Any]`; extra keys pass validation. | Per-item metadata can live inline (`_meta`) with no Pydantic model change. |
| F5 | Skill fields are defined in `skills/<slug>/schema.yaml`. Spine agents have no yaml; each has an `allowed` whitelist in `_sanitise`. The whitelists describe what the **LLM may emit**, not what a **user may edit** (e.g. compliance's set has `source_type` but not `source_document`; ambiguity's adds `resolved`/`clarified_by`). | Build edit specs separately; do not repurpose extraction whitelists (§4.4). |
| F6 | `review.html` is read-only; ~500 lines of JS are inline **inside an IIFE**, and begin with `const JOB_ID = '{{ job_id }}';` (a Jinja placeholder). `esc()`, `_result`, `renderTabPanel`, `makeSkillRow` are private to the IIFE; only `window.switchTab` is exposed. `V2` in `app.js` exports `apiGet, apiPostFile, pollJob, skillClass, makeTag, makeConfidenceBar, priorityLabel, confidencePct` — **no `esc`**. `renderAMSArtifacts` builds rows with `makeSkillRow(...).outerHTML`. | Static JS cannot contain `{{ job_id }}`; split files cannot see IIFE-private state; `outerHTML` drops listeners → event delegation on `data-uid`. |
| F7 | `renderStats` counts all items. `render_markdown(output, primary_file_path)` is a pure function over `SynthesizedOutput`. `_classify_items` puts every skill item whose `type` is not `functional`/`non_functional`/`staffing_mandate` (and whose id doesn't start `NFR-`) into **Functional** — so AMS, rate-card and packaged items all land there. | Stats must count active items only. The classifier is fine for Markdown but wrong for Excel (§7.2). |
| F8 | `call_llm_async(system_prompt, user_content, …) -> list[dict]`; a single JSON object is wrapped in a list; **returns `[]` after exhausting retries**. | Enhance must treat `[]` as failure, not as an empty proposal. |
| F9 | Only the `SynthesizedOutput` is stored; document chunks are not persisted. | Enhance can ground only on the item's own fields/`raw_excerpt` unless chunks are persisted (§6.4). |
| F10 | `app/main.py` imports `logging_config`, which is absent from the uploaded zip. | Confirm it exists in the real repo; otherwise add it. |
| F11 | `schema.yaml` for design-build documents IDs as `FR-/NFR-`, but `design_build.py` assigns `DB-NNN`. | Don't rely on ID prefixes to classify; require `type` on add. |
| F12 | `_run_pipeline` calls `store.set_complete(...)` **before** writing `result.json`/`result.md`, then assigns `job.output_dir` directly on the job object. `outputs/{job_id}/` does not exist until then. | job.json written inside `set_complete` would lack `output_dir`; a crash between steps leaves a "complete" job with no result (§4.6). |
| F13 | `find_duplicate_candidates(items)` compares **every pair** with `difflib.SequenceMatcher`. | Unusable per-edit on large buckets (§4.5). |
| F14 | Codebase root is a Windows path (`c:\Agents\V2`). | `os.replace` may raise `PermissionError` if another process holds the file; retry briefly. |
| F15 | `renderSkillPanel`/`renderSpinePanel` return early on "No data/No items". `renderTabBar` has no count badges. | Add buttons must render before those returns (§5.3). |

---

## 2. Design decisions

**D1. Baseline + working copy**, created **at job completion** (inside `_run_pipeline`, after baseline files are written). A lazy fallback creates it under the job lock for older jobs that have none. `GET` never writes.

```
outputs/{job_id}/
  result.json            # AI baseline — immutable
  result.md              # AI baseline markdown — immutable
  job.json               # job metadata sidecar (reload after restart)
  working.json           # edited SynthesizedOutput + per-item _meta + top-level _edit
  working.<ts>.bak.json  # archived working copies from "Reset"
  edits.log.jsonl        # append-only audit log
  exports/               # optional cache
```

`GET /jobs/{id}` returns the working copy when present, otherwise the baseline (job must be `complete`; while `running` it behaves as today).

**D2. Autosave per operation**, atomic write (temp file + `os.replace` with a short retry loop for Windows). UI shows "Saved · rev N".

**D3. Soft delete.** `_meta.status="removed"`; restorable; excluded from exports by default; no hard purge in v1.

**D4. Enhance is propose-then-accept.** The LLM never writes to an item directly.

**D5. Honest provenance.** User content is labelled everywhere. `origin:"user"` items are exempt from excerpt-traceability audits; an edited `raw_excerpt` sets `excerpt_edited:true`.

**D6. Repository abstraction** (`ResultRepository`: `load`, `save`, `append_log`, **`read_log`**) with a file implementation now, DB later.

**D7. Optimistic concurrency is per item.** Each item carries `_meta.version` (int, +1 per change to that item). PATCH/accept send `base_version`; mismatch → `409` with the current item. `_edit.revision` still increments on every write but is **display-only** (save indicator, history), never a conflict check.

---

## 3. Data model

### 3.1 Per-item metadata (inline `_meta`)

```json
{
  "id": "DB-014",
  "title": "...", "description": "...",
  "_meta": {
    "uid": "itm_7f3a9c21",
    "origin": "ai",                // "ai" | "user"
    "status": "active",            // "active" | "removed"
    "version": 1,
    "edited": false,
    "edited_fields": [],
    "excerpt_edited": false,
    "enhanced": false,
    "created_at": "...", "updated_at": "...", "updated_by": "local",
    "baseline": { ...original item without _meta... }   // ai items only
  }
}
```

`baseline` is stored in `working.json` but **not sent on every GET**. `GET /jobs/{id}` returns items with `_meta.baseline` omitted; the drawer fetches it on demand via `GET /jobs/{id}/items/{uid}/baseline` (and `edited_fields` already tells the row what to badge). Exports that need it read the file directly.

### 3.2 Top-level `_edit`

`{ "revision": 12, "created_at", "updated_at", "counters": { "DB": 3, "AMS-SLA": 1 } }`

### 3.3 IDs for user-added items

Existing prefix plus a `U` namespace: `DB-U001`, `CR-U002`, `RSK-U001`, `CS-U001`, `RR-U001`, `AMS-SLA-U001`, packaged `<SLUG>-U001`. Never renumber; counters never reuse.

The prefix mapping must come from one place. **AMS's mapping is currently inline in `skills_engine/ams.py`** (`AMS-L1/L2/L3`, `AMS-SLA`, `AMS-APP`, plus the penalty/clarification/assumption/etc. prefixes): extract it into `core/edit/id_prefix.py` (or a neutral `core/ids.py`) and have `ams.py` import it, with a test proving extraction output is unchanged. Confirm the ambiguity prefix and the packaged slug-derived prefix while doing so.

### 3.4 Buckets

- `{"kind":"skill","key":"<slug>"}` → `skill_outputs[slug].items` (create the `SkillOutput` entry if the skill fired but has none)
- `{"kind":"spine","key":"compliance|ambiguity|risk|commercial_structure"}` → `spine.<key>`

---

## 4. Backend

### 4.1 Layout

```
app/routers/edits.py, app/routers/exports.py
app/exporters/__init__.py, app/exporters/xlsx.py, app/exporters/classify.py   # classify = Excel grouping (§7.2)
core/edit/{__init__,repository,service,field_specs,id_prefix,enhance}.py
core/prompts/enhance.py
```

### 4.2 Endpoints

`job_id` is validated as a UUID by a **single FastAPI dependency applied to every `/jobs/{id}…` route, including the existing ones**.

| Method & path | Purpose |
|---|---|
| `GET /jobs/{id}` | Working copy (or baseline), `edit_state:{revision,has_edits,counts:{active,removed,user_added,edited}}`. `?view=original` = baseline. `_meta.baseline` omitted. |
| `GET /jobs/{id}/items/{uid}/baseline` | Original AI values for the diff view. |
| `POST /jobs/{id}/items` | Add item `{bucket, fields}`; creates missing `SkillOutput`. |
| `PATCH /jobs/{id}/items/{uid}` | `{base_version, fields}` → updated item (+`warnings`); `409` on version mismatch. |
| `DELETE /jobs/{id}/items/{uid}` | Soft-remove. |
| `POST …/items/{uid}/restore` · `…/revert` | Undo remove · reset an AI item to baseline (`400` for user items). |
| `POST …/items/{uid}/enhance` · `…/enhance/accept` | §6. |
| `POST /jobs/{id}/items/bulk` | `{uids[], op:"remove"\|"restore"\|"set_field", field?, value?}` — **single in-memory pass, one save, one revision bump**, per-uid result list, one log entry per uid written as a batch. |
| `POST /jobs/{id}/reset?confirm=true` | **Archives** `working.json` to `working.<ts>.bak.json`, writes a log entry, then re-bootstraps. |
| `GET /jobs/{id}/history` | Paginated audit log, newest first. |
| `GET /jobs/{id}/fields?kind=&key=` | Editable field spec (UI caches per bucket). |
| `GET /jobs/{id}/export?format=xlsx\|md\|json&include_removed=false&view=edited\|original&strip_meta=` | Download. |

Status codes: `404` unknown job/uid; `409` version mismatch (response includes current item); `422` validation; `400` job not complete / disallowed operation; `502` LLM failure on enhance.

### 4.3 Concurrency and atomicity

- One `asyncio.Lock` per job around read-modify-write; **never held across an LLM call**.
- Atomic save: temp file → `os.replace`, retried a few times on `PermissionError` (Windows).
- Log lines appended after a successful save: `{ts, op, uid, id, bucket, fields_changed:{field:{from,to}}, actor, revision}`.
- Item operations return only affected item(s) + new revision.
- The JobStore and locks are in-process; running multiple uvicorn workers is unsupported until the DB migration.

### 4.4 Field specs

`get_field_spec(kind, key) -> list[FieldSpec{name,label,type,required,enum_values,editable,help}]`.

- **Skills:** from `skills/<slug>/schema.yaml` via the registry. Read-only: `id`, `source_document`, `document_role`, `source_type`, `clarified_by`.
- **Spine:** a **separate static dict in `core/edit/field_specs.py`** describing user-editable fields (compliance/risk/ambiguity: `title, description, source_section, priority, confidence, ambiguity_flag, clarification_question, raw_excerpt, archetype_tag`; ambiguity also `resolved, clarified_by`; commercial_structure: `category, cost_type, pricing_dimension, quantified_value, mandatory, raw_excerpt, source_section, archetype_tag`).
- **Spine agents are not modified.** Add a test asserting every editable spine field exists in that agent's emitted shape or is an explicitly listed edit-only addition, so the two lists cannot drift silently.
- **AMS:** `artifact_type` is a required editable enum and determines which AMS section the item appears in.

### 4.5 Validation

- Required fields, enum membership, type coercion (`confidence` clamped 0–1, `min_headcount` int ≥ 0, booleans); unknown keys stripped; writes to non-editable fields → `422`.
- `title` > 10 words → warning only.
- `raw_excerpt` required for AI items, optional for `origin:"user"`; editing it on an AI item sets `excerpt_edited`.
- **Duplicate warning:** a new helper `find_similar_to(item, bucket_items, threshold=0.90)` in `synthesizer/dedup.py` compares **only the new/edited item against its bucket's active items** using the existing `_item_fingerprint` and `difflib` ratio (O(n)). Result goes in `warnings:[{type:"possible_duplicate",uid,id,similarity}]`; non-blocking. Do not call the all-pairs function.
- Text is stored raw; the UI escapes on render.

### 4.6 Persistence and reload (Phase 0)

1. Persist order in `_run_pipeline`: write `result.json` and `result.md` atomically → bootstrap and write `working.json` → write `job.json` with `status:"complete"` and `output_dir` → only then set the in-memory status. `output_dir` is deterministic (`OUTPUT_DIR/{job_id}`), set at job creation.
2. `create_job` creates `outputs/{job_id}/` and writes an initial `job.json` (status `pending`); `set_running`/`set_failed` update it.
3. Startup `JobStore.load_from_disk(OUTPUT_DIR)` scans `outputs/*/job.json` (fallback: folders with only `result.json`). Jobs left `running` → `failed: "interrupted by restart"`. A job marked `complete` whose `result.json` is missing → `failed`. Results are **not** loaded into memory; reads go through the repository.

---

## 5. Frontend (Review page)

### 5.0 Prerequisite refactor (behaviour-preserving)

- Keep a tiny inline script in `review.html`: `window.JOB_ID = '{{ job_id }}';`. Static JS reads `window.JOB_ID`.
- Move the review script to `static/js/review.js`, **removing the IIFE** and exposing one namespace, e.g. `window.RV = { state, render, renderTabPanel, makeSkillRow, makeSpineRow, findItemByUid, … }`. `review-edit.js` extends `RV` and uses its state; nothing it needs may stay private.
- Move `esc()` to `app.js` as `V2.esc`; add `V2.apiSend(method, url, body)` (JSON, error parsing, 409/422 bodies surfaced).
- Load order: inline `JOB_ID` → `app.js` → `review.js` → `review-edit.js`.
- Exit check: page loads, tabs switch, all tabs render identically to before.

### 5.1 State

`RV.state = { result, activeTab, revision, showRemoved:false, selection:Set }`. `activeItems(arr)` filters removed items unless `showRemoved`; apply at **every** item read, including `renderStats` and each AMS section array.

### 5.2 Row actions

Rows carry `data-uid` and an action cluster (Edit · Enhance · Remove; Restore · Revert when applicable). One delegated click handler on `#tab-panels`. AMS tables get a trailing actions cell. After a mutation, **replace only the affected `[data-uid]` row** (and refresh stats/section counts); do not re-render the tab, and preserve scroll. Cache field specs per bucket.

Badges: `User-added`, `Edited` (tooltip `edited_fields`), `AI-enhanced`, `Removed` (dimmed, struck-through).

### 5.3 Add

`＋ Add requirement` at the top of every tab; AMS gets one button per artifact section presetting `artifact_type`. **Render the button before any "No data / No items" early return** so empty tabs and fired-but-empty skills are addable. Opens the drawer in create mode with the bucket fixed.

### 5.4 Edit drawer

Right-side drawer from the field spec: input / textarea / select / checkbox / 0–1 slider / number; read-only system fields as muted text. Footer: Save, Cancel, and for AI items Revert and a "Show original" toggle (fetches `…/baseline`). Inline `422` errors; non-blocking duplicate banner; on `409` show the server version with *Overwrite* / *Discard mine*.

### 5.5 Safety

All item text through `V2.esc` / `textContent`. Audit **every** `innerHTML` template that interpolates item or `_meta` fields (including `edited_fields`, history entries, drawer values).

### 5.6 Toolbar

Save indicator (`Saved · rev N` / `Saving…` / `Save failed — retry`); **Export ▾** with format (Excel/Markdown/JSON), a view radio (Edited / Original AI), and an "Include removed items" checkbox; History panel; Reset all edits (confirm; archives, see §4.2). Stats bar recomputed from active items, plus a small "n edited · n added · n removed" line.

### 5.7 Removed items

Tab footer `Show removed (n)`. Bulk select/remove in Phase 2.

### 5.8 Special cases

- **Rate card:** `no_roles_stated` keeps describing the RFP. If the user has added roles, the notice text becomes "This RFP does not specify roles. The roles below were added manually." (an explicit change in `renderSkillPanel`); added rows carry `User-added`.
- **Ambiguity:** `resolved`/`clarified_by` are user-editable; manual resolution is visible via `edited_fields`.
- **`related_ids`:** keep; render references to removed items struck-through.

---

## 6. Enhance

### 6.1 Modes (one item each)

| mode | Purpose | May propose |
|---|---|---|
| `clarify_wording` | Clear, atomic, implementation-neutral rewording | title, description |
| `measurable` | Make testable only if the excerpt supports it; otherwise state what's missing | description, clarification_question |
| `clarification_question` | Draft the client question for an ambiguous item | clarification_question, ambiguity_flag |
| `custom` | Free-text `instruction` | title, description, clarification_question |

### 6.2 Guardrails (`core/prompts/enhance.py`, one shared fragment)

- Use only the item's own fields and `raw_excerpt`; no new numbers, dates, SLAs, roles, product names or obligations.
- If information is missing, return it as `needs_info` / a clarification question, never as invented text.
- Never change `priority`, `confidence`, `raw_excerpt`, IDs, source fields.
- Strict JSON: `{"proposed":{…},"rationale":"…","needs_info":[…]}`.
- Server post-validation: reject fields outside the mode's set; flag `unverified_numbers` when the proposal contains a digit-bearing token absent from the item's text **and** the user's `instruction`.

### 6.3 Flow and failures

1. Enhance → proposal (not persisted). 2. UI shows per-field before/after with editable "after", rationale, `needs_info`, and any `unverified_numbers` banner. 3. Accept → `…/enhance/accept` `{base_version, proposal}` → persisted like PATCH, `enhanced=true`, logged `op:"enhance"`. Reject → nothing saved.
- `call_llm_async` returning `[]` (or an unparsable object) → `502 {"error":"enhancement_failed"}`; UI shows a retry message.
- The per-job lock is taken only for the read before and the write on accept — not during the LLM call.
- `origin:"user"` items without `raw_excerpt` may only use `clarify_wording`.

### 6.4 Optional grounding

Persist `chunks.json` per job so enhancement can see neighbouring chunks. Not part of this change (decision O3).

### 6.5 Cost

One call per enhance, small `max_tokens`; one in-flight enhance per job in the UI.

---

## 7. Export

### 7.1 Common

Source = working copy unless `view=original`. Removed items excluded unless `include_removed=true` (then a `Status` column). Filename `<primary_file_stem>_requirements_<YYYYMMDD>.<ext>`, sent with an RFC 5987-encoded `Content-Disposition` (`filename*=UTF-8''…` plus ASCII fallback) so spaces and non-ASCII names work.

### 7.2 Excel (`app/exporters/xlsx.py`, openpyxl)

Sheets:

1. **Summary** — file name, job id, export timestamp, view, counts per sheet (active / user-added / edited / removed).
2. **Content sheets, grouped by bucket** (`app/exporters/classify.py`, *not* the Markdown classifier):
   - one sheet per spine category: Compliance, Ambiguity, Risk, Commercial Structure;
   - design-build: **Functional**, **Non Functional**, **Staffing Mandates** split by `type`;
   - every other fired skill: its own sheet (AMS additionally gets an `Artifact Type` column; optionally one sheet per artifact group).
3. **Change Log** — from `read_log`.

Columns per sheet: common (`ID, Title, Description, Priority, Confidence, Section, Page, Source Document, Origin, Edited, Edited Fields, AI-Enhanced, Status, Raw Excerpt, Clarification Question`) plus category-specific columns derived from the field spec (Commercial: pricing dimension, cost type, value, mandatory; AMS: SLA type, severity, target, penalty; Rate card: role, seniority, location, pricing model, rate unit, rate value). Header bold + frozen, autofilter, wrapped text, user-origin rows tinted, removed rows grey/strikethrough. Cells beginning `=`, `+`, `-`, `@` are written as literal text.

### 7.3 Markdown

Filter removed items from the dict **before** building the model, then call `render_markdown`. Add `✎ Edited` / `＋ Added` markers and an "Edited version — exported <date>" header line. The renderer's existing classifier stays as is; **baseline `result.md` must remain byte-identical** (regression check).

### 7.4 JSON

Working document as-is (with `_meta`, including baseline) by default for lossless handoff; `strip_meta=true` gives a clean schema-only dump.

### 7.5 "Save to disk"

Server disk: automatic via autosave. User's machine: via export download.

---

## 8. Build sequence (each step ships with its tests)

**Phase 0 — Foundations**
1. Confirm/add `logging_config.py`. UUID path-safety dependency on all `/jobs/{id}…` routes.
2. Persistence: `job.json` lifecycle, ordered writes in `_run_pipeline`, atomic writes with Windows retry, `load_from_disk` (§4.6).
3. JS extraction per §5.0 (inline `JOB_ID`, no IIFE, `RV` namespace, `V2.esc`, `V2.apiSend`).
4. `ResultRepository` (incl. `read_log`), `bootstrap_working_copy` at completion + lazy fallback, `GET /jobs/{id}` changes, `edit_state`.
5. Removed-item filtering in every render path + `showRemoved`.

**Phase 1 — Core edit + export (MVP)**
6. `field_specs` (+ consistency test) and `GET /fields`; `id_prefix` extraction from `ams.py` with unchanged-output test.
7. Add / PATCH / remove / restore / revert / reset(archive); validation; per-item versions; audit log.
8. Drawer, delegated row actions (incl. AMS tables), badges, Add buttons (rendered above early returns), save indicator, row-level replace, removed toggle.
9. Exporters (Excel by bucket, Markdown with regression check, JSON), toolbar Export menu options; `excerpt_edited`; rate-card notice wording.
10. History endpoint + panel.

**Phase 2 — Enhance + scale**
11. `bulk` (single save) + selection UI — prioritise: this is the real answer to 1,000+ item volume.
12. Duplicate warnings via `find_similar_to`.
13. Enhance prompt, orchestration, endpoints, diff UI.

**Phase 3 — Follow-ons:** chunk persistence for grounded enhance; named snapshots/diff; import of edited files (match on `uid`); real identity in `updated_by`; downstream steps consume the working copy.

---

## 9. Acceptance tests

Backend (pytest + pytest-asyncio + FastAPI TestClient, LLM mocked; add the test setup in Phase 0):

1. Completed job has `working.json` created at completion; every item has a unique `uid`; baseline files byte-identical afterwards; `GET` of a running job never creates a working copy.
2. Add into `skill/design-build` → `DB-U001`; again → `DB-U002`; remove `U001` then add → `DB-U003`. Add into a fired skill with no `SkillOutput` creates it.
3. PATCH → `edited`, `edited_fields`, baseline unchanged, log line written; revert restores baseline and clears flags; editing `raw_excerpt` sets `excerpt_edited`.
4. Stale `base_version` → `409` with current item; **editing item A does not cause a 409 when later editing item B**; unknown/non-editable field → `422`; bad enum → `422`.
5. Remove hides from default export and counts; restore returns it; `include_removed=true` includes it with `Status=removed`.
6. Restart simulation: new `JobStore.load_from_disk` → job `complete`, edited working copy returned; a `running` job → `failed`; a `complete` job with no `result.json` → `failed`.
7. Enhance: number not in excerpt/instruction → `unverified_numbers`; proposal touching `priority` → rejected; LLM `[]` → `502`; nothing persisted until accept; lock not held during LLM call.
8. Excel: opens in openpyxl; Summary counts match; **AMS, rate-card and design-build items appear on their own sheets, not all under Functional**; `=SUM(A1)` stored as text; Change Log row count equals mutating operations.
9. Markdown: markers present, removed omitted; baseline `result.md` regression is byte-identical; `_classify_items` extraction (if any) leaves `result.md` unchanged.
10. Path safety: `job_id="../x"` → `4xx` on **every** `/jobs/{id}…` route, existing ones included.
11. Reset archives `working.json` (file exists afterwards) and logs the reset.
12. Duplicate check: adding an item to a 2,000-item bucket completes quickly (single-item comparison; assert it never calls the all-pairs function).
13. Bulk remove of 500 uids → exactly one file write and one revision bump.
14. `ams.py` ID output unchanged after prefix extraction; spine field-spec consistency test passes.

Frontend (manual checklist, or Playwright if you add it — decide up front):

15. Page loads after JS extraction; `JOB_ID` resolves; all tabs render as before.
16. Edit/remove works in AMS tables after re-render (delegation); only the changed row updates and scroll position is kept.
17. Add works on every tab type, **including an empty tab and a fired skill with no items**.
18. Removing items updates stats immediately; "Show removed (n)" reveals/restores them.
19. Reload and server restart preserve all edits.
20. `<img src=x onerror=alert(1)>` as an item field renders as text in rows, drawer, history and Excel.
21. Rate-card notice reads "roles below were added manually" after a user adds a role.
22. Export menu: format, view (edited/original) and include-removed options each change the download as expected; filenames with spaces/non-ASCII download correctly.

---

## 10. Open decisions (recommended default in bold)

- **O1 — Save model:** **autosave per operation** vs. explicit Save button.
- **O2 — Delete:** **soft delete only** vs. also permanent purge.
- **O3 — Enhance grounding:** **excerpt-only for now** vs. persisting chunks (§6.4).
- **O4 — Rate-card flag:** **keep `no_roles_stated` as RFP description and label manual roles User-added** vs. flipping the flag.
- **O5 — Cross-bucket moves:** **not in v1** (Functional ↔ Non Functional via `type` is supported).
- **O6 — Excel for AMS:** **one AMS sheet with an `Artifact Type` column** vs. one sheet per artifact group (SLA matrix, penalties, transition, …).
- **O7 — Frontend tests:** **manual checklist** vs. add Playwright.
