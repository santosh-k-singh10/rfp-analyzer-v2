"""
core/skill_registry.py
-----------------------
Registry-driven skill loader — the spine of the V2 architecture.

Walks the /skills/ directory tree on first access, loads every SKILL.md
(parses YAML frontmatter) and every schema.yaml, and assembles a SkillMeta
dataclass for each skill discovered.

Design:
- Adding a new skill folder (with SKILL.md + schema.yaml) requires ZERO code
  changes — the registry auto-discovers it on next startup.
- The registry is a process-level singleton (get_registry()) so the disk scan
  happens once and is cached for the lifetime of the process.
- get_classifier_context() returns a formatted string of all top-level skills'
  slug + description, used to template the classifier prompt at request time.

SKILL.md frontmatter schema (required fields):
    ---
    name: <slug>               # e.g. "design-build"
    display_name: <str>
    description: <str>         # used in classifier prompt
    parent: null | <slug>      # null for top-level, slug for nested sub-skills
    ---

schema.yaml is loaded and stored as a dict; its 'version' field is used as
SkillMeta.schema_version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
import structlog

log = structlog.get_logger()


def _extract_section(body: str, heading: str) -> str:
    """
    Extract the text content of a named ``## Heading`` section from a Markdown body.

    Returns the section's text (stripped), or an empty string if the heading is
    not present. Stops at the next ``##`` heading.
    """
    pattern = re.compile(
        r"^#{1,3}\s+" + re.escape(heading) + r"\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    match = pattern.search(body)
    if not match:
        return ""
    start = match.end()
    # Find the start of the next heading (any level)
    next_heading = re.search(r"^#{1,3}\s+", body[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(body)
    return body[start:end].strip()


# Resolved once at import time — skills/ lives alongside the package root
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = _PACKAGE_ROOT / "skills"


# ─────────────────────────────────────────────────────────────────────────────
# SkillMeta dataclass
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SkillMeta:
    """Metadata for a single skill, loaded from its SKILL.md and schema.yaml."""

    slug: str
    display_name: str
    description: str
    parent: Optional[str]          # None for top-level skills
    skill_md_path: Path
    schema_path: Path
    test_cases_path: Path
    schema_dict: dict              # parsed schema.yaml contents
    schema_version: str = "1.0"

    def read_skill_md_body(self) -> str:
        """Return the body (non-frontmatter) text of SKILL.md."""
        text = self.skill_md_path.read_text(encoding="utf-8")
        # Strip YAML frontmatter (--- ... ---)
        match = re.match(r"^---\n.*?---\n", text, re.DOTALL)
        if match:
            return text[match.end():].strip()
        return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# SkillRegistry class
# ─────────────────────────────────────────────────────────────────────────────


class SkillRegistry:
    """
    Discovers and indexes all skills in the /skills/ directory tree.

    Constructor: walks skills_root looking for SKILL.md files.
    Each directory containing a SKILL.md is treated as a skill.
    """

    def __init__(self, skills_root: Path) -> None:
        self._root = skills_root
        self._skills: dict[str, SkillMeta] = {}
        self._load()

    def _load(self) -> None:
        """Walk the skills directory tree and load all SKILL.md files."""
        if not self._root.exists():
            log.warning("skills_root_not_found", path=str(self._root))
            return

        skill_md_files = list(self._root.rglob("SKILL.md"))
        log.info("skill_registry_loading", count=len(skill_md_files), root=str(self._root))

        for skill_md_path in sorted(skill_md_files):
            try:
                meta = self._load_skill(skill_md_path)
                if meta:
                    self._skills[meta.slug] = meta
                    log.debug("skill_loaded", slug=meta.slug, parent=meta.parent)
            except Exception as exc:
                log.error("skill_load_error", path=str(skill_md_path), error=str(exc))

        log.info("skill_registry_ready", skills=list(self._skills.keys()))

    def _load_skill(self, skill_md_path: Path) -> Optional[SkillMeta]:
        """Parse a single SKILL.md and its accompanying schema.yaml."""
        skill_dir = skill_md_path.parent
        text = skill_md_path.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not fm_match:
            log.warning("skill_no_frontmatter", path=str(skill_md_path))
            return None

        frontmatter = yaml.safe_load(fm_match.group(1))
        if not isinstance(frontmatter, dict):
            log.warning("skill_invalid_frontmatter", path=str(skill_md_path))
            return None

        slug = frontmatter.get("name")
        display_name = frontmatter.get("display_name", slug)
        description = frontmatter.get("description", "")
        parent = frontmatter.get("parent")  # None or str

        if not slug:
            log.warning("skill_missing_name", path=str(skill_md_path))
            return None

        # Load schema.yaml
        schema_path = skill_dir / "schema.yaml"
        schema_dict: dict = {}
        schema_version = "1.0"
        if schema_path.exists():
            schema_dict = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
            schema_version = str(schema_dict.get("version", "1.0"))
        else:
            log.warning("skill_no_schema", slug=slug, path=str(schema_path))

        test_cases_path = skill_dir / "test_cases"

        return SkillMeta(
            slug=slug,
            display_name=display_name,
            description=description,
            parent=parent if parent else None,
            skill_md_path=skill_md_path,
            schema_path=schema_path,
            test_cases_path=test_cases_path,
            schema_dict=schema_dict,
            schema_version=schema_version,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Query methods
    # ─────────────────────────────────────────────────────────────────────────

    def get_all_skills(self) -> list[SkillMeta]:
        """Return all registered skills (top-level and nested)."""
        return list(self._skills.values())

    def get_top_level_skills(self) -> list[SkillMeta]:
        """Return skills with parent=None (direct children of /skills/)."""
        return [s for s in self._skills.values() if s.parent is None]

    def get_children(self, parent_slug: str) -> list[SkillMeta]:
        """Return all sub-skills whose parent matches parent_slug."""
        return [s for s in self._skills.values() if s.parent == parent_slug]

    def get_skill(self, slug: str) -> Optional[SkillMeta]:
        """Return the SkillMeta for the given slug, or None if not found."""
        return self._skills.get(slug)

    def get_classifier_context(self) -> str:
        """
        Return a formatted string of all top-level skills for the classifier prompt.

        Includes each skill's slug, description, and — if present — the content
        of the ``## Classification cues`` section from its SKILL.md body.
        This makes classification guidance self-contained per skill: adding a new
        skill folder automatically adds its cues to the classifier prompt with no
        code changes.

        Format per skill:
            - <slug>: <description>
              Classification cues: <cues text, if present>
        """
        top_level = self.get_top_level_skills()
        lines: list[str] = []
        for skill in sorted(top_level, key=lambda s: s.slug):
            entry = f"- {skill.slug}: {skill.description}"
            cues = _extract_section(skill.read_skill_md_body(), "Classification cues")
            if cues:
                # Indent cue lines so they read as a sub-block under the skill
                indented = "\n".join(f"  {ln}" for ln in cues.splitlines())
                entry += f"\n  Classification cues:\n{indented}"
            lines.append(entry)
        return "\n".join(lines)

    def reload(self) -> None:
        """Re-scan the skills directory (useful for development hot-reloads)."""
        self._skills.clear()
        self._load()


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────

_registry_instance: Optional[SkillRegistry] = None


def get_registry() -> SkillRegistry:
    """
    Return the process-level SkillRegistry singleton.

    The disk scan happens only on first call; subsequent calls return the
    cached instance. Thread-safe for read operations (no writes after init).
    """
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = SkillRegistry(SKILLS_ROOT)
    return _registry_instance
