"""
tests/test_bulk_operations.py
------------------------------
Tests for sub-task 2.1 — Bulk Operations.

Covers AT-13:
  - bulk_op service: remove/restore/set_field across many items
  - Single revision bump for all ops (AT-13: one file write)
  - Per-item version conflict returns ok=False without aborting others
  - Endpoint POST /jobs/{id}/items/bulk: lock, save, log entries
  - Empty uids → 422
  - Unknown op → 422
  - Unknown uid returns ok=False in results (partial failure)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.edit.service import (
    bootstrap_working_copy,
    bulk_op,
    compute_edit_state,
    find_item_by_uid,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _fresh_doc(golden: dict) -> dict:
    return bootstrap_working_copy(golden, str(uuid.uuid4()))


def _all_uids(doc: dict) -> list[str]:
    uids = []
    for so in doc.get("skill_outputs", []):
        for item in so.get("items", []):
            u = item.get("_meta", {}).get("uid")
            if u:
                uids.append(u)
    for key in ["compliance", "risk", "ambiguity", "commercial_structure"]:
        for item in doc.get("spine", {}).get(key, []):
            u = item.get("_meta", {}).get("uid")
            if u:
                uids.append(u)
    return uids


def _seed_complete_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden: dict,
    *,
    with_working: bool = True,
) -> tuple[str, TestClient]:
    from app.job_store import JobStore

    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))

    job_id = str(uuid.uuid4())
    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True)

    (job_dir / "result.json").write_text(json.dumps(golden), encoding="utf-8")

    if with_working:
        doc = bootstrap_working_copy(golden, job_id)
        (job_dir / "working.json").write_text(json.dumps(doc), encoding="utf-8")

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


# ── Service-layer unit tests ───────────────────────────────────────────────────


class TestBulkOpService:
    def test_bulk_remove_all_sets_status_removed(self, golden_result: dict) -> None:
        """bulk_op remove → every uid in the list is status=removed."""
        doc = _fresh_doc(golden_result)
        uids = _all_uids(doc)[:5]  # first 5
        results = bulk_op(doc, uids, "remove")

        assert all(r["ok"] for r in results)
        assert all(r["meta"]["status"] == "removed" for r in results)

        for uid in uids:
            found = find_item_by_uid(doc, uid)
            assert found is not None
            assert found[0]["_meta"]["status"] == "removed"

    def test_bulk_remove_bumps_revision_once(self, golden_result: dict) -> None:
        """AT-13: N bulk removes → exactly one revision increment."""
        doc = _fresh_doc(golden_result)
        rev_before = doc["_edit"]["revision"]
        uids = _all_uids(doc)[:10]

        bulk_op(doc, uids, "remove")

        rev_after = doc["_edit"]["revision"]
        assert rev_after == rev_before + 1, (
            f"Expected exactly one revision bump; got {rev_after - rev_before}"
        )

    def test_bulk_restore_reverses_remove(self, golden_result: dict) -> None:
        """bulk_op restore sets status=active."""
        doc = _fresh_doc(golden_result)
        uids = _all_uids(doc)[:3]

        bulk_op(doc, uids, "remove")
        results = bulk_op(doc, uids, "restore")

        assert all(r["ok"] for r in results)
        for uid in uids:
            found = find_item_by_uid(doc, uid)
            assert found is not None
            assert found[0]["_meta"]["status"] == "active"

    def test_bulk_remove_restore_two_revision_bumps(self, golden_result: dict) -> None:
        """Two separate bulk_op calls → two total revision bumps."""
        doc = _fresh_doc(golden_result)
        rev_start = doc["_edit"]["revision"]
        uids = _all_uids(doc)[:3]

        bulk_op(doc, uids, "remove")
        bulk_op(doc, uids, "restore")

        assert doc["_edit"]["revision"] == rev_start + 2

    def test_bulk_op_unknown_uid_returns_ok_false(self, golden_result: dict) -> None:
        """Unknown uid produces ok=False in results; known uids still succeed."""
        doc = _fresh_doc(golden_result)
        known = _all_uids(doc)[:2]
        fake = "itm_doesnotexist"
        uids = known + [fake]

        results = bulk_op(doc, uids, "remove")

        by_uid = {r["uid"]: r for r in results}
        assert by_uid[fake]["ok"] is False
        assert "not found" in by_uid[fake]["error"]
        for uid in known:
            assert by_uid[uid]["ok"] is True

    def test_bulk_op_unknown_uid_no_revision_bump_for_unchanged(
        self, golden_result: dict
    ) -> None:
        """If only unknown uids: no revision bump (nothing changed)."""
        doc = _fresh_doc(golden_result)
        rev_before = doc["_edit"]["revision"]

        results = bulk_op(doc, ["itm_fake1", "itm_fake2"], "remove")

        assert all(not r["ok"] for r in results)
        assert doc["_edit"]["revision"] == rev_before

    def test_bulk_op_empty_uids_returns_empty_results(self, golden_result: dict) -> None:
        """Empty uid list → empty results, no revision bump."""
        doc = _fresh_doc(golden_result)
        rev_before = doc["_edit"]["revision"]
        results = bulk_op(doc, [], "remove")
        assert results == []
        assert doc["_edit"]["revision"] == rev_before

    def test_bulk_op_invalid_op_raises(self, golden_result: dict) -> None:
        """Unknown op string raises ValueError."""
        doc = _fresh_doc(golden_result)
        with pytest.raises(ValueError, match="Unknown bulk op"):
            bulk_op(doc, [], "noop")

    def test_bulk_set_field_conflict_returns_ok_false(self, golden_result: dict) -> None:
        """set_field with wrong base_version → ok=False for that item."""
        doc = _fresh_doc(golden_result)
        uids = _all_uids(doc)[:2]

        # Correct version for uid[0], wrong for uid[1]
        item0 = find_item_by_uid(doc, uids[0])[0]
        item1 = find_item_by_uid(doc, uids[1])[0]
        v0 = item0["_meta"]["version"]
        v1_wrong = item1["_meta"]["version"] + 999

        results = bulk_op(
            doc,
            uids,
            "set_field",
            field="priority",
            value="must",
            base_versions={uids[0]: v0, uids[1]: v1_wrong},
        )

        by_uid = {r["uid"]: r for r in results}
        assert by_uid[uids[0]]["ok"] is True
        assert by_uid[uids[1]]["ok"] is False
        assert "version mismatch" in by_uid[uids[1]]["error"]

    def test_at13_500_items_single_revision_bump(self, golden_result: dict) -> None:
        """AT-13: 500 bulk removes → exactly one revision bump."""
        import copy as _copy
        doc = _fresh_doc(golden_result)

        # Inflate the first skill bucket to 500 items
        so = doc["skill_outputs"][0]
        original_item = so["items"][0]
        used = set(_all_uids(doc))

        from core.edit.service import _random_uid
        for i in range(500 - len(so["items"])):
            new_item = _copy.deepcopy(original_item)
            new_uid = _random_uid(used)
            used.add(new_uid)
            new_item["_meta"]["uid"] = new_uid
            new_item["id"] = f"TEST-{i+2:04d}"
            so["items"].append(new_item)

        uids = [item["_meta"]["uid"] for item in so["items"]]
        assert len(uids) == 500

        rev_before = doc["_edit"]["revision"]
        results = bulk_op(doc, uids, "remove")
        rev_after = doc["_edit"]["revision"]

        assert len(results) == 500
        assert all(r["ok"] for r in results)
        assert rev_after == rev_before + 1, (
            f"Expected exactly 1 revision bump for 500 items; got {rev_after - rev_before}"
        )


# ── Endpoint integration tests ────────────────────────────────────────────────


class TestBulkEndpoint:
    def _get_working_doc(self, tmp_path: Path, job_id: str) -> dict:
        path = tmp_path / job_id / "working.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _first_uid(
        self, client: TestClient, job_id: str, slug: str = "design-build"
    ) -> str:
        resp = client.get(f"/jobs/{job_id}")
        for so in resp.json()["result"]["skill_outputs"]:
            if so["skill_slug"] == slug:
                return so["items"][0]["_meta"]["uid"]
        raise ValueError(f"No items in {slug!r}")

    def test_bulk_remove_returns_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """POST /items/bulk op=remove returns results/edit_state/revision."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = self._first_uid(client, job_id)

        resp = client.post(
            f"/jobs/{job_id}/items/bulk",
            json={"uids": [uid], "op": "remove"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert "edit_state" in data
        assert "revision" in data
        assert len(data["results"]) == 1
        assert data["results"][0]["ok"] is True
        assert data["results"][0]["meta"]["status"] == "removed"

    def test_bulk_restore_returns_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """POST /items/bulk op=restore marks items active."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = self._first_uid(client, job_id)

        # Remove first
        client.post(f"/jobs/{job_id}/items/bulk", json={"uids": [uid], "op": "remove"})

        resp = client.post(
            f"/jobs/{job_id}/items/bulk",
            json={"uids": [uid], "op": "restore"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["results"][0]["ok"] is True
        assert data["results"][0]["meta"]["status"] == "active"

    def test_bulk_remove_single_file_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AT-13: bulk remove of N items results in exactly one revision bump."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)

        # Collect all uids via the GET endpoint
        resp = client.get(f"/jobs/{job_id}")
        data = resp.json()
        uids = []
        for so in data["result"]["skill_outputs"]:
            for item in so["items"]:
                uids.append(item["_meta"]["uid"])
        uids = uids[:10]

        # Get revision before
        doc_before = self._get_working_doc(tmp_path, job_id)
        rev_before = doc_before["_edit"]["revision"]

        resp = client.post(
            f"/jobs/{job_id}/items/bulk",
            json={"uids": uids, "op": "remove"},
        )
        assert resp.status_code == 200

        doc_after = self._get_working_doc(tmp_path, job_id)
        rev_after = doc_after["_edit"]["revision"]

        assert rev_after == rev_before + 1, (
            f"Expected exactly 1 revision bump for {len(uids)} items; got {rev_after - rev_before}"
        )

    def test_bulk_writes_log_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """POST /items/bulk appends one log entry per uid."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = self._first_uid(client, job_id)

        client.post(f"/jobs/{job_id}/items/bulk", json={"uids": [uid], "op": "remove"})

        log_path = tmp_path / job_id / "edit_log.jsonl"
        assert log_path.exists()
        entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        bulk_entries = [e for e in entries if e.get("op", "").startswith("bulk_")]
        assert len(bulk_entries) == 1
        assert bulk_entries[0]["uid"] == uid

    def test_bulk_empty_uids_returns_422(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Empty uids list → 422."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        resp = client.post(
            f"/jobs/{job_id}/items/bulk",
            json={"uids": [], "op": "remove"},
        )
        assert resp.status_code == 422

    def test_bulk_unknown_op_returns_422(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Unknown op → 422."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = self._first_uid(client, job_id)
        resp = client.post(
            f"/jobs/{job_id}/items/bulk",
            json={"uids": [uid], "op": "destroy"},
        )
        assert resp.status_code == 422

    def test_bulk_unknown_uid_ok_false_in_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Unknown uid returns ok=False in results; endpoint still 200."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        known_uid = self._first_uid(client, job_id)
        fake_uid = "itm_notexistent"

        resp = client.post(
            f"/jobs/{job_id}/items/bulk",
            json={"uids": [known_uid, fake_uid], "op": "remove"},
        )
        assert resp.status_code == 200
        data = resp.json()
        by_uid = {r["uid"]: r for r in data["results"]}
        assert by_uid[known_uid]["ok"] is True
        assert by_uid[fake_uid]["ok"] is False

    def test_bulk_job_not_found_returns_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Job not found → 404."""
        _, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f"/jobs/{fake_id}/items/bulk",
            json={"uids": ["itm_whatever"], "op": "remove"},
        )
        assert resp.status_code == 404
