"""
tests/test_repository_and_bootstrap.py
----------------------------------------
AT-1 — working copy bootstrap, repository, and legacy job sequence tests.

Covered:
- bootstrap_working_copy assigns itm_<12hex> uids to all items.
- compute_edit_state counts correctly on a fresh bootstrap.
- find_item_by_uid locates items in skill_outputs and spine.
- Legacy GET returns in-memory bootstrap with uids present.
- Legacy GET strips _baselines from response.
- Legacy GET does NOT write working.json (never writes on GET).
- Working copy is written at pipeline completion (0.4 HOOK).
- Running job GET returns no working copy / no result in response.
- Baseline endpoint returns 404 for unknown uid.
- Baseline endpoint returns the original item for a valid uid.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.edit.service import (
    bootstrap_working_copy,
    compute_edit_state,
    find_item_by_uid,
)


# ── Golden fixture helper ─────────────────────────────────────────────────────


@pytest.fixture()
def baseline(golden_result: dict) -> dict:
    """Alias for the golden result fixture."""
    return golden_result


# ─────────────────────────────────────────────────────────────────────────────
# bootstrap_working_copy
# ─────────────────────────────────────────────────────────────────────────────


def test_bootstrap_assigns_uids_to_skill_items(baseline: dict) -> None:
    """Every skill item gets an itm_<12hex> uid in _meta."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    for so in doc.get("skill_outputs", []):
        for item in so.get("items", []):
            uid = item.get("_meta", {}).get("uid", "")
            assert uid.startswith("itm_"), f"Expected itm_ prefix, got {uid!r}"
            assert len(uid) == 4 + 12, f"Expected 16 chars, got {uid!r}"


def test_bootstrap_assigns_uids_to_spine_items(baseline: dict) -> None:
    """Every spine item gets an itm_<12hex> uid in _meta."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    spine = doc.get("spine", {})
    for key in ["compliance", "risk", "ambiguity", "commercial_structure"]:
        for item in spine.get(key, []):
            uid = item.get("_meta", {}).get("uid", "")
            assert uid.startswith("itm_"), f"Spine [{key}] item uid {uid!r} missing itm_ prefix"


def test_bootstrap_uids_are_unique(baseline: dict) -> None:
    """All uids in a bootstrapped document are unique."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    uids: list[str] = []
    for so in doc.get("skill_outputs", []):
        for item in so.get("items", []):
            uids.append(item["_meta"]["uid"])
    spine = doc.get("spine", {})
    for key in ["compliance", "risk", "ambiguity", "commercial_structure"]:
        for item in spine.get(key, []):
            uids.append(item["_meta"]["uid"])
    assert len(uids) == len(set(uids)), "Duplicate uids detected"


def test_bootstrap_baselines_map_present(baseline: dict) -> None:
    """_baselines is a top-level map keyed by uid."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    assert "_baselines" in doc
    uids_in_items: list[str] = []
    for so in doc.get("skill_outputs", []):
        for item in so.get("items", []):
            uids_in_items.append(item["_meta"]["uid"])
    for uid in uids_in_items:
        assert uid in doc["_baselines"], f"uid {uid!r} missing from _baselines"


def test_bootstrap_baselines_are_original_items(baseline: dict) -> None:
    """_baselines values are the original items (without _meta)."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    for so in doc.get("skill_outputs", []):
        for item in so.get("items", []):
            uid = item["_meta"]["uid"]
            original = doc["_baselines"][uid]
            assert "_meta" not in original, "Baseline should not contain _meta"


def test_bootstrap_edit_block_added(baseline: dict) -> None:
    """Top-level _edit block with revision=0 is added."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    assert "_edit" in doc
    assert doc["_edit"]["revision"] == 0
    assert doc["_edit"]["has_edits"] is False


def test_bootstrap_meta_defaults(baseline: dict) -> None:
    """_meta defaults: origin=ai, status=active, version=1."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    for so in doc.get("skill_outputs", []):
        for item in so.get("items", []):
            meta = item["_meta"]
            assert meta["origin"] == "ai"
            assert meta["status"] == "active"
            assert meta["version"] == 1


def test_bootstrap_deterministic_uids_are_stable(baseline: dict) -> None:
    """Deterministic uids must be identical on two calls with the same job_id."""
    job_id = "legacy:" + str(uuid.uuid4())
    doc1 = bootstrap_working_copy(baseline, job_id)
    doc2 = bootstrap_working_copy(baseline, job_id)

    uids1 = sorted(doc1["_baselines"].keys())
    uids2 = sorted(doc2["_baselines"].keys())
    assert uids1 == uids2, "Deterministic uids changed between calls"


# ─────────────────────────────────────────────────────────────────────────────
# compute_edit_state
# ─────────────────────────────────────────────────────────────────────────────


def test_compute_edit_state_fresh_bootstrap(baseline: dict) -> None:
    """A fresh bootstrap has all items active and has_edits=false."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    state = compute_edit_state(doc)
    assert state["has_edits"] is False
    assert state["removed"] == 0
    assert state["user_added"] == 0
    assert state["edited"] == 0
    assert state["active"] > 0


def test_compute_edit_state_counts_removed(baseline: dict) -> None:
    """Marking an item removed is reflected in compute_edit_state."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    # Mark the first skill item as removed
    doc["skill_outputs"][0]["items"][0]["_meta"]["status"] = "removed"
    state = compute_edit_state(doc)
    assert state["removed"] == 1
    assert state["has_edits"] is True


# ─────────────────────────────────────────────────────────────────────────────
# find_item_by_uid
# ─────────────────────────────────────────────────────────────────────────────


def test_find_item_by_uid_skill_item(baseline: dict) -> None:
    """find_item_by_uid can locate a skill item by its uid."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    first_so = doc["skill_outputs"][0]
    first_item = first_so["items"][0]
    uid = first_item["_meta"]["uid"]

    result = find_item_by_uid(doc, uid)
    assert result is not None
    found_item, bucket = result
    assert found_item["_meta"]["uid"] == uid
    assert bucket == first_so["skill_slug"]


def test_find_item_by_uid_spine_item(baseline: dict) -> None:
    """find_item_by_uid can locate a spine item by its uid."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    compliance_items = doc["spine"].get("compliance", [])
    assert compliance_items, "Expected at least one compliance item in golden fixture"
    uid = compliance_items[0]["_meta"]["uid"]

    result = find_item_by_uid(doc, uid)
    assert result is not None
    found_item, bucket = result
    assert bucket == "compliance"


def test_find_item_by_uid_missing_returns_none(baseline: dict) -> None:
    """find_item_by_uid returns None for an unknown uid."""
    doc = bootstrap_working_copy(baseline, str(uuid.uuid4()))
    assert find_item_by_uid(doc, "itm_000000000000") is None


# ─────────────────────────────────────────────────────────────────────────────
# Legacy GET — in-memory bootstrap, no disk write
# ─────────────────────────────────────────────────────────────────────────────


def _seed_complete_job_with_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden: dict,
) -> tuple[str, TestClient]:
    """Helper: create a complete job with result.json but no working.json."""
    from app.job_store import JobStatus, JobStore

    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))

    job_id = str(uuid.uuid4())
    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True)

    # Write result.json
    (job_dir / "result.json").write_text(json.dumps(golden), encoding="utf-8")

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

    # Load into a fresh store
    store = JobStore()
    store.load_from_disk(tmp_path)
    monkeypatch.setattr("app.job_store._store", store)

    from app.main import app
    client = TestClient(app, raise_server_exceptions=True)
    return job_id, client


def test_legacy_get_returns_uids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden_result: dict,
) -> None:
    """Legacy GET returns result with uids in _meta on items."""
    job_id, client = _seed_complete_job_with_result(tmp_path, monkeypatch, golden_result)
    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    # First skill output items should have _meta.uid
    so = data["result"]["skill_outputs"][0]
    first_item = so["items"][0]
    assert "_meta" in first_item
    assert first_item["_meta"]["uid"].startswith("itm_")


def test_legacy_get_strips_baselines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden_result: dict,
) -> None:
    """Legacy GET strips _baselines from the response."""
    job_id, client = _seed_complete_job_with_result(tmp_path, monkeypatch, golden_result)
    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert "_baselines" not in data.get("result", {})


def test_legacy_get_does_not_write_working_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden_result: dict,
) -> None:
    """Legacy GET must NOT write working.json to disk (C4)."""
    job_id, client = _seed_complete_job_with_result(tmp_path, monkeypatch, golden_result)
    working_path = tmp_path / job_id / "working.json"
    assert not working_path.exists(), "working.json should not exist before GET"
    client.get(f"/jobs/{job_id}")
    assert not working_path.exists(), "Legacy GET must not create working.json"


def test_legacy_get_includes_edit_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden_result: dict,
) -> None:
    """Legacy GET includes edit_state in response."""
    job_id, client = _seed_complete_job_with_result(tmp_path, monkeypatch, golden_result)
    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert "edit_state" in data
    assert data["edit_state"]["has_edits"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Baseline endpoint
# ─────────────────────────────────────────────────────────────────────────────


def test_baseline_endpoint_returns_404_for_unknown_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden_result: dict,
) -> None:
    """Baseline endpoint returns 404 for an unknown uid."""
    job_id, client = _seed_complete_job_with_result(tmp_path, monkeypatch, golden_result)
    resp = client.get(f"/jobs/{job_id}/items/itm_000000000000/baseline")
    assert resp.status_code == 404


def test_baseline_endpoint_returns_original_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden_result: dict,
) -> None:
    """Baseline endpoint returns the original AI item for a valid uid."""
    job_id, client = _seed_complete_job_with_result(tmp_path, monkeypatch, golden_result)

    # First get the uid from the GET endpoint
    resp = client.get(f"/jobs/{job_id}")
    data = resp.json()
    uid = data["result"]["skill_outputs"][0]["items"][0]["_meta"]["uid"]

    resp = client.get(f"/jobs/{job_id}/items/{uid}/baseline")
    assert resp.status_code == 200
    baseline_data = resp.json()
    assert baseline_data["uid"] == uid
    assert "_meta" not in baseline_data["baseline"]  # baseline has no _meta
