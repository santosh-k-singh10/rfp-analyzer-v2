"""
skills_engine/ams.py
---------------------
AMS (Application Management Services) skill extraction agent.

Comprehensive Senior AMS Solution Architect and RFP Analyst extraction engine.
Evaluates RFPs across 18 operational dimensions and yields structured data for
the 6 core AMS report artifacts:
1. Artifact 1: AMS Executive Summary & KPI Snapshot
2. Artifact 2: AMS Requirement Register (L1/L2/L3, Ticket Volumes, Tools, Governance, Deliverables, Resources)
3. Artifact 3: SLA Matrix (Severity Targets, Availability, RTO/RPO, Service Requests & Changes)
4. Artifact 4: Transition Assessment (Phases, Knowledge Transfer, Exit Obligations)
5. Artifact 5: Penalty & Commercial Risk Register (SLA Credits, Liquidated Damages)
6. Artifact 6: Clarification Register & Assumptions (Missing Info & Clarifications, Assumptions/Dependencies)
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog

from core.edit.id_prefix import get_ams_prefix
from core.llm_client import call_llm_async, DEFAULT_MAX_CONCURRENT, pack_chunks, format_batch_user_content
from core.prompts.qna_reconciliation import QNA_RECONCILIATION_FRAGMENT
from core.schemas import DocumentChunk
from core.skill_registry import SkillMeta

log = structlog.get_logger()

_SYSTEM_TEMPLATE = """\
You are a Senior AMS Solution Architect and RFP Analyst.
You are evaluating this RFP solely from the perspective of an AMS service provider preparing a managed services proposal.
Extract every detail that impacts resource planning, support scope, SLA commitments, transition effort, governance, pricing, risk, and support delivery model.

SKILL FRAMING:
{skill_framing}

INSTRUCTIONS:
1. Review the provided chunks and extract all requirements relevant to Application Maintenance & Support (AMS), Managed Application Services, Application Support, Production Support, Run Operations, Service Desk, and Enhancement Support.
2. Ignore infrastructure-only, network-only, hosting-only, or pure application development requirements unless they directly impact AMS delivery.
3. For each extracted item, classify it into one of the following `artifact_type` categories:
   - `overview_app`: Applications in scope, business function, tech stack, criticality, environment, objectives
   - `req_classification`: L1, L2, L3 support requirements, functional support, ticket logging, defect fix
   - `ticket_volume`: Ticket volumes per month, incident/SR breakdown, peak volume, or note if missing
   - `tool`: ITSM, monitoring, observability, CMDB, or reporting tools
   - `governance`: Daily, weekly, monthly, quarterly reviews, reporting cadence, governance models
   - `deliverable`: SOPs, runbooks, knowledge base, RCA reports, dashboards, service reports
   - `resource`: Team roles, headcounts, skill requirements, shift models, language, location
   - `sla_target`: Priority P1-P4 definitions, response targets, resolution targets, escalation SLA, support hours
   - `availability`: Application uptime and availability percentages (e.g. 99.9%)
   - `rto_rpo`: Disaster recovery RTO, RPO, backup, DR testing
   - `sr_change_sla`: Service request fulfillment SLAs, standard/normal/emergency change SLAs
   - `transition_phase`: Shadow, reverse shadow, go-live/hypercare, steady state timelines & milestones
   - `kt_requirement`: Knowledge transfer topics, method, duration, acceptance criteria
   - `exit_obligation`: Notice period, parallel run, asset/knowledge handover obligations
   - `penalty`: SLA service credits, penalties, trigger conditions, penalty caps
   - `liquidated_damages`: Liquidated damages clauses, trigger conditions, maximum liabilities
   - `clarification_question`: Missing ticket data, undefined SLAs, missing RTO/RPO, vague requirements
   - `assumption_dependency`: Customer responsibilities, third-party dependencies, key solution assumptions

For each item found, output a JSON array of objects with EXACTLY these fields:
[
  {{
    "artifact_type": "overview_app | req_classification | ticket_volume | tool | governance | deliverable | resource | sla_target | availability | rto_rpo | sr_change_sla | transition_phase | kt_requirement | exit_obligation | penalty | liquidated_damages | clarification_question | assumption_dependency",
    "title": "Short descriptive label (e.g. 'Severity 1 Response Time', 'L1 Ticket Logging', 'Oracle ERP Core')",
    "description": "Clear specification or question text",
    "category": "e.g. Service Desk | Incident Mgmt | Problem Mgmt | Change Mgmt | Monitoring | Transition | Governance | Tools | SLA | RTO/RPO | Commercial",
    "support_level": "L1 | L2 | L3 | L4_OEM | cross_functional | not_applicable",
    "priority": "must | should | could | wont | high | medium | low",
    "impact": "high | medium | low | info",
    "sla_type": "response_time | resolution_time | availability | service_hours | escalation_time | service_request | change_sla | ticket_volume | transition_kt | staffing_ratio | continuous_improvement | other",
    "severity_level": "P1 | P2 | P3 | P4 | Critical | High | Medium | Low | null",
    "target_value": "e.g. '15 minutes', '4 hours', '99.9%', '1200/mo', '4 weeks', or null",
    "measurement_period": "e.g. 'monthly', 'per incident', 'quarterly', or null",
    "penalty_type": "service_credit | financial_penalty | liquidated_damages | none | unspecified",
    "penalty_value": "e.g. '2% of monthly fee', 'USD 5,000' or null",
    "confidence": 0.95,
    "is_inferred": false,
    "raw_excerpt": "Exact verbatim quote from the RFP",
    "source_section": "Section heading",
    "source_type": "text | diagram"
  }}
]

RULES:
- Quote exact RFP text in raw_excerpt for auditability.
- When critical information (like ticket volumes, SLA values, or transition duration) is ambiguous or not provided, generate a `clarification_question` item with `impact: "high"` or `"medium"`.
- Return [] if no AMS content is found in the chunk.
- Output AT MOST 30 items per call.
- Output ONLY valid JSON — no markdown fences, no extra text.
"""

_VALID_ARTIFACT_TYPES = {
    "overview_app", "req_classification", "ticket_volume", "tool", "governance",
    "deliverable", "resource", "sla_target", "availability", "rto_rpo", "sr_change_sla",
    "transition_phase", "kt_requirement", "exit_obligation", "penalty",
    "liquidated_damages", "clarification_question", "assumption_dependency",
}

_VALID_SUPPORT_LEVELS = {"L1", "L2", "L3", "L4_OEM", "cross_functional", "not_applicable"}
_VALID_IMPACT_LEVELS = {"high", "medium", "low", "info"}
_VALID_SLA_TYPES = {
    "response_time", "resolution_time", "availability", "service_hours",
    "escalation_time", "service_request", "change_sla", "ticket_volume",
    "transition_kt", "staffing_ratio", "continuous_improvement", "other",
}
_VALID_PENALTY_TYPES = {"service_credit", "financial_penalty", "liquidated_damages", "none", "unspecified"}


async def extract(
    chunks: list[DocumentChunk],
    skill_meta: SkillMeta,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[dict]:
    """
    Extract comprehensive AMS items and artifacts from document chunks.
    """
    skill_framing = skill_meta.read_skill_md_body()
    system_prompt = _SYSTEM_TEMPLATE.format(skill_framing=skill_framing) + "\n\n" + QNA_RECONCILIATION_FRAGMENT

    semaphore = asyncio.Semaphore(max_concurrent)
    substantial = [c for c in chunks if c.is_substantial()]
    batches = pack_chunks(substantial)

    async def _process_batch(batch: list[DocumentChunk]) -> list[dict]:
        async with semaphore:
            user_content = format_batch_user_content(batch)
            raw_items = await call_llm_async(system_prompt, user_content)
            ref_chunk = batch[0]
            return [_sanitise(item, ref_chunk) for item in raw_items]

    results = await asyncio.gather(*[_process_batch(b) for b in batches])

    items: list[dict] = []
    for batch_result in results:
        for item in batch_result:
            items.append(item)

    items = _deduplicate(items)
    items = _assign_ids(items)

    log.info("ams_extraction_done", count=len(items), batches=len(batches))
    return items


def _sanitise(item: dict, chunk: DocumentChunk) -> dict:
    """Normalise fields and enforce AMS schema 2.0 constraints."""
    allowed = {
        "id", "artifact_type", "title", "description", "category", "support_level",
        "priority", "impact", "sla_type", "severity_level", "target_value",
        "measurement_period", "penalty_type", "penalty_value", "confidence",
        "is_inferred", "raw_excerpt", "source_section", "source_page",
        "source_type", "source_document", "document_role", "clarified_by",
    }
    clean = {k: v for k, v in item.items() if k in allowed}

    # Strip LLM-generated ID so we assign deterministic structured IDs
    clean.pop("id", None)

    # Artifact type normalisation
    art = str(clean.get("artifact_type", "req_classification")).lower().strip()
    clean["artifact_type"] = art if art in _VALID_ARTIFACT_TYPES else "req_classification"

    # Support level
    sup = str(clean.get("support_level", "not_applicable")).strip()
    if sup.upper() in {"L1", "L2", "L3"}:
        clean["support_level"] = sup.upper()
    elif sup in _VALID_SUPPORT_LEVELS:
        clean["support_level"] = sup
    else:
        clean["support_level"] = "not_applicable"

    # Title cleanup
    if isinstance(clean.get("title"), str):
        clean["title"] = re.sub(r"^[A-Z0-9_\-]+[:\s\-]+\s*", "", clean["title"]).strip()
        words = clean["title"].split()
        if len(words) > 12:
            clean["title"] = " ".join(words[:12]) + "…"

    # SLA type normalisation
    if clean.get("sla_type"):
        st_raw = str(clean["sla_type"]).lower().strip().replace(" ", "_").replace("-", "_")
        clean["sla_type"] = st_raw if st_raw in _VALID_SLA_TYPES else "other"
    elif clean["artifact_type"] in {"sla_target", "availability", "sr_change_sla"}:
        clean["sla_type"] = "response_time"
    else:
        clean["sla_type"] = None

    # Penalty type normalisation
    if clean.get("penalty_type"):
        pt_raw = str(clean["penalty_type"]).lower().strip().replace(" ", "_")
        clean["penalty_type"] = pt_raw if pt_raw in _VALID_PENALTY_TYPES else "unspecified"
    elif clean["artifact_type"] in {"penalty", "liquidated_damages"}:
        clean["penalty_type"] = "service_credit" if clean["artifact_type"] == "penalty" else "liquidated_damages"
    else:
        clean["penalty_type"] = None

    # Impact normalisation
    imp = str(clean.get("impact", "medium")).lower().strip()
    clean["impact"] = imp if imp in _VALID_IMPACT_LEVELS else "medium"

    # Priority normalisation
    pri = str(clean.get("priority", "should")).lower().strip()
    clean["priority"] = pri if pri in {"must", "should", "could", "wont", "high", "medium", "low"} else "should"

    # Confidence normalisation
    try:
        clean["confidence"] = round(float(clean.get("confidence", 0.85)), 2)
    except (ValueError, TypeError):
        clean["confidence"] = 0.85

    # Booleans
    clean["is_inferred"] = bool(clean.get("is_inferred", False))

    # Provenance fields from chunk
    clean.setdefault("raw_excerpt", chunk.text[:200] if hasattr(chunk, "text") else "")
    clean.setdefault("source_section", chunk.section or "General")
    clean["source_page"] = getattr(chunk, "page", None)
    clean["source_type"] = getattr(chunk, "source_type", "text")
    clean["source_document"] = getattr(chunk, "source_document", None)
    clean["document_role"] = getattr(chunk, "document_role", "primary")

    if hasattr(chunk, "qna_question") and chunk.qna_question:
        clean["clarified_by"] = chunk.qna_question
    else:
        clean.setdefault("clarified_by", None)

    return clean


def _deduplicate(items: list[dict]) -> list[dict]:
    """Remove duplicate items keyed on (artifact_type, normalized_title)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for item in items:
        norm_title = re.sub(r"\s+", " ", str(item.get("title", "")).strip().lower())
        art = str(item.get("artifact_type", ""))
        key = (art, norm_title)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _assign_ids(items: list[dict]) -> list[dict]:
    """Assign domain-specific IDs based on artifact type and level."""
    counters: dict[str, int] = {}
    for item in items:
        prefix = get_ams_prefix(item.get("artifact_type"), item.get("support_level"))
        cnt = counters.get(prefix, 1)
        counters[prefix] = cnt + 1
        item["id"] = f"{prefix}-{cnt:03d}"
    return items
