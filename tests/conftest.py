"""
tests/conftest.py
------------------
Shared pytest fixtures for the V2 test suite.

Note: Do NOT call configure_logging() here — structlog is already configured
by app/main.py at import time, and calling it again in tests causes duplicate
handler registration.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def tmp_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary OUTPUT_DIR and patch the environment variable.

    Automatically cleans up after the test.
    """
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    monkeypatch.setenv("OUTPUT_DIR", str(out_dir))
    return out_dir


@pytest.fixture()
def tmp_upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary UPLOAD_DIR and patch the environment variable."""
    up_dir = tmp_path / "uploads"
    up_dir.mkdir()
    monkeypatch.setenv("UPLOAD_DIR", str(up_dir))
    return up_dir


@pytest.fixture()
def client(tmp_output_dir: Path, tmp_upload_dir: Path) -> TestClient:
    """Return a synchronous FastAPI TestClient with isolated tmp directories."""
    # Import app after env vars are patched so get_output_dir/get_upload_dir
    # pick up the monkeypatched values on their first call within the request.
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def golden_result() -> dict:
    """Load the golden result fixture from tests/fixtures/result.json."""
    import json
    fixture_path = Path(__file__).parent / "fixtures" / "result.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))
