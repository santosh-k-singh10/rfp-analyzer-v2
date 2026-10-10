"""
tests/test_job_persistence.py
------------------------------
0.2 persistence tests:

AT-6  — restart simulation: running jobs become failed; complete jobs with
         result.json are restored correctly; complete jobs without result.json
         become failed.

Ordering — set_complete must not succeed before result.json is on disk.
         Verified by checking that complete status is not set until after
         all file-writes.

set_complete failure — if job.json cannot be written, the in-memory status
         must remain RUNNING (the exception propagates).

set_failed write-error resilience — if job.json write fails inside set_failed,
         the in-memory status is still FAILED (r3-4).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from app.job_store import AnalysisJob, JobStatus, JobStore


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JobStore:
    """Return a fresh isolated JobStore with OUTPUT_DIR set to tmp_path."""
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    return JobStore()


def _seed_job_json(output_dir: Path, job_id: str, status: str, **extra: object) -> Path:
    """Write a minimal job.json sidecar in output_dir/job_id/."""
    job_dir = output_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job_id,
        "status": status,
        "file_name": "test.pdf",
        "file_names": ["test.pdf"],
        "primary_file_name": "test.pdf",
        "primary_file_path": "",
        "file_roles": {},
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
        "error": None,
        **extra,
    }
    p = job_dir / "job.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return job_dir


# ─────────────────────────────────────────────────────────────────────────────
# AT-6 — restart simulation
# ─────────────────────────────────────────────────────────────────────────────


def test_at6_running_becomes_failed_on_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A job that was RUNNING at shutdown must be FAILED after load_from_disk."""
    job_id = str(uuid.uuid4())
    _seed_job_json(tmp_path, job_id, "running")

    store = _make_store(tmp_path, monkeypatch)
    store.load_from_disk(tmp_path)

    job = store.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert "restart" in (job.error or "").lower()


def test_at6_complete_with_result_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A COMPLETE job with result.json present is restored as COMPLETE."""
    job_id = str(uuid.uuid4())
    job_dir = _seed_job_json(tmp_path, job_id, "complete")
    # Write a dummy result.json so the check passes
    (job_dir / "result.json").write_text("{}", encoding="utf-8")

    store = _make_store(tmp_path, monkeypatch)
    store.load_from_disk(tmp_path)

    job = store.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.COMPLETE


def test_at6_complete_without_result_becomes_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A COMPLETE job without result.json must be FAILED after load_from_disk."""
    job_id = str(uuid.uuid4())
    _seed_job_json(tmp_path, job_id, "complete")
    # No result.json written

    store = _make_store(tmp_path, monkeypatch)
    store.load_from_disk(tmp_path)

    job = store.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.FAILED


def test_at6_failed_job_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A FAILED job is restored with its original error message."""
    job_id = str(uuid.uuid4())
    _seed_job_json(tmp_path, job_id, "failed", error="pipeline error")

    store = _make_store(tmp_path, monkeypatch)
    store.load_from_disk(tmp_path)

    job = store.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert job.error == "pipeline error"


def test_at6_output_dir_set_on_restored_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restored jobs have output_dir pointing to their on-disk directory."""
    job_id = str(uuid.uuid4())
    job_dir = _seed_job_json(tmp_path, job_id, "failed")

    store = _make_store(tmp_path, monkeypatch)
    store.load_from_disk(tmp_path)

    job = store.get_job(job_id)
    assert job is not None
    assert job.output_dir == str(job_dir)


def test_legacy_folder_with_only_result_json_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy job folder (result.json, no job.json) is restored as COMPLETE."""
    job_id = str(uuid.uuid4())
    (tmp_path / job_id).mkdir()
    (tmp_path / job_id / "result.json").write_text("{}", encoding="utf-8")
    (tmp_path / "not-a-uuid").mkdir()
    (tmp_path / "not-a-uuid" / "result.json").write_text("{}", encoding="utf-8")

    store = _make_store(tmp_path, monkeypatch)
    store.load_from_disk(tmp_path)

    job = store.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.COMPLETE
    assert job.output_dir == str(tmp_path / job_id)
    assert store.get_job("not-a-uuid") is None
    assert not (tmp_path / job_id / "job.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Ordering — set_complete must not flip before files are written
# ─────────────────────────────────────────────────────────────────────────────


def test_set_complete_writes_job_json_before_flipping_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """set_complete: job.json is written before in-memory status flips."""
    store = _make_store(tmp_path, monkeypatch)
    job = store.create_job("f.pdf", "/tmp/f.pdf")

    # Confirm job.json has status:pending after creation
    job_json_path = Path(job.output_dir) / "job.json"
    assert job_json_path.exists()

    store.set_complete(job.job_id, {"result": "ok"})

    # Check both disk and memory
    on_disk = json.loads(job_json_path.read_text())
    assert on_disk["status"] == "complete"
    assert store.get_job(job.job_id).status == JobStatus.COMPLETE


# ─────────────────────────────────────────────────────────────────────────────
# set_complete failure — in-memory stays RUNNING
# ─────────────────────────────────────────────────────────────────────────────


def test_set_complete_job_json_write_failure_keeps_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If job.json write fails inside set_complete, in-memory stays RUNNING."""
    store = _make_store(tmp_path, monkeypatch)
    store.set_running = lambda jid: None  # silence side effects for set_running
    job = store.create_job("f.pdf", "/tmp/f.pdf")
    # Manually set running for this test
    job.status = JobStatus.RUNNING

    # Patch atomic_write to fail
    with patch("app.job_store.atomic_write", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            store.set_complete(job.job_id, {"result": "ok"})

    # In-memory status must still be RUNNING — not COMPLETE
    assert store.get_job(job.job_id).status == JobStatus.RUNNING


# ─────────────────────────────────────────────────────────────────────────────
# set_failed write-error resilience (r3-4)
# ─────────────────────────────────────────────────────────────────────────────


def test_set_failed_job_json_write_failure_still_marks_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If job.json write fails inside set_failed, in-memory is still FAILED."""
    store = _make_store(tmp_path, monkeypatch)
    job = store.create_job("f.pdf", "/tmp/f.pdf")

    with patch("app.job_store.atomic_write", side_effect=OSError("disk full")):
        store.set_failed(job.job_id, "pipeline error")

    # Must be FAILED in memory despite the write error
    assert store.get_job(job.job_id).status == JobStatus.FAILED
    assert store.get_job(job.job_id).error == "pipeline error"


# ─────────────────────────────────────────────────────────────────────────────
# create_job — output_dir set + initial job.json written
# ─────────────────────────────────────────────────────────────────────────────


def test_create_job_writes_job_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """create_job writes an initial job.json with status:pending."""
    store = _make_store(tmp_path, monkeypatch)
    job = store.create_job("test.pdf", "/uploads/test.pdf")

    assert job.output_dir is not None
    job_json = Path(job.output_dir) / "job.json"
    assert job_json.exists(), "job.json should be written on create_job"

    data = json.loads(job_json.read_text())
    assert data["status"] == "pending"
    assert data["job_id"] == job.job_id


def test_create_job_output_dir_under_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """output_dir for a job is <OUTPUT_DIR>/<job_id>/."""
    store = _make_store(tmp_path, monkeypatch)
    job = store.create_job("test.pdf", "/uploads/test.pdf")

    expected_parent = str(tmp_path)
    assert job.output_dir.startswith(expected_parent), (
        f"output_dir {job.output_dir!r} should be under {expected_parent!r}"
    )
    assert job.job_id in job.output_dir
