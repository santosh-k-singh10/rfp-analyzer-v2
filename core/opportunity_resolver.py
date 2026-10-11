"""
core/opportunity_resolver.py
-----------------------------
Locate and classify RFP document files for a given Opportunity ID.

Usage::

    from core.opportunity_resolver import resolve_opportunity_files

    files = resolve_opportunity_files("OPP-2024-001")
    print(files.primary_file_path)   # Path | None
    print(files.file_paths)          # list[Path]
    print(files.file_roles)          # dict[str, str]  filename → role
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from app.config import get_raw_storage_dir

# ---------------------------------------------------------------------------
# Supported document extensions
# ---------------------------------------------------------------------------
VALID_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".docx", ".doc", ".txt", ".xlsx", ".xls"}
)

# ---------------------------------------------------------------------------
# Filename heuristic patterns
# ---------------------------------------------------------------------------

# Word-boundary helper that works with filenames (underscores are word chars
# in Python regex, so \b does not fire between a letter and '_').
# We treat any non-alpha character (_, -, ., space, digit boundary) as a
# separator, using lookahead/lookbehind for letter characters only.
def _wb(keyword: str) -> str:
    """Wrap *keyword* so it matches as a whole token inside a filename."""
    return r"(?<![A-Za-z])" + keyword + r"(?![A-Za-z])"


# Patterns that strongly suggest the primary / master RFP document.
_PRIMARY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(_wb("rfp"), re.IGNORECASE),
    re.compile(r"request[\s_\-]*for[\s_\-]*proposal", re.IGNORECASE),
    re.compile(r"tender[\s_\-]*document", re.IGNORECASE),
    re.compile(_wb("solicitation"), re.IGNORECASE),
    re.compile(_wb("main"), re.IGNORECASE),
    re.compile(_wb("master"), re.IGNORECASE),
]

# Patterns that suggest a Q&A / amendments document.
_QNA_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"q[\s_\-]*&[\s_\-]*a", re.IGNORECASE),
    re.compile(r"q[\s_\-]*and[\s_\-]*a", re.IGNORECASE),
    re.compile(r"question", re.IGNORECASE),
    re.compile(_wb("faq"), re.IGNORECASE),
    re.compile(r"amendment", re.IGNORECASE),
    re.compile(r"clarification", re.IGNORECASE),
    re.compile(r"addendum", re.IGNORECASE),
]

# Patterns that suggest an annexure / exhibit / attachment.
_ANNEXURE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"annex", re.IGNORECASE),
    re.compile(_wb("exhibit"), re.IGNORECASE),
    re.compile(r"attach", re.IGNORECASE),
    re.compile(_wb("schedule"), re.IGNORECASE),
    re.compile(r"appendix", re.IGNORECASE),
    re.compile(_wb("sow"), re.IGNORECASE),
    re.compile(r"statement[\s_\-]*of[\s_\-]*work", re.IGNORECASE),
    re.compile(r"price[\s_\-]*sheet", re.IGNORECASE),
    re.compile(_wb("pricing"), re.IGNORECASE),
    re.compile(_wb("template"), re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class OpportunityFiles:
    """Container for all resolved document files for an opportunity.

    Attributes:
        opp_id: The opportunity identifier that was resolved.
        opp_dir: The directory where the files were found.
        file_paths: All valid document files found in the directory.
        primary_file_path: The single best-guess primary RFP document, or
            ``None`` if it could not be determined.
        file_roles: Mapping of *filename* (not full path) to role string.
            Role values: ``"primary"``, ``"annexure"``, ``"qna"``,
            ``"unknown"``.
    """

    opp_id: str
    opp_dir: Path
    file_paths: List[Path] = field(default_factory=list)
    primary_file_path: Optional[Path] = None
    file_roles: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _classify_filename(name: str) -> str:
    """Return a role label for *name* based on heuristic pattern matching."""
    for pattern in _PRIMARY_PATTERNS:
        if pattern.search(name):
            return "primary"
    for pattern in _QNA_PATTERNS:
        if pattern.search(name):
            return "qna"
    for pattern in _ANNEXURE_PATTERNS:
        if pattern.search(name):
            return "annexure"
    return "unknown"


def _collect_valid_files(directory: Path) -> list[Path]:
    """Return all files with a supported extension directly inside *directory*.

    Only top-level files are returned (no recursive traversal) because the
    storage layout places documents directly under ``{base}/{opp_id}/``.
    """
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in VALID_EXTENSIONS
    )


def _find_opp_directory(base: Path, opp_id: str) -> Optional[Path]:
    """Locate the directory for *opp_id* inside *base*.

    Strategy (in order):
    1. ``{base}/{opp_id}`` exists as a directory → use it directly.
    2. Scan immediate sub-directories whose name *contains* opp_id (case-
       insensitive substring match, e.g. ``OPP-001_Procurement``).
    3. Scan immediate sub-directories that themselves contain a sub-directory
       or file whose name matches opp_id (one extra level of UUID wrapping).

    Returns the first match found, or ``None``.
    """
    if not base.is_dir():
        return None

    # Strategy 1: exact match
    exact = base / opp_id
    if exact.is_dir():
        return exact

    # Strategy 2: sub-directory name contains opp_id
    opp_id_lower = opp_id.lower()
    for child in sorted(base.iterdir()):
        if child.is_dir() and opp_id_lower in child.name.lower():
            return child

    # Strategy 3: one UUID wrapper level — look inside each sub-dir
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        nested = child / opp_id
        if nested.is_dir():
            return nested

    return None


def _pick_primary(files: list[Path], roles: dict[str, str]) -> Optional[Path]:
    """Choose the single primary file from *files* given pre-computed *roles*.

    Preference order:
    1. Any file already classified as ``"primary"``.
    2. The largest PDF (likely the full tender document).
    3. The largest file overall.
    Returns ``None`` when *files* is empty.
    """
    if not files:
        return None

    primary_files = [f for f in files if roles.get(f.name) == "primary"]
    if primary_files:
        return max(primary_files, key=lambda p: p.stat().st_size)

    pdfs = [f for f in files if f.suffix.lower() == ".pdf"]
    if pdfs:
        return max(pdfs, key=lambda p: p.stat().st_size)

    return max(files, key=lambda p: p.stat().st_size)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_opportunity_files(
    opp_id: str,
    base_dir: Optional[Path] = None,
) -> OpportunityFiles:
    """Resolve all RFP document files for *opp_id*.

    Args:
        opp_id: Opportunity identifier (e.g. ``"OPP-2024-001"``).
        base_dir: Override the storage root (defaults to
            :func:`app.config.get_raw_storage_dir`).  Primarily useful for
            testing.

    Returns:
        :class:`OpportunityFiles` populated with discovered paths and roles.

    Raises:
        FileNotFoundError: When the raw storage directory does not exist or
            when no directory matching *opp_id* can be located within it.
    """
    base = base_dir if base_dir is not None else get_raw_storage_dir()

    if not base.exists():
        raise FileNotFoundError(
            f"Raw storage directory does not exist: {base}"
        )

    opp_dir = _find_opp_directory(base, opp_id)
    if opp_dir is None:
        raise FileNotFoundError(
            f"No directory found for opportunity '{opp_id}' under {base}"
        )

    valid_files = _collect_valid_files(opp_dir)
    roles: dict[str, str] = {f.name: _classify_filename(f.name) for f in valid_files}
    primary = _pick_primary(valid_files, roles)

    return OpportunityFiles(
        opp_id=opp_id,
        opp_dir=opp_dir,
        file_paths=valid_files,
        primary_file_path=primary,
        file_roles=roles,
    )
