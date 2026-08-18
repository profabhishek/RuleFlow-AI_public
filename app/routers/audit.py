# ============================================================
# FILE   : app/routers/audit.py
# OWNER  : ROLE 3 — Quality & Audit
# PURPOSE: GET /audit — read the decision / AI-change history. Thin router:
#          it only wires HTTP to the AuditService, no business logic.
# ============================================================
"""``GET /audit`` — expose the append-only audit trail.

The router is intentionally thin: it resolves an :class:`AuditService` via
dependency injection and delegates entirely to it. All storage and query logic
lives in ``app.services.audit_log``; this file just maps HTTP <-> service.

Wiring handoff (Role 2, in ``app/main.py``)::

    from app.routers import audit
    app.include_router(audit.router)
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, status

from app.services.audit_log import (
    AuditEventType,
    AuditLogError,
    AuditRecord,
    AuditService,
    JsonFileAuditRepository,
)

router = APIRouter(prefix="/audit", tags=["audit"])


@lru_cache
def get_audit_service() -> AuditService:
    """Provide a process-wide :class:`AuditService` (dependency injection).

    Backed by the JSON-file repository today; swapping in a database-backed
    repository here is the only change needed to migrate storage — callers and
    this router stay untouched.
    """
    return AuditService(JsonFileAuditRepository())


@router.get("", response_model=list[AuditRecord])
def get_audit(
    event_type: AuditEventType | None = None,
    service: AuditService = Depends(get_audit_service),
) -> list[AuditRecord]:
    """Return audit records, optionally filtered by ``event_type``.

    An unknown ``event_type`` is rejected by FastAPI with 422; a storage
    failure is surfaced as a clean 500.
    """
    try:
        if event_type is not None:
            return service.get_by_event(event_type)
        return service.get_all()
    except AuditLogError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not read the audit trail.",
        ) from exc
