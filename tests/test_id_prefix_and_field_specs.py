"""
tests/test_id_prefix_and_field_specs.py
----------------------------------------
Tests for Sub-Task 1.1:
- AT-14: AMS ID prefix extraction preserves exact output.
- get_prefix covers all skill, spine, and AMS prefixes.
- Spine field-spec consistency probe test (_sanitise keeps all editable fields).
- get_field_spec for skills (from schema.yaml) and spine (from static dict).
- GET /jobs/{id}/fields endpoint testing and path validation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.edit.field_specs import FieldSpec, get_field_spec
from core.edit.id_prefix import (
    ALL_AMS_PREFIXES,
    get_ams_prefix,
    get_prefix,
)
from core.schemas import DocumentChunk
from skills_engine.ams import _assign_ids
import spine.ambiguity as spine_ambiguity
import spine.commercial_structure as spine_commercial_structure
import spine.compliance as spine_compliance
import spine.risk as spine_risk


# ── AT-14: AMS ID Output Unchanged ───────────────────────────────────────────


def test_ams_assign_ids_unchanged() -> None:
    """Verify that _assign_ids produces exact expected IDs for all AMS artifacts."""
    test_items = [
        # Req classification L1, L2, L3
        {"artifact_type": "req_classification", "support_level": "L1", "title": "Item 1"},
        {"artifact_type": "req_classification", "support_level": "L2", "title": "Item 2"},
        {"artifact_type": "req_classification", "support_level": "L3", "title": "Item 3"},
        # SLA targets
        {"artifact_type": "sla_target", "title": "SLA 1"},
        {"artifact_type": "availability", "title": "SLA 2"},
        {"artifact_type": "sr_change_sla", "title": "SLA 3"},
        # Application, Volume, Tool, Governance, Deliverable, Resource
        {"artifact_type": "overview_app", "title": "App 1"},
        {"artifact_type": "ticket_volume", "title": "Vol 1"},
        {"artifact_type": "tool", "title": "Tool 1"},
        {"artifact_type": "governance", "title": "Gov 1"},
        {"artifact_type": "deliverable", "title": "Del 1"},
        {"artifact_type": "resource", "title": "Res 1"},
        # Transition
        {"artifact_type": "transition_phase", "title": "Trn 1"},
        {"artifact_type": "kt_requirement", "title": "Trn 2"},
        {"artifact_type": "exit_obligation", "title": "Trn 3"},
        # Penalty, Liquidated Damages, Clarification, Assumption
        {"artifact_type": "penalty", "title": "Pen 1"},
        {"artifact_type": "liquidated_damages", "title": "LD 1"},
        {"artifact_type": "clarification_question", "title": "CQ 1"},
        {"artifact_type": "assumption_dependency", "title": "Ass 1"},
        # Generic / Fallback
        {"artifact_type": "unknown_artifact", "title": "Generic 1"},
        # Second instances to verify counter incrementing
        {"artifact_type": "req_classification", "support_level": "L1", "title": "Item 4"},
        {"artifact_type": "sla_target", "title": "SLA 4"},
    ]

    assigned = _assign_ids(test_items)
    ids = [it["id"] for it in assigned]

    expected_ids = [
        "AMS-L1-001",
        "AMS-L2-001",
        "AMS-L3-001",
        "AMS-SLA-001",
        "AMS-SLA-002",
        "AMS-SLA-003",
        "AMS-APP-001",
        "AMS-VOL-001",
        "AMS-TOOL-001",
        "AMS-GOV-001",
        "AMS-DEL-001",
        "AMS-RES-001",
        "AMS-TRN-001",
        "AMS-TRN-002",
        "AMS-TRN-003",
        "PEN-001",
        "LD-001",
        "CQ-001",
        "ASS-001",
        "AMS-REQ-001",
        "AMS-L1-002",
        "AMS-SLA-004",
    ]

    assert ids == expected_ids


def test_get_prefix_all_cases() -> None:
    """Verify get_prefix covers all skill and spine prefixes accurately."""
    # Spine categories
    assert get_prefix("spine", "compliance") == "CR"
    assert get_prefix("spine", "risk") == "RSK"
    assert get_prefix("spine", "commercial_structure") == "CS"
    assert get_prefix("spine", "ambiguity") == "AMB"
    assert get_prefix("spine", "spine_compliance") == "CR"

    # Skills
    assert get_prefix("skill", "design-build") == "DB"
    assert get_prefix("skill", "design_build") == "DB"
    assert get_prefix("skill", "rate-card") == "RR"
    assert get_prefix("skill", "rate_card") == "RR"
    assert get_prefix("skill", "packaged-sap") == "PACKAGEDSAP"
    assert get_prefix("skill", "oracle") == "ORACLE"

    # AMS artifact types
    assert get_prefix("skill", "ams", artifact_type="req_classification", support_level="L1") == "AMS-L1"
    assert get_prefix("skill", "ams", artifact_type="req_classification", support_level="L2") == "AMS-L2"
    assert get_prefix("skill", "ams", artifact_type="req_classification", support_level="L3") == "AMS-L3"
    assert get_prefix("skill", "ams", artifact_type="sla_target") == "AMS-SLA"
    assert get_prefix("skill", "ams", artifact_type="availability") == "AMS-SLA"
    assert get_prefix("skill", "ams", artifact_type="sr_change_sla") == "AMS-SLA"
    assert get_prefix("skill", "ams", artifact_type="overview_app") == "AMS-APP"
    assert get_prefix("skill", "ams", artifact_type="ticket_volume") == "AMS-VOL"
    assert get_prefix("skill", "ams", artifact_type="tool") == "AMS-TOOL"
    assert get_prefix("skill", "ams", artifact_type="governance") == "AMS-GOV"
    assert get_prefix("skill", "ams", artifact_type="deliverable") == "AMS-DEL"
    assert get_prefix("skill", "ams", artifact_type="resource") == "AMS-RES"
    assert get_prefix("skill", "ams", artifact_type="transition_phase") == "AMS-TRN"
    assert get_prefix("skill", "ams", artifact_type="kt_requirement") == "AMS-TRN"
    assert get_prefix("skill", "ams", artifact_type="exit_obligation") == "AMS-TRN"
    assert get_prefix("skill", "ams", artifact_type="penalty") == "PEN"
    assert get_prefix("skill", "ams", artifact_type="liquidated_damages") == "LD"
    assert get_prefix("skill", "ams", artifact_type="clarification_question") == "CQ"
    assert get_prefix("skill", "ams", artifact_type="assumption_dependency") == "ASS"
    assert get_prefix("skill", "ams", artifact_type="other") == "AMS-REQ"


# ── Spine Consistency Probe Tests (Amendment 8) ──────────────────────────────


def test_spine_compliance_consistency_probe() -> None:
    """Every editable compliance field must survive _sanitise with its value intact."""
    specs = get_field_spec("spine", "compliance")
    editable_names = {s.name for s in specs if s.editable}

    chunk = DocumentChunk(section="Section 1", text="Sample", page=1)
    probe_input = {
        "title": "Probe Title",
        "description": "Probe Description",
        "source_section": "Section 1",
        "priority": "must",
        "confidence": 0.85,
        "ambiguity_flag": False,
        "clarification_question": "Probe question?",
        "raw_excerpt": "Exact text",
        "archetype_tag": "arch_1",
    }

    assert editable_names == set(probe_input.keys())

    sanitised = spine_compliance._sanitise(probe_input, chunk)
    for field_name, original_val in probe_input.items():
        assert field_name in sanitised, f"Field '{field_name}' dropped by compliance _sanitise"
        assert sanitised[field_name] == original_val, f"Field '{field_name}' mutated by compliance _sanitise"


def test_spine_risk_consistency_probe() -> None:
    """Every editable risk field must survive _sanitise with its value intact."""
    specs = get_field_spec("spine", "risk")
    editable_names = {s.name for s in specs if s.editable}

    chunk = DocumentChunk(section="Section 2", text="Sample", page=1)
    probe_input = {
        "title": "Risk Title",
        "description": "Risk Description",
        "source_section": "Section 2",
        "priority": "should",
        "confidence": 0.85,
        "ambiguity_flag": False,
        "clarification_question": "Risk question?",
        "raw_excerpt": "Risk excerpt",
        "archetype_tag": "arch_2",
    }

    assert editable_names == set(probe_input.keys())

    sanitised = spine_risk._sanitise(probe_input, chunk)
    for field_name, original_val in probe_input.items():
        assert field_name in sanitised, f"Field '{field_name}' dropped by risk _sanitise"
        assert sanitised[field_name] == original_val, f"Field '{field_name}' mutated by risk _sanitise"


def test_spine_ambiguity_consistency_probe() -> None:
    """Every editable ambiguity field must survive _sanitise."""
    specs = get_field_spec("spine", "ambiguity")
    editable_names = {s.name for s in specs if s.editable}

    chunk = DocumentChunk(section="Section 3", text="Sample", page=1)
    probe_input = {
        "title": "Ambiguity Title",
        "description": "Ambiguity Description",
        "source_section": "Section 3",
        "priority": "must",
        "confidence": 0.8,
        "ambiguity_flag": True,
        "clarification_question": "Ambiguity question?",
        "raw_excerpt": "Ambiguity excerpt",
        "archetype_tag": "arch_3",
        "resolved": True,
        "clarified_by": "QnA Answer #4",
    }

    assert editable_names == set(probe_input.keys())

    sanitised = spine_ambiguity._sanitise(probe_input, chunk)
    for field_name, original_val in probe_input.items():
        assert field_name in sanitised, f"Field '{field_name}' dropped by ambiguity _sanitise"
        assert sanitised[field_name] == original_val, f"Field '{field_name}' mutated by ambiguity _sanitise"


def test_spine_commercial_structure_consistency_probe() -> None:
    """Every editable commercial_structure field must survive _sanitise with its value intact."""
    specs = get_field_spec("spine", "commercial_structure")
    editable_names = {s.name for s in specs if s.editable}

    chunk = DocumentChunk(section="Section 4", text="Sample", page=1)
    probe_input = {
        "category": "CommercialStructure",
        "cost_type": "build_cost",
        "pricing_dimension": "Annual Support",
        "quantified_value": "USD 50,000",
        "mandatory": True,
        "raw_excerpt": "Commercial quote",
        "source_section": "Section 4",
        "archetype_tag": "arch_4",
    }

    assert editable_names == set(probe_input.keys())

    sanitised = spine_commercial_structure._sanitise(probe_input, chunk)
    for field_name, original_val in probe_input.items():
        assert field_name in sanitised, f"Field '{field_name}' dropped by commercial _sanitise"
        assert sanitised[field_name] == original_val, f"Field '{field_name}' mutated by commercial _sanitise"


# ── Field Specs Tests ────────────────────────────────────────────────────────


def test_design_build_field_specs() -> None:
    """Verify design-build field specifications from schema.yaml."""
    specs = get_field_spec("skill", "design-build")
    assert len(specs) > 0
    spec_map = {s.name: s for s in specs}

    assert "id" in spec_map
    assert not spec_map["id"].editable

    assert "type" in spec_map
    assert spec_map["type"].editable
    assert spec_map["type"].required
    assert spec_map["type"].type == "enum"
    assert spec_map["type"].enum_values == ["functional", "non_functional", "staffing_mandate"]

    assert "title" in spec_map
    assert spec_map["title"].editable
    assert spec_map["title"].required

    assert "source_document" in spec_map
    assert not spec_map["source_document"].editable
    assert not spec_map["document_role"].editable
    assert not spec_map["source_type"].editable
    assert not spec_map["clarified_by"].editable


def test_ams_field_specs() -> None:
    """Verify AMS field specifications including editable artifact_type with help text."""
    specs = get_field_spec("skill", "ams")
    assert len(specs) > 0
    spec_map = {s.name: s for s in specs}

    assert "artifact_type" in spec_map
    art_spec = spec_map["artifact_type"]
    assert art_spec.editable
    assert art_spec.required
    assert art_spec.type == "enum"
    assert art_spec.help is not None and len(art_spec.help) > 0
    assert "overview_app" in (art_spec.enum_values or [])
    assert "req_classification" in (art_spec.enum_values or [])

    assert "id" in spec_map
    assert not spec_map["id"].editable


def test_unknown_field_specs_return_empty() -> None:
    """Unknown kind or key must return an empty list."""
    assert get_field_spec("unknown", "unknown") == []
    assert get_field_spec("skill", "non_existent_skill_xyz") == []
    assert get_field_spec("spine", "non_existent_spine_xyz") == []


# ── GET /jobs/{id}/fields Endpoint Tests ─────────────────────────────────────


def test_get_fields_endpoint_success(client: TestClient) -> None:
    """GET /jobs/{id}/fields returns 200 with list of field specs for valid job and bucket."""
    from app.job_store import get_job_store

    store = get_job_store()
    job = store.create_job("test.pdf", "test.pdf")

    # Skill bucket
    resp = client.get(f"/jobs/{job.job_id}/fields?kind=skill&key=design-build")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    names = [f["name"] for f in data]
    assert "title" in names
    assert "type" in names

    # Spine bucket
    resp2 = client.get(f"/jobs/{job.job_id}/fields?kind=spine&key=compliance")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert isinstance(data2, list)
    spine_names = [f["name"] for f in data2]
    assert "title" in spine_names
    assert "raw_excerpt" in spine_names


def test_get_fields_endpoint_not_found(client: TestClient) -> None:
    """GET /jobs/{id}/fields returns 404 for unknown job or unknown bucket."""
    from app.job_store import get_job_store

    store = get_job_store()
    job = store.create_job("test.pdf", "test.pdf")

    # Unknown bucket
    resp = client.get(f"/jobs/{job.job_id}/fields?kind=unknown&key=foo")
    assert resp.status_code == 404

    # Unknown job
    fake_job_id = "00000000-0000-0000-0000-000000000000"
    resp2 = client.get(f"/jobs/{fake_job_id}/fields?kind=skill&key=design-build")
    assert resp2.status_code == 404
