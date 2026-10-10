# RFP Analyzer V2 — Requirement Editing, Enhancement, Save & Export

Spec for a coding agent. Written against the V2 codebase as uploaded (`V2 - Copy.zip`).

## 0. Goal

After an analysis completes, a user can, on the Review page:

1. **Add** a requirement the tool missed.
2. **Edit** any field of a generated requirement.
3. **Remove** a requirement (reversibly).
4. **Enhance** a requirement with AI help (reword, sharpen, add a clarification question), always as a proposal the user accepts or rejects.
5. **Save** all of this to disk so it survives page reloads and server restarts.
6. **Export** the edited result as Excel, Markdown or JSON.

Non-goals for this change: multi-user permissions/login, re-running extraction, re-running the synthesizer/dedup after edits, import of externally edited files (see Phase 3).

Guiding principle (consistent with the tool's extraction-only stance): the **AI baseline is never overwritten**. Human edits live in a separate working copy so the original extraction stays auditable and any item can be reverted.

---

## 1. Findings from the current code (what the design must respect)

| # | Finding | Consequence |
|---|---|---|
| F1 | `JobStore` is a process-local dict. `result.json` / `result.md` are written **once** after the pipeline completes (`analysis.py::_run_pipeline`). Nothing reloads jobs from `outputs/` on startup. | After a restart, `/review/{id}` 404s even though files exist. Edits must persist to disk **and** jobs must be reloadable. |
| F2 | `get_job` returns `output_files` as **server filesystem paths**; the only content endpoint is `/jobs/{id}/markdown`. There is no JSON download and no Excel anywhere. | Need proper download endpoints with `Content-Disposition`. `openpyxl` is already a dependency, so Excel needs no new package. |
| F3 | Items are untyped `dict`s inside `SynthesizedOutput.skill_outputs[].items` and `SynthesizedOutput.spine.{compliance,ambiguity,risk,commercial_structure}`. IDs are system-assigned **positional counters** per module (`DB-001`, `CR-001`, `RSK-001`, `CS-001`, `RR-001`, `AMS-L1-001`, `AMS-SLA-001`, packaged `{SLUG}-001`, …). | There is no stable unique key. Add a `uid` per item. Never renumber display IDs after delete (would silently change references such as `related_ids`). |
| F4 | `SynthesizedOutput` items are `dict[str, Any]`, so extra keys pass Pydantic validation untouched. | The working copy can carry per-item metadata inline (`_meta`) with **no change to the Pydantic models**. |
| F5 | Field definitions for skills live in `skills/<slug>/schema.yaml`. Spine agents have no yaml; each has an `allowed` field whitelist in `_sanitise` (`spine/compliance.py`, `ambiguity.py`, `risk.py`, `commercial_structure.py`). | The edit form can be **generated from schema.yaml** for skills (keeps the "add a skill folder, no code change" promise). For spine, expose the whitelists as one shared field-spec module (see §4.4). |
| F6 | `review.html` is read-only; ~500 lines of JS are inline in the template. `renderAMSArtifacts` builds rows with `makeSkillRow(i,'ams').outerHTML`. | `outerHTML` **drops event listeners**. Row actions must use event delegation on `data-uid`, not per-element handlers. Extract the inline JS to static files first (Phase 0). |
| F7 | `renderStats` counts all items. `markdown_renderer.render_markdown(output, primary_file_path)` is a pure function over `SynthesizedOutput`. | Stats must count only active (non-removed) items. The Markdown renderer can be reused for export if it skips removed items and shows origin markers. |
| F8 | `core/llm_client.call_llm_async(system_prompt, user_content, …) -> list[dict]` (a single JSON object is wrapped into a list). `core/prompts/` holds shared prompt fragments. | Enhancement reuses `call_llm_async`; prompt goes in `core/prompts/enhance.py`. |
| F9 | Only the `SynthesizedOutput` is stored on the job; document chunks are not persisted with the result. | Enhancement can ground only on the item's own `raw_excerpt` unless chunks are persisted (optional, §6.4). |
| F10 | `app/main.py` imports `from logging_config import configure_logging`, but **`logging_config.py` is not in the zip**. | App will fail to start from this archive. Confirm the file exists in the real repo, or add it (Phase 0). |
| F11 | Minor: `schema.yaml` for design-build documents IDs as `FR-001/NFR-012`, but `design_build.py` assigns `DB-NNN`. The Markdown renderer's `NFR-` prefix fallback therefore never matches. | Harmless today (renderer keys off `type`), but don't rely on prefixes when classifying user-added items; always require `type`. |

---

## 2. Design decisions

**D1. Baseline + working copy.** Per job directory:

```
outputs/{job_id}/
  result.json            # AI baseline — immutable after first write
  result.md              # AI baseline markdown — immutable
  job.json               # NEW: job metadata sidecar (files, roles, status, created_at) so jobs reload after restart
  working.json           # NEW: edited SynthesizedOutput (same shape as result.json + per-item _meta + top-level _edit)
  edits.log.jsonl        # NEW: append-only audit log, one JSON line per operation
  exports/               # NEW: generated export files (optional cache)
```

`GET /jobs/{id}` returns `working.json` when it exists, otherwise the baseline. The UI therefore keeps reading `data.result` as today.

**D2. Persist on every operation (autosave).** Each add/edit/remove/accept-enhancement is written to `working.json` immediately (atomic write: temp file + `os.replace`). The UI shows "Saved · rev N". This satisfies "save back to disk" with no extra click and no lost work. Export is a separate, explicit action. (Open decision O1 if an explicit Save button is preferred.)

**D3. Soft delete.** "Remove" sets `_meta.status = "removed"`; the item stays in the file, hidden by default, restorable, excluded from exports by default. Hard purge is not offered in v1.

**D4. Enhance is propose-then-accept.** The LLM never writes to the item directly. It returns a proposal; the user sees a before/after diff and chooses Accept / Edit / Reject.

**D5. Provenance stays honest.** User-added and user-edited content is labelled as such everywhere (UI badge, exports). User-added items are `origin: "user"` and are exempt from excerpt-traceability audits; edited `raw_excerpt` values set `excerpt_edited: true` so the audit can skip or flag them.

**D6. Repository abstraction.** All reads/writes go through a small `ResultRepository` interface (`load(job_id)`, `save(job_id, doc, expected_revision)`, `append_log(job_id, entry)`), with a file-based implementation now. This keeps the planned PostgreSQL migration a drop-in replacement.

---

## 3. Data model

### 3.1 Per-item metadata (inline, key `_meta`)

Added to **every** item in `working.json` when the working copy is first created:

```json
{
  "id": "DB-014",                 // existing display ID, unchanged
  "title": "...", "description": "...",   // existing fields
  "_meta": {
    "uid": "itm_7f3a9c21",        // stable, never reused, assigned at working-copy creation
    "origin": "ai",               // "ai" | "user"
    "status": "active",           // "active" | "removed"
    "edited": false,
    "edited_fields": [],          // e.g. ["description","priority"]
    "excerpt_edited": false,
    "enhanced": false,            // true once an AI enhancement was accepted
    "created_at": "2026-10-08T10:15:00Z",   // user-added only
    "updated_at": "2026-10-08T10:20:11Z",
    "updated_by": "local",        // placeholder until login exists
    "baseline": { ...copy of the original item without _meta... }   // ai items only; used for revert/diff
  }
}
```

`baseline` is stored only for AI items (it is the "reset this item" and "show diff" source). For user-added items it is absent.

### 3.2 Top-level block (key `_edit` on the result document)

```json
"_edit": { "revision": 12, "created_at": "...", "updated_at": "...", "counters": { "DB": 3, "CR": 0, "AMS-SLA": 1 } }
```

`counters` generates IDs for user-added items (§3.3).

### 3.3 IDs for user-added items

Use the module's existing prefix plus a **`U` namespace** so they can never collide with system counters: `DB-U001`, `CR-U002`, `RSK-U001`, `CS-U001`, `RR-U001`, `AMS-SLA-U001`, packaged `SAP-U001`. Prefix is derived from the bucket (and, for AMS, `artifact_type`) using the same mapping the extractors use; put that mapping in one helper (`core/edit/id_prefix.py`) rather than duplicating it. Display IDs are **never renumbered** on delete or add.

### 3.4 Buckets

An item lives in exactly one bucket:

- `{"kind": "skill", "key": "<skill_slug>"}` → `skill_outputs[slug].items`
- `{"kind": "spine", "key": "compliance|ambiguity|risk|commercial_structure"}` → `spine.<key>`

---

## 4. Backend

### 4.1 New module layout

```
app/routers/edits.py          # all new endpoints below
app/routers/exports.py        # export endpoints
core/edit/__init__.py
core/edit/repository.py       # ResultRepository + FileResultRepository
core/edit/service.py          # add/update/remove/restore/revert/bulk logic, validation, uid/ID generation
core/edit/field_specs.py      # editable field specs (skills from schema.yaml, spine from static dict)
core/edit/enhance.py          # enhancement orchestration
core/edit/id_prefix.py        # bucket → ID prefix mapping
core/prompts/enhance.py       # enhancement prompt fragments
app/exporters/xlsx.py         # openpyxl exporter
app/exporters/__init__.py
```

### 4.2 Endpoints

All under the existing router style; `job_id` must be validated as a UUID **before** it is used to build a path.

| Method & path | Purpose |
|---|---|
| `GET /jobs/{id}` | **Changed:** returns working copy if present (else baseline) plus `edit_state: {revision, has_edits, counts:{active,removed,user_added,edited}}`. `?view=original` returns the baseline. |
| `POST /jobs/{id}/items` | Add item. Body `{bucket, fields}`. Returns the new item. |
| `PATCH /jobs/{id}/items/{uid}` | Edit fields. Body `{base_revision, fields}`. Returns updated item + new revision. |
| `DELETE /jobs/{id}/items/{uid}` | Soft-remove. |
| `POST /jobs/{id}/items/{uid}/restore` | Undo remove. |
| `POST /jobs/{id}/items/{uid}/revert` | Reset an AI item to its baseline (clears `edited`, `enhanced`). 400 for user-added items. |
| `POST /jobs/{id}/items/{uid}/enhance` | Body `{mode, instruction?}`. Returns a **proposal** (not persisted). See §6. |
| `POST /jobs/{id}/items/{uid}/enhance/accept` | Body `{base_revision, proposal}` — applies the (possibly user-modified) proposal via the same path as PATCH, sets `enhanced=true`. |
| `POST /jobs/{id}/items/bulk` *(Phase 2)* | Body `{uids[], op: "remove"|"restore"|"set_field", field?, value?}`. |
| `POST /jobs/{id}/reset` | Discard `working.json` (requires `?confirm=true`); log entry kept. |
| `GET /jobs/{id}/history` | Reads `edits.log.jsonl`, newest first, paginated. |
| `GET /jobs/{id}/fields?kind=&key=` | Editable field spec for a bucket (drives the form). Alias: `GET /skills/{slug}/schema` for skills. |
| `GET /jobs/{id}/export?format=xlsx\|md\|json&include_removed=false&scope=all\|<bucket key>` | Download. Streams with `Content-Disposition: attachment; filename="<rfp-name>_requirements_<yyyymmdd>.<ext>"`. |
| `GET /jobs/{id}/export?view=original` | Same but from the AI baseline. |

Status codes: `404` unknown job/uid; `409` revision mismatch (client edited a stale copy — response includes the current item so the UI can show a merge prompt); `422` validation failure with per-field messages; `400` job not `complete`.

### 4.3 Concurrency and atomicity

- `working.json` carries `_edit.revision` (int, +1 per successful write). Mutating endpoints take `base_revision` (PATCH/accept) and return `409` on mismatch. Add/remove/restore don't need it (they don't overwrite another user's field values) but still bump the revision.
- One `asyncio.Lock` per job id around read-modify-write. Atomic file replace on save.
- Append a line to `edits.log.jsonl` **after** a successful save: `{ts, op, uid, id, bucket, fields_changed:{field:{from,to}}, actor, revision}`.
- Response payloads for item operations return only the affected item(s) + new revision; the client updates local state (no full refetch — result sets can be >1,000 items).

### 4.4 Field specs (`core/edit/field_specs.py`)

Single function `get_field_spec(kind, key) -> list[FieldSpec]` returning `{name, label, type, required, enum_values, editable, help}`.

- **Skills:** parse `skills/<slug>/schema.yaml` (the registry already loads it; reuse its loader). `editable=false` for system fields: `id`, `source_document`, `document_role`, `source_type`, `clarified_by`. `source_type`/`source_document`/`document_role` stay read-only for AI items; for user items they default to `text` / `""` / `primary` and are hidden.
- **Spine:** static dict built from the existing `_sanitise` whitelists (compliance/risk/ambiguity: `title, description, source_section, priority, confidence, ambiguity_flag, clarification_question, raw_excerpt, archetype_tag`; ambiguity also `resolved, clarified_by`; commercial_structure: `category, cost_type, pricing_dimension, quantified_value, mandatory, raw_excerpt, source_section, archetype_tag`). Have the spine agents import their whitelist from this module so there is one source of truth, not two. (Small refactor; keep behaviour identical.)
- **AMS:** `artifact_type` is a required, editable enum and **determines which AMS section the item appears in**; changing it re-buckets within the AMS view. Show a help text saying so.

### 4.5 Validation (server-side, authoritative)

- Required fields per spec; enum membership; type coercion (`confidence` float clamped 0–1, `min_headcount` int ≥ 0, booleans).
- Strip unknown keys silently; reject writes to non-editable fields (`422`).
- `title`: soft limit of 10 words (warning in response, not an error — mirrors the schema guidance).
- `raw_excerpt` is required by schema for AI items, but **optional for `origin="user"`**. Editing it on an AI item sets `_meta.excerpt_edited=true`.
- **Duplicate warning on add/edit:** run `synthesizer.dedup.find_duplicate_candidates` against active items in the same bucket; if similarity ≥ 0.90 return `warnings:[{type:"possible_duplicate", uid, id, similarity}]` alongside the successful write (non-blocking). Reuses existing code; no LLM call.
- HTML is never trusted: store raw text; the UI escapes on render (see §5.5).

### 4.6 Persistence and reload (prerequisite — Phase 0)

1. On job creation write `outputs/{job_id}/job.json` (file names, roles, primary file, created_at, status). Update on completion/failure.
2. On app startup, `JobStore.load_from_disk(OUTPUT_DIR)` scans `outputs/*/job.json` (and falls back to `result.json` for older folders without a sidecar) and rehydrates `AnalysisJob` entries as `complete`/`failed`. Jobs that were `running` at shutdown are marked `failed: "interrupted by restart"`.
3. Make `result.json`, `result.md` writes atomic.

---

## 5. Frontend (Review page)

### 5.0 Prerequisite refactor (no behaviour change)

Move the inline script out of `review.html` into `static/js/review.js`; put new code in `static/js/review-edit.js` and edit styles in `static/css/app.css` (match the existing "ledger" look: bordered rows, ledger-green accent, muted tags — no Carbon). Keep `V2.*` helpers in `app.js`; add `apiSend(method, url, body)` (JSON POST/PATCH/DELETE with error parsing) next to `apiGet`.

### 5.1 State

Keep `_result` as the single in-memory document (the working copy). Add `_revision`, `_showRemoved` (default false), `_selection` (Set of uids, Phase 2). A helper `findItemByUid(uid)` walks both buckets. Items with `_meta.status==='removed'` are filtered out of every render path **including** `renderStats` and `renderAMSArtifacts` (grep for every `.items` / `spine[...]` read).

### 5.2 Row actions (event delegation)

Each rendered row (`makeSkillRow`, `makeSpineRow`, and AMS table rows) gets `data-uid` and a small action cluster: **Edit · Enhance · Remove** (and **Restore · Revert** when applicable). A single click handler on `#tab-panels` dispatches on `closest('[data-action]')`. This is required because AMS rows are re-inserted via `outerHTML` (F6). AMS tables (apps, SLA matrix) need a trailing actions cell.

Badges on rows: `User-added` (origin=user), `Edited` (edited=true; tooltip lists `edited_fields`), `AI-enhanced`, and `Removed` (when shown). Removed rows are dimmed with strikethrough title.

### 5.3 Add

A `＋ Add requirement` button at the top of every tab (skill and spine). For the AMS tab, one button per artifact section ("＋ Add SLA", "＋ Add penalty", …) which presets `artifact_type`. Opens the edit drawer in "create" mode with the bucket fixed.

### 5.4 Edit drawer

Right-side drawer, fields rendered from `GET /jobs/{id}/fields`:

- `string` → input (title) / textarea (description, raw_excerpt, clarification_question)
- `enum` → select; `boolean` → checkbox; `float` (confidence) → 0–1 slider with numeric input; `integer` → number
- Read-only system fields shown as muted text (ID, source document, role)
- Footer: **Save**, **Cancel**; for AI items also **Revert to AI version** and a "Show original" toggle that displays the baseline value under each changed field
- Inline field errors from `422`; non-blocking duplicate warning banner
- On `409`: show "This item was changed elsewhere", display the server version, offer *Overwrite* / *Discard mine*

### 5.5 Safety

All item text rendered via the existing `esc()` (or `textContent`). Because users can now enter arbitrary text, **audit every `innerHTML` template that interpolates item fields** and ensure each interpolation goes through `esc()` (the AMS tables and `makeSkillRow` currently do; keep it that way for new fields such as `_meta` badges and `edited_fields`).

### 5.6 Toolbar (page header)

- Save indicator: `Saved · rev 12` / `Saving…` / `Save failed — retry` (red, with retry).
- **Export ▾**: Excel (.xlsx) · Markdown (.md) · JSON (.json), plus a checkbox "Include removed items" and a radio "Edited version / Original AI version". Triggers `GET /jobs/{id}/export?...` as a normal browser download.
- **History** (opens a side panel listing the audit log) and **Reset all edits** (confirm dialog; destructive).
- Stats bar recomputed from active items; add a small "n edited · n added · n removed" line (this is edit-state info, not requirement statistics).

### 5.7 Removed-items affordance

Footer of each tab: `Show removed (n)` toggle. Bulk select + bulk remove arrives in Phase 2 (important for the large-volume problem: remove a whole theme/section at once).

### 5.8 Special cases

- **Rate-card `no_roles_stated`:** leave the flag as-is (it describes what the *RFP* says). If the user adds roles, the notice text becomes "This RFP does not specify roles. The roles below were added manually." and those rows carry the `User-added` badge. (Decision O4.)
- **Ambiguity items:** `resolved`/`clarified_by` are editable by the user (manual resolution), but set `edited_fields` so it is clear the resolution is human, not QnA-derived.
- **`related_ids`:** keep as-is; when an item referenced there is removed, render the reference struck-through rather than dropping it.

---

## 6. Enhance (AI-assisted)

### 6.1 Modes (all operate on **one item**)

| mode | What it does | Fields it may propose |
|---|---|---|
| `clarify_wording` | Rewrite `title`/`description` to be clear, atomic, implementation-neutral | title, description |
| `measurable` | Make vague wording testable *only if the excerpt supports it*; otherwise say what is missing | description, clarification_question |
| `clarification_question` | Draft the question to send to the client for an ambiguous item | clarification_question, ambiguity_flag |
| `custom` | Free-text `instruction` from the user (e.g. "tailor for AMS delivery") | title, description, clarification_question |

### 6.2 Hard guardrails (put in `core/prompts/enhance.py`, one shared fragment, like `QNA_RECONCILIATION_FRAGMENT`)

- Use **only** the item's own fields and its `raw_excerpt`. Do **not** introduce numbers, dates, SLAs, roles, product names or obligations that are not in them.
- If the improvement would need information not present, return it as a `clarification_question`, not as invented text.
- Never change `priority`, `confidence`, `raw_excerpt`, IDs or source fields.
- Return strict JSON: `{"proposed": {field: value,…}, "rationale": "<one sentence>", "needs_info": ["…"]}`.
- Post-validate server-side: reject any proposed field outside the mode's allowed set; reject if the proposal introduces a digit-bearing token not present in the item's original text (cheap anti-fabrication check; returns the proposal flagged `unverified_numbers` rather than silently dropping).

### 6.3 Flow

1. User clicks Enhance → chooses mode (and optional instruction) → `POST …/enhance` → proposal.
2. UI shows a per-field before/after diff with editable "after" text, `rationale`, and any `needs_info` list.
3. Accept → `POST …/enhance/accept` with the (possibly edited) values → written like a PATCH, `_meta.enhanced=true`, logged with `op:"enhance"` and the mode. Reject → nothing persisted.
4. Items with `origin="user"` and no `raw_excerpt` may only use `clarify_wording` (nothing to ground the other modes on).

### 6.4 Optional (decision O3): grounding on source text

Because chunks are not persisted (F9), enhancement can only see the excerpt. If SMEs need enhancement grounded in surrounding RFP text, persist `chunks.json` per job at the end of the vision pass (`outputs/{job_id}/chunks.json`, ~text only) and pass the 1–2 neighbouring chunks (matched by `source_document` + `source_section`) as read-only context. Not required for Phase 2.

### 6.5 Cost/latency

One LLM call per enhance, `max_tokens` small (≈800), no streaming needed. Rate-limit to 1 in-flight enhance per job per user session in the UI (disable the button while pending).

---

## 7. Export

### 7.1 Common rules

- Source = `working.json` if present, else baseline; `?view=original` forces baseline.
- Default excludes `removed` items; `include_removed=true` includes them with a `Status` column.
- Filename: `<primary_file_stem>_requirements_<YYYYMMDD>.<ext>`.
- Exports are generated on request (stream from memory); optional cache in `exports/`.

### 7.2 Excel (`app/exporters/xlsx.py`, openpyxl)

Workbook sheets:

1. **Summary** — RFP file name, job id, export timestamp, view (edited/original), counts per sheet (active / user-added / edited / removed). Simple and factual; no analysis.
2. **One sheet per category**, in this order: Functional, Non Functional, Compliance, Ambiguity, Risk, Commercial Structure — then one sheet per fired skill whose items don't fit those (e.g. AMS, Rate Card, packaged). Reuse the same classification rules as `markdown_renderer._classify_items`, extracted into a shared function so Markdown and Excel can't drift.
3. **Change Log** — from `edits.log.jsonl`: timestamp, op, item ID, field, from, to, actor.

Per-category columns (union of fields present in that category, stable order): `ID, Title, Description, Priority, Confidence, Section, Page, Source Document, Origin (AI/User), Edited (Y/N), Edited Fields, AI-Enhanced (Y/N), Status, Raw Excerpt, Clarification Question`, plus category-specific columns (e.g. Commercial: `Pricing Dimension, Cost Type, Value, Mandatory`; AMS SLA: `SLA Type, Severity, Target Value, Penalty`; Rate Card: `Role, Seniority, Location, Pricing Model, Rate Unit, Rate Value`). Derive category-specific columns from the field spec (§4.4), not hard-coded per sheet.

Formatting: header row bold + frozen, autofilter, sensible column widths, wrap text on Description/Excerpt, conditional fill for `User`-origin rows and strikethrough/grey for `Removed` rows. Cells beginning with `=`, `+`, `-`, `@` must be written as literal text (prevent formula injection from user-entered content).

### 7.3 Markdown

Call `render_markdown(result, primary_file_path)` on the filtered document. Changes to the renderer: skip removed items (unless included), append ` ✎ Edited` / ` ＋ Added` after the heading, and add a short header line "Edited version — exported <date>". Keep the existing output otherwise identical so baseline `result.md` is unchanged.

### 7.4 JSON

The working document as-is (including `_meta` and `_edit`) — this is the lossless format and the one downstream steps should consume. Provide `?strip_meta=true` for a clean schema-only dump.

### 7.5 "Save to disk" clarified

- **Server disk:** automatic via autosave (`outputs/{job_id}/working.json`) and optional cached exports.
- **User's machine:** via the browser download of the export.

---

## 8. Build sequence

**Phase 0 — Foundations (no user-visible feature)**
1. Confirm/add `logging_config.py` (F10).
2. `job.json` sidecar + `JobStore.load_from_disk` + atomic writes (§4.6).
3. Extract inline JS from `review.html` → `static/js/review.js`; add `apiSend`.
4. `ResultRepository` + working-copy creation (uid assignment, `_meta`, `_edit`).
5. `GET /jobs/{id}` returns working copy and `edit_state`; filter removed items in every render path and stats.

**Phase 1 — Core edit + export (MVP)**
6. `field_specs` + `GET …/fields`; spine whitelist refactor.
7. Add / PATCH / soft-remove / restore / revert endpoints, validation, audit log, revision handling.
8. Drawer UI, row actions with event delegation (including AMS tables), badges, add buttons, save indicator, removed toggle.
9. Exports: JSON, Markdown (renderer tweaks), Excel (shared classification function); toolbar Export menu.

**Phase 2 — Enhance + scale**
10. `core/prompts/enhance.py`, enhance/accept endpoints, diff UI, anti-fabrication post-check.
11. Duplicate warnings on add/edit.
12. Bulk select + bulk remove/restore/set-priority; History panel.

**Phase 3 — Follow-ons (not in this change)**
- Optional chunk persistence for source-grounded enhancement (§6.4).
- Snapshots/named versions and diff between versions.
- Import an edited Excel/JSON back in (match on `uid`).
- Real user identity in `updated_by` once login exists; per-field permissions if skill-owner governance is introduced.
- Feed the edited working copy (not the baseline) into downstream steps (e.g. offering mapping) as the source of truth.

---

## 9. Acceptance tests

Backend (pytest, FastAPI TestClient, LLM mocked):

1. Completed job → first `GET /jobs/{id}` creates `working.json`; every item has a unique `_meta.uid`; baseline files byte-identical afterwards.
2. `POST /items` into `skill/design-build` with valid fields → item gets `DB-U001`, `origin="user"`; second add → `DB-U002`; removing `DB-U001` then adding again → `DB-U003` (no reuse).
3. `PATCH` changes `description` → `edited=true`, `edited_fields=["description"]`, baseline copy unchanged, log line written; `revert` restores the baseline value and clears flags.
4. `PATCH` with stale `base_revision` → `409` with current item; fields not in the spec → `422`; `priority:"urgent"` → `422`.
5. `DELETE` → item hidden from default export and `GET` stats; `restore` brings it back; `include_removed=true` export contains it with `Status=removed`.
6. Restart simulation: create `JobStore`, call `load_from_disk` → job is `complete`, `GET /jobs/{id}` returns the edited working copy.
7. Enhance: mocked LLM returns a proposal containing a number not present in the excerpt → response flagged `unverified_numbers`; proposal touching `priority` → rejected; nothing persisted until `accept`.
8. Excel export: opens with openpyxl; sheets present; Summary counts match; a description starting with `=SUM(A1)` is stored as text; Change Log rows equal the number of mutating operations.
9. Markdown export of an edited job contains `✎ Edited` / `＋ Added` markers and omits removed items; baseline `result.md` unchanged.
10. Path safety: `job_id="../x"` → `400/404`, never touches disk outside `outputs/`.

Frontend (manual checklist, or Playwright if available):

11. Edit in the AMS tab (table rows) works after the tab is re-rendered (event delegation; no dead buttons).
12. Add on every tab type (skill, each spine tab, each AMS section) produces a row in the right place without a page reload.
13. Removing items updates stat counts immediately; "Show removed (n)" reveals and restores them.
14. Reload the page and restart the server: all edits present.
15. Item text containing `<img src=x onerror=alert(1)>` renders as text everywhere (table cells, drawer, history).

---

## 10. Open decisions (recommended default in bold)

- **O1 — Save model:** **autosave per operation** (spec'd above) vs. a batch "Save" button. Autosave is simpler and safer; say if you want explicit save.
- **O2 — Delete:** **soft delete only** vs. also offering permanent purge.
- **O3 — Enhance grounding:** **excerpt-only for now** vs. persisting chunks so enhancement can see surrounding RFP text (§6.4).
- **O4 — Rate-card flag:** **keep `no_roles_stated` describing the RFP and label manual roles as User-added** vs. flipping the flag when a user adds roles.
- **O5 — Cross-bucket moves:** **not supported in v1** (e.g. turning a Risk into a Compliance item; Functional ↔ Non Functional is just the `type` field and is supported). Add later if SMEs ask.
