"""
skills_engine/skill_runner.py
------------------------------
Generic skill runner — runs a single skill module against a list of DocumentChunks.

Each skill agent (design_build.py, ams.py, etc.) implements:
    async def extract(chunks, skill_meta) -> list[dict]

This module provides:
- skill_runner(): dispatches to the right agent based on skill_meta.slug
- A fallback generic extractor for skills without a dedicated agent

The router maps skill slugs to their dedicated agent modules. Sub-skills that
share extraction logic (e.g. all packaged/* sub-skills) are routed through
the same agent (packaged.py) with sub-skill-specific framing from skill_meta.
"""

from __future__ import annotations

import importlib
import structlog

from core.schemas import DocumentChunk, SkillOutput
from core.skill_registry import SkillMeta

log = structlog.get_logger()

# Map skill slugs to their agent module name (within skills_engine/)
# Sub-skills of packaged all route through packaged.py
_SKILL_TO_MODULE: dict[str, str] = {
    "design-build": "skills_engine.design_build",
    "ams": "skills_engine.ams",
    "rate-card": "skills_engine.rate_card",
    "packaged": "skills_engine.packaged",
    "packaged-sap": "skills_engine.packaged",
    "packaged-salesforce": "skills_engine.packaged",
    "packaged-oracle": "skills_engine.packaged",
    "packaged-other": "skills_engine.packaged",
}


async def run_skill(
    chunks: list[DocumentChunk],
    skill_meta: SkillMeta,
) -> SkillOutput:
    """
    Run the extraction agent for a single skill against the provided chunks.

    Dispatches to the dedicated agent module for the skill slug, or falls
    back to a generic extractor when no dedicated module is registered.

    Parameters
    ----------
    chunks : list[DocumentChunk]
        All document chunks (the agent filters to substantial ones internally).
    skill_meta : SkillMeta
        Loaded skill metadata including SKILL.md body and schema.yaml.

    Returns
    -------
    SkillOutput
        Extracted items for this skill.
    """
    module_name = _SKILL_TO_MODULE.get(skill_meta.slug)

    if module_name:
        try:
            mod = importlib.import_module(module_name)
            items = await mod.extract(chunks, skill_meta)
            # Check for rate-card no_roles_stated sentinel
            no_roles_stated = False
            if items and isinstance(items[0], dict) and items[0].get("__no_roles_stated__"):
                items = []
                no_roles_stated = True
            return SkillOutput(
                skill_slug=skill_meta.slug,
                items=items,
                schema_version=skill_meta.schema_version,
                no_roles_stated=no_roles_stated,
            )
        except Exception as exc:
            log.error(
                "skill_runner_agent_error",
                slug=skill_meta.slug,
                module=module_name,
                error=str(exc),
            )
            return SkillOutput(skill_slug=skill_meta.slug, items=[])

    # Fallback: no dedicated agent — use generic extractor
    log.warning("skill_runner_no_dedicated_agent", slug=skill_meta.slug)
    items = await _generic_extract(chunks, skill_meta)
    return SkillOutput(
        skill_slug=skill_meta.slug,
        items=items,
        schema_version=skill_meta.schema_version,
    )


async def _generic_extract(
    chunks: list[DocumentChunk],
    skill_meta: SkillMeta,
) -> list[dict]:
    """
    Generic extraction agent used when no dedicated agent is registered.

    Builds a prompt from the SKILL.md body and schema.yaml field list,
    then calls the LLM once per *batch* of packed chunks.
    """
    import asyncio
    from core.llm_client import (
        call_llm_async,
        DEFAULT_MAX_CONCURRENT,
        pack_chunks,
        format_batch_user_content,
    )

    skill_body = skill_meta.read_skill_md_body()
    fields = skill_meta.schema_dict.get("items", [])
    field_names = [f.get("field", "") for f in fields if f.get("field")]

    system_prompt = (
        f"You are an expert analyst extracting structured information from an RFP.\n\n"
        f"SKILL FRAMING:\n{skill_body}\n\n"
        f"Extract items matching these fields: {', '.join(field_names)}.\n"
        f"Return a JSON array of objects. Each object must include all listed fields.\n"
        f"Return [] if no relevant content is found in the chunk.\n"
        f"Output AT MOST 25 items per call. If there are more, return the 25 highest-confidence ones.\n"
        f"Output ONLY valid JSON — no markdown fences, no extra text."
    )

    semaphore = asyncio.Semaphore(DEFAULT_MAX_CONCURRENT)
    substantial = [c for c in chunks if c.is_substantial()]
    batches = pack_chunks(substantial)

    async def _process_batch(batch: list[DocumentChunk]) -> list[dict]:
        async with semaphore:
            user_content = format_batch_user_content(batch)
            return await call_llm_async(system_prompt, user_content)

    results = await asyncio.gather(*[_process_batch(b) for b in batches])
    items: list[dict] = []
    for batch_result in results:
        items.extend(batch_result)
    return items
