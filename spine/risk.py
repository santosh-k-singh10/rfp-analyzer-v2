"""
spine/risk.py
--------------
Risk spine agent — identifies delivery risks from the bidder's perspective for
every RFP regardless of archetype.

Ported from rfp-analyzer/agents/risk.py with V2 adaptations:
- No v1 Requirement model; returns raw dicts with source_type and archetype_tag
- No org_context dependency
- archetype_tag: set when the risk clearly relates to a specific fired skill
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
You are a delivery risk analyst reviewing an RFP on behalf of a bidding vendor.

Identify DELIVERY RISKS — things that make it harder or riskier to successfully deliver:

1. TIMELINE: Aggressive or fixed go-live dates with large scope
2. SCOPE CREEP: Open-ended language ("and other modules as required")
3. TECHNICAL COMPLEXITY: Legacy system integrations, unusual tech stack mandates, large data migrations
4. SLA / PENALTY RISK: Uptime commitments, liquidated damages, penalty clauses hard to meet
5. DEPENDENCY: Risks dependent on the client (e.g. "client will provide test data within 2 weeks")
6. GOVERNANCE: Unclear decision-making authority, too many approval layers
7. RESOURCE: Requires rare skills or certifications the average vendor may not have
8. DATA: Large volume migrations, complex transformations, unspecified legacy data quality

For each risk found, output a JSON array:
[
  {
    "title": "Short risk label",
    "description": "What the risk is, why it matters, and what could go wrong",
    "source_section": "Section heading",
    "priority": "must | should | could",
    "confidence": 0.9,
    "ambiguity_flag": false,
    "clarification_question": null,
    "raw_excerpt": "Exact text that triggered this risk",
    "source_type": "text | diagram",
    "archetype_tag": null
  }
]

PRIORITY MAPPING:
- "shall", "must", "liquidated damages", "penalty" → "must"
- "should", "expected" → "should"
- "may", "could be" → "could"

RULES:
- Focus on DELIVERY risk from the bidder's perspective, not business risk to the client
- Be specific — "tight timeline" is not useful; "6-month timeline for 15 modules including data migration" is
- Do NOT extract pure contractual penalty clauses — only extract when the penalty creates a concrete delivery risk
- Return [] if the chunk contains nothing risky
- Output AT MOST 15 items per call. If there are more, return the 15 highest-confidence ones.
- Output ONLY valid JSON — no markdown fences, no extra text
"""


async def extract_risks(
    chunks: list[DocumentChunk],
    fired_skills: Optional[list[str]] = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[dict]:
    """
    Extract delivery risks from document chunks.

    Parameters
    ----------
    chunks : list[DocumentChunk]
        All document chunks.
    fired_skills : list[str], optional
        Fired skill slugs for archetype_tag assignment.
    max_concurrent : int
        Maximum parallel LLM calls.

    Returns
    -------
    list[dict]
        Risk items with source_type and archetype_tag fields.
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
            item["id"] = f"RSK-{counter:03d}"
            items.append(item)
            counter += 1

    log.info("risk_extraction_done", count=len(items), batches=len(batches))
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

    raw_priority = str(clean.get("priority", "should")).lower().strip()
    priority_map = {"must": "must", "should": "should", "could": "could"}
    clean["priority"] = priority_map.get(raw_priority, "should")

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
