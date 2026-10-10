"""
tests/test_ams_render_snapshot.py
----------------------------------
Verifies that the AMS section renderers in review.js (post-split) produce
output that matches the committed snapshot in tests/fixtures/ams_render_snapshot.html.

Skips automatically when Node.js is unavailable, so CI without Node does not fail.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parent.parent
CAPTURE_SCRIPT = WORKSPACE / "tests" / "capture_ams_snapshot.js"
SNAPSHOT = WORKSPACE / "tests" / "fixtures" / "ams_render_snapshot.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not available")
def test_ams_render_matches_snapshot() -> None:
    """Run the Node snapshot verifier and assert it passes."""
    assert SNAPSHOT.exists(), (
        "Snapshot file missing. Run: node tests/capture_ams_snapshot.js"
    )
    result = subprocess.run(
        ["node", str(CAPTURE_SCRIPT), "--verify"],
        capture_output=True,
        text=True,
        cwd=str(WORKSPACE),
    )
    if result.returncode != 0:
        pytest.fail(
            f"AMS render snapshot mismatch:\n{result.stderr or result.stdout}"
        )
