"""
app/dependencies.py
--------------------
FastAPI dependency functions shared across all routers.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException


def validate_job_id(job_id: str) -> str:
    """Validate and canonicalise a job UUID from the URL path.

    Accepts only canonical lowercase-hyphenated UUIDs
    (e.g. ``123e4567-e89b-12d3-a456-426614174000``).

    Rejects:
    - Non-UUID strings (path traversal like ``../x``, ``foo``, etc.)
    - Braced form: ``{123e4567-…}``
    - URN form: ``urn:uuid:123e4567-…``
    - No-hyphen form: ``123e4567e89b12d3a456426614174000``
    - Uppercase letters

    Returns the canonical lowercase hyphenated string on success.
    Raises :class:`fastapi.HTTPException` (400) on validation failure.
    """
    try:
        parsed = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid job_id: {job_id!r}")

    canonical = str(parsed)  # always lowercase, hyphenated
    if job_id != canonical:
        raise HTTPException(status_code=400, detail=f"Invalid job_id: {job_id!r}")

    return canonical
