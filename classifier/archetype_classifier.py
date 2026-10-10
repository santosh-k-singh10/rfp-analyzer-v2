"""
classifier/archetype_classifier.py
-----------------------------------
Registry-driven archetype classifier.

Classifies an RFP against all registered top-level skills by calling the LLM
with a prompt templated from SkillRegistry.get_classifier_context(). The LLM
returns a JSON dict of {archetype_slug: confidence_float}. Skills whose
confidence meets or exceeds ARCHETYPE_THRESHOLD are added to fired_skills.

Sub-skill routing:
If a top-level skill fires AND has children registered in the registry (e.g.
'packaged' has children packaged-sap, packaged-salesforce, etc.), a second
LLM pass determines which sub-skill(s) apply. Sub-skill scores and their
parent field are included in the ClassifierOutput.

Design:
- The classifier prompt is templated at call-time from the registry — NO
  hardcoded archetype names anywhere in this file.
- Unknown archetypes returned by the LLM (not in registry) are silently ignored.
- The threshold is read from env ARCHETYPE_THRESHOLD (default 0.25).
"""

from __future__ import annotations

import json
import os
from typing import Optional

import structlog

from core.llm_client import call_llm_async, DEFAULT_MAX_CONCURRENT
from core.schemas import ArchetypeScore, ClassifierOutput
from core.skill_registry import SkillRegistry

log = structlog.get_logger()

ARCHETYPE_THRESHOLD = float(os.getenv("ARCHETYPE_THRESHOLD", "0.25"))

# ─────────────────────────────────────────────────────────────────────────────
# Prompt templates
# ─────────────────────────────────────────────────────────────────────────────

_CLASSIFIER_SYSTEM = """\
You are an expert RFP analyst. Your task is to classify an RFP excerpt against
a set of known engagement archetypes and assign a confidence score to each.

Available archetypes (name: description — classification cues):
{archetype_context}

Instructions:
- Read the RFP text carefully.
- For each archetype listed above, output a confidence score between 0.0 and 1.0.
  - 0.0 = definitely does NOT match this archetype
  - 1.0 = extremely strong match
- A single RFP may match multiple archetypes (hybrid RFP). Score each independently.
- Use the description and classification cues above to guide your scoring.
  Do not rely on any prior knowledge of archetype names — use only what is listed.

Output ONLY a valid JSON object with archetype slugs as keys and confidence
floats as values. Example:
{{"design-build": 0.85, "ams": 0.30, "rate-card": 0.10}}

Do not include any explanation or markdown fences.
"""

_SUB_SKILL_SYSTEM = """\
You are an expert RFP analyst. A top-level archetype '{parent_slug}' has fired
for this RFP. Your task is to identify which sub-skill(s) apply.

Available sub-skills of '{parent_slug}':
{sub_skill_context}

Instructions:
- For each sub-skill, output a confidence score between 0.0 and 1.0.
- A sub-skill fires when the RFP explicitly refers to that product or platform.
- Multiple sub-skills may fire simultaneously (e.g. both sap and oracle in scope).

Output ONLY a valid JSON object with sub-skill slugs as keys and confidence
floats as values. Example:
{{"packaged-sap": 0.90, "packaged-salesforce": 0.05, "packaged-oracle": 0.05, "packaged-other": 0.10}}

Do not include any explanation or markdown fences.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


def _build_rfp_summary(text: str, max_chars: int = 12000) -> str:
    """
    Sample the classification text evenly across the full document set so the
    LLM sees content from every document, not just the first file's opening.

    With multi-document uploads (primary RFP + annexures) the combined text can
    exceed 90 000 chars.  The old head+tail approach fed only the first ~3 000
    chars to the classifier, which skewed scores toward whatever archetype
    dominated the first document (e.g. rate-card) and starved AMS / Design-Build
    content that lives in later sections.

    Strategy: divide the full text into N equal windows and take a proportional
    slice from each window.  This guarantees that content from every document
    region — rate card tables, SLA matrices, functional requirements — is
    represented in the classification call.
    """
    if len(text) <= max_chars:
        return text

    n_windows = 6
    window_size = max(len(text) // n_windows, 1)
    slice_chars = max_chars // n_windows   # 2 000 chars per window
    parts: list[str] = []
    for i in range(n_windows):
        start = i * window_size
        parts.append(text[start: start + slice_chars])
    return "\n\n[...]\n\n".join(parts)


def _parse_scores(raw_result: list[dict] | dict, known_slugs: set[str]) -> dict[str, float]:
    """
    Extract {slug: confidence} from the LLM result.

    call_llm_async wraps a dict response in a list; handle both shapes.
    Unknown slugs (not in registry) are ignored.
    """
    obj: dict = {}
    if isinstance(raw_result, list) and raw_result:
        obj = raw_result[0] if isinstance(raw_result[0], dict) else {}
    elif isinstance(raw_result, dict):
        obj = raw_result

    scores: dict[str, float] = {}
    for slug, conf in obj.items():
        if slug not in known_slugs:
            log.debug("classifier_unknown_archetype_ignored", slug=slug)
            continue
        try:
            scores[slug] = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            log.warning("classifier_invalid_confidence", slug=slug, value=conf)
    return scores


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


async def classify_rfp(
    text: str,
    registry: SkillRegistry,
    threshold: float = ARCHETYPE_THRESHOLD,
) -> ClassifierOutput:
    """
    Classify an RFP text against all registered top-level skills.

    Steps:
    1. Build the classifier prompt from registry.get_classifier_context()
    2. Call the LLM; parse {slug: confidence} dict
    3. Ignore unknown archetypes
    4. For any top-level skill above threshold that has children, run a
       second pass to score the sub-skills
    5. Return ClassifierOutput with all scores and fired_skills list

    Parameters
    ----------
    text : str
        The full (or sampled) RFP text.
    registry : SkillRegistry
        The loaded skill registry.
    threshold : float
        Minimum confidence for a skill to be added to fired_skills.

    Returns
    -------
    ClassifierOutput
    """
    top_level_skills = registry.get_top_level_skills()
    known_top_slugs = {s.slug for s in top_level_skills}

    archetype_context = registry.get_classifier_context()
    system_prompt = _CLASSIFIER_SYSTEM.format(archetype_context=archetype_context)
    user_content = _build_rfp_summary(text)

    log.info("classifier_calling_llm", top_level_count=len(top_level_skills))
    raw = await call_llm_async(system_prompt, user_content, max_tokens=512)
    top_scores = _parse_scores(raw, known_top_slugs)

    # Fill in 0.0 for any top-level skill the LLM omitted
    for slug in known_top_slugs:
        top_scores.setdefault(slug, 0.0)

    all_scores: list[ArchetypeScore] = []
    fired_skills: list[str] = []

    for slug, conf in top_scores.items():
        all_scores.append(ArchetypeScore(archetype=slug, confidence=conf, parent=None))
        if conf >= threshold:
            fired_skills.append(slug)

    # Sub-skill routing: for each fired top-level skill with children, run a second pass
    for parent_slug in list(fired_skills):
        children = registry.get_children(parent_slug)
        if not children:
            continue

        sub_context_lines = [f"- {c.slug}: {c.description}" for c in sorted(children, key=lambda x: x.slug)]
        sub_context = "\n".join(sub_context_lines)
        sub_system = _SUB_SKILL_SYSTEM.format(
            parent_slug=parent_slug,
            sub_skill_context=sub_context,
        )

        log.info("classifier_sub_skill_pass", parent=parent_slug, children=[c.slug for c in children])
        sub_raw = await call_llm_async(sub_system, user_content, max_tokens=256)
        known_child_slugs = {c.slug for c in children}
        sub_scores = _parse_scores(sub_raw, known_child_slugs)

        # Fill in 0.0 for any child the LLM omitted
        for c in children:
            sub_scores.setdefault(c.slug, 0.0)

        for child_slug, child_conf in sub_scores.items():
            all_scores.append(
                ArchetypeScore(archetype=child_slug, confidence=child_conf, parent=parent_slug)
            )
            if child_conf >= threshold:
                fired_skills.append(child_slug)

    log.info(
        "classifier_done",
        fired_skills=fired_skills,
        all_scores={s.archetype: s.confidence for s in all_scores},
    )
    return ClassifierOutput(scores=all_scores, fired_skills=fired_skills)
