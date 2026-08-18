# ============================================================
# FILE   : tests/test_api_decisions.py
# OWNER  : ROLE 3 — Quality & Audit
# PURPOSE: Integration tests for POST /decide — exercised end to end through
#          FastAPI against the seeded rule store (rules/rules.json).
# ============================================================
"""Integration tests for the ``POST /decide`` endpoint.

These run the whole stack: HTTP -> router -> rule_store (SQLite, seeded from
``rules/rules.json``) -> engine -> ``Decision.to_dict()``. A throwaway SQLite
database in a temp dir keeps the run isolated and repeatable.

Behavioural note: ``/decide`` accepts any JSON object and the engine is
fail-safe by design (missing fields and type mismatches become safe
non-matches rather than errors). So malformed-but-well-typed payloads still
return ``200`` with a valid decision — usually the policy default ``REVIEW`` —
which these tests assert explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

#: Keys guaranteed by rule_engine.Decision.to_dict() — the response contract.
DECISION_KEYS = {
    "decision",
    "confidence",
    "score",
    "rules_evaluated",
    "rules_matched",
    "rules_rejected",
    "explanation",
    "trace",
}

#: Clears every fintech gate in the current seeds (kyc / fraud /
#: underwriting / affordability) — see rules/rules.json.
APPROVE_REQUEST = {
    "age": 35,
    "identity_verified": True,
    "credit_score": 760,
    "dti": 28,
    "fraud_score": 12,
    "on_watchlist": False,
}


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory):
    """A TestClient wired to an isolated, freshly-seeded SQLite database."""
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path_factory.mktemp("decide") / "test.db"

    import os

    previous = {
        key: os.environ.get(key)
        for key in ("DATABASE_URL", "RULES_PATH", "AUDIT_LOG_PATH")
    }
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["RULES_PATH"] = str(repo_root / "rules" / "rules.json")
    os.environ["AUDIT_LOG_PATH"] = str(
        tmp_path_factory.mktemp("decide-audit") / "audit.jsonl"
    )

    # Reset cached settings and the lazily-built engine so the overrides apply.
    from app.config import get_settings
    from app.routers.audit import get_audit_service
    from app.services import rule_store

    get_settings.cache_clear()
    get_audit_service.cache_clear()
    rule_store._engine = None

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    # Restore environment so other test modules are unaffected.
    get_settings.cache_clear()
    get_audit_service.cache_clear()
    rule_store._engine = None
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def assert_decision_schema(body: object) -> None:
    """Assert the response matches the Decision.to_dict() contract."""
    assert isinstance(body, dict)
    assert DECISION_KEYS <= set(body), f"missing keys: {DECISION_KEYS - set(body)}"
    assert isinstance(body["decision"], str) and body["decision"]
    assert isinstance(body["confidence"], (int, float))
    assert 0.0 <= body["confidence"] <= 1.0
    assert isinstance(body["score"], (int, float))
    for key in ("rules_evaluated", "rules_matched", "rules_rejected", "trace"):
        assert isinstance(body[key], list)
    assert isinstance(body["explanation"], str)


class TestDecideEndpoint:
    def test_successful_approval(self, client: TestClient) -> None:
        response = client.post("/decide", json=APPROVE_REQUEST)
        assert response.status_code == 200
        body = response.json()
        assert_decision_schema(body)
        assert body["decision"] == "APPROVE"
        assert "uw_approve_good_credit" in body["rules_matched"]

    def test_successful_rejection(self, client: TestClient) -> None:
        # age < 18 triggers the highest-priority reject rule.
        response = client.post("/decide", json={**APPROVE_REQUEST, "age": 16})
        assert response.status_code == 200
        body = response.json()
        assert_decision_schema(body)
        assert body["decision"] == "REJECT"
        assert "kyc_reject_underage" in body["rules_matched"]

    def test_missing_required_field(self, client: TestClient) -> None:
        # No credit_score -> the approve rule cannot match; nothing else fires,
        # so the engine falls through to the policy default (fail-safe).
        response = client.post("/decide", json={"age": 35, "income": 80000})
        assert response.status_code == 200
        body = response.json()
        assert_decision_schema(body)
        assert body["decision"] == "REVIEW"
        assert body["rules_matched"] == []

    def test_invalid_datatype(self, client: TestClient) -> None:
        # A non-numeric age makes numeric conditions fail safely (no crash);
        # with no other usable fields, the decision falls to the default.
        response = client.post(
            "/decide", json={"age": "thirty", "identity_verified": "maybe"}
        )
        assert response.status_code == 200
        body = response.json()
        assert_decision_schema(body)
        assert body["decision"] == "REVIEW"

    def test_empty_request(self, client: TestClient) -> None:
        response = client.post("/decide", json={})
        assert response.status_code == 200
        body = response.json()
        assert_decision_schema(body)
        assert body["decision"] == "REVIEW"
        assert body["rules_matched"] == []

    def test_unknown_field(self, client: TestClient) -> None:
        # Extra fields no rule references are ignored; the decision is unaffected.
        response = client.post(
            "/decide", json={**APPROVE_REQUEST, "favorite_color": "blue"}
        )
        assert response.status_code == 200
        body = response.json()
        assert_decision_schema(body)
        assert body["decision"] == "APPROVE"
