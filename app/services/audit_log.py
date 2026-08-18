# ============================================================
# FILE   : app/services/audit_log.py
# OWNER  : ROLE 3 — Quality & Audit
# PURPOSE: Append-only audit service for decision evaluations and
#          AI-generated schema/rule changes. JSON-file backed today,
#          hidden behind a repository interface so a database can replace
#          it later without touching a single caller.
# ============================================================
"""Append-only audit trail (clean architecture + dependency injection).

Two layers, deliberately separated:

* :class:`AuditRepository` (data-access port) — the storage abstraction.
  Its only concerns are *append*, *read-all*, and *reset*. Callers depend on
  this port, never on a concrete backend, so a SQL-backed repository can be
  swapped in later by changing one wiring line in the DI layer
  (``app/dependencies.py``, owned by Role 2) — no caller changes.
* :class:`AuditService` (use-case layer) — the application-facing API the
  rest of the app uses. It receives a repository via constructor injection
  and adds query logic (``get_by_event``) on top of raw storage.

The trail is **append-only**: records are only ever added, never mutated.
The on-disk format is JSON Lines (one JSON object per line) so appends are
O(1) and never rewrite the whole file.
"""

from __future__ import annotations

import logging
import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("app.services.audit_log")

#: Relative fallback filename, used only when neither a path argument nor the
#: ``AUDIT_LOG_PATH`` env var is provided. Resolved against the current working
#: directory — never an absolute, hardcoded location.
DEFAULT_AUDIT_FILENAME = "audit_log.jsonl"

#: Environment variable consulted when no explicit path is injected.
AUDIT_LOG_PATH_ENV = "AUDIT_LOG_PATH"

JSONDict = dict[str, Any]


class AuditLogError(RuntimeError):
    """Raised when the audit trail cannot be read from or written to storage."""


class AuditEventType(str, Enum):
    """The kinds of events the audit trail records.

    ``str``-based so records serialize to plain strings in JSON and
    ``get_by_event`` accepts either an enum member or its raw string value.
    """

    DECISION_EVALUATION = "decision_evaluation"
    AI_SCHEMA_CHANGE = "ai_schema_change"
    AI_RULE_CHANGE = "ai_rule_change"


@runtime_checkable
class DecisionLike(Protocol):
    """Structural type of a ``rule_engine.Decision``.

    Declared here (instead of importing the engine) so this service stays
    loosely coupled to Role 1 — any object exposing these attributes works.
    """

    decision: str
    rules_matched: list[str]
    rules_rejected: list[str]
    explanation: str


class AuditRecord(BaseModel):
    """One immutable entry in the audit trail.

    A single schema spans all event types; fields not relevant to a given
    event stay at their defaults. ``timestamp`` defaults to construction time
    (UTC). Use the ``for_*`` factory helpers for correct, readable creation.
    """

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: AuditEventType

    # AI-generation context (schema / rule change events)
    user_prompt: str | None = None
    generated_json: Any | None = None
    validation_result: Any | None = None

    # Decision-evaluation context
    decision: str | None = None
    matched_rules: list[str] = Field(default_factory=list)
    rejected_rules: list[str] = Field(default_factory=list)

    # Shared, human-readable rationale
    explanation: str = ""

    @classmethod
    def for_decision(cls, decision: DecisionLike) -> "AuditRecord":
        """Build a DECISION_EVALUATION record from an engine ``Decision``.

        Maps the engine's ``rules_matched`` / ``rules_rejected`` onto the
        audit schema and carries the human-readable explanation.
        """
        return cls(
            event_type=AuditEventType.DECISION_EVALUATION,
            decision=decision.decision,
            matched_rules=list(decision.rules_matched),
            rejected_rules=list(decision.rules_rejected),
            explanation=decision.explanation,
        )

    @classmethod
    def for_schema_change(
        cls,
        user_prompt: str,
        generated_json: Any,
        validation_result: Any,
        explanation: str = "",
    ) -> "AuditRecord":
        """Build an AI_SCHEMA_CHANGE record for an AI-proposed schema change."""
        return cls(
            event_type=AuditEventType.AI_SCHEMA_CHANGE,
            user_prompt=user_prompt,
            generated_json=generated_json,
            validation_result=validation_result,
            explanation=explanation,
        )

    @classmethod
    def for_rule_change(
        cls,
        user_prompt: str,
        generated_json: Any,
        validation_result: Any,
        explanation: str = "",
    ) -> "AuditRecord":
        """Build an AI_RULE_CHANGE record for an AI-proposed rule change."""
        return cls(
            event_type=AuditEventType.AI_RULE_CHANGE,
            user_prompt=user_prompt,
            generated_json=generated_json,
            validation_result=validation_result,
            explanation=explanation,
        )


# ----------------------------------------------------------------------------
# Data-access port (the database-swap seam)
# ----------------------------------------------------------------------------


class AuditRepository(ABC):
    """Storage-agnostic, append-only persistence port for audit records.

    Implementations must guarantee append-only semantics: ``append`` adds an
    entry, existing entries are never modified. This is the single abstraction
    a future database implementation must satisfy.
    """

    @abstractmethod
    def append(self, record: AuditRecord) -> AuditRecord:
        """Persist one record and return it. Never overwrites prior records."""

    @abstractmethod
    def get_all(self) -> list[AuditRecord]:
        """Return every stored record in insertion order (oldest first)."""

    @abstractmethod
    def clear(self) -> None:
        """Remove all stored records (primarily for tests / resets)."""


class JsonFileAuditRepository(AuditRepository):
    """JSON Lines file-backed audit repository.

    Thread-safe: a lock serializes reads and writes so concurrent requests
    cannot interleave a half-written line. Appends use OS append mode, so the
    trail is genuinely append-only on disk.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = self._resolve_path(path)
        self._lock = threading.Lock()
        logger.info("audit trail backed by file: %s", self._path)

    @staticmethod
    def _resolve_path(path: str | os.PathLike[str] | None) -> Path:
        """Injected path > ``AUDIT_LOG_PATH`` env var > relative default."""
        if path is not None:
            return Path(path)
        env_path = os.environ.get(AUDIT_LOG_PATH_ENV)
        if env_path:
            return Path(env_path)
        return Path.cwd() / DEFAULT_AUDIT_FILENAME

    @property
    def path(self) -> Path:
        """The resolved file location (useful for diagnostics and tests)."""
        return self._path

    def append(self, record: AuditRecord) -> AuditRecord:
        line = record.model_dump_json()
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            logger.error("failed to append audit record: %s", exc)
            raise AuditLogError(
                f"Could not write audit record to {self._path}: {exc}"
            ) from exc
        return record

    def get_all(self) -> list[AuditRecord]:
        try:
            with self._lock:
                if not self._path.exists():
                    return []
                raw_lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.error("failed to read audit trail: %s", exc)
            raise AuditLogError(
                f"Could not read audit trail at {self._path}: {exc}"
            ) from exc

        records: list[AuditRecord] = []
        for number, line in enumerate(raw_lines, start=1):
            if not line.strip():
                continue
            try:
                records.append(AuditRecord.model_validate_json(line))
            except ValidationError as exc:
                # A single malformed line must not sink the whole trail.
                logger.warning("skipping corrupt audit line %d: %s", number, exc)
        return records

    def clear(self) -> None:
        try:
            with self._lock:
                if self._path.exists():
                    self._path.unlink()
        except OSError as exc:
            logger.error("failed to clear audit trail: %s", exc)
            raise AuditLogError(
                f"Could not clear audit trail at {self._path}: {exc}"
            ) from exc


# ----------------------------------------------------------------------------
# Use-case layer (application-facing service)
# ----------------------------------------------------------------------------


class AuditService:
    """Append-only audit service used by the rest of the application.

    Depends only on the :class:`AuditRepository` abstraction, injected via the
    constructor (dependency inversion). Storage details — JSON file today, a
    database tomorrow — never leak past this boundary.
    """

    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository

    def append(self, record: AuditRecord) -> AuditRecord:
        """Record one audit entry (decision, schema change, or rule change)."""
        saved = self._repository.append(record)
        logger.info("audit event recorded: %s", saved.event_type.value)
        return saved

    def get_all(self) -> list[AuditRecord]:
        """Return the full audit trail, oldest first."""
        return self._repository.get_all()

    def get_by_event(self, event_type: AuditEventType | str) -> list[AuditRecord]:
        """Return only the records matching a given event type.

        Accepts either an :class:`AuditEventType` member or its raw string
        value (e.g. ``"ai_rule_change"``).
        """
        wanted = (
            event_type.value
            if isinstance(event_type, AuditEventType)
            else str(event_type)
        )
        return [r for r in self._repository.get_all() if r.event_type.value == wanted]

    def clear(self) -> None:
        """Reset the audit trail (primarily for tests / resets)."""
        self._repository.clear()
