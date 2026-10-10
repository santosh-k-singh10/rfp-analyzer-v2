"""
core/edit/id_prefix.py
-----------------------
ID prefix resolution for all skill and spine items.

Extracted from skills_engine/ams.py (spec §3.3, §4.4, C8).
Provides get_prefix() and get_ams_prefix().
"""

from __future__ import annotations

from typing import Optional

# ── AMS artifact_type -> prefix mapping ──────────────────────────────────────

AMS_SLA_ARTIFACTS = frozenset({"sla_target", "availability", "sr_change_sla"})
AMS_TRN_ARTIFACTS = frozenset({"transition_phase", "kt_requirement", "exit_obligation"})

AMS_STATIC_PREFIX_MAP: dict[str, str] = {
    "overview_app": "AMS-APP",
    "ticket_volume": "AMS-VOL",
    "tool": "AMS-TOOL",
    "governance": "AMS-GOV",
    "deliverable": "AMS-DEL",
    "resource": "AMS-RES",
    "penalty": "PEN",
    "liquidated_damages": "LD",
    "clarification_question": "CQ",
    "assumption_dependency": "ASS",
}

ALL_AMS_PREFIXES = [
    "AMS-L1",
    "AMS-L2",
    "AMS-L3",
    "AMS-SLA",
    "AMS-APP",
    "AMS-VOL",
    "AMS-TOOL",
    "AMS-GOV",
    "AMS-DEL",
    "AMS-RES",
    "AMS-TRN",
    "PEN",
    "LD",
    "CQ",
    "ASS",
    "AMS-REQ",
]

SPINE_PREFIX_MAP: dict[str, str] = {
    "compliance": "CR",
    "risk": "RSK",
    "commercial_structure": "CS",
    "ambiguity": "AMB",
}

SKILL_PREFIX_MAP: dict[str, str] = {
    "design-build": "DB",
    "design_build": "DB",
    "rate-card": "RR",
    "rate_card": "RR",
}


def get_ams_prefix(
    artifact_type: Optional[str] = None,
    support_level: Optional[str] = None,
) -> str:
    """Return the ID prefix for an AMS item based on artifact_type and support_level."""
    art = (artifact_type or "").strip()
    sup = (support_level or "").strip()

    if art == "req_classification" and sup in {"L1", "L2", "L3"}:
        return f"AMS-{sup}"
    if art in AMS_SLA_ARTIFACTS:
        return "AMS-SLA"
    if art in AMS_STATIC_PREFIX_MAP:
        return AMS_STATIC_PREFIX_MAP[art]
    if art in AMS_TRN_ARTIFACTS:
        return "AMS-TRN"
    return "AMS-REQ"


def get_prefix(
    kind: str,
    key: str,
    artifact_type: Optional[str] = None,
    support_level: Optional[str] = None,
) -> str:
    """Return the ID prefix for any skill or spine item.

    Parameters
    ----------
    kind:
        "skill" or "spine"
    key:
        Skill slug (e.g. "ams", "design-build", "packaged-sap") or spine category
        (e.g. "compliance", "risk", "ambiguity", "commercial_structure").
    artifact_type:
        For AMS items, the artifact_type field.
    support_level:
        For AMS items, the support_level field (e.g. "L1", "L2", "L3").
    """
    kind = kind.lower().strip()
    norm_key = key.lower().strip().removeprefix("spine_").removeprefix("spine.")

    if kind == "spine":
        if norm_key in SPINE_PREFIX_MAP:
            return SPINE_PREFIX_MAP[norm_key]
        return norm_key.upper()[:4]

    if kind == "skill":
        if norm_key == "ams":
            return get_ams_prefix(artifact_type, support_level)
        if norm_key in SKILL_PREFIX_MAP:
            return SKILL_PREFIX_MAP[norm_key]
        return norm_key.upper().replace("-", "")

    return norm_key.upper().replace("-", "")
