"""
tests/test_path_safety.py
--------------------------
AT-10 — path-safety tests for all /jobs/{id}… routes.

Every route that accepts a job_id URL parameter must reject non-canonical
UUIDs with a 4xx status code, preventing path-traversal and injection attacks.

Covered cases:
- Path traversal:  ../x
- Braced form:     {123e4567-e89b-12d3-a456-426614174000}
- URN prefix:      urn:uuid:123e4567-e89b-12d3-a456-426614174000
- No hyphens:      123e4567e89b12d3a456426614174000
- Uppercase:       123E4567-E89B-12D3-A456-426614174000
- Plaintext:       not-a-uuid
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# Canonical (valid) UUID — used for positive-path baseline checks
VALID_UUID = "123e4567-e89b-12d3-a456-426614174000"

# Invalid / non-canonical job_id values that MUST be rejected with 4xx
INVALID_IDS = [
    "../x",
    "../../etc/passwd",
    "{123e4567-e89b-12d3-a456-426614174000}",
    "urn:uuid:123e4567-e89b-12d3-a456-426614174000",
    "123e4567e89b12d3a456426614174000",           # no hyphens
    "123E4567-E89B-12D3-A456-426614174000",        # uppercase
    "not-a-uuid",
    "00000000-0000-0000-0000-00000000000Z",         # invalid hex char
]

# Routes that expose a job_id path parameter.
# Use a format string — the test fills in the job_id.
JOB_ID_ROUTES = [
    "/jobs/{job_id}",
    "/jobs/{job_id}/markdown",
    "/jobs/{job_id}/fields",
]


@pytest.mark.parametrize("route_template", JOB_ID_ROUTES)
@pytest.mark.parametrize("bad_id", INVALID_IDS)
def test_invalid_job_id_rejected(client: TestClient, route_template: str, bad_id: str) -> None:
    """Non-canonical job IDs must return 4xx on every /jobs/{id}… route."""
    url = route_template.format(job_id=bad_id)
    response = client.get(url)
    assert response.status_code in (400, 404, 422), (
        f"Expected 4xx for job_id={bad_id!r} on {url}, got {response.status_code}"
    )


def test_valid_uuid_passes_validation(client: TestClient) -> None:
    """A canonical UUID should pass validation (may 404 — just not 400)."""
    url = f"/jobs/{VALID_UUID}"
    response = client.get(url)
    # 404 is acceptable (job doesn't exist); 400 means the validator wrongly rejected it
    assert response.status_code != 400, (
        f"Valid UUID {VALID_UUID!r} was rejected with 400"
    )
