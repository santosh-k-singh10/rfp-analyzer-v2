"""
logging_config.py
-----------------
Shared structlog configuration for V2.

Call configure_logging() once at application startup (in app/main.py).
All modules use structlog.get_logger() for structured, levelled output.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog


def configure_logging() -> None:
    """
    Configure structlog with a consistent processor chain.

    - JSON output in production (LOG_FORMAT=json)
    - Coloured human-readable output otherwise (default)
    - Log level controlled by LOG_LEVEL env var (default INFO)
    """
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    log_format = os.getenv("LOG_FORMAT", "console").lower()

    # Ensure windows console encoding or standard streams don't choke on special unicode
    # characters (like non-breaking hyphens \u2011 or em-dashes \u2014) when printing logs.
    class Utf8SafeStream:
        def __init__(self, target_stream: Any):
            self.target_stream = target_stream

        def write(self, s: str) -> int:
            try:
                return self.target_stream.write(s)
            except UnicodeEncodeError:
                encoding = getattr(self.target_stream, "encoding", "utf-8") or "utf-8"
                safe = s.encode(encoding, errors="replace").decode(encoding)
                return self.target_stream.write(safe)

        def flush(self) -> None:
            if hasattr(self.target_stream, "flush"):
                self.target_stream.flush()

        def isatty(self) -> bool:
            return bool(getattr(self.target_stream, "isatty", lambda: False)())

    safe_stream = Utf8SafeStream(sys.stderr)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    if log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False if sys.platform == "win32" and not safe_stream.isatty() else safe_stream.isatty()))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=safe_stream),
        cache_logger_on_first_use=True,
    )

    # Suppress noisy stdlib loggers from third-party libs
    logging.basicConfig(format="%(levelname)s: %(message)s", stream=sys.stderr, level=log_level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
