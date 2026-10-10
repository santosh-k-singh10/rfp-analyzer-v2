"""
spine/commercial_structure.py
------------------------------
Commercial Structure spine agent — extracts pricing mechanics and cost-structure
requirements from every RFP regardless of archetype.

Ported from rfp-analyzer/agents/commercial_structure.py with V2 adaptations:
- No v1 CommercialStructureItem Pydantic model; returns raw dicts
- No org_context dependency
- Added source_type and archetype_tag fields per V2 spec
- Explicit boundary note re: Rate Card skill:
  - CS-* owns: unnamed generic day rates, milestone payment structures,
    gain-share, delivery-mix thresholds, cost-type separation
  - Rate Card skill owns: named role titles, per-role rate structures,
    seniority/location tiers, min commitment periods

Note: Commercial Structure is deliberately separate from the Rate Card *skill*.
The Rate Card skill extracts named roles with rates from rate card tables.
This agent extracts pricing *mechanics* (how costs are structured) from the
RFP body, not named role rates.
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
You are extracting COMMERCIAL PRICING STRUCTURE items from an RFP section.
These are pricing mechanics and cost-structure requirements — NOT T&C contract clauses,
NOT named role rate cards in an appendix.

Extract items in these five scope categories:

1. COST-TYPE SEPARATION: RFP mandates the bid price be broken into line items by type
   (license fees, implementation/build costs, AMS run-rate, staff augmentation, etc.)

2. DAY RATES IN THE RFP BODY: A sentence that states an ACTUAL NUMERIC RATE (e.g. "$800/day",
   "INR 12,000 per day", "£500/hour") cited inline in the RFP body text.
   The sentence MUST contain a currency symbol or number+unit to qualify.
   (NOT a headcount mandate, NOT a role list, NOT a rate card table)

3. MILESTONE-LINKED PAYMENT STRUCTURES: Payment schedules tied to project milestones
   (e.g. "30% on contract signing, 40% on UAT sign-off, 30% on go-live")

4. OUTCOME-LINKED / GAIN-SHARE PRICING: Compensation tied to measurable outcomes
   (e.g. "5% gain-share on verified cost savings above baseline")

5. DELIVERY-LOCATION MIX THRESHOLDS: Min/max percentages of effort by geography
   (e.g. "minimum 70% offshore delivery")

DO NOT extract — these are NOT commercial structure items:
- Staffing headcount mandates: "the proposed team shall include N engineers" — these have
  no price, rate, or payment term and belong to the Rate Card or Design-Build skill
- Minimum team composition requirements: "as a minimum, include the following roles"
- Named roles with rates in a rate card table (those go to Rate Card skill)
- T&C legal clauses (indemnification, IP, liability, termination)
- Technical/functional requirements
- Any item where quantified_value would be null AND cost_type is staff_aug_rate

Return a JSON array with EXACTLY these fields:
[
  {
    "category": "CommercialStructure",
    "cost_type": "build_cost | ams_run_rate | staff_aug_rate | blended | unspecified",
    "pricing_dimension": "Short label for what is being priced (≤12 words)",
    "quantified_value": "exact figure from RFP or null",
    "mandatory": true,
    "raw_excerpt": "Verbatim sentence(s) from the RFP",
    "source_section": "Section heading",
    "source_type": "text | diagram",
    "archetype_tag": null
  }
]

- "category" MUST always be exactly "CommercialStructure"
- "mandatory": true if RFP uses "shall", "must", "is required"
- Do NOT include "id" — IDs are assigned by the system
- Return [] if no commercial pricing structure items are present
- Output AT MOST 15 items per call. If there are more, return the 15 highest-confidence ones.
- Output ONLY valid JSON — no markdown fences, no extra text
"""

_VALID_COST_TYPES = {"build_cost", "ams_run_rate", "staff_aug_rate", "blended", "unspecified"}


async def extract_commercial_structure(
    chunks: list[DocumentChunk],
    fired_skills: Optional[list[str]] = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[dict]:
    """
    Extract commercial pricing structure items from document chunks.

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
        Commercial structure items with source_type and archetype_tag fields.
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

    # Collect, validate category, and filter out misclassified headcount items
    raw_items: list[dict] = []
    for batch_result in results:
        for item in batch_result:
            if item.get("category") != "CommercialStructure":
                log.warning("cs_category_mismatch_dropped", category=item.get("category"))
                continue
            if _is_headcount_item(item):
                log.debug(
                    "cs_headcount_filtered",
                    dimension=item.get("pricing_dimension", "")[:60],
                )
                continue
            raw_items.append(item)

    # Deduplicate on (pricing_dimension, cost_type)
    items = _deduplicate(raw_items)

    # Assign IDs
    for i, item in enumerate(items, start=1):
        item["id"] = f"CS-{i:03d}"

    log.info("commercial_structure_extraction_done", count=len(items), batches=len(batches))
    return items


# Phrases that identify staffing headcount mandates — not commercial pricing items
_HEADCOUNT_PHRASES = re.compile(
    r"(proposed\s+team\s+shall\s+include"
    r"|as\s+a\s+minimum.*include"
    r"|minimum.*resources?"
    r"|minimum.*engineer"
    r"|resources?\s+shall\s+include"
    r"|team\s+composition"
    r"|staffing\s+requirement)",
    re.IGNORECASE,
)

# A genuine commercial item must contain a numeric value of some kind
_HAS_NUMERIC = re.compile(r"\d")


def _is_headcount_item(item: dict) -> bool:
    """
    Return True when an item is a staffing headcount mandate rather than a
    commercial pricing structure item.

    Two independent signals are checked:
    1. The raw_excerpt or pricing_dimension matches known headcount phrases
       ("proposed team shall include N engineers", "as a minimum, include…").
    2. quantified_value is null/empty AND cost_type is staff_aug_rate — a
       staffing item with no price figure is never a commercial structure item.
    """
    excerpt = str(item.get("raw_excerpt") or item.get("pricing_dimension") or "")

    # Signal 1: phrase match
    if _HEADCOUNT_PHRASES.search(excerpt):
        return True

    # Signal 2: staff_aug_rate with no numeric quantified_value
    qv = item.get("quantified_value")
    ct = str(item.get("cost_type", "")).lower()
    if ct == "staff_aug_rate" and (not qv or not _HAS_NUMERIC.search(str(qv))):
        return True

    return False


def _sanitise(item: dict, chunk: DocumentChunk) -> dict:
    """Normalise field aliases and validate enum values."""
    _ALIASES: dict[str, str] = {
        "dimension": "pricing_dimension",
        "pricing_item": "pricing_dimension",
        "label": "pricing_dimension",
        "title": "pricing_dimension",
        "value": "quantified_value",
        "amount": "quantified_value",
        "figure": "quantified_value",
        "excerpt": "raw_excerpt",
        "text": "raw_excerpt",
        "source": "source_section",
        "section": "source_section",
        "source_location": "source_section",
        "reference": "source_section",
        "type": "cost_type",
        "cost_category": "cost_type",
    }
    normalised: dict = {}
    for k, v in item.items():
        canonical = _ALIASES.get(k, k)
        if canonical not in normalised:
            normalised[canonical] = v

    normalised.pop("id", None)
    normalised.pop("workstream", None)

    allowed = {
        "category", "cost_type", "pricing_dimension",
        "quantified_value", "mandatory", "raw_excerpt",
        "source_section", "source_type", "archetype_tag",
    }
    clean = {k: v for k, v in normalised.items() if k in allowed}

    clean.setdefault("category", "CommercialStructure")

    ct = str(clean.get("cost_type", "unspecified")).lower().strip()
    clean["cost_type"] = ct if ct in _VALID_COST_TYPES else "unspecified"

    if "mandatory" not in clean:
        clean["mandatory"] = True

    # V2 additions
    st = str(clean.get("source_type", chunk.source_type)).lower().strip()
    clean["source_type"] = st if st in ("text", "diagram") else chunk.source_type
    clean.setdefault("archetype_tag", None)
    clean.setdefault("source_section", chunk.section)
    clean.setdefault("raw_excerpt", "")
    clean.setdefault("pricing_dimension", "")
    clean.setdefault("quantified_value", None)

    return clean


def _deduplicate(items: list[dict]) -> list[dict]:
    """Remove duplicates keyed on (pricing_dimension, cost_type)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for item in items:
        norm_dim = re.sub(r"\s+", " ", str(item.get("pricing_dimension", "")).strip().lower())
        key = (norm_dim, str(item.get("cost_type", "")))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out
