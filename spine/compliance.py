"""
spine/compliance.py
--------------------
Compliance spine agent — extracts regulatory mandates, certifications, and
standards adherence requirements from every RFP regardless of archetype.

Ported from rfp-analyzer/agents/compliance.py with V2 adaptations:
- No v1 Requirement model; returns raw dicts with source_type and archetype_tag
- No org_context dependency
- archetype_tag: set when the compliance item clearly relates to a specific
  fired skill slug; null for cross-cutting findings
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

import structlog

from core.llm_client import call_llm_async, DEFAULT_MAX_CONCURRENT, pack_chunks, format_batch_user_content
from core.prompts.qna_reconciliation import QNA_RECONCILIATION_FRAGMENT
from core.schemas import DocumentChunk

log = structlog.get_logger()

_SYSTEM_PROMPT = """\
You are a compliance and legal analyst extracting COMPLIANCE, REGULATORY, and CERTIFICATION
requirements from RFP documents.

Look specifically for:
- Data privacy laws: GDPR, CCPA, PDPA, DPDP Act (India)
- Security standards: ISO 27001, SOC 2 Type II, NIST, CIS benchmarks
- Healthcare: HIPAA, HL7, FHIR
- Payment: PCI-DSS
- Government / public sector: FedRAMP, IL ratings, government cloud requirements
- Accessibility: WCAG 2.0/2.1 AA/AAA, Section 508, EN 301 549
- Industry-specific: SEBI, RBI, IRDA, RERA (India context), FSA, MiFID
- Data residency: "data must reside in India / EU / US"
- Audit and logging obligations
- Certification requirements for the vendor

Output a JSON array with EXACTLY these fields:
[
  {
    "title": "Standard or regulation name + what it requires",
    "description": "What specifically must be done to comply.",
    "source_section": "Section heading",
    "priority": "must | should | could",
    "confidence": 0.95,
    "ambiguity_flag": false,
    "clarification_question": null,
    "raw_excerpt": "Exact source text",
    "source_type": "text | diagram",
    "archetype_tag": null
  }
]

PRIORITY MAPPING:
- "shall", "must", "is required", "mandatory" → "must"
- "should", "is expected" → "should"
- "may", "can", "optionally" → "could"

RULES:
- Extract ONLY statements that cite a named regulation, standard, or certification
- Do NOT extract system capability requirements (those belong to Design-Build)
- Do NOT extract quality attributes or SLA terms (those belong to AMS/Design-Build)
- Vendor certifications ("the vendor must hold ISO 27001") ARE compliance requirements
- Return [] if no compliance content is found in the chunk
- Output AT MOST 15 items per call. If there are more, return the 15 highest-confidence ones.
- Output ONLY valid JSON
"""


async def extract_compliance(
    chunks: list[DocumentChunk],
    fired_skills: Optional[list[str]] = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[dict]:
    """
    Extract compliance requirements from document chunks.

    Parameters
    ----------
    chunks : list[DocumentChunk]
        All document chunks.
    fired_skills : list[str], optional
        Fired skill slugs — used to set archetype_tag on items that clearly
        relate to a specific skill.
    max_concurrent : int
        Maximum parallel LLM calls.

    Returns
    -------
    list[dict]
        Compliance items with source_type and archetype_tag fields.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    substantial = [c for c in chunks if c.is_substantial()]
    batches = pack_chunks(substantial)
    system_prompt = _SYSTEM_PROMPT + "\n\n" + QNA_RECONCILIATION_FRAGMENT

    async def _process_batch(batch: list[DocumentChunk]) -> list[dict]:
        async with semaphore:
            user_content = format_batch_user_content(batch)
            raw_items = await call_llm_async(system_prompt, user_content)
            ref_chunk = batch[0]
            return [_sanitise(item, ref_chunk) for item in raw_items]

    results = await asyncio.gather(*[_process_batch(b) for b in batches])

    items: list[dict] = []
    counter = 1
    for batch_result in results:
        for item in batch_result:
            item["id"] = f"CR-{counter:03d}"
            items.append(item)
            counter += 1

    log.info("compliance_extraction_done", count=len(items), batches=len(batches))
    return items


def _sanitise(item: dict, chunk: DocumentChunk) -> dict:
    """Normalise fields and add V2-specific fields."""
    allowed = {
        "id", "title", "description", "source_section",
        "priority", "confidence", "ambiguity_flag",
        "clarification_question", "raw_excerpt",
        "source_type", "archetype_tag",
    }
    clean = {k: v for k, v in item.items() if k in allowed}
    clean.pop("id", None)
    clean.pop("workstream", None)

    if isinstance(clean.get("title"), str):
        clean["title"] = re.sub(r"^[A-Z]+-\d+[:\s\-]+\s*", "", clean["title"]).strip()
        words = clean["title"].split()
        if len(words) > 10:
            clean["title"] = " ".join(words[:10]) + "…"

    raw_priority = str(clean.get("priority", "must")).lower().strip()
    priority_map = {"must": "must", "should": "should", "could": "could"}
    clean["priority"] = priority_map.get(raw_priority, "must")

    try:
        clean["confidence"] = max(0.0, min(1.0, float(clean.get("confidence", 0.9))))
    except (TypeError, ValueError):
        clean["confidence"] = 0.9

    if clean["confidence"] < 0.7:
        clean["ambiguity_flag"] = True
    if clean.get("ambiguity_flag"):
        clean["confidence"] = min(clean["confidence"], 0.8)

    # V2 additions
    st = str(clean.get("source_type", chunk.source_type)).lower().strip()
    clean["source_type"] = st if st in ("text", "diagram") else chunk.source_type
    clean.setdefault("archetype_tag", None)
    clean.setdefault("source_section", chunk.section)
    clean.setdefault("raw_excerpt", "")
    clean.setdefault("clarification_question", None)
    clean.setdefault("ambiguity_flag", False)

    return clean
