"""
spine/ambiguity.py
-------------------
Ambiguity spine agent — identifies vague terms, gaps, and contradictions in
every RFP regardless of archetype.

Ported from rfp-analyzer/agents/ambiguity.py with V2 adaptations:
- No v1 Requirement model; returns raw dicts with source_type and archetype_tag
- No org_context dependency
- archetype_tag: set when the ambiguity clearly relates to a specific fired skill
- Low-confidence items (< 0.75) are filtered out — not actionable enough
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
You are an experienced RFP reviewer identifying AMBIGUITIES, GAPS, and CONTRADICTIONS
in RFP documents.

Your job is NOT to extract requirements — find places where the RFP is:
1. VAGUE: Uses undefined terms like "user-friendly", "fast", "industry-standard", "modern"
2. INCOMPLETE: Missing critical information (e.g. expected user count, data volumes, go-live date)
3. CONTRADICTORY: Two statements that cannot both be true
4. UNDEFINED SCOPE: "and other features as required" — open-ended scope creep language
5. ASSUMPTION-HEAVY: Statements that assume unshared context

For each issue found, output a JSON array:
[
  {
    "title": "Short label of the ambiguity (≤10 words)",
    "description": "One sentence: what is vague/missing and why it matters (≤25 words)",
    "source_section": "Section heading",
    "priority": "must",
    "confidence": 1.0,
    "ambiguity_flag": true,
    "clarification_question": "One specific question for the issuer (≤20 words)",
    "raw_excerpt": "The exact vague text (≤30 words — truncate with … if longer)",
    "source_type": "text | diagram",
    "archetype_tag": null,
    "resolved": false,
    "clarified_by": null,
    "source_document": "",
    "document_role": "primary"
  }
]

RULES:
- Every item MUST have ambiguity_flag: true
- Every item MUST have a specific, actionable clarification_question
- KEEP ALL TEXT FIELDS CONCISE — obey the word limits above strictly
- Do not flag things that are clearly and completely stated
- When a QnA answer resolves this ambiguity, set resolved=true and clarified_by to the QnA question — do NOT omit the item
- Return [] if the chunk is well-specified with no ambiguities
- Output AT MOST 15 items per call. If there are more, return the 15 highest-confidence ones.
- Output ONLY valid JSON — no preamble, no markdown fences

PDF TABLE ARTEFACTS (do NOT flag):
- Text extracted from PDF tables often has line-wrapped content that appears truncated.
  Only flag text as incomplete if there is genuinely NO answer or continuation in the chunk.
"""


async def extract_ambiguities(
    chunks: list[DocumentChunk],
    fired_skills: Optional[list[str]] = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[dict]:
    """
    Extract ambiguities from document chunks.

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
        Ambiguity items with source_type and archetype_tag fields.
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
            # Filter low-confidence items — not actionable enough
            try:
                conf = float(item.get("confidence", 1.0))
            except (TypeError, ValueError):
                conf = 1.0
            if conf < 0.75:
                log.debug("ambiguity_low_confidence_filtered", conf=conf)
                continue
            item["id"] = f"AMB-{counter:03d}"
            items.append(item)
            counter += 1

    log.info("ambiguity_extraction_done", count=len(items), batches=len(batches))
    return items


def _sanitise(item: dict, chunk: DocumentChunk) -> dict:
    """Normalise fields and add V2-specific fields."""
    allowed = {
        "id", "title", "description", "source_section",
        "priority", "confidence", "ambiguity_flag",
        "clarification_question", "raw_excerpt",
        "source_type", "archetype_tag",
        "resolved", "clarified_by", "source_document", "document_role",
    }
    clean = {k: v for k, v in item.items() if k in allowed}
    clean.pop("id", None)
    clean.pop("workstream", None)

    # Force ambiguity items to have these values
    clean["ambiguity_flag"] = True
    clean["priority"] = "must"

    if isinstance(clean.get("title"), str):
        clean["title"] = re.sub(r"^[A-Z]+-\d+[:\s\-]+\s*", "", clean["title"]).strip()
        words = clean["title"].split()
        if len(words) > 10:
            clean["title"] = " ".join(words[:10]) + "…"

    try:
        clean["confidence"] = max(0.0, min(1.0, float(clean.get("confidence", 1.0))))
    except (TypeError, ValueError):
        clean["confidence"] = 1.0

    # Ambiguous items cap confidence at 0.8
    clean["confidence"] = min(clean["confidence"], 0.8)

    # V2 additions
    st = str(clean.get("source_type", chunk.source_type)).lower().strip()
    clean["source_type"] = st if st in ("text", "diagram") else chunk.source_type
    clean.setdefault("archetype_tag", None)
    clean.setdefault("source_section", chunk.section)
    clean.setdefault("raw_excerpt", "")
    clean.setdefault("clarification_question", None)
    # Multi-document provenance
    clean.setdefault("source_document", chunk.source_document)
    clean.setdefault("document_role", chunk.document_role)
    # QnA reconciliation fields
    clean.setdefault("resolved", False)
    clean.setdefault("clarified_by", None)

    return clean
