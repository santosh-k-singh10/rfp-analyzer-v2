"""
synthesizer/synthesizer.py
---------------------------
Synthesizer — assembles the final SynthesizedOutput from skill and spine outputs.

Generic interface: does NOT special-case archetype names. All skill merging
logic operates on the generic SkillOutput/SpineOutput data structures.

V1 merge strategy (pragmatic):
1. Per-skill simple dedup (exact fingerprint match within each skill's items)
2. Cross-skill semantic dedup using dedup.py (difflib + optional LLM confirmation)
3. Per-spine-category simple dedup

IMPORTANT LIMITATION DOCUMENTED:
The synthesizer does NOT handle generic cross-archetype merge rules for new
skill combinations (e.g. "if design-build AND ams both fire, merge build-phase
SLAs into design-build output"). This must be added as explicit merge rules per
skill combination. For now, items from each skill are kept separate and
cross-skill dedup only removes true duplicates (same requirement described twice).

The Synthesizer is the last step before output — it receives all skill outputs
and spine outputs from the pipeline and returns a SynthesizedOutput.
"""

from __future__ import annotations

from typing import Any

import structlog

from core.schemas import SkillOutput, SpineOutput, SynthesizedOutput
from core.skill_registry import SkillRegistry
from synthesizer.dedup import (
    MergedItem,
    confirm_duplicates,
    find_duplicate_candidates,
    simple_dedup,
)

log = structlog.get_logger()


async def synthesize(
    skill_outputs: list[SkillOutput],
    spine: SpineOutput,
    registry: SkillRegistry,
    run_llm_dedup: bool = False,
    fired_skills: list[str] | None = None,
) -> SynthesizedOutput:
    """
    Assemble the final output from skill outputs and spine results.

    Steps:
    1. Per-skill simple dedup (removes exact duplicates within each skill)
    2. Cross-skill semantic dedup using difflib (no LLM by default)
    3. Optional LLM confirmation pass on cross-skill candidates (run_llm_dedup=True)
    4. Per-spine-category simple dedup
    5. Assemble SynthesizedOutput

    Parameters
    ----------
    skill_outputs : list[SkillOutput]
        Raw outputs from all fired skill agents (may contain duplicates).
    spine : SpineOutput
        Raw outputs from all four spine agents.
    registry : SkillRegistry
        Used to resolve display names and metadata.
    run_llm_dedup : bool
        If True, run an LLM confirmation pass on cross-skill duplicate candidates.
        Default False for fast processing; set True for high-quality runs.

    Returns
    -------
    SynthesizedOutput
    """
    log.info(
        "synthesize_start",
        skill_count=len(skill_outputs),
        spine_compliance=len(spine.compliance),
        spine_ambiguity=len(spine.ambiguity),
        spine_risk=len(spine.risk),
        spine_cs=len(spine.commercial_structure),
    )

    # Step 1: Per-skill simple dedup
    deduped_skill_outputs: list[SkillOutput] = []
    for so in skill_outputs:
        deduped_items = simple_dedup(so.items)
        deduped_skill_outputs.append(SkillOutput(
            skill_slug=so.skill_slug,
            items=deduped_items,
            schema_version=so.schema_version,
            no_roles_stated=so.no_roles_stated,
        ))
        if len(deduped_items) < len(so.items):
            log.info(
                "per_skill_dedup",
                slug=so.skill_slug,
                before=len(so.items),
                after=len(deduped_items),
            )

    # Step 2 & 3: Cross-skill semantic dedup
    # Collect all items with skill slug tags for merge tracking
    all_items_with_source: list[tuple[str, dict]] = []
    for so in deduped_skill_outputs:
        for item in so.items:
            all_items_with_source.append((so.skill_slug, item))

    if len(all_items_with_source) > 1:
        flat_items = [item for _, item in all_items_with_source]
        candidates = find_duplicate_candidates(flat_items)

        if candidates:
            if run_llm_dedup:
                merged_results = await confirm_duplicates(flat_items, candidates)
            else:
                # Without LLM, use only high-confidence similarity (>= 0.90) as auto-merge
                high_conf_candidates = [(i, j, r) for i, j, r in candidates if r >= 0.90]
                if high_conf_candidates:
                    merged_results = await confirm_duplicates(flat_items, high_conf_candidates)
                else:
                    merged_results = [MergedItem(item=item) for item in flat_items]

            # Reconstruct skill outputs from merged results
            surviving_flat = [m.item for m in merged_results]
            # Re-map surviving items back to their skill slugs by identity match
            surviving_ids = {id(m.item) for m in merged_results}
            deduped_skill_outputs = _redistribute_to_skills(
                deduped_skill_outputs, surviving_ids
            )

    # Step 4: Per-spine-category simple dedup
    deduped_spine = SpineOutput(
        compliance=simple_dedup(spine.compliance),
        ambiguity=simple_dedup(spine.ambiguity),
        risk=simple_dedup(spine.risk),
        commercial_structure=simple_dedup(spine.commercial_structure),
    )

    # Use the classifier's fired_skills list when provided so that skills which
    # fired but produced 0 items (e.g. due to gateway truncation) still appear
    # in the output and trigger the correct UI tabs.  Fall back to computing
    # from the deduped outputs only when fired_skills was not passed in.
    if fired_skills is None:
        fired_skills = [so.skill_slug for so in deduped_skill_outputs if so.items or so.no_roles_stated]

    result = SynthesizedOutput(
        skill_outputs=deduped_skill_outputs,
        spine=deduped_spine,
        fired_skills=fired_skills,
        metadata={
            "total_skill_items": sum(len(so.items) for so in deduped_skill_outputs),
            "total_spine_items": (
                len(deduped_spine.compliance)
                + len(deduped_spine.ambiguity)
                + len(deduped_spine.risk)
                + len(deduped_spine.commercial_structure)
            ),
        },
    )

    log.info(
        "synthesize_complete",
        total_skill_items=result.metadata["total_skill_items"],
        total_spine_items=result.metadata["total_spine_items"],
        fired_skills=fired_skills,
    )
    return result


def _redistribute_to_skills(
    skill_outputs: list[SkillOutput],
    surviving_ids: set[int],
) -> list[SkillOutput]:
    """
    After cross-skill dedup, rebuild skill outputs retaining only surviving items.

    Items are matched by Python object identity (id()), so the original list
    references must be preserved (not copied) when building flat_items.
    """
    result: list[SkillOutput] = []
    for so in skill_outputs:
        retained = [item for item in so.items if id(item) in surviving_ids]
        result.append(SkillOutput(
            skill_slug=so.skill_slug,
            items=retained,
            schema_version=so.schema_version,
            no_roles_stated=so.no_roles_stated,
        ))
    return result
