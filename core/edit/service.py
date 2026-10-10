"""
core/edit/service.py
---------------------
Domain services for bootstrap, state computation, item lookup, and mutations.

Key design decisions (spec §3.1, §3.2, §4.2–§4.5, D1, D6, D7, C4, C6, C9,
C11, amendments 7, 13–14, r2-1, r2-5, r3-1, r3-4):

- ``bootstrap_working_copy`` assigns ``itm_<12hex>`` uids to every item.
  * New jobs: 12 random hex chars (collision-checked per job).
  * Legacy jobs: sha256(job_id + bucket + index + display_id)[:12].
- Baselines are stored in a **separate top-level ``_baselines`` map**
  (amendment 13) — GET drops the key before returning to clients.
- ``_meta`` defaults are added to each item; ``_edit`` block (revision=0) added
  at doc level.
- ``compute_edit_state`` counts active/removed/user_added/edited items.
- ``find_item_by_uid`` (module-level) walks the full document.
- ``add_item``, ``update_item``, ``remove_item``, ``restore_item``,
  ``revert_item``, ``reset_working_copy`` implement CRUD mutations.
- All mutations bump ``_meta.version`` on the affected item and
  ``_edit.revision`` on the document.
- ``reset_working_copy``: if working.json exists archives it; if not (legacy job)
  returns in-memory bootstrap without writing (r3-4).
"""

from __future__ import annotations

import copy
import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

import structlog

from core.edit.field_specs import get_field_spec
from core.edit.id_prefix import get_prefix
from synthesizer.dedup import _item_fingerprint, find_similar_to

log = structlog.get_logger(__name__)

# ── UID generation ────────────────────────────────────────────────────────────


def _random_uid(used: set[str]) -> str:
    """Return a collision-free ``itm_<12hex>`` uid using os.urandom."""
    for _ in range(100):
        candidate = "itm_" + os.urandom(6).hex()  # 12 hex chars from 6 bytes
        if candidate not in used:
            return candidate
    raise RuntimeError("Could not generate a unique uid after 100 attempts")  # pragma: no cover


def _deterministic_uid(job_id: str, bucket: str, index: int, display_id: str) -> str:
    """Return a deterministic ``itm_<12hex>`` uid for legacy jobs.

    Uses sha256(job_id + bucket + str(index) + display_id), truncated to 12 hex.
    """
    raw = f"{job_id}:{bucket}:{index}:{display_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return "itm_" + digest[:12]


# ── Bootstrap ─────────────────────────────────────────────────────────────────


def bootstrap_working_copy(baseline_dict: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Create a working-copy document from a baseline ``result.json`` dict.

    Algorithm:
    1. Assign ``itm_<12hex>`` uids to every item in every bucket.
       - If ``job_id`` starts with ``"legacy:"`` the deterministic path is used;
         otherwise random uids are generated.
       - Collision-checked within the document.
    2. Add ``_meta`` defaults to each item (origin, status, version, uid).
    3. Store a deep copy of each original item in ``_baselines[uid]``.
    4. Add top-level ``_edit`` block (revision=0).

    The returned document has the shape::

        {
          "skill_outputs": [...],   # items have _meta.uid set
          "spine": {...},           # items have _meta.uid set
          "fired_skills": [...],
          "metadata": {...},
          "_baselines": {uid: original_item, ...},
          "_edit": {"revision": 0, "has_edits": false, "counters": {}},
        }
    """
    doc = copy.deepcopy(baseline_dict)
    use_deterministic = job_id.startswith("legacy:")
    real_job_id = job_id.removeprefix("legacy:")

    used_uids: set[str] = set()
    baselines: dict[str, dict] = {}

    def _assign_uid(item: dict, bucket: str, index: int) -> str:
        display_id = str(item.get("id", ""))
        if use_deterministic:
            uid = _deterministic_uid(real_job_id, bucket, index, display_id)
        else:
            uid = _random_uid(used_uids)
        used_uids.add(uid)
        return uid

    def _stamp(item: dict, uid: str) -> None:
        item["_meta"] = {
            "uid": uid,
            "origin": "ai",
            "status": "active",
            "version": 1,
            "edited": False,
            "edited_fields": [],
            "excerpt_edited": False,
            "enhanced": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": "local",
        }

    # Walk skill_outputs
    for so in doc.get("skill_outputs", []):
        bucket = so.get("skill_slug", "unknown")
        for idx, item in enumerate(so.get("items", [])):
            uid = _assign_uid(item, bucket, idx)
            baselines[uid] = copy.deepcopy(item)  # snapshot before _meta is added
            _stamp(item, uid)

    # Walk spine categories
    spine = doc.get("spine", {})
    for spine_key in ["compliance", "risk", "ambiguity", "commercial_structure"]:
        for idx, item in enumerate(spine.get(spine_key, [])):
            uid = _assign_uid(item, f"spine_{spine_key}", idx)
            baselines[uid] = copy.deepcopy(item)
            _stamp(item, uid)

    doc["_baselines"] = baselines
    doc["_edit"] = {
        "revision": 0,
        "has_edits": False,
        "counters": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    return doc


# ── Edit state ────────────────────────────────────────────────────────────────


def compute_edit_state(doc: dict[str, Any]) -> dict[str, Any]:
    """Compute summary counts from a working-copy document.

    Returns::

        {
          "active": int,
          "removed": int,
          "user_added": int,
          "edited": int,
          "has_edits": bool,
          "revision": int,
        }
    """
    active = removed = user_added = edited = 0

    def _process_items(items: list[dict]) -> None:
        nonlocal active, removed, user_added, edited
        for item in items:
            meta = item.get("_meta", {})
            status = meta.get("status", "active")
            origin = meta.get("origin", "ai")
            is_edited = meta.get("edited", False)
            version = meta.get("version", 1)

            if status == "removed":
                removed += 1
            else:
                active += 1

            if origin == "user":
                user_added += 1

            if is_edited or version > 1:
                edited += 1

    for so in doc.get("skill_outputs", []):
        _process_items(so.get("items", []))

    spine = doc.get("spine", {})
    for spine_key in ["compliance", "risk", "ambiguity", "commercial_structure"]:
        _process_items(spine.get(spine_key, []))

    edit_block = doc.get("_edit", {})
    revision = edit_block.get("revision", 0)
    has_edits = edited > 0 or user_added > 0 or removed > 0

    return {
        "active": active,
        "removed": removed,
        "user_added": user_added,
        "edited": edited,
        "has_edits": has_edits,
        "revision": revision,
    }


# ── Item lookup ───────────────────────────────────────────────────────────────


def find_item_by_uid(
    doc: dict[str, Any], uid: str
) -> Optional[Tuple[dict[str, Any], str]]:
    """Find an item in a working-copy document by its ``_meta.uid``.

    Returns ``(item, bucket)`` or ``None`` if not found.
    *bucket* is the skill slug or spine category key.
    """
    for so in doc.get("skill_outputs", []):
        bucket = so.get("skill_slug", "unknown")
        for item in so.get("items", []):
            if item.get("_meta", {}).get("uid") == uid:
                return item, bucket

    spine = doc.get("spine", {})
    for spine_key in ["compliance", "risk", "ambiguity", "commercial_structure"]:
        for item in spine.get(spine_key, []):
            if item.get("_meta", {}).get("uid") == uid:
                return item, spine_key

    return None


# ── Validation ────────────────────────────────────────────────────────────────


class ValidationError(Exception):
    """Raised when field validation fails (→ HTTP 422)."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__(str(errors))


def _get_kind_and_key(bucket: str) -> tuple[str, str]:
    """Infer (kind, key) from a bucket string.

    Spine keys start with ``spine_`` or are one of the four spine categories.
    Everything else is treated as a skill.
    """
    spine_keys = {"compliance", "risk", "ambiguity", "commercial_structure"}
    norm = bucket.removeprefix("spine_").removeprefix("spine.")
    if norm in spine_keys or bucket in spine_keys:
        return "spine", norm
    return "skill", bucket


def _validate_fields(
    bucket: str,
    fields: dict[str, Any],
    is_new: bool = False,
    origin: str = "ai",
) -> tuple[dict[str, Any], list[str]]:
    """Validate and coerce *fields* against the field spec for *bucket*.

    Returns ``(clean_fields, warnings)`` where warnings is a list of
    human-readable messages (non-blocking).

    Raises:
        ValidationError: On required-field absence, enum mismatch, or
            write to a non-editable field.
    """
    kind, key = _get_kind_and_key(bucket)
    specs = get_field_spec(kind, key)
    spec_map = {s.name: s for s in specs}

    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    clean: dict[str, Any] = {}

    for field_name, value in fields.items():
        spec = spec_map.get(field_name)
        if spec is None:
            # Strip unknown keys silently
            continue
        if not spec.editable:
            errors.append({"field": field_name, "msg": f"Field '{field_name}' is not editable"})
            continue

        # Type coercion
        try:
            value = _coerce_value(field_name, value, spec.type)
        except (ValueError, TypeError) as exc:
            errors.append({"field": field_name, "msg": str(exc)})
            continue

        # Enum check
        if spec.type == "enum" and spec.enum_values and value not in spec.enum_values:
            errors.append({
                "field": field_name,
                "msg": f"'{value}' is not a valid value for '{field_name}'. "
                       f"Must be one of: {spec.enum_values}",
            })
            continue

        clean[field_name] = value

    # Check required fields for new items
    if is_new:
        for spec in specs:
            if spec.required and spec.editable and spec.name not in clean:
                # raw_excerpt is optional for user-added items
                if spec.name == "raw_excerpt" and origin == "user":
                    continue
                errors.append({"field": spec.name, "msg": f"Field '{spec.name}' is required"})

    if errors:
        raise ValidationError(errors)

    # Warnings (non-blocking)
    title = clean.get("title", "")
    if title and len(title.split()) > 10:
        warnings.append(f"title exceeds 10 words ({len(title.split())} words)")

    return clean, warnings


def _coerce_value(field_name: str, value: Any, field_type: str) -> Any:
    """Coerce *value* to the expected *field_type*."""
    if value is None:
        return value
    if field_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    if field_type in ("float",):
        v = float(value)
        if field_name == "confidence":
            v = max(0.0, min(1.0, v))
        return v
    if field_type in ("integer",):
        v = int(value)
        if field_name == "min_headcount":
            v = max(0, v)
        return v
    return value


# ── Counter helpers ───────────────────────────────────────────────────────────


def _next_user_id(doc: dict[str, Any], prefix: str) -> str:
    """Return the next user-added ID for *prefix* (e.g. ``DB-U003``).

    Counters in ``_edit.counters`` are keyed by prefix and track the highest
    counter used; they are never reused after delete.
    """
    counters: dict[str, int] = doc.get("_edit", {}).get("counters", {})
    n = counters.get(prefix, 0) + 1
    counters[prefix] = n
    if "_edit" not in doc:
        doc["_edit"] = {"revision": 0, "has_edits": False, "counters": {}}
    doc["_edit"]["counters"] = counters
    return f"{prefix}-U{n:03d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bump_revision(doc: dict[str, Any]) -> int:
    """Increment doc-level revision; return new value."""
    edit = doc.setdefault("_edit", {"revision": 0, "has_edits": True, "counters": {}})
    edit["revision"] = edit.get("revision", 0) + 1
    edit["has_edits"] = True
    edit["updated_at"] = _now_iso()
    return edit["revision"]


# ── Duplicate warnings ────────────────────────────────────────────────────────


def _bucket_items(doc: dict[str, Any], bucket: str) -> list[dict[str, Any]]:
    """Return the item list for *bucket* (skill slug or spine category)."""
    kind, key = _get_kind_and_key(bucket)
    if kind == "spine":
        return doc.get("spine", {}).get(key, [])
    for so in doc.get("skill_outputs", []):
        if so.get("skill_slug") == key:
            return so.get("items", [])
    return []


def _duplicate_warnings(
    doc: dict[str, Any], bucket: str, item: dict[str, Any]
) -> list[dict[str, Any]]:
    """Single-item O(n) duplicate check against the bucket's active items (C3)."""
    return [
        {"type": "possible_duplicate", **match}
        for match in find_similar_to(item, _bucket_items(doc, bucket))
    ]


# ── Mutation: add_item ────────────────────────────────────────────────────────


def add_item(
    doc: dict[str, Any],
    bucket: str,
    fields: dict[str, Any],
    used_uids: Optional[set[str]] = None,
) -> tuple[dict[str, Any], list[Any]]:
    """Add a new user item to *bucket* in *doc*.

    Applies defaults for user items (amendment 7):
    - ``origin="user"``, ``source_type="text"``, ``document_role="primary"``
    - ``source_document=""``, ``source_section`` defaults to ``"User-added"``
    - ``ambiguity_flag=False``, ``confidence=None`` (rendered as "—")
    - ``raw_excerpt`` optional for user items

    ID generation: ``get_prefix`` + ``_edit.counters`` + ``U`` namespace.
    Adding into a skill bucket whose SkillOutput entry is missing creates it (C9).

    Returns the new item dict.
    """
    kind, key = _get_kind_and_key(bucket)

    # Validate (new item, origin=user)
    clean, warnings = _validate_fields(bucket, fields, is_new=True, origin="user")

    # Apply user-item defaults (amendment 7)
    clean.setdefault("source_type", "text")
    clean.setdefault("document_role", "primary")
    clean.setdefault("source_document", "")
    if not clean.get("source_section"):
        clean["source_section"] = "User-added"
    clean.setdefault("ambiguity_flag", False)
    if "confidence" not in clean:
        clean["confidence"] = None
    if "raw_excerpt" not in clean:
        clean["raw_excerpt"] = None

    # Determine ID prefix
    if kind == "skill":
        artifact_type = clean.get("artifact_type")
        support_level = clean.get("support_level")
        prefix = get_prefix(kind, key, artifact_type=artifact_type, support_level=support_level)
    else:
        prefix = get_prefix(kind, key)

    item_id = _next_user_id(doc, prefix)
    clean["id"] = item_id

    # Generate uid
    all_used: set[str] = used_uids or set()
    if not all_used:
        # Collect all existing uids for collision check
        for so in doc.get("skill_outputs", []):
            for it in so.get("items", []):
                u = it.get("_meta", {}).get("uid")
                if u:
                    all_used.add(u)
        for sk in ["compliance", "risk", "ambiguity", "commercial_structure"]:
            for it in doc.get("spine", {}).get(sk, []):
                u = it.get("_meta", {}).get("uid")
                if u:
                    all_used.add(u)

    uid = _random_uid(all_used)
    all_used.add(uid)

    now = _now_iso()
    clean["_meta"] = {
        "uid": uid,
        "origin": "user",
        "status": "active",
        "version": 1,
        "edited": False,
        "edited_fields": [],
        "excerpt_edited": False,
        "enhanced": False,
        "created_at": now,
        "updated_at": now,
        "updated_by": "local",
    }

    # Insert into the correct bucket
    if kind == "skill":
        # Find or create the SkillOutput entry (C9)
        so_entry: Optional[dict] = None
        for so in doc.get("skill_outputs", []):
            if so.get("skill_slug") == key:
                so_entry = so
                break
        if so_entry is None:
            so_entry = {"skill_slug": key, "schema_version": "1.0", "items": []}
            doc.setdefault("skill_outputs", []).append(so_entry)
        so_entry.setdefault("items", []).append(clean)
    else:
        # Spine bucket
        doc.setdefault("spine", {}).setdefault(key, []).append(clean)

    _bump_revision(doc)

    warnings.extend(_duplicate_warnings(doc, bucket, clean))

    log.info("item_added", uid=uid, bucket=bucket, item_id=item_id)
    return clean, warnings


# ── Mutation: update_item ─────────────────────────────────────────────────────


def update_item(
    doc: dict[str, Any],
    uid: str,
    fields: dict[str, Any],
    base_version: int,
) -> tuple[dict[str, Any], list[Any]]:
    """Update fields on an existing item.

    Raises:
        KeyError: If *uid* is not found (→ 404).
        ValueError: If ``base_version`` doesn't match current version (→ 409).
        ValidationError: On invalid fields (→ 422).

    Returns ``(updated_item, warnings)``.
    """
    result = find_item_by_uid(doc, uid)
    if result is None:
        raise KeyError(f"uid '{uid}' not found")
    item, bucket = result

    # Per-item optimistic concurrency (D7, C6)
    current_version = item.get("_meta", {}).get("version", 1)
    if base_version != current_version:
        raise ValueError(f"version mismatch: expected {current_version}, got {base_version}")

    # Validate
    clean, warnings = _validate_fields(bucket, fields, is_new=False)

    fingerprint_before = _item_fingerprint(item)

    # Track edited fields
    meta = item["_meta"]
    edited_fields: list[str] = list(meta.get("edited_fields", []))
    changed_from: dict[str, Any] = {}

    for fname, new_val in clean.items():
        old_val = item.get(fname)
        if old_val != new_val:
            changed_from[fname] = old_val
            item[fname] = new_val
            if fname not in edited_fields:
                edited_fields.append(fname)

    if not changed_from:
        # No actual change — return current item without bumping versions
        return item, warnings

    # excerpt_edited flag (D5)
    if "raw_excerpt" in changed_from and meta.get("origin") == "ai":
        meta["excerpt_edited"] = True

    meta["edited"] = True
    meta["edited_fields"] = edited_fields
    meta["version"] = current_version + 1
    meta["updated_at"] = _now_iso()
    meta["updated_by"] = "local"

    _bump_revision(doc)

    # Duplicate check only when the compared text fields changed
    if _item_fingerprint(item) != fingerprint_before:
        warnings.extend(_duplicate_warnings(doc, bucket, item))

    log.info("item_updated", uid=uid, changed_fields=list(changed_from.keys()))
    return item, warnings


# ── Mutation: remove_item ─────────────────────────────────────────────────────


def remove_item(doc: dict[str, Any], uid: str) -> dict[str, Any]:
    """Soft-delete an item (D3).

    Sets ``_meta.status = "removed"``; bumps version and doc revision.
    Returns the updated item.

    Raises:
        KeyError: If uid is not found.
    """
    result = find_item_by_uid(doc, uid)
    if result is None:
        raise KeyError(f"uid '{uid}' not found")
    item, _bucket = result

    meta = item["_meta"]
    meta["status"] = "removed"
    meta["version"] = meta.get("version", 1) + 1
    meta["updated_at"] = _now_iso()
    meta["updated_by"] = "local"

    _bump_revision(doc)
    log.info("item_removed", uid=uid)
    return item


# ── Mutation: restore_item ────────────────────────────────────────────────────


def restore_item(doc: dict[str, Any], uid: str) -> dict[str, Any]:
    """Restore a previously removed item.

    Sets ``_meta.status = "active"``; bumps version and doc revision.
    Returns the updated item.

    Raises:
        KeyError: If uid is not found.
    """
    result = find_item_by_uid(doc, uid)
    if result is None:
        raise KeyError(f"uid '{uid}' not found")
    item, _bucket = result

    meta = item["_meta"]
    meta["status"] = "active"
    meta["version"] = meta.get("version", 1) + 1
    meta["updated_at"] = _now_iso()
    meta["updated_by"] = "local"

    _bump_revision(doc)
    log.info("item_restored", uid=uid)
    return item


# ── Mutation: revert_item ─────────────────────────────────────────────────────


def revert_item(doc: dict[str, Any], uid: str) -> dict[str, Any]:
    """Revert an AI item to its baseline (clears all edit flags).

    Raises:
        KeyError: If uid is not found.
        ValueError: If the item is user-added (origin="user") → 400.
    """
    result = find_item_by_uid(doc, uid)
    if result is None:
        raise KeyError(f"uid '{uid}' not found")
    item, _bucket = result

    meta = item["_meta"]
    if meta.get("origin") == "user":
        raise ValueError("Cannot revert a user-added item (no AI baseline)")

    # Restore from _baselines
    baseline = doc.get("_baselines", {}).get(uid)
    if baseline is None:
        raise ValueError(f"No baseline found for uid '{uid}'")

    # Replace all data fields with baseline values (keeping same dict object)
    data_fields = [k for k in list(item.keys()) if k != "_meta"]
    for k in data_fields:
        del item[k]
    for k, v in baseline.items():
        item[k] = copy.deepcopy(v)

    # Reset _meta edit flags
    meta["edited"] = False
    meta["edited_fields"] = []
    meta["excerpt_edited"] = False
    meta["enhanced"] = False
    meta["status"] = "active"
    meta["version"] = meta.get("version", 1) + 1
    meta["updated_at"] = _now_iso()
    meta["updated_by"] = "local"

    _bump_revision(doc)
    log.info("item_reverted", uid=uid)
    return item


# ── Mutation: reset_working_copy ──────────────────────────────────────────────


def reset_working_copy(
    doc: Optional[dict[str, Any]],
    job_id: str,
    output_dir: Any,  # Path
) -> tuple[dict[str, Any], Optional[str]]:
    """Archive working.json and re-bootstrap from result.json.

    ``doc`` is unused (kept for API compatibility); pass ``None`` freely.

    - If ``working.json`` exists: renames it to ``working.<ts>.bak.json``;
      re-reads ``result.json``; bootstraps a fresh copy.
    - If ``working.json`` does not exist (legacy job): re-bootstraps in-memory
      from result.json and does not archive anything (r3-4).

    Returns ``(new_doc, archive_path_or_None)``.
    The caller is responsible for saving the new_doc and logging.
    """
    from pathlib import Path

    output_path = Path(output_dir)
    working_path = output_path / "working.json"
    result_path = output_path / "result.json"

    archive_path: Optional[str] = None

    if working_path.exists():
        # Archive existing working copy
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak_path = output_path / f"working.{ts}.bak.json"
        working_path.rename(bak_path)
        archive_path = str(bak_path)
        log.info("working_copy_archived", job_id=job_id, archive=archive_path)

    # Re-bootstrap from baseline
    import json

    if not result_path.exists():
        raise FileNotFoundError(f"result.json not found at {result_path}")

    baseline = json.loads(result_path.read_text(encoding="utf-8"))
    new_doc = bootstrap_working_copy(baseline, job_id)

    log.info("working_copy_reset", job_id=job_id, archived=archive_path is not None)
    return new_doc, archive_path


# ── Mutation: bulk_op ─────────────────────────────────────────────────────────


def bulk_op(
    doc: dict[str, Any],
    uids: list[str],
    op: str,
    field: Optional[str] = None,
    value: Any = None,
    base_versions: Optional[dict[str, int]] = None,
) -> list[dict[str, Any]]:
    """Apply *op* to every uid in *uids* within a single doc pass.

    Supported ops: ``"remove"``, ``"restore"``, ``"set_field"``.

    For ``set_field`` a per-item ``base_version`` must be supplied via
    *base_versions* (mapping uid → version).  Items whose version doesn't
    match get an error result without being modified (per-item optimistic
    concurrency, C12).

    All ops that succeed bump the per-item version and contribute to a
    **single** ``_bump_revision`` call at the end (one file write, one
    revision bump — AT-13).

    Returns a list of per-item result dicts::

        [
          {"uid": "itm_...", "ok": True,  "meta": {"status": .., "version": ..}},
          {"uid": "itm_...", "ok": False, "error": "...",
           "meta": {"status": .., "version": ..}},
          ...
        ]
    """
    if op not in ("remove", "restore", "set_field"):
        raise ValueError(f"Unknown bulk op '{op}'")

    if base_versions is None:
        base_versions = {}

    results: list[dict[str, Any]] = []
    any_changed = False

    for uid in uids:
        found = find_item_by_uid(doc, uid)
        if found is None:
            results.append({"uid": uid, "ok": False, "error": f"uid '{uid}' not found",
                             "meta": {"status": "unknown", "version": 0}})
            continue

        item, bucket = found
        meta = item["_meta"]

        try:
            if op == "remove":
                meta["status"] = "removed"
                meta["version"] = meta.get("version", 1) + 1
                meta["updated_at"] = _now_iso()
                meta["updated_by"] = "local"
                any_changed = True
                log.info("bulk_item_removed", uid=uid)

            elif op == "restore":
                meta["status"] = "active"
                meta["version"] = meta.get("version", 1) + 1
                meta["updated_at"] = _now_iso()
                meta["updated_by"] = "local"
                any_changed = True
                log.info("bulk_item_restored", uid=uid)

            elif op == "set_field":
                if field is None:
                    raise ValueError("'field' is required for set_field op")
                bv = base_versions.get(uid)
                if bv is None:
                    raise ValueError(f"base_version missing for uid '{uid}'")
                current_version = meta.get("version", 1)
                if bv != current_version:
                    raise ValueError(
                        f"version mismatch for uid '{uid}': "
                        f"expected {current_version}, got {bv}"
                    )
                clean, _ = _validate_fields(bucket, {field: value}, is_new=False)
                if field in clean and item.get(field) != clean[field]:
                    item[field] = clean[field]
                    edited_fields: list[str] = list(meta.get("edited_fields", []))
                    if field not in edited_fields:
                        edited_fields.append(field)
                    meta["edited"] = True
                    meta["edited_fields"] = edited_fields
                    meta["version"] = current_version + 1
                    meta["updated_at"] = _now_iso()
                    meta["updated_by"] = "local"
                    any_changed = True
                    log.info("bulk_item_set_field", uid=uid, field=field)

            results.append({
                "uid": uid,
                "ok": True,
                "meta": {"status": meta["status"], "version": meta.get("version", 1)},
            })

        except (ValueError, ValidationError) as exc:
            results.append({
                "uid": uid,
                "ok": False,
                "error": str(exc),
                "meta": {"status": meta.get("status", "unknown"),
                         "version": meta.get("version", 0)},
            })

    if any_changed:
        _bump_revision(doc)

    return results
