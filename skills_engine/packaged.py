"""
skills_engine/packaged.py
--------------------------
Packaged Solutions skill extraction agent.

Handles all packaged/* sub-skills (SAP, Salesforce, Oracle, Other) by using
the sub-skill's SKILL.md framing and schema.yaml field definitions.

Each sub-skill has its own distinct schema, so this agent dynamically builds
the extraction prompt from the sub-skill's schema.yaml items list.

Extracts:
- Product/module scope
- Configuration vs. customization decisions
- License model and metrics
- Data migration scope
- Product-specific integration patterns
- Deployment model and upgrade path
"""

from __future__ import annotations

import asyncio
import re

import structlog

from core.llm_client import call_llm_async, DEFAULT_MAX_CONCURRENT, pack_chunks, format_batch_user_content
from core.prompts.qna_reconciliation import QNA_RECONCILIATION_FRAGMENT
from core.schemas import DocumentChunk
from core.skill_registry import SkillMeta

log = structlog.get_logger()

_SYSTEM_TEMPLATE = """\
You are a packaged solutions implementation specialist extracting structured information
from an RFP that involves a commercial off-the-shelf (COTS) or SaaS product.

SKILL FRAMING:
{skill_framing}

SCHEMA FIELDS TO EXTRACT:
{schema_fields}

For each item found, output a JSON array with EXACTLY these fields:
{json_template}

RULES:
- Extract only what is explicitly stated in the RFP — do NOT infer or hallucinate
- set mandatory=true when the RFP uses "shall", "must", "is required", "mandatory"
- set mandatory=false when the RFP uses "may", "can", "optionally"
- raw_excerpt must be verbatim source text
- source_type="diagram" when the excerpt references a [FIGURE ...] annotation
- Return [] if no relevant packaged-solution content is found
- Output AT MOST 25 items per call. If there are more, return the 25 highest-confidence ones.
- Output ONLY valid JSON — no markdown fences, no extra text
"""


def _build_json_template(skill_meta: SkillMeta) -> tuple[str, str]:
    """Build schema fields description and JSON template from schema.yaml."""
    fields = skill_meta.schema_dict.get("items", [])
    field_lines: list[str] = []
    json_fields: list[str] = []

    for f in fields:
        name = f.get("field", "")
        ftype = f.get("type", "string")
        desc = f.get("description", "")
        values = f.get("values", [])
        required = f.get("required", True)

        if not name or name == "id":
            continue

        if values:
            field_lines.append(f"  - {name} ({ftype}): one of {values} | {desc}")
            json_fields.append(f'    "{name}": "<one of: {" | ".join(str(v) for v in values)}>"')
        else:
            req_str = "required" if required else "optional"
            field_lines.append(f"  - {name} ({ftype}, {req_str}): {desc}")
            json_fields.append(f'    "{name}": "<{ftype}>"')

    schema_fields = "\n".join(field_lines)
    json_template = "[\n  {{\n" + ",\n".join(json_fields) + "\n  }}\n]"
    return schema_fields, json_template


async def extract(
    chunks: list[DocumentChunk],
    skill_meta: SkillMeta,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[dict]:
    """
    Extract packaged-solution items from document chunks.

    Works for all packaged/* sub-skills; the framing and schema are loaded
    dynamically from skill_meta so the same agent handles SAP, Salesforce,
    Oracle, and Other sub-skills.

    Parameters
    ----------
    chunks : list[DocumentChunk]
    skill_meta : SkillMeta
    max_concurrent : int

    Returns
    -------
    list[dict]
        Items matching the sub-skill's schema.yaml.
    """
    skill_framing = skill_meta.read_skill_md_body()
    schema_fields, json_template = _build_json_template(skill_meta)
    system_prompt = _SYSTEM_TEMPLATE.format(
        skill_framing=skill_framing,
        schema_fields=schema_fields,
        json_template=json_template,
    ) + "\n\n" + QNA_RECONCILIATION_FRAGMENT

    semaphore = asyncio.Semaphore(max_concurrent)
    substantial = [c for c in chunks if c.is_substantial()]
    batches = pack_chunks(substantial)

    async def _process_batch(batch: list[DocumentChunk]) -> list[dict]:
        async with semaphore:
            user_content = format_batch_user_content(batch)
            raw_items = await call_llm_async(system_prompt, user_content)
            ref_chunk = batch[0]
            return [_sanitise(item, ref_chunk, skill_meta) for item in raw_items]

    results = await asyncio.gather(*[_process_batch(b) for b in batches])

    items: list[dict] = []
    counter = 1
    for batch_result in results:
        for item in batch_result:
            item["id"] = f"{skill_meta.slug.upper().replace('-', '')}-{counter:03d}"
            items.append(item)
            counter += 1

    log.info("packaged_extraction_done", skill=skill_meta.slug, count=len(items), batches=len(batches))
    return items


def _sanitise(item: dict, chunk: DocumentChunk, skill_meta: SkillMeta) -> dict:
    """Normalise common field aliases and enforce type constraints."""
    # Strip LLM-assigned id
    item.pop("id", None)

    # Title cleanup
    if isinstance(item.get("title"), str):
        item["title"] = re.sub(r"^[A-Z]+-\d+[:\s\-]+\s*", "", item["title"]).strip()
        words = item["title"].split()
        if len(words) > 15:
            item["title"] = " ".join(words[:15]) + "…"

    # mandatory normalisation
    mandatory = item.get("mandatory")
    if not isinstance(mandatory, bool):
        item["mandatory"] = bool(mandatory) if mandatory is not None else True

    # source_type
    st = str(item.get("source_type", chunk.source_type)).lower().strip()
    item["source_type"] = st if st in ("text", "diagram") else chunk.source_type

    # source_section fallback
    item.setdefault("source_section", chunk.section)
    item.setdefault("raw_excerpt", "")
    item.setdefault("description", "")
    item.setdefault("title", "")

    return item
