"""
tests/test_edit_ui_mutations.py
--------------------------------
Runs the jsdom-based mutation tests for sub-task 1.3b:
  mutationSuccess, replaceRow, rerenderSection.

Skips automatically when Node.js is unavailable, matching the pattern in
test_ams_render_snapshot.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WORKSPACE   = Path(__file__).resolve().parent.parent
TEST_SCRIPT = WORKSPACE / "tests" / "test_edit_ui_mutations.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not available")
def test_edit_ui_mutations() -> None:
    """Run the jsdom mutation test suite and assert it passes."""
    result = subprocess.run(
        ["node", str(TEST_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(WORKSPACE),
    )
    if result.returncode != 0:
        pytest.fail(
            f"edit-UI mutation tests failed:\n{result.stderr or result.stdout}"
        )
