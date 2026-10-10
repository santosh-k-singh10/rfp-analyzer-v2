"""
app/config.py
-------------
Lazily-evaluated configuration getters for directory paths.

All getters read environment variables at **call time** — never at import time.
No mkdir() calls happen at import; callers are responsible for ensuring
directories exist before writing.
"""

from __future__ import annotations

import os
from pathlib import Path

# Base directory: two levels up from this file (app/ → V2/)
_APP_BASE = Path(__file__).resolve().parent.parent


def get_output_dir() -> Path:
    """Return the job output directory root (reads OUTPUT_DIR env at call time)."""
    return Path(os.getenv("OUTPUT_DIR", str(_APP_BASE / "outputs")))


def get_upload_dir() -> Path:
    """Return the file upload directory root (reads UPLOAD_DIR env at call time)."""
    return Path(os.getenv("UPLOAD_DIR", str(_APP_BASE / "uploads")))
