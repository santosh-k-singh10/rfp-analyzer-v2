"""
tests/test_duplicate_warnings.py
---------------------------------
Tests for sub-task 2.2 — Duplicate Warning Helper.

Covers:
  AT-12  find_similar_to is O(n) single-item vs bucket; the all-pairs
         find_duplicate_candidates is never called from the edit path;
         2,000-item bucket completes quickly.
  - find_similar_to excludes the item itself and removed items; threshold.
  - add_item / update_item return possible_duplicate warnings (non-blocking).
  - POST / PATCH endpoints include warnings in the response.
"""

from __future__ import annotations

import time
import uuid

import pytest

import synthesizer.dedup as dedup
from core.edit.service import add_item, bootstrap_working_copy, update_item
from synthesizer.dedup import find_similar_to

from tests.test_edit_endpoints import _seed_complete_job


def _it(uid: str, title: str, desc: str = "", status: str = "active", id_: str = "") -> dict:
    return {
        "id": id_ or uid.upper(),
        "title": title,
        "description": desc,
        "_meta": {"uid": uid, "status": status},
    }


def _no_all_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("find_duplicate_candidates must not be called")

    monkeypatch.setattr(dedup, "find_duplicate_candidates", _boom)


_DB_FIELDS = {
    "type": "functional",
    "title": "Cloud Infrastructure Design",
    "description": "Design and build a resilient multi-region cloud infrastructure on IBM Cloud.",
    "source_section": "Section 3.1",
    "priority": "must",
    "confidence": 0.8,
    "ambiguity_flag": False,
}


# ── find_similar_to unit tests ────────────────────────────────────────────────


def test_find_similar_to_matches_near_duplicate() -> None:
    target = _it("itm_a", "Cloud infrastructure design", "Multi-region cloud on IBM Cloud")
    other = _it("itm_b", "Cloud infrastructure design", "Multi-region cloud on IBM Cloud.", id_="DB-002")
    unrelated = _it("itm_c", "Staff training plan", "Train operators in year one")
    res = find_similar_to(target, [target, other, unrelated])
    assert len(res) == 1
    assert res[0]["uid"] == "itm_b"
    assert res[0]["id"] == "DB-002"
    assert 0.9 <= res[0]["similarity"] <= 1.0


def test_find_similar_to_excludes_self_and_removed() -> None:
    target = _it("itm_a", "Same title", "Same description")
    self_copy = _it("itm_a", "Same title", "Same description")  # same uid, different object
    removed = _it("itm_r", "Same title", "Same description", status="removed")
    assert find_similar_to(target, [target, self_copy, removed]) == []


def test_find_similar_to_threshold_and_empty() -> None:
    target = _it("itm_a", "abcdefghij")
    other = _it("itm_b", "abcdefghxy")  # ratio 0.8
    assert find_similar_to(target, [other]) == []
    assert len(find_similar_to(target, [other], threshold=0.75)) == 1
    assert find_similar_to(_it("itm_e", ""), [other]) == []


def test_find_similar_to_matches_sequence_matcher_ratio() -> None:
    """Fast-path pruning must not change results vs plain ratio()."""
    import difflib

    target = _it("itm_a", "network security monitoring", "24x7 soc coverage")
    others = [
        _it(f"itm_{i}", t, d)
        for i, (t, d) in enumerate([
            ("network security monitoring", "24x7 soc coverage!"),
            ("network monitoring", "soc"),
            ("security monitoring network", "coverage 24x7 soc"),
        ])
    ]
    fp = dedup._item_fingerprint(target)
    expected = {
        o["_meta"]["uid"]
        for o in others
        if difflib.SequenceMatcher(None, fp, dedup._item_fingerprint(o)).ratio() >= 0.9
    }
    got = {m["uid"] for m in find_similar_to(target, others)}
    assert got == expected


def test_at12_large_bucket_is_fast_and_never_calls_all_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AT-12: 2,000-item bucket; single-item comparison; all-pairs never called."""
    _no_all_pairs(monkeypatch)
    items = [
        _it(f"itm_{i:012x}", f"Requirement number {i} for module {i % 37}",
            f"Detailed description {i * 7919} of the deliverable")
        for i in range(2000)
    ]
    target = _it("itm_new", "Requirement number 5 for module 5",
                 "Detailed description 39595 of the deliverable")
    t0 = time.perf_counter()
    res = find_similar_to(target, items)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"find_similar_to took {elapsed:.2f}s"
    assert any(m["uid"] == f"itm_{5:012x}" for m in res)


# ── Service integration ───────────────────────────────────────────────────────


def test_add_item_returns_duplicate_warning(
    golden_result: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_all_pairs(monkeypatch)
    doc = bootstrap_working_copy(golden_result, str(uuid.uuid4()))
    item, warnings = add_item(doc, "design-build", dict(_DB_FIELDS))
    dups = [w for w in warnings if isinstance(w, dict)]
    assert dups and dups[0]["type"] == "possible_duplicate"
    assert dups[0]["id"] == "DB-001"
    assert dups[0]["uid"] != item["_meta"]["uid"]


def test_add_item_no_warning_when_distinct(golden_result: dict) -> None:
    doc = bootstrap_working_copy(golden_result, str(uuid.uuid4()))
    fields = dict(_DB_FIELDS, title="Quantum widget polishing", description="Polish widgets")
    _, warnings = add_item(doc, "design-build", fields)
    assert not [w for w in warnings if isinstance(w, dict)]


def test_add_item_ignores_removed_duplicate(golden_result: dict) -> None:
    doc = bootstrap_working_copy(golden_result, str(uuid.uuid4()))
    db = next(so for so in doc["skill_outputs"] if so["skill_slug"] == "design-build")
    db["items"][0]["_meta"]["status"] = "removed"
    _, warnings = add_item(doc, "design-build", dict(_DB_FIELDS))
    assert not [w for w in warnings if isinstance(w, dict) and w["id"] == "DB-001"]


def test_update_item_warns_only_when_text_changes(
    golden_result: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_all_pairs(monkeypatch)
    doc = bootstrap_working_copy(golden_result, str(uuid.uuid4()))
    db = next(so for so in doc["skill_outputs"] if so["skill_slug"] == "design-build")
    first, second = db["items"][0], db["items"][1]
    uid = second["_meta"]["uid"]

    # Non-text change → no duplicate check
    _, warnings = update_item(doc, uid, {"priority": "should"}, second["_meta"]["version"])
    assert not [w for w in warnings if isinstance(w, dict)]

    # Text change that makes it a duplicate of DB-001 → warning, never itself
    _, warnings = update_item(
        doc, uid,
        {"title": first["title"], "description": first["description"]},
        second["_meta"]["version"],
    )
    dups = [w for w in warnings if isinstance(w, dict)]
    assert [d["id"] for d in dups] == ["DB-001"]
    assert all(d["uid"] != uid for d in dups)


# ── Endpoints ─────────────────────────────────────────────────────────────────


def test_post_items_response_includes_duplicate_warning(
    tmp_path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    _no_all_pairs(monkeypatch)
    resp = client.post(
        f"/jobs/{job_id}/items", json={"bucket": "design-build", "fields": dict(_DB_FIELDS)}
    )
    assert resp.status_code == 200
    warnings = resp.json()["warnings"]
    assert warnings and warnings[0]["type"] == "possible_duplicate"
    assert warnings[0]["id"] == "DB-001"
    assert set(warnings[0]) == {"type", "uid", "id", "similarity"}


def test_post_items_no_warnings_when_distinct(
    tmp_path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result)
    fields = dict(_DB_FIELDS, title="Quantum widget polishing", description="Polish widgets")
    resp = client.post(f"/jobs/{job_id}/items", json={"bucket": "design-build", "fields": fields})
    assert resp.status_code == 200
    assert resp.json()["warnings"] is None


def test_patch_item_response_includes_duplicate_warning(
    tmp_path, monkeypatch: pytest.MonkeyPatch, golden_result: dict
) -> None:
    job_id, client = _seed_complete_job(tmp_path, monkeypatch, golden_result, with_working=True)
    _no_all_pairs(monkeypatch)
    data = client.get(f"/jobs/{job_id}").json()
    db = next(so for so in data["result"]["skill_outputs"] if so["skill_slug"] == "design-build")
    second = db["items"][1]
    resp = client.patch(
        f"/jobs/{job_id}/items/{second['_meta']['uid']}",
        json={
            "base_version": second["_meta"]["version"],
            "fields": {"title": _DB_FIELDS["title"], "description": _DB_FIELDS["description"]},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["item"]["_meta"]["version"] == second["_meta"]["version"] + 1  # non-blocking
    assert [w["id"] for w in body["warnings"]] == ["DB-001"]
