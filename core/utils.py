"""
core/utils.py
-------------
Shared low-level utilities for the V2 pipeline.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


def atomic_write(path: Path, content: str, retries: int = 3) -> None:
    """Write *content* to *path* atomically using a sibling temp file.

    The write goes to ``<path>.tmp``, which is then renamed to ``<path>``.
    On POSIX this rename is atomic; on Windows it replaces the destination.

    Retries up to *retries* times (with 50 ms backoff between attempts) before
    re-raising the last exception.

    Args:
        path:    Target file path.
        content: UTF-8 string to write.
        retries: Number of attempts (default 3; set to 1 for no retry).

    Raises:
        OSError: If all attempts fail.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    last_exc: Exception | None = None

    for attempt in range(max(retries, 1)):
        try:
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(path)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning(
                "atomic_write_retry",
                path=str(path),
                attempt=attempt + 1,
                error=str(exc),
            )
            if attempt < retries - 1:
                time.sleep(0.05)

    raise last_exc  # type: ignore[misc]
