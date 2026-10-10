"""
app/routers/skills.py
----------------------
Skills registry endpoint:
  GET /skills — returns all registered skills with metadata
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from core.skill_registry import get_registry

router = APIRouter()


@router.get("/skills")
async def get_skills() -> dict[str, Any]:
    """
    Return all registered skills from the skill registry.

    Includes both top-level skills and nested sub-skills.
    Each entry shows slug, display_name, description, parent, and schema_version.
    """
    registry = get_registry()
    all_skills = registry.get_all_skills()

    top_level = [s for s in all_skills if s.parent is None]
    nested = [s for s in all_skills if s.parent is not None]

    def _skill_summary(s: Any) -> dict:
        return {
            "slug": s.slug,
            "display_name": s.display_name,
            "description": s.description,
            "parent": s.parent,
            "schema_version": s.schema_version,
            "has_test_cases": s.test_cases_path.exists() and any(s.test_cases_path.glob("*.yaml")),
        }

    top_level_list = [_skill_summary(s) for s in sorted(top_level, key=lambda x: x.slug)]
    sub_skills_list = [_skill_summary(s) for s in sorted(nested, key=lambda x: x.slug)]

    return {
        "total": len(all_skills),
        # Flat list consumed by review.html loadSkillLabels()
        "skills": top_level_list + sub_skills_list,
        # Structured breakdown (kept for other consumers)
        "top_level": top_level_list,
        "sub_skills": sub_skills_list,
    }
