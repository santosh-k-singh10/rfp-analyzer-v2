"""
skills_engine/rate_card.py
---------------------------
Rate Card / Staff Augmentation skill extraction agent.

Ported from rfp-analyzer/agents/role_rate.py with V2 adaptations:
- Uses SKILL.md framing from skill_meta
- Hard requirement: extraction-only — never derive roles from project scope
- If the RFP has no explicit role list or rate card, set no_roles_stated=True
  on the SkillOutput and return empty items
- Outputs items matching the rate-card schema.yaml
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

import structlog

from core.llm_client import call_llm_async, DEFAULT_MAX_CONCURRENT, pack_chunks, format_batch_user_content
from core.prompts.qna_reconciliation import QNA_RECONCILIATION_FRAGMENT
from core.schemas import DocumentChunk, SkillOutput
from core.skill_registry import SkillMeta

log = structlog.get_logger()

_SYSTEM_TEMPLATE = """\
You are a procurement analyst extracting RATE CARD / STAFF AUGMENTATION information from an RFP.

SKILL FRAMING:
{skill_framing}

CRITICAL — EXTRACTION-ONLY GUARDRAIL:
If the RFP does NOT explicitly name specific roles or provide a rate card table, you MUST return:
  {{"items": [], "no_roles_stated": true}}

Do NOT derive or suggest roles from project scope or requirements.
Do NOT invent roles based on project description.

ROW-LEVEL GRANULARITY — MOST IMPORTANT RULE:
Tables in the text are formatted as:
  HEADER: Col1 | Col2 | Col3 ...
  ROW: Val1 | Val2 | Val3 ...
  ROW: Val1 | Val2 | Val3 ...

You MUST produce ONE separate item in "items" for EVERY "ROW:" line that contains a role.
Do NOT merge multiple ROW lines into a single item.
Do NOT stop after the first row.
Each ROW: line is a distinct role — extract ALL of them.

Also create ONE item per distinct role/seniority/location combination found in prose text.
"Senior Developer — Onsite" and "Senior Developer — Offshore" are TWO separate items.

RATE VALUE GUARDRAIL:
Never fabricate a rate value. If not stated, set rate_value to null.

When roles ARE explicitly listed, output a JSON object:
{{
  "items": [
    {{
      "role_title": "Exact title as named in RFP",
      "seniority_level": "Senior | L3 | null",
      "location_tier": "Onsite | Offshore | Hybrid | null",
      "pricing_model": "time_and_material | fixed_monthly | outcome_based | unspecified",
      "rate_unit": "per_hour | per_day | per_month | unspecified",
      "rate_value": "verbatim rate or null — NEVER fabricate",
      "min_commitment_period": "e.g. '3 months' or null",
      "replacement_backfill_terms": "verbatim text or null",
      "background_verification_required": true | false | null,
      "raw_excerpt": "the single ROW: line this item was extracted from",
      "source_section": "Section heading",
      "source_type": "text | diagram"
    }}
  ],
  "no_roles_stated": false
}}

Output AT MOST 40 role items in "items". If there are more distinct roles, include the 40 most explicitly stated.
Output ONLY valid JSON — no markdown fences, no extra text.
"""

_VALID_PRICING_MODELS = {"time_and_material", "fixed_monthly", "outcome_based", "unspecified"}
_VALID_RATE_UNITS = {"per_hour", "per_day", "per_month", "unspecified"}


async def extract(
    chunks: list[DocumentChunk],
    skill_meta: SkillMeta,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[dict]:
    """
    Extract Role & Rate Card items from document chunks.

    Returns a list of item dicts. If no roles are stated anywhere in the
    document, returns an empty list AND sets a sentinel item:
        [{"__no_roles_stated__": True}]
    The skill_runner reads this sentinel and sets SkillOutput.no_roles_stated=True.

    Parameters
    ----------
    chunks : list[DocumentChunk]
    skill_meta : SkillMeta
    max_concurrent : int

    Returns
    -------
    list[dict]
    """
    skill_framing = skill_meta.read_skill_md_body()
    system_prompt = _SYSTEM_TEMPLATE.format(skill_framing=skill_framing) + "\n\n" + QNA_RECONCILIATION_FRAGMENT

    semaphore = asyncio.Semaphore(max_concurrent)
    substantial = [c for c in chunks if c.is_substantial()]
    batches = pack_chunks(substantial)

    async def _process_batch(batch: list[DocumentChunk]) -> dict:
        async with semaphore:
            user_content = format_batch_user_content(batch)
            raw = await call_llm_async(system_prompt, user_content)
            # call_llm_async always returns list[dict].
            #
            # Expected: LLM returns the envelope object → _parse_json_response
            # wraps it → raw = [{"items": [...], "no_roles_stated": false}]
            # → raw[0] is the envelope → .get("items") yields all roles.  ✓
            #
            # Fallback: LLM ignores the envelope and returns a flat array of
            # role dicts → raw = [{"role_title": "A"}, {"role_title": "B"}, ...]
            # → raw[0] has no "items" key.  Detect this and repack so the
            # accumulation loop below receives all roles, not just the first.
            if isinstance(raw, list) and raw and isinstance(raw[0], dict):
                first = raw[0]
                if "items" in first or "no_roles_stated" in first:
                    # Envelope format — standard path
                    return first
                # Flat-array format — the whole list is the items array
                return {"items": raw, "no_roles_stated": False}
            if isinstance(raw, dict):
                return raw
            return {"items": [], "no_roles_stated": None}

    results = await asyncio.gather(*[_process_batch(b) for b in batches])

    all_items: list[dict] = []
    all_batches_no_roles = True  # flipped to False as soon as any batch yields items

    for result in results:
        chunk_items = result.get("items") or []
        if chunk_items:
            all_batches_no_roles = False
        for item in chunk_items:
            all_items.append(_sanitise(item))

    # Deduplicate on (role_title, seniority_level, location_tier)
    all_items = _deduplicate(all_items)

    # Assign IDs
    for i, item in enumerate(all_items, start=1):
        item["id"] = f"RR-{i:03d}"

    # Only signal no_roles_stated when EVERY batch returned empty items AND
    # at least one batch explicitly stated no_roles_stated=true.  If even one
    # batch extracted roles (e.g. from an annexure), those items are real.
    if not all_items and all_batches_no_roles and any(
        r.get("no_roles_stated") for r in results
    ):
        log.info("rate_card_no_roles_stated")
        # Return sentinel that skill_runner can detect
        return [{"__no_roles_stated__": True}]

    log.info("rate_card_extraction_done", count=len(all_items))
    return all_items


def _sanitise(item: dict) -> dict:
    """Normalise field names and validate enum values."""
    _ALIASES: dict[str, str] = {
        "title": "role_title",
        "role": "role_title",
        "name": "role_title",
        "seniority": "seniority_level",
        "level": "seniority_level",
        "location": "location_tier",
        "tier": "location_tier",
        "rate": "rate_value",
        "source": "source_section",
        "section": "source_section",
        "source_location": "source_section",
        "excerpt": "raw_excerpt",
    }
    normalised: dict = {}
    for k, v in item.items():
        canonical = _ALIASES.get(k, k)
        if canonical not in normalised:
            normalised[canonical] = v
    normalised.pop("id", None)

    allowed = {
        "role_title", "seniority_level", "location_tier",
        "pricing_model", "rate_unit", "rate_value",
        "min_commitment_period", "replacement_backfill_terms",
        "background_verification_required",
        "raw_excerpt", "source_section", "source_type",
    }
    clean = {k: v for k, v in normalised.items() if k in allowed}

    clean.setdefault("role_title", "")

    pm = str(clean.get("pricing_model", "unspecified")).lower().strip()
    clean["pricing_model"] = pm if pm in _VALID_PRICING_MODELS else "unspecified"

    ru = str(clean.get("rate_unit", "unspecified")).lower().strip()
    clean["rate_unit"] = ru if ru in _VALID_RATE_UNITS else "unspecified"

    st = str(clean.get("source_type", "text")).lower().strip()
    clean["source_type"] = st if st in ("text", "diagram") else "text"

    return clean


def _deduplicate(items: list[dict]) -> list[dict]:
    """Remove duplicates keyed on (role_title, seniority_level, location_tier)."""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for item in items:
        norm = lambda s: re.sub(r"\s+", " ", str(s or "").strip().lower())
        key = (norm(item.get("role_title")), norm(item.get("seniority_level")), norm(item.get("location_tier")))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out
