# ============================================================
# FILE   : tests/test_audit.py
# OWNER  : ROLE 3 — Quality & Audit
# PURPOSE: Integration tests for GET /audit.
# ============================================================
"""Integration tests for the audit history endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import audit
from app.services.audit_log import (
    AuditEventType,
    AuditRecord,
    AuditService,
    JsonFileAuditRepository,
)

AUDIT_RECORD_KEYS = {
    "timestamp",
    "event_type",
    "user_prompt",
    "generated_json",
    "validation_result",
    "decision",
    "matched_rules",
    "rejected_rules",
    "explanation",
}


@pytest.fixture
def audit_client(tmp_path: Path) -> tuple[TestClient, AuditService, Path]:
    """Create an isolated app and inject a temporary audit repository."""
    audit_path = tmp_path / "audit.jsonl"
    service = AuditService(JsonFileAuditRepository(audit_path))

    app = FastAPI()
    app.include_router(audit.router)
    app.dependency_overrides[audit.get_audit_service] = lambda: service

    with TestClient(app) as client:
        yield client, service, audit_path

    app.dependency_overrides.clear()


def assert_audit_record_schema(record: object) -> None:
    """Assert the JSON representation of one AuditRecord."""
    assert isinstance(record, dict)
    assert set(record) == AUDIT_RECORD_KEYS
    assert isinstance(record["timestamp"], str)
    assert record["event_type"] in {event.value for event in AuditEventType}
    assert record["user_prompt"] is None or isinstance(record["user_prompt"], str)
    assert record["decision"] is None or isinstance(record["decision"], str)
    assert isinstance(record["matched_rules"], list)
    assert isinstance(record["rejected_rules"], list)
    assert isinstance(record["explanation"], str)


class TestGetAudit:
    def test_no_entries(
        self, audit_client: tuple[TestClient, AuditService, Path]
    ) -> None:
        client, _, _ = audit_client

        response = client.get("/audit")

        assert response.status_code == 200
        assert response.json() == []

    def test_single_entry(
        self, audit_client: tuple[TestClient, AuditService, Path]
    ) -> None:
        client, service, _ = audit_client
        service.append(
            AuditRecord(
                event_type=AuditEventType.DECISION_EVALUATION,
                decision="APPROVE",
                matched_rules=["rule_approve"],
                rejected_rules=["rule_reject"],
                explanation="Applicant met the approval criteria.",
            )
        )

        response = client.get("/audit")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert_audit_record_schema(body[0])
        assert body[0]["decision"] == "APPROVE"
        assert body[0]["matched_rules"] == ["rule_approve"]

    def test_multiple_entries(
        self, audit_client: tuple[TestClient, AuditService, Path]
    ) -> None:
        client, service, _ = audit_client
        service.append(
            AuditRecord(
                event_type=AuditEventType.DECISION_EVALUATION,
                decision="REJECT",
                matched_rules=["rule_underage"],
                explanation="Applicant is underage.",
            )
        )
        service.append(
            AuditRecord.for_rule_change(
                user_prompt="Reject applicants under 21.",
                generated_json={"id": "rule_under_21"},
                validation_result={"valid": True, "errors": [], "warnings": []},
                explanation="AI-generated rule passed validation.",
            )
        )
        service.append(
            AuditRecord.for_schema_change(
                user_prompt="Add annual_income.",
                generated_json={
                    "operation": "add_field",
                    "field": "annual_income",
                    "datatype": "number",
                },
                validation_result={"valid": True, "errors": [], "warnings": []},
            )
        )

        response = client.get("/audit")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 3
        assert [entry["event_type"] for entry in body] == [
            "decision_evaluation",
            "ai_rule_change",
            "ai_schema_change",
        ]
        for entry in body:
            assert_audit_record_schema(entry)

    def test_corrupted_audit_file_skips_bad_record(
        self, audit_client: tuple[TestClient, AuditService, Path]
    ) -> None:
        client, service, audit_path = audit_client
        service.append(
            AuditRecord(
                event_type=AuditEventType.DECISION_EVALUATION,
                decision="REVIEW",
                explanation="Manual review required.",
            )
        )
        with audit_path.open("a", encoding="utf-8") as audit_file:
            audit_file.write("{this is not valid json}\n")

        response = client.get("/audit")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert_audit_record_schema(body[0])
        assert body[0]["decision"] == "REVIEW"
