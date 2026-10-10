"""
skills_engine/design_build.py
------------------------------
Design-Build skill extraction agent.

Ported from rfp-analyzer/agents/functional.py and nonfunctional.py, adapted
to V2 conventions:
- Uses SKILL.md framing from skill_meta (not a hardcoded prompt)
- Outputs items matching the design-build schema.yaml (type, title, description,
  source_section, priority, confidence, ambiguity_flag, clarification_question,
  raw_excerpt, source_type)
- No org_context dependency
- Async-only, fan-out per chunk bounded by semaphore
"""

from __future__ import annotations

import asyncio
import re

import structlog

from core.llm_client import (
    call_llm_async,
    DEFAULT_MAX_CONCURRENT,
    pack_chunks,
    format_batch_user_content,
)
from core.prompts.qna_reconciliation import QNA_RECONCILIATION_FRAGMENT
from core.schemas import DocumentChunk
from core.skill_registry import SkillMeta

log = structlog.get_logger()

_SYSTEM_TEMPLATE = """\
You are a senior business analyst and systems architect extracting DESIGN & BUILD requirements from an RFP.

SKILL FRAMING:
{skill_framing}

Extract THREE types of items:

A) FUNCTIONAL requirements -- what the system must DO
B) NON-FUNCTIONAL requirements -- how well it must perform (performance, security, scalability, reliability)
C) STAFFING MANDATES -- any explicit vendor team or personnel requirement, in ANY of these forms:
   - A table with columns Role / Count / Responsibility (rows are prefixed ROW: in the input)
   - A prose sentence: "Vendor should/must onboard [role]", "vendor must provide [role]",
     "dedicated [role] must be assigned", "as a minimum the proposed team shall include"
   - A list of required roles with bullet/numbered items describing skills or responsibilities
   Extract ONE staffing_mandate item per distinct named role.

For functional and non-functional requirements use these fields:
{{
  "type": "functional | non_functional",
  "title": "Short label, max 10 words",
  "description": "Clear, implementation-neutral description (max 25 words)",
  "source_section": "Section heading where this was found",
  "priority": "must | should | could | wont",
  "confidence": 0.9,
  "ambiguity_flag": false,
  "clarification_question": null,
  "min_headcount": null,
  "primary_responsibility": null,
  "raw_excerpt": "Verbatim source text (max 30 words)",
  "source_type": "text | diagram"
}}

For STAFFING MANDATES use these fields (one object per named role):
{{
  "type": "staffing_mandate",
  "title": "The role name exactly as written (e.g. 'Banking SMEs', 'Solution Architects')",
  "description": "What this role must do or what skills they must have (max 30 words)",
  "source_section": "Section heading",
  "priority": "must | should",
  "confidence": 1.0,
  "ambiguity_flag": false,
  "clarification_question": null,
  "min_headcount": 2,
  "primary_responsibility": "Verbatim requirement text for this role (max 60 words)",
  "raw_excerpt": "The ROW: line, table row, or verbatim prose sentence",
  "source_type": "text"
}}

STAFFING MANDATE RULES:
- min_headcount: use the explicit integer from the table Count column. If no count is stated
  in prose (e.g. "Vendor should onboard seasoned Banking SMEs"), set min_headcount to null.
- priority: "must" when the text uses "shall", "must", "is required"; "should" when it uses
  "should", "is expected", "vendor should onboard".
- Extract EVERY distinct named role -- do NOT collapse multiple roles into one item.
- For table format: extract EVERY ROW: line -- one item per role, never merge rows.

PRIORITY MAPPING:
- "shall", "must", "is required", "mandatory" -> "must"
- "should", "is expected" -> "should"
- "may", "can", "optionally" -> "could"
- "will not", "out of scope" -> "wont"

RULES:
- Extract only what is explicitly present -- do NOT invent requirements
- Set source_type="diagram" when the excerpt references a [FIGURE ...] annotation
- Do NOT extract SLA/operational terms (those belong to the AMS skill)
- Do NOT extract named-role rate information (belongs to Rate Card skill)
- Do NOT extract pure regulatory compliance mandates (belongs to Compliance spine agent)
- Return [] if no design-build content is found in the chunk
- Output AT MOST 25 items per call. If there are more, return the 25 highest-confidence ones.
- Output ONLY valid JSON -- no markdown fences, no extra text
"""


async def extract(
    chunks: list[DocumentChunk],
    skill_meta: SkillMeta,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[dict]:
    """
    Extract Design-Build requirements from document chunks.

    Parameters
    ----------
    chunks : list[DocumentChunk]
        All document chunks from the ingestor.
    skill_meta : SkillMeta
        Loaded skill metadata; the SKILL.md body is injected into the prompt.
    max_concurrent : int
        Maximum parallel LLM calls.

    Returns
    -------
    list[dict]
        Extracted items matching the design-build schema.yaml.
    """
    skill_framing = skill_meta.read_skill_md_body()
    system_prompt = _SYSTEM_TEMPLATE.format(skill_framing=skill_framing) + "\n\n" + QNA_RECONCILIATION_FRAGMENT

    semaphore = asyncio.Semaphore(max_concurrent)
    substantial = [c for c in chunks if c.is_substantial()]
    batches = pack_chunks(substantial)

    # Build a chunk→batch lookup so _sanitise can find the right source chunk
    chunk_to_batch_repr: dict[int, DocumentChunk] = {}
    for batch in batches:
        for c in batch:
            chunk_to_batch_repr[id(c)] = c

    async def _process_batch(batch: list[DocumentChunk]) -> list[dict]:
        async with semaphore:
            user_content = format_batch_user_content(batch)
            raw_items = await call_llm_async(system_prompt, user_content)
            # Use the first chunk in the batch as the source reference for _sanitise
            ref_chunk = batch[0]
            return [_sanitise(item, ref_chunk) for item in raw_items]

    results = await asyncio.gather(*[_process_batch(b) for b in batches])

    items: list[dict] = []
    counter = 1
    for batch_result in results:
        for item in batch_result:
            item.setdefault("id", f"DB-{counter:03d}")
            items.append(item)
            counter += 1

    log.info("design_build_extraction_done", count=len(items), batches=len(batches))
    return items


def _sanitise(item: dict, chunk: DocumentChunk) -> dict:
    """Normalise fields and enforce schema constraints."""
    allowed = {
        "id", "type", "title", "description", "source_section",
        "priority", "confidence", "ambiguity_flag",
        "clarification_question", "raw_excerpt", "source_type",
        "min_headcount", "primary_responsibility",
    }
    clean = {k: v for k, v in item.items() if k in allowed}

    # Strip LLM-generated ID prefixes from title
    if isinstance(clean.get("title"), str):
        clean["title"] = re.sub(r"^[A-Z]+-\d+[:\s\-]+\s*", "", clean["title"]).strip()
        words = clean["title"].split()
        if len(words) > 10:
            clean["title"] = " ".join(words[:10]) + "…"

    # Normalise type
    raw_type = str(clean.get("type", "functional")).lower().strip()
    clean["type"] = raw_type if raw_type in ("functional", "non_functional", "staffing_mandate") else "functional"

    # Coerce min_headcount to int for staffing_mandate items
    if clean.get("min_headcount") is not None:
        try:
            clean["min_headcount"] = int(clean["min_headcount"])
        except (TypeError, ValueError):
            clean["min_headcount"] = None

    # Normalise priority
    raw_priority = str(clean.get("priority", "must")).lower().strip()
    priority_map = {"must": "must", "should": "should", "could": "could", "wont": "wont", "won't": "wont"}
    clean["priority"] = priority_map.get(raw_priority, "must")

    # Clamp confidence
    try:
        clean["confidence"] = max(0.0, min(1.0, float(clean.get("confidence", 0.9))))
    except (TypeError, ValueError):
        clean["confidence"] = 0.9

    # Confidence/ambiguity consistency
    if clean["confidence"] < 0.7:
        clean["ambiguity_flag"] = True
    if clean.get("ambiguity_flag"):
        clean["confidence"] = min(clean["confidence"], 0.8)

    # source_type: inherit from chunk if LLM didn't set it correctly
    st = str(clean.get("source_type", chunk.source_type)).lower().strip()
    clean["source_type"] = st if st in ("text", "diagram") else chunk.source_type

    clean.setdefault("source_section", chunk.section)
    clean.setdefault("raw_excerpt", "")
    clean.setdefault("clarification_question", None)
    clean.setdefault("ambiguity_flag", False)

    return clean
