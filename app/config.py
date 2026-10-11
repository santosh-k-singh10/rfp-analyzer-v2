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


def get_raw_storage_dir() -> Path:
    """Return the raw storage base directory (reads RAW_STORAGE_BASE_DIR env at call time).

    Falls back first to the StarterKit standard path, then to a relative
    ``data/storage/inputs/raw`` directory under the project root when the
    absolute StarterKit path does not exist and the variable is unset.
    """
    _starterkit_default = (
        r"C:\Agents\StarterKit-v0.1\Framework\backend\data\storage\inputs\raw"
    )
    raw = os.getenv("RAW_STORAGE_BASE_DIR", _starterkit_default)
    path = Path(raw)
    # If the env-var default doesn't exist on this machine, fall back to a
    # relative path so tests and non-StarterKit deployments work out of the box.
    if not path.exists() and raw == _starterkit_default:
        path = _APP_BASE / "data" / "storage" / "inputs" / "raw"
    return path
