"""
promotion/runner.py
--------------------
CLI entrypoint for running the skill promotion workflow.

Usage:
    # Check tests without promoting:
    python -m promotion.runner --skill ams
    python -m promotion.runner --all

    # Run tests and promote draft → live on pass:
    python -m promotion.runner --skill ams --promote
    python -m promotion.runner --all --promote

Draft/live staging:
    The registry always loads the live SKILL.md at each skill's canonical path
    (skills/<slug>/SKILL.md).  A draft lives at skills/<slug>/SKILL.md.draft.

    --promote:
        1. Checks that a draft exists (exits with code 2 if not).
        2. Runs the skill's test_cases/ against the DRAFT content (by temporarily
           swapping it in for the test run only — the live file is not touched until
           tests pass).
        3. On pass: copies draft → live, removes the draft, calls registry.reload().
        4. On fail: leaves live untouched, prints the failing cases.

    Without --promote:
        Runs the test harness against the current live SKILL.md (no draft involved).
        Useful for CI validation of the live content.

Exit codes:
    0 — all tests passed (and, if --promote, promotion succeeded)
    1 — one or more tests failed
    2 — skill not found, draft not found (with --promote), or other error
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from core.skill_registry import get_registry, SKILLS_ROOT
from promotion.test_harness import PromotionResult, run_promotion_check_path


def _print_result(result: PromotionResult) -> None:
    """Print a formatted summary of the promotion result."""
    status = "✓ PASSED" if result.passed else "✗ FAILED"
    print(f"\n{'='*60}")
    print(f"Skill:  {result.skill_slug}")
    print(f"Status: {status}")
    print(f"Cases:  {result.passed_cases}/{result.total_cases} passed")

    if result.failed_cases:
        print("\nFailed cases:")
        for case in result.failed_cases:
            print(f"\n  [{case.case_file}] {case.description}")
            if case.error:
                print(f"    Error: {case.error}")
            for failure in case.failures:
                print(f"    ✗ {failure}")

    if result.passed and result.total_cases > 0:
        print("\nAll test cases passed.")
    elif result.total_cases == 0:
        print("\nNo test cases found. Add test cases before promoting.")
    print()


def _find_skill_dir(slug: str) -> Path | None:
    """Locate a skill directory by slug (searches the SKILLS_ROOT tree)."""
    for skill_md in SKILLS_ROOT.rglob("SKILL.md"):
        skill_dir = skill_md.parent
        # Quick frontmatter slug check
        text = skill_md.read_text(encoding="utf-8")
        if f"name: {slug}" in text:
            return skill_dir
    return None


async def _run_check(slug: str, promote: bool) -> int:
    """
    Run promotion check for one skill.  Returns exit code (0/1/2).
    """
    registry = get_registry()

    if promote:
        # ── Draft-gated promotion ──────────────────────────────────────────
        skill_dir = _find_skill_dir(slug)
        if skill_dir is None:
            print(f"Error: skill '{slug}' not found in registry.", file=sys.stderr)
            return 2

        draft_path = skill_dir / "SKILL.md.draft"
        live_path  = skill_dir / "SKILL.md"

        if not draft_path.exists():
            print(
                f"Error: no draft found for '{slug}'.\n"
                f"  Expected: {draft_path}\n"
                f"  Create a draft by saving your edits as SKILL.md.draft before promoting.",
                file=sys.stderr,
            )
            return 2

        print(f"Running tests against DRAFT for '{slug}'...")

        # Temporarily swap draft → live for the test run only
        live_backup = skill_dir / "SKILL.md.live_backup"
        shutil.copy2(live_path, live_backup)
        shutil.copy2(draft_path, live_path)
        registry.reload()  # pick up the draft content

        try:
            result = await run_promotion_check_path(slug, registry)
        finally:
            # Always restore live, regardless of test outcome
            shutil.copy2(live_backup, live_path)
            live_backup.unlink()
            registry.reload()  # restore live content in registry

        _print_result(result)

        if result.passed and result.total_cases > 0:
            # Promote: copy draft over live permanently
            shutil.copy2(draft_path, live_path)
            draft_path.unlink()
            registry.reload()
            print(f"✓ Promoted draft → live for skill '{slug}'.")
            print(f"  Draft removed: {draft_path}")
            return 0
        else:
            print(f"✗ Promotion blocked for '{slug}' — live SKILL.md unchanged.")
            return 1

    else:
        # ── Standard check against live SKILL.md ──────────────────────────
        result = await run_promotion_check_path(slug, registry)
        _print_result(result)
        return 0 if (result.passed or result.total_cases == 0) else 1


async def _run(args: argparse.Namespace) -> int:
    registry = get_registry()

    if args.all:
        slugs = [s.slug for s in registry.get_all_skills()]
    elif args.skill:
        slugs = [args.skill]
    else:
        print("Error: specify --skill <slug> or --all", file=sys.stderr)
        return 2

    overall = 0
    for slug in slugs:
        code = await _run_check(slug, promote=args.promote)
        if code != 0:
            overall = max(overall, code)
    return overall


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run skill promotion checks for the V2 RFP Analyzer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check live content (no change to disk):
  python -m promotion.runner --skill ams
  python -m promotion.runner --all

  # Promote a draft (SKILL.md.draft → SKILL.md on pass):
  python -m promotion.runner --skill ams --promote
        """,
    )
    parser.add_argument("--skill", metavar="SLUG",
                        help="Skill slug to test (e.g. ams, design-build, packaged-sap)")
    parser.add_argument("--all", action="store_true",
                        help="Run promotion checks for all registered skills")
    parser.add_argument("--promote", action="store_true",
                        help="Gate on SKILL.md.draft; copy to live only on pass")

    args = parser.parse_args()
    if not args.skill and not args.all:
        parser.print_help()
        sys.exit(2)

    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
