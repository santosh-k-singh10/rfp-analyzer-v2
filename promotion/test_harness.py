"""
promotion/test_harness.py
--------------------------
Skill promotion test harness.

Loads all test_cases/*.yaml files for a skill, runs the skill's extraction
agent against the input_excerpt in each test case, and checks that all
expected_fields are present and match.

Test case format (YAML):
    version: "1.0"
    skill: <skill-slug>
    description: "..."
    input_excerpt: |
      The RFP text to process
    expected_fields:
      - path: "items[0].title"
        check: contains     # or "equals"
        value: "Severity 1"

Check types:
- contains: str(actual).lower() contains str(value).lower()
- equals: actual == value (type-sensitive)

Usage:
    result = await run_promotion_check("ams", registry)
    print(result.passed, result.failed_cases)
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
import structlog

from core.skill_registry import SkillRegistry, SkillMeta

log = structlog.get_logger()


@dataclass
class TestCaseResult:
    """Result of a single test case execution."""

    case_file: str
    description: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    extracted_output: Optional[list[dict]] = None
    error: Optional[str] = None


@dataclass
class PromotionResult:
    """Aggregated result of all test cases for a skill."""

    skill_slug: str
    passed: bool
    total_cases: int
    passed_cases: int
    failed_cases: list[TestCaseResult] = field(default_factory=list)
    all_case_results: list[TestCaseResult] = field(default_factory=list)


async def run_promotion_check(
    skill_slug: str,
    registry: SkillRegistry,
) -> PromotionResult:
    """
    Run all test cases for a skill and report pass/fail.

    Parameters
    ----------
    skill_slug : str
        The skill slug to test (e.g. "ams", "design-build").
    registry : SkillRegistry
        Loaded registry containing the skill's metadata.

    Returns
    -------
    PromotionResult
        Aggregated result with per-case details.
    """
    meta = registry.get_skill(skill_slug)
    if meta is None:
        log.error("promotion_skill_not_found", slug=skill_slug)
        return PromotionResult(
            skill_slug=skill_slug,
            passed=False,
            total_cases=0,
            passed_cases=0,
        )

    test_cases_path = meta.test_cases_path
    if not test_cases_path.exists():
        log.warning("promotion_no_test_cases", slug=skill_slug, path=str(test_cases_path))
        return PromotionResult(
            skill_slug=skill_slug,
            passed=True,  # no cases = vacuously pass
            total_cases=0,
            passed_cases=0,
        )

    case_files = sorted(test_cases_path.glob("*.yaml"))
    if not case_files:
        return PromotionResult(
            skill_slug=skill_slug,
            passed=True,
            total_cases=0,
            passed_cases=0,
        )

    log.info("promotion_start", slug=skill_slug, cases=len(case_files))

    all_results: list[TestCaseResult] = []
    for case_file in case_files:
        result = await _run_test_case(case_file, meta)
        all_results.append(result)

    passed_count = sum(1 for r in all_results if r.passed)
    failed_results = [r for r in all_results if not r.passed]
    overall_passed = len(failed_results) == 0

    promotion_result = PromotionResult(
        skill_slug=skill_slug,
        passed=overall_passed,
        total_cases=len(all_results),
        passed_cases=passed_count,
        failed_cases=failed_results,
        all_case_results=all_results,
    )

    log.info(
        "promotion_done",
        slug=skill_slug,
        passed=overall_passed,
        total=len(all_results),
        passed_count=passed_count,
    )
    return promotion_result


async def _run_test_case(case_file: Path, meta: SkillMeta) -> TestCaseResult:
    """Execute a single test case file."""
    from core.schemas import DocumentChunk
    from skills_engine.skill_runner import run_skill

    try:
        case_data = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return TestCaseResult(
            case_file=str(case_file.name),
            description="",
            passed=False,
            error=f"Failed to parse YAML: {exc}",
        )

    description = case_data.get("description", "")
    input_excerpt = case_data.get("input_excerpt", "")
    expected_fields = case_data.get("expected_fields", [])

    log.info("promotion_case_start", case=case_file.name, skill=meta.slug)

    # Create a synthetic chunk from the input excerpt
    synthetic_chunk = DocumentChunk(
        section="Test Input",
        text=input_excerpt,
        page=1,
        char_count=len(input_excerpt),
        source_type="text",
    )

    # Run the skill
    try:
        skill_output = await run_skill([synthetic_chunk], meta)
    except Exception as exc:
        return TestCaseResult(
            case_file=str(case_file.name),
            description=description,
            passed=False,
            error=f"Skill extraction failed: {exc}",
        )

    # Build the output structure for path-based assertions
    # rate-card returns a top-level no_roles_stated flag
    output_struct: dict[str, Any] = {
        "items": skill_output.items,
        "no_roles_stated": skill_output.no_roles_stated,
    }

    # Check expected fields
    failures: list[str] = []
    for assertion in expected_fields:
        path = assertion.get("path", "")
        check = assertion.get("check", "equals")
        expected_value = assertion.get("value")

        actual = _resolve_path(output_struct, path)
        if actual is _MISSING:
            failures.append(f"Path '{path}' not found in output")
            continue

        if check == "equals":
            if actual != expected_value:
                failures.append(
                    f"Path '{path}': expected {expected_value!r}, got {actual!r}"
                )
        elif check == "contains":
            if not _check_contains(actual, expected_value):
                failures.append(
                    f"Path '{path}': expected to contain {expected_value!r}, got {actual!r}"
                )
        else:
            failures.append(f"Unknown check type '{check}' in assertion")

    passed = len(failures) == 0
    log.info(
        "promotion_case_done",
        case=case_file.name,
        passed=passed,
        failures=failures,
    )
    return TestCaseResult(
        case_file=str(case_file.name),
        description=description,
        passed=passed,
        failures=failures,
        extracted_output=skill_output.items,
    )


# Sentinel for missing path resolution
_MISSING = object()


def _resolve_path(obj: Any, path: str) -> Any:
    """
    Resolve a dotted/indexed path against a nested structure.

    Supports:
    - "items[0].title"
    - "no_roles_stated"
    - "items"
    """
    parts = re.split(r"\.(?![^\[]*\])", path)  # split on . but not inside []
    current = obj
    for part in parts:
        idx_match = re.match(r"^(\w+)\[(\d+)\]$", part)
        if idx_match:
            key, idx = idx_match.group(1), int(idx_match.group(2))
            if not isinstance(current, dict) or key not in current:
                return _MISSING
            current = current[key]
            if not isinstance(current, list) or idx >= len(current):
                return _MISSING
            current = current[idx]
        elif isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        else:
            return _MISSING
    return current


def _check_contains(actual: Any, expected: Any) -> bool:
    """Check if actual contains expected (string or list membership)."""
    if isinstance(actual, list):
        # Check if any list item contains the expected string
        return any(
            str(expected).lower() in str(item).lower() or item == expected
            for item in actual
        )
    return str(expected).lower() in str(actual).lower()


# Alias so promotion/runner.py can import either name
run_promotion_check_path = run_promotion_check
