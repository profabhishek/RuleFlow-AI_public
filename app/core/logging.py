# ============================================================
# FILE   : app/core/logging.py
# OWNER  : ROLE 3 — Quality & Audit
# PURPOSE: Structured (JSON) logging. One-time, idempotent configuration and
#          a reusable get_logger(name). Stdlib only — no third-party deps.
# ============================================================
"""Structured logging for the application.

Every log line is emitted as a single JSON object containing at least a
``timestamp``, ``level``, ``module``, and ``message`` — machine-parseable for
log aggregators while staying readable. Any extra fields passed via the
standard ``logging`` ``extra=`` mechanism are merged in, and exceptions /
stack traces are captured when present.

Usage::

    from app.core.logging import configure_logging, get_logger

    configure_logging()               # once, at app startup (R2's main.py)
    log = get_logger(__name__)        # anywhere else
    log.info("decision made", extra={"decision": "APPROVE", "elapsed_ms": 1.2})

``configure_logging`` is safe to call more than once — it only configures the
root logger the first time. ``get_logger`` also configures lazily, so logging
works even if startup forgot to call it. The log level comes from the
``LOG_LEVEL`` environment variable (default ``INFO``), never a hardcoded path.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from typing import TextIO

#: Environment variable that sets the root log level (e.g. DEBUG, INFO, WARNING).
LOG_LEVEL_ENV = "LOG_LEVEL"
DEFAULT_LEVEL = "INFO"

_configure_lock = threading.Lock()
_configured = False

#: Standard ``LogRecord`` attributes, derived at import time so it stays
#: correct across Python versions. Anything on a record NOT in this set is
#: treated as a user-supplied ``extra`` field and included in the JSON output.
_RESERVED_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}

__all__ = ["JsonFormatter", "configure_logging", "get_logger"]


class JsonFormatter(logging.Formatter):
    """Render a ``LogRecord`` as a single-line JSON object.

    Guaranteed keys: ``timestamp`` (ISO-8601 UTC), ``level``, ``module``
    (the logger name), and ``message``. Adds ``exception`` / ``stack`` when the
    record carries them, plus any ``extra`` fields the caller supplied.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def _resolve_level(level: str | int | None) -> int:
    """Turn a level name/number/env value into a numeric logging level."""
    candidate = (
        level if level is not None else os.environ.get(LOG_LEVEL_ENV, DEFAULT_LEVEL)
    )
    if isinstance(candidate, int):
        return candidate
    numeric = logging.getLevelName(str(candidate).upper())
    return numeric if isinstance(numeric, int) else logging.INFO


def configure_logging(
    level: str | int | None = None, stream: TextIO | None = None
) -> None:
    """Configure the root logger for JSON output — exactly once.

    Idempotent: subsequent calls are no-ops, so it is safe to call from both
    app startup and lazily from :func:`get_logger`.

    Args:
        level:  Log level name/number. Defaults to ``$LOG_LEVEL`` then ``INFO``.
        stream: Output stream. Defaults to ``stdout`` (container-friendly).
    """
    global _configured
    with _configure_lock:
        if _configured:
            return
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(JsonFormatter())

        root = logging.getLogger()
        root.handlers.clear()  # avoid duplicate lines if a default handler exists
        root.addHandler(handler)
        root.setLevel(_resolve_level(level))

        _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger by name, ensuring structured logging is configured.

    Prefer ``get_logger(__name__)`` so the ``module`` field reflects where the
    log came from.
    """
    configure_logging()
    return logging.getLogger(name)
