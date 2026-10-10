"""
tests/test_enhance.py
----------------------
Tests for AT-7 — Enhancement Prompt and Orchestration (sub-task 2.3).

Covers:
  - enhance_item: unverified numbers flagged
  - enhance_item: proposal touching protected fields (priority) is rejected
  - enhance_item: LLM [] → raises EnhancementFailed
  - enhance_item: user-origin item without raw_excerpt → only clarify_wording allowed
  - POST .../enhance: 502 when LLM returns []
  - POST .../enhance: lock not held during LLM call (confirmed by ordering)
  - POST .../enhance: nothing persisted (no working.json change)
  - POST .../enhance/accept: persists proposal, sets enhanced=True, logs "enhance"
  - POST .../enhance/accept: 409 on version mismatch (stale base_version)
  - POST .../enhance/accept: invalid field (non-editable) → 422
  - build_enhance_prompt: all four modes generate (system, user) tuple
  - build_enhance_prompt: unknown mode raises ValueError
  - build_enhance_prompt: custom mode requires instruction
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from core.edit.enhance import (
    EnhanceProposal,
    EnhancementFailed,
    _check_unverified_numbers,
    _digit_tokens,
    _validate_proposed,
    enhance_item,
)
from core.edit.service import bootstrap_working_copy, find_item_by_uid
from core.prompts.enhance import ENHANCE_MODES, build_enhance_prompt


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _seed_complete_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden: dict,
    *,
    with_working: bool = True,
) -> tuple[str, TestClient]:
    """Create a complete job on disk and return (job_id, TestClient)."""
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


def _first_skill_uid(client: TestClient, job_id: str, slug: str = "design-build") -> str:
    resp = client.get(f"/jobs/{job_id}")
    data = resp.json()
    for so in data["result"]["skill_outputs"]:
        if so["skill_slug"] == slug:
            return so["items"][0]["_meta"]["uid"]
    raise ValueError(f"No items in {slug!r}")


def _make_ai_item(**overrides) -> dict:
    base = {
        "id": "DB-001",
        "title": "Cloud Infrastructure Design",
        "description": "Design a multi-region cloud infrastructure.",
        "type": "functional",
        "priority": "must",
        "confidence": 0.92,
        "source_section": "Section 3.1",
        "source_document": "RFP_main.pdf",
        "document_role": "primary",
        "raw_excerpt": "The vendor shall design and deploy a multi-region cloud infrastructure.",
        "_meta": {
            "uid": "itm_abc123def456",
            "origin": "ai",
            "status": "active",
            "version": 1,
            "edited": False,
            "edited_fields": [],
            "excerpt_edited": False,
            "enhanced": False,
        },
    }
    base.update(overrides)
    return base


def _good_llm_response(proposed: dict, rationale: str = "Improved wording.") -> list[dict]:
    return [{"proposed": proposed, "rationale": rationale, "needs_info": []}]


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: core/prompts/enhance.py
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildEnhancePrompt:
    def test_clarify_wording_returns_tuple(self):
        item = _make_ai_item()
        system, user = build_enhance_prompt(item, "clarify_wording")
        assert isinstance(system, str) and len(system) > 50
        assert isinstance(user, str) and "REQUIREMENT ITEM" in user

    def test_measurable_includes_item_json(self):
        item = _make_ai_item()
        _, user = build_enhance_prompt(item, "measurable")
        assert "Cloud Infrastructure Design" in user

    def test_clarification_question_mode(self):
        item = _make_ai_item()
        system, user = build_enhance_prompt(item, "clarification_question")
        assert "clarification_question" in system.lower()

    def test_custom_mode_includes_instruction(self):
        item = _make_ai_item()
        _, user = build_enhance_prompt(item, "custom", instruction="Make it SMART.")
        assert "Make it SMART." in user

    def test_custom_mode_requires_instruction(self):
        item = _make_ai_item()
        with pytest.raises(ValueError, match="instruction.*required"):
            build_enhance_prompt(item, "custom")

    def test_unknown_mode_raises(self):
        item = _make_ai_item()
        with pytest.raises(ValueError, match="Unknown enhance mode"):
            build_enhance_prompt(item, "nonexistent_mode")

    def test_meta_not_in_user_content(self):
        """_meta internals should not be in the item JSON sent to the LLM."""
        item = _make_ai_item()
        _, user = build_enhance_prompt(item, "clarify_wording")
        # _meta block should not appear in user content
        assert '"_meta"' not in user

    def test_all_modes_covered(self):
        item = _make_ai_item()
        for mode in ENHANCE_MODES:
            instruction = "Do something." if mode == "custom" else None
            system, user = build_enhance_prompt(item, mode, instruction=instruction)
            assert system
            assert user


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: core/edit/enhance.py — helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestDigitTokens:
    def test_simple_number(self):
        # The regex captures "200ms" as a single digit-bearing token
        tokens = _digit_tokens("respond within 200ms")
        assert any("200" in t for t in tokens)

    def test_percentage(self):
        assert "99.9%" in _digit_tokens("availability of 99.9%")

    def test_no_digits(self):
        assert _digit_tokens("no numbers here") == set()


class TestValidateProposed:
    def test_allowed_fields_pass(self):
        proposed = {"title": "New title", "description": "New desc"}
        result = _validate_proposed(proposed, "clarify_wording")
        assert result == proposed

    def test_forbidden_field_raises(self):
        """priority is not in clarify_wording's allowed set → ValueError."""
        proposed = {"title": "OK", "priority": "should"}
        with pytest.raises(ValueError, match="fields outside allowed set"):
            _validate_proposed(proposed, "clarify_wording")

    def test_only_allowed_fields_returned(self):
        """Fields not in mode's set are rejected (not silently dropped)."""
        proposed = {"title": "T", "confidence": 0.5}
        with pytest.raises(ValueError):
            _validate_proposed(proposed, "clarify_wording")


class TestCheckUnverifiedNumbers:
    def test_number_from_excerpt_is_verified(self):
        item = _make_ai_item(
            description="Response within 200ms",
            raw_excerpt="P99 latency must not exceed 200ms.",
        )
        proposed = {"description": "Response time must be under 200ms at P99."}
        unverified = _check_unverified_numbers(proposed, item, instruction=None)
        assert "200ms" not in unverified

    def test_new_number_not_in_excerpt_is_unverified(self):
        item = _make_ai_item(
            description="Cloud infrastructure design.",
            raw_excerpt="The vendor shall design and deploy infrastructure.",
        )
        proposed = {"description": "Infrastructure with 500TB storage capacity."}
        unverified = _check_unverified_numbers(proposed, item, instruction=None)
        assert len(unverified) > 0
        assert any("500" in t for t in unverified)

    def test_number_in_instruction_is_verified(self):
        item = _make_ai_item(raw_excerpt="Deploy infrastructure.")
        proposed = {"description": "Must handle 1000 concurrent users."}
        unverified = _check_unverified_numbers(
            proposed, item, instruction="Target: 1000 concurrent users"
        )
        assert not unverified

    def test_no_numbers_no_unverified(self):
        item = _make_ai_item()
        proposed = {"description": "Improve the design for better clarity."}
        unverified = _check_unverified_numbers(proposed, item, instruction=None)
        assert unverified == []


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: enhance_item() orchestration
# ─────────────────────────────────────────────────────────────────────────────


class TestEnhanceItemUnit:
    """Tests for enhance_item() with mocked LLM."""

    def test_successful_enhance_returns_proposal(self):
        item = _make_ai_item()
        llm_resp = _good_llm_response({"title": "Clearer wording for cloud infra."})

        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=llm_resp)
        ):
            proposal = asyncio.get_event_loop().run_until_complete(
                enhance_item(item, "clarify_wording")
            )

        assert isinstance(proposal, EnhanceProposal)
        assert proposal.proposed == {"title": "Clearer wording for cloud infra."}
        assert proposal.mode == "clarify_wording"
        assert proposal.needs_info == []

    def test_llm_empty_raises_enhancement_failed(self):
        """AT-7: LLM [] → raises EnhancementFailed."""
        item = _make_ai_item()

        with patch("core.edit.enhance.call_llm_async", new=AsyncMock(return_value=[])):
            with pytest.raises(EnhancementFailed):
                asyncio.get_event_loop().run_until_complete(
                    enhance_item(item, "clarify_wording")
                )

    def test_proposed_priority_is_rejected(self):
        """AT-7: proposal touching priority → rejected (ValueError from _validate_proposed)."""
        item = _make_ai_item()
        llm_resp = [{"proposed": {"priority": "should"}, "rationale": "x", "needs_info": []}]

        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=llm_resp)
        ):
            with pytest.raises(ValueError, match="fields outside allowed set"):
                asyncio.get_event_loop().run_until_complete(
                    enhance_item(item, "clarify_wording")
                )

    def test_unverified_numbers_are_flagged(self):
        """AT-7: number not in excerpt/instruction → unverified_numbers populated."""
        item = _make_ai_item(
            raw_excerpt="The vendor shall design and deploy a multi-region cloud infrastructure."
        )
        llm_resp = _good_llm_response(
            {"description": "Must support 500TB of storage per node."}
        )

        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=llm_resp)
        ):
            proposal = asyncio.get_event_loop().run_until_complete(
                enhance_item(item, "clarify_wording")
            )

        assert len(proposal.unverified_numbers) > 0

    def test_verified_numbers_not_flagged(self):
        """Numbers already in the item are NOT flagged as unverified."""
        item = _make_ai_item(
            description="Response time within 200ms.",
            raw_excerpt="P99 latency must not exceed 200ms for all API calls.",
        )
        llm_resp = _good_llm_response(
            {"description": "P99 latency must not exceed 200ms for all API responses."}
        )

        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=llm_resp)
        ):
            proposal = asyncio.get_event_loop().run_until_complete(
                enhance_item(item, "clarify_wording")
            )

        assert proposal.unverified_numbers == []

    def test_user_origin_no_excerpt_non_clarify_raises(self):
        """AT-7: user-origin item without raw_excerpt + non-clarify_wording mode → 400."""
        item = _make_ai_item(raw_excerpt=None)
        item["_meta"]["origin"] = "user"

        with pytest.raises(ValueError, match="clarify_wording"):
            asyncio.get_event_loop().run_until_complete(
                enhance_item(item, "measurable")
            )

    def test_user_origin_no_excerpt_clarify_wording_allowed(self):
        """AT-7: user-origin item without raw_excerpt + clarify_wording is allowed."""
        item = _make_ai_item(raw_excerpt=None)
        item["_meta"]["origin"] = "user"
        llm_resp = _good_llm_response({"title": "Clearer title."})

        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=llm_resp)
        ):
            proposal = asyncio.get_event_loop().run_until_complete(
                enhance_item(item, "clarify_wording")
            )
        assert proposal.mode == "clarify_wording"

    def test_custom_mode_without_instruction_raises(self):
        item = _make_ai_item()
        with pytest.raises(ValueError, match="instruction.*required"):
            asyncio.get_event_loop().run_until_complete(
                enhance_item(item, "custom")  # no instruction
            )

    def test_needs_info_returned_in_proposal(self):
        item = _make_ai_item()
        llm_resp = [
            {
                "proposed": {"clarification_question": "What is the target SLA?"},
                "rationale": "Metric missing.",
                "needs_info": ["What availability is required?"],
            }
        ]

        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=llm_resp)
        ):
            proposal = asyncio.get_event_loop().run_until_complete(
                enhance_item(item, "measurable")
            )

        assert "What availability is required?" in proposal.needs_info

    def test_proposal_to_dict_is_serialisable(self):
        item = _make_ai_item()
        llm_resp = _good_llm_response({"title": "Better title."})

        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=llm_resp)
        ):
            proposal = asyncio.get_event_loop().run_until_complete(
                enhance_item(item, "clarify_wording")
            )

        d = proposal.to_dict()
        assert json.dumps(d)  # must be JSON-serialisable
        assert "proposed" in d
        assert "rationale" in d
        assert "needs_info" in d
        assert "unverified_numbers" in d
        assert "mode" in d


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests: POST .../enhance endpoint
# ─────────────────────────────────────────────────────────────────────────────


class TestEnhanceEndpoint:
    """Integration tests for POST /jobs/{id}/items/{uid}/enhance."""

    def test_enhance_returns_proposal_not_persisted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AT-7: proposal is returned but nothing is written to disk."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        working_path = tmp_path / job_id / "working.json"
        mtime_before = working_path.stat().st_mtime

        llm_resp = _good_llm_response({"title": "Enhanced cloud infrastructure."})
        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=llm_resp)
        ):
            resp = client.post(
                f"/jobs/{job_id}/items/{uid}/enhance",
                json={"mode": "clarify_wording"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["uid"] == uid
        assert "proposal" in data
        assert "proposed" in data["proposal"]

        # File must NOT have been modified
        mtime_after = working_path.stat().st_mtime
        assert mtime_before == mtime_after, "working.json must not be modified by enhance"

    def test_enhance_502_on_llm_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AT-7: LLM returns [] → 502."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=[])
        ):
            resp = client.post(
                f"/jobs/{job_id}/items/{uid}/enhance",
                json={"mode": "clarify_wording"},
            )

        assert resp.status_code == 502
        assert resp.json()["detail"]["error"] == "enhancement_failed"

    def test_enhance_lock_not_held_during_llm_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AT-7: the per-job lock must NOT be held when call_llm_async is running.

        We verify this by checking that the lock is unlocked at the moment
        call_llm_async is invoked.
        """
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        lock_held_during_llm: list[bool] = []

        import app.routers.edits as edits_mod
        repo = edits_mod._repo
        lock = repo.lock_for(job_id)

        async def fake_llm_async(*args, **kwargs):
            # Capture whether the lock is locked at LLM call time
            lock_held_during_llm.append(lock.locked())
            return _good_llm_response({"title": "Enhanced."})

        with patch("core.edit.enhance.call_llm_async", new=fake_llm_async):
            resp = client.post(
                f"/jobs/{job_id}/items/{uid}/enhance",
                json={"mode": "clarify_wording"},
            )

        assert resp.status_code == 200
        assert lock_held_during_llm == [False], (
            "Lock must NOT be held during the LLM call"
        )

    def test_enhance_400_for_user_item_without_excerpt_non_clarify(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AT-7: user-origin item without raw_excerpt + non-clarify_wording mode → 400."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)

        # Add a user-origin item without raw_excerpt
        resp = client.post(
            f"/jobs/{job_id}/items",
            json={
                "bucket": "design-build",
                "fields": {
                    "type": "functional",
                    "title": "User added item",
                    "description": "No excerpt.",
                    "source_section": "Sec 1",
                    "priority": "must",
                    "confidence": 0.8,
                    "ambiguity_flag": False,
                },
            },
        )
        assert resp.status_code == 200
        uid = resp.json()["item"]["_meta"]["uid"]

        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=[])
        ):
            resp2 = client.post(
                f"/jobs/{job_id}/items/{uid}/enhance",
                json={"mode": "measurable"},
            )

        assert resp2.status_code == 400
        assert "clarify_wording" in resp2.json()["detail"]

    def test_enhance_returns_base_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Proposal includes base_version for use in accept step."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        llm_resp = _good_llm_response({"title": "Better title."})
        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=llm_resp)
        ):
            resp = client.post(
                f"/jobs/{job_id}/items/{uid}/enhance",
                json={"mode": "clarify_wording"},
            )

        assert resp.status_code == 200
        assert resp.json()["base_version"] == 1

    def test_enhance_unknown_mode_returns_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Unknown mode → 400."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        resp = client.post(
            f"/jobs/{job_id}/items/{uid}/enhance",
            json={"mode": "nonexistent_mode"},
        )
        assert resp.status_code == 400

    def test_enhance_unknown_uid_returns_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)

        resp = client.post(
            f"/jobs/{job_id}/items/itm_doesnotexist/enhance",
            json={"mode": "clarify_wording"},
        )
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests: POST .../enhance/accept endpoint
# ─────────────────────────────────────────────────────────────────────────────


class TestEnhanceAcceptEndpoint:
    """Integration tests for POST /jobs/{id}/items/{uid}/enhance/accept."""

    def _propose_then_accept(
        self,
        client: TestClient,
        job_id: str,
        uid: str,
        proposed: dict,
    ) -> dict:
        """Helper: return accept response JSON."""
        resp = client.post(
            f"/jobs/{job_id}/items/{uid}/enhance/accept",
            json={
                "base_version": 1,
                "proposal": {
                    "proposed": proposed,
                    "rationale": "Improved wording.",
                    "needs_info": [],
                    "unverified_numbers": [],
                    "mode": "clarify_wording",
                },
            },
        )
        return resp

    def test_accept_persists_fields_and_sets_enhanced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AT-7: accept persists the proposal and sets _meta.enhanced=True."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        resp = self._propose_then_accept(
            client, job_id, uid, {"title": "AI-enhanced cloud infra title."}
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["item"]["title"] == "AI-enhanced cloud infra title."
        assert data["item"]["_meta"]["enhanced"] is True
        assert data["item"]["_meta"]["edited"] is True
        assert "edit_state" in data

    def test_accept_version_bumped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """After accept the item version is incremented."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        resp = self._propose_then_accept(
            client, job_id, uid, {"title": "New title."}
        )
        assert resp.json()["item"]["_meta"]["version"] == 2

    def test_accept_logs_enhance_operation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AT-7: accept writes a log entry with op='enhance'."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        self._propose_then_accept(client, job_id, uid, {"title": "Logged title."})

        log_path = tmp_path / job_id / "edit_log.jsonl"
        assert log_path.exists()
        entries = [
            json.loads(line)
            for line in log_path.read_text().splitlines()
            if line.strip()
        ]
        enhance_entries = [e for e in entries if e.get("op") == "enhance"]
        assert len(enhance_entries) == 1
        assert enhance_entries[0]["uid"] == uid

    def test_accept_409_on_stale_base_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AT-7: 409 on version mismatch."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        # Bump the item's version with a regular patch first
        client.patch(
            f"/jobs/{job_id}/items/{uid}",
            json={"base_version": 1, "fields": {"title": "Interim edit."}},
        )

        # Now accept with stale base_version=1 (item is now version 2)
        resp = client.post(
            f"/jobs/{job_id}/items/{uid}/enhance/accept",
            json={
                "base_version": 1,  # stale
                "proposal": {
                    "proposed": {"title": "Should fail."},
                    "rationale": "x",
                    "needs_info": [],
                    "unverified_numbers": [],
                    "mode": "clarify_wording",
                },
            },
        )

        assert resp.status_code == 409
        assert "version mismatch" in resp.json()["detail"]["msg"]

    def test_accept_422_on_invalid_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AT-7: accept with a non-editable field returns 422."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        resp = client.post(
            f"/jobs/{job_id}/items/{uid}/enhance/accept",
            json={
                "base_version": 1,
                "proposal": {
                    "proposed": {"source_document": "hacked.pdf"},  # non-editable
                    "rationale": "x",
                    "needs_info": [],
                    "unverified_numbers": [],
                    "mode": "clarify_wording",
                },
            },
        )

        # source_document is not editable → 422
        assert resp.status_code == 422

    def test_accept_response_shape_matches_mutation_response(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Accept response matches MutationResponse shape (r3-1)."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        resp = self._propose_then_accept(
            client, job_id, uid, {"description": "Clearer description."}
        )

        assert resp.status_code == 200
        data = resp.json()
        # Must have item with _meta.version and _meta.status
        assert "_meta" in data["item"]
        assert "version" in data["item"]["_meta"]
        assert "status" in data["item"]["_meta"]
        # Must have edit_state
        assert "edit_state" in data
        for k in ("active", "removed", "user_added", "edited", "has_edits", "revision"):
            assert k in data["edit_state"], f"edit_state missing key: {k}"

    def test_accept_persists_to_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """Accept actually writes to working.json."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        working_path = tmp_path / job_id / "working.json"

        self._propose_then_accept(
            client, job_id, uid, {"title": "Persisted title."}
        )

        # Re-read from disk
        doc = json.loads(working_path.read_text(encoding="utf-8"))
        result = find_item_by_uid(doc, uid)
        assert result is not None
        item, _ = result
        assert item["title"] == "Persisted title."
        assert item["_meta"]["enhanced"] is True

    def test_accept_unknown_uid_returns_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)

        resp = client.post(
            f"/jobs/{job_id}/items/itm_doesnotexist/enhance/accept",
            json={
                "base_version": 1,
                "proposal": {"proposed": {}, "rationale": "", "needs_info": [], "unverified_numbers": [], "mode": "clarify_wording"},
            },
        )
        assert resp.status_code == 404

    def test_enhance_then_accept_full_flow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
    ) -> None:
        """AT-7: end-to-end: propose → accept → verify persisted."""
        job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
        uid = _first_skill_uid(client, job_id)

        # Step 1: propose
        llm_resp = _good_llm_response({"title": "E2E enhanced title."})
        with patch(
            "core.edit.enhance.call_llm_async", new=AsyncMock(return_value=llm_resp)
        ):
            propose_resp = client.post(
                f"/jobs/{job_id}/items/{uid}/enhance",
                json={"mode": "clarify_wording"},
            )

        assert propose_resp.status_code == 200
        proposal_data = propose_resp.json()
        base_version = proposal_data["base_version"]

        # Verify NOT yet persisted
        working_path = tmp_path / job_id / "working.json"
        doc_before = json.loads(working_path.read_text(encoding="utf-8"))
        item_before, _ = find_item_by_uid(doc_before, uid)
        assert item_before["title"] != "E2E enhanced title."

        # Step 2: accept
        accept_resp = client.post(
            f"/jobs/{job_id}/items/{uid}/enhance/accept",
            json={
                "base_version": base_version,
                "proposal": proposal_data["proposal"],
            },
        )

        assert accept_resp.status_code == 200
        assert accept_resp.json()["item"]["_meta"]["enhanced"] is True

        # Step 3: verify persisted
        doc_after = json.loads(working_path.read_text(encoding="utf-8"))
        item_after, _ = find_item_by_uid(doc_after, uid)
        assert item_after["title"] == "E2E enhanced title."
        assert item_after["_meta"]["enhanced"] is True
