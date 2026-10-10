"""
tests/test_edit_endpoints.py
-----------------------------
Integration tests for app/routers/edits.py — Sub-task 1.2.

Covers:
  AT-2  POST /items: user IDs, add into empty skill creates SkillOutput
  AT-3  PATCH /items/{uid}: edited flags, baseline unchanged, log written, revert
  AT-4  409 on stale version; cross-item no 409; 422 on non-editable/bad enum
  AT-5  DELETE + POST restore; 404 on unknown uid
  AT-11 POST /reset: archives working.json; logs reset; legacy path (no working.json) no archive
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.edit.service import bootstrap_working_copy


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _seed_complete_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden: dict,
    *,
    with_working: bool = False,
) -> tuple[str, TestClient]:
    """Create a complete job on disk and return (job_id, TestClient).

    Parameters
    ----------
    with_working:
        If True, also write a bootstrapped working.json.
    """
    from app.job_store import JobStore

    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))

    job_id = str(uuid.uuid4())
    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True)

    # Write result.json
    (job_dir / "result.json").write_text(json.dumps(golden), encoding="utf-8")

    if with_working:
        doc = bootstrap_working_copy(golden, job_id)
        (job_dir / "working.json").write_text(json.dumps(doc), encoding="utf-8")

    # Write job.json
    job_json = {
        "job_id": job_id,
        "status": "complete",
        "file_name": "test.pdf",
        "file_names": ["test.pdf"],
        "primary_file_name": "test.pdf",
        "primary_file_path": "",
        "file_roles": {},
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
        "error": None,
    }
    (job_dir / "job.json").write_text(json.dumps(job_json), encoding="utf-8")

    # Patch the repo singleton so tests don't bleed across
    from core.edit.repository import FileResultRepository
    repo = FileResultRepository(tmp_path)
    import app.routers.edits as edits_mod
    monkeypatch.setattr(edits_mod, "_repo", repo)

    store = JobStore()
    store.load_from_disk(tmp_path)
    monkeypatch.setattr("app.job_store._store", store)

    from app.main import app
    client = TestClient(app, raise_server_exceptions=True)
    return job_id, client


def _first_skill_uid(client: TestClient, job_id: str, slug: str = "design-build") -> str:
    """Return the uid of the first item in *slug* from GET /jobs/{job_id}."""
    resp = client.get(f"/jobs/{job_id}")
    data = resp.json()
    for so in data["result"]["skill_outputs"]:
        if so["skill_slug"] == slug:
            return so["items"][0]["_meta"]["uid"]
    raise ValueError(f"No items in {slug!r}")


def _first_spine_uid(client: TestClient, job_id: str, category: str = "compliance") -> str:
    """Return uid of first item in *category* spine bucket."""
    resp = client.get(f"/jobs/{job_id}")
    data = resp.json()
    return data["result"]["spine"][category][0]["_meta"]["uid"]


# ── AT-2: POST /items ─────────────────────────────────────────────────────────


def test_add_item_returns_db_u001(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """POST /items → DB-U001 for first user-added design-build item."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    resp = client.post(
        f"/jobs/{job_id}/items",
        json={
            "bucket": "design-build",
            "fields": {
                "type": "functional",
                "title": "New cloud requirement",
                "description": "Provision a backup region.",
                "source_section": "Section 11",
                "priority": "must",
                "confidence": 0.85,
                "ambiguity_flag": False,
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["item"]["id"] == "DB-U001"
    assert data["item"]["_meta"]["origin"] == "user"
    assert "edit_state" in data
    assert data["edit_state"]["user_added"] == 1


def test_add_item_second_gets_db_u002(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """Second POST /items to same bucket → DB-U002."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    payload = {
        "bucket": "design-build",
        "fields": {
            "type": "functional",
            "title": "Req A",
            "description": "Desc",
            "source_section": "S1",
            "priority": "must",
            "confidence": 0.8,
            "ambiguity_flag": False,
        },
    }
    resp1 = client.post(f"/jobs/{job_id}/items", json=payload)
    resp2 = client.post(f"/jobs/{job_id}/items", json=payload)
    assert resp1.json()["item"]["id"] == "DB-U001"
    assert resp2.json()["item"]["id"] == "DB-U002"


def test_add_item_creates_working_json_on_legacy_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """First mutation on a legacy job (no working.json) creates working.json (r2-1)."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result, with_working=False)
    working_path = tmp_path / job_id / "working.json"
    assert not working_path.exists()

    resp = client.post(
        f"/jobs/{job_id}/items",
        json={
            "bucket": "design-build",
            "fields": {
                "type": "functional",
                "title": "Newly added",
                "description": "D",
                "source_section": "S",
                "priority": "must",
                "confidence": 0.8,
                "ambiguity_flag": False,
            },
        },
    )
    assert resp.status_code == 200
    assert working_path.exists(), "working.json should be created by first mutation"


def test_add_item_into_spine_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """POST /items into compliance spine bucket succeeds."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    resp = client.post(
        f"/jobs/{job_id}/items",
        json={
            "bucket": "compliance",
            "fields": {
                "title": "New compliance item",
                "source_section": "Section X",
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["item"]["_meta"]["origin"] == "user"
    assert data["item"]["id"].startswith("CR-U")


def test_add_item_invalid_enum_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """POST /items with invalid enum value returns 422."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    resp = client.post(
        f"/jobs/{job_id}/items",
        json={
            "bucket": "design-build",
            "fields": {
                "type": "not_valid",
                "title": "X",
                "description": "D",
                "source_section": "S",
                "priority": "must",
                "confidence": 0.8,
                "ambiguity_flag": False,
            },
        },
    )
    assert resp.status_code == 422


def test_add_item_job_not_found_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """POST /items for unknown job returns 404."""
    _, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/jobs/{fake_id}/items",
        json={"bucket": "design-build", "fields": {"title": "X"}},
    )
    assert resp.status_code == 404


# ── AT-3: PATCH /items/{uid} ──────────────────────────────────────────────────


def test_patch_item_sets_edited_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """PATCH /items/{uid} sets edited=True and records edited_fields."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    resp = client.patch(
        f"/jobs/{job_id}/items/{uid}",
        json={"base_version": 1, "fields": {"title": "Updated title"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["item"]["_meta"]["edited"] is True
    assert "title" in data["item"]["_meta"]["edited_fields"]
    assert "edit_state" in data
    assert data["edit_state"]["edited"] == 1


def test_patch_item_baseline_not_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """PATCH does not modify the baseline in _baselines."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result, with_working=False)
    uid = _first_skill_uid(client, job_id)

    # Get original title from GET response
    get_resp = client.get(f"/jobs/{job_id}")
    original_title = None
    for so in get_resp.json()["result"]["skill_outputs"]:
        if so["skill_slug"] == "design-build":
            original_title = so["items"][0]["title"]
            break
    assert original_title is not None

    # Patch it
    client.patch(
        f"/jobs/{job_id}/items/{uid}",
        json={"base_version": 1, "fields": {"title": "Changed title"}},
    )

    # Read working.json directly and verify baseline
    working_path = tmp_path / job_id / "working.json"
    assert working_path.exists()
    doc = json.loads(working_path.read_text())
    assert doc["_baselines"][uid]["title"] == original_title


def test_patch_item_logs_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """PATCH writes a log entry to edit_log.jsonl."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    client.patch(
        f"/jobs/{job_id}/items/{uid}",
        json={"base_version": 1, "fields": {"title": "Logged update"}},
    )

    log_path = tmp_path / job_id / "edit_log.jsonl"
    assert log_path.exists()
    entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    patch_entries = [e for e in entries if e.get("op") == "patch"]
    assert len(patch_entries) == 1
    assert patch_entries[0]["uid"] == uid


def test_patch_item_excerpt_edited_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """PATCH on raw_excerpt for AI item sets excerpt_edited=True."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    resp = client.patch(
        f"/jobs/{job_id}/items/{uid}",
        json={"base_version": 1, "fields": {"raw_excerpt": "Modified excerpt text"}},
    )
    assert resp.status_code == 200
    assert resp.json()["item"]["_meta"]["excerpt_edited"] is True


def test_revert_item_clears_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """POST /items/{uid}/revert restores baseline and clears edit flags."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    # Get original title
    get_resp = client.get(f"/jobs/{job_id}")
    original_title = None
    for so in get_resp.json()["result"]["skill_outputs"]:
        if so["skill_slug"] == "design-build":
            original_title = so["items"][0]["title"]
            break

    # Patch it
    client.patch(
        f"/jobs/{job_id}/items/{uid}",
        json={"base_version": 1, "fields": {"title": "Temporary change"}},
    )

    # Revert
    resp = client.post(f"/jobs/{job_id}/items/{uid}/revert")
    assert resp.status_code == 200
    data = resp.json()
    assert data["item"]["title"] == original_title
    assert data["item"]["_meta"]["edited"] is False
    assert data["item"]["_meta"]["edited_fields"] == []


def test_revert_user_item_returns_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """POST /items/{uid}/revert on user-added item returns 400."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)

    # Add a user item
    add_resp = client.post(
        f"/jobs/{job_id}/items",
        json={
            "bucket": "design-build",
            "fields": {
                "type": "functional",
                "title": "User req",
                "description": "D",
                "source_section": "S",
                "priority": "must",
                "confidence": 0.8,
                "ambiguity_flag": False,
            },
        },
    )
    uid = add_resp.json()["item"]["_meta"]["uid"]

    resp = client.post(f"/jobs/{job_id}/items/{uid}/revert")
    assert resp.status_code == 400


# ── AT-4: 409 version conflict / 422 validation ───────────────────────────────


def test_patch_stale_version_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """PATCH with stale base_version returns 409 with current item."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    resp = client.patch(
        f"/jobs/{job_id}/items/{uid}",
        json={"base_version": 999, "fields": {"title": "Stale edit"}},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "current_item" in detail
    assert "version mismatch" in detail.get("msg", "").lower() or detail.get("current_item") is not None


def test_patch_cross_item_no_version_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """Editing item A does not cause 409 on subsequent edit of item B (D7)."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid_a = _first_skill_uid(client, job_id, "design-build")
    uid_b = _first_spine_uid(client, job_id, "compliance")

    # Edit item A
    resp_a = client.patch(
        f"/jobs/{job_id}/items/{uid_a}",
        json={"base_version": 1, "fields": {"title": "A modified"}},
    )
    assert resp_a.status_code == 200

    # Edit item B with its original version (should succeed)
    resp_b = client.patch(
        f"/jobs/{job_id}/items/{uid_b}",
        json={"base_version": 1, "fields": {"title": "B modified"}},
    )
    assert resp_b.status_code == 200


def test_patch_non_editable_field_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """PATCH with non-editable field (id) returns 422."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    resp = client.patch(
        f"/jobs/{job_id}/items/{uid}",
        json={"base_version": 1, "fields": {"id": "HACKED"}},
    )
    assert resp.status_code == 422


def test_patch_invalid_enum_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """PATCH with invalid enum value returns 422."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    resp = client.patch(
        f"/jobs/{job_id}/items/{uid}",
        json={"base_version": 1, "fields": {"priority": "superurgent"}},
    )
    assert resp.status_code == 422


def test_patch_unknown_uid_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """PATCH on unknown uid returns 404."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    resp = client.patch(
        f"/jobs/{job_id}/items/itm_000000000000",
        json={"base_version": 1, "fields": {"title": "X"}},
    )
    assert resp.status_code == 404


# ── AT-5: DELETE (remove) / restore ───────────────────────────────────────────


def test_delete_item_returns_removed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """DELETE /items/{uid} sets status=removed."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    resp = client.delete(f"/jobs/{job_id}/items/{uid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["item"]["_meta"]["status"] == "removed"
    assert data["edit_state"]["removed"] == 1


def test_delete_item_updates_edit_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """DELETE updates edit_state counts."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    resp = client.delete(f"/jobs/{job_id}/items/{uid}")
    assert resp.json()["edit_state"]["removed"] == 1
    assert resp.json()["edit_state"]["has_edits"] is True


def test_delete_unknown_uid_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """DELETE on unknown uid returns 404."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    resp = client.delete(f"/jobs/{job_id}/items/itm_000000000000")
    assert resp.status_code == 404


def test_restore_item_returns_active_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """POST /items/{uid}/restore sets status=active."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    client.delete(f"/jobs/{job_id}/items/{uid}")
    resp = client.post(f"/jobs/{job_id}/items/{uid}/restore")
    assert resp.status_code == 200
    assert resp.json()["item"]["_meta"]["status"] == "active"


def test_restore_item_edit_state_reflects_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """After restore, edit_state.removed == 0 again."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    client.delete(f"/jobs/{job_id}/items/{uid}")
    resp = client.post(f"/jobs/{job_id}/items/{uid}/restore")
    assert resp.json()["edit_state"]["removed"] == 0


def test_restore_unknown_uid_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """POST /items/{uid}/restore on unknown uid returns 404."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    resp = client.post(f"/jobs/{job_id}/items/itm_000000000000/restore")
    assert resp.status_code == 404


# ── AT-11: POST /reset ────────────────────────────────────────────────────────


def test_reset_requires_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """POST /reset without ?confirm=true returns 400."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result, with_working=True)
    resp = client.post(f"/jobs/{job_id}/reset")
    assert resp.status_code == 400


def test_reset_archives_working_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """POST /reset?confirm=true archives working.json and logs reset."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result, with_working=True)
    working_path = tmp_path / job_id / "working.json"
    assert working_path.exists()

    resp = client.post(f"/jobs/{job_id}/reset?confirm=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["archived"] is not None
    assert ".bak.json" in data["archived"]
    assert Path(data["archived"]).exists()
    # working.json should still exist (reset creates a new one)
    # but actually working.json is NOT recreated by the current reset handler
    # for a normal job: it archives and does NOT write the new doc until
    # working_existed was True (which it was), but we only save new_doc if working_existed
    # The log entry should be present
    log_path = tmp_path / job_id / "edit_log.jsonl"
    assert log_path.exists()
    entries = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    reset_entries = [e for e in entries if e.get("op") == "reset"]
    assert len(reset_entries) == 1


def test_reset_legacy_job_no_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """POST /reset on legacy job (no working.json) returns None for archived (r3-4)."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result, with_working=False)
    working_path = tmp_path / job_id / "working.json"
    assert not working_path.exists()

    resp = client.post(f"/jobs/{job_id}/reset?confirm=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["archived"] is None
    # working.json should NOT be created by reset on a legacy job
    assert not working_path.exists()
    # No log entry since no archive happened
    log_path = tmp_path / job_id / "edit_log.jsonl"
    if log_path.exists():
        entries = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        reset_entries = [e for e in entries if e.get("op") == "reset"]
        assert len(reset_entries) == 0


# ── Mutation response shape ───────────────────────────────────────────────────


def test_mutation_response_has_item_edit_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """All mutating endpoints return {item (with _meta.version/status), edit_state}."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    # PATCH
    resp = client.patch(
        f"/jobs/{job_id}/items/{uid}",
        json={"base_version": 1, "fields": {"title": "New title"}},
    )
    data = resp.json()
    assert "item" in data
    assert "edit_state" in data
    assert "_meta" in data["item"]
    assert "version" in data["item"]["_meta"]
    assert "status" in data["item"]["_meta"]


def test_history_endpoint_returns_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """GET /history returns log entries after mutations."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    client.patch(
        f"/jobs/{job_id}/items/{uid}",
        json={"base_version": 1, "fields": {"title": "For history"}},
    )

    resp = client.get(f"/jobs/{job_id}/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert isinstance(data["entries"], list)
    assert data["entries"][0]["op"] == "patch"  # newest first


# ── AT-20 / 1.6: History endpoint — pagination and ordering ──────────────────


def test_history_empty_returns_zero_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """GET /history with no mutations returns empty entries and total=0."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result, with_working=True)

    resp = client.get(f"/jobs/{job_id}/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["entries"] == []
    assert data["page"] == 1
    assert data["page_size"] == 50


def test_history_entries_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """GET /history returns entries newest-first after multiple mutations."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    # Patch twice
    client.patch(f"/jobs/{job_id}/items/{uid}", json={"base_version": 1, "fields": {"title": "First edit"}})
    client.patch(f"/jobs/{job_id}/items/{uid}", json={"base_version": 2, "fields": {"title": "Second edit"}})

    resp = client.get(f"/jobs/{job_id}/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    # newest first: timestamps should be descending (or equal for fast tests)
    ts0 = data["entries"][0]["ts"]
    ts1 = data["entries"][1]["ts"]
    assert ts0 >= ts1, "entries should be newest-first"


def test_history_pagination_page_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """GET /history?page=1&page_size=1 returns only one entry."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    client.patch(f"/jobs/{job_id}/items/{uid}", json={"base_version": 1, "fields": {"title": "Edit A"}})
    client.patch(f"/jobs/{job_id}/items/{uid}", json={"base_version": 2, "fields": {"title": "Edit B"}})

    resp = client.get(f"/jobs/{job_id}/history?page=1&page_size=1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["entries"]) == 1
    assert data["page"] == 1
    assert data["page_size"] == 1


def test_history_pagination_page_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """GET /history?page=2&page_size=1 returns second entry."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    client.patch(f"/jobs/{job_id}/items/{uid}", json={"base_version": 1, "fields": {"title": "Edit A"}})
    client.patch(f"/jobs/{job_id}/items/{uid}", json={"base_version": 2, "fields": {"title": "Edit B"}})

    resp_p1 = client.get(f"/jobs/{job_id}/history?page=1&page_size=1")
    resp_p2 = client.get(f"/jobs/{job_id}/history?page=2&page_size=1")
    assert resp_p2.status_code == 200
    data_p1 = resp_p1.json()
    data_p2 = resp_p2.json()
    # Pages should be distinct entries
    assert data_p1["entries"][0]["ts"] != data_p2["entries"][0]["ts"] or \
           data_p1["entries"][0]["revision"] != data_p2["entries"][0]["revision"]


def test_history_entry_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    """Each history entry has the required fields: ts, op, uid, id, bucket, fields_changed, actor, revision."""
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    uid = _first_skill_uid(client, job_id)

    client.patch(f"/jobs/{job_id}/items/{uid}", json={"base_version": 1, "fields": {"title": "Shaped entry"}})

    resp = client.get(f"/jobs/{job_id}/history")
    entry = resp.json()["entries"][0]

    for field in ("ts", "op", "uid", "id", "bucket", "fields_changed", "actor", "revision"):
        assert field in entry, f"missing field: {field}"
    assert entry["op"] == "patch"
    assert entry["actor"] == "local"
    assert isinstance(entry["fields_changed"], dict)
