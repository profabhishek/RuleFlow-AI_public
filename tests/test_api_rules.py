# ============================================================
# FILE   : tests/test_api_rules.py
# OWNER  : ROLE 3 — Quality & Audit
# PURPOSE: Integration tests for the /rules CRUD endpoints.
# ============================================================
"""End-to-end tests for rule creation, retrieval, update, and deletion."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RULE_KEYS = {
    "id",
    "description",
    "type",
    "logic",
    "outcome",
    "weight",
    "priority",
    "version",
    "enabled",
    "category",
    "conditions",
}


def make_rule(
    rule_id: str = "test_high_income",
    *,
    outcome: str = "APPROVE",
    priority: int = 10,
    threshold: int = 100000,
) -> dict:
    """Return a valid rule payload for the public API.

    ``threshold`` varies the condition so tests that create several rules
    at once get genuinely distinct rules. Two rules with identical
    conditions are now reported as duplicates/conflicts by the API (see
    TestEquivalentRuleDetection), so reusing one threshold across rules
    would trip that check rather than testing what the test intends.
    """
    return {
        "id": rule_id,
        "description": "Approve applicants with high income.",
        "type": "conditional",
        "logic": "AND",
        "conditions": [{"field": "income", "operator": "gte", "value": threshold}],
        "outcome": outcome,
        "weight": 25,
        "priority": priority,
        "version": 1,
        "enabled": True,
    }


def assert_rule_schema(rule: object) -> None:
    """Assert the JSON contract produced by rules_to_dicts()."""
    assert isinstance(rule, dict)
    assert set(rule) == RULE_KEYS
    assert isinstance(rule["id"], str) and rule["id"]
    assert isinstance(rule["description"], str)
    assert rule["type"] == "conditional"
    assert rule["logic"] in {"AND", "OR"}
    assert isinstance(rule["outcome"], str) and rule["outcome"]
    assert isinstance(rule["weight"], (int, float))
    assert isinstance(rule["priority"], int)
    assert isinstance(rule["version"], int)
    assert isinstance(rule["enabled"], bool)
    assert isinstance(rule["conditions"], list) and rule["conditions"]
    assert {"field", "operator", "value"} <= set(rule["conditions"][0])


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """Use a fresh, unseeded SQLite database for every test."""
    database_path = tmp_path / "rules.db"
    environment = {
        "DATABASE_URL": f"sqlite:///{database_path.as_posix()}",
        "RULES_PATH": str(tmp_path / "no-seed-rules.json"),
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)

    from app.config import get_settings
    from app.services import rule_store

    get_settings.cache_clear()
    if rule_store._engine is not None:
        rule_store._engine.dispose()
    rule_store._engine = None

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    if rule_store._engine is not None:
        rule_store._engine.dispose()
    rule_store._engine = None
    get_settings.cache_clear()
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class TestRuleCrud:
    def test_create_rule(self, client: TestClient) -> None:
        payload = make_rule()

        response = client.post("/rules", json=payload)

        assert response.status_code == 201
        body = response.json()
        assert_rule_schema(body)
        assert body["id"] == payload["id"]
        assert body["outcome"] == "APPROVE"

    def test_update_rule(self, client: TestClient) -> None:
        payload = make_rule()
        assert client.post("/rules", json=payload).status_code == 201
        updated = {
            **payload,
            "description": "Send high-income applicants for manual review.",
            "outcome": "REVIEW",
            "priority": 50,
        }

        response = client.put(f"/rules/{payload['id']}", json=updated)

        assert response.status_code == 200
        body = response.json()
        assert_rule_schema(body)
        assert body["id"] == payload["id"]
        assert body["outcome"] == "REVIEW"
        assert body["priority"] == 50

        persisted = client.get(f"/rules/{payload['id']}")
        assert persisted.status_code == 200
        assert persisted.json()["outcome"] == "REVIEW"

    def test_delete_rule(self, client: TestClient) -> None:
        payload = make_rule()
        assert client.post("/rules", json=payload).status_code == 201

        response = client.delete(f"/rules/{payload['id']}")

        assert response.status_code == 204
        assert response.content == b""
        assert client.get(f"/rules/{payload['id']}").status_code == 404

    def test_get_rules(self, client: TestClient) -> None:
        first = make_rule("test_rule_one")
        second = make_rule(
            "test_rule_two", outcome="REVIEW", priority=20, threshold=250000
        )
        assert client.post("/rules", json=first).status_code == 201
        assert client.post("/rules", json=second).status_code == 201

        response = client.get("/rules")

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) == 2
        assert {rule["id"] for rule in body} == {"test_rule_one", "test_rule_two"}
        for rule in body:
            assert_rule_schema(rule)

    def test_invalid_rule_creation(self, client: TestClient) -> None:
        invalid_rule = {
            "id": "invalid_rule",
            "description": "Missing outcome and conditions.",
        }

        response = client.post("/rules", json=invalid_rule)

        assert response.status_code == 422


class TestEquivalentRuleDetection:
    """Users describe rules in plain English, so the same intent easily
    arrives twice in different words ("credit score 670+ clears
    underwriting" vs "approve if credit score above 670"). Those produce
    different ids, so id-uniqueness never catches them and the ruleset
    silently accumulates redundant — or worse, contradictory — rules.
    Equivalent matching logic is now detected and surfaced as a 409 the
    caller can override with force=true."""

    def _base(self, rule_id: str, *, outcome: str = "APPROVE") -> dict:
        return {
            "id": rule_id,
            "outcome": outcome,
            "priority": 5,
            "conditions": [{"field": "credit_score", "operator": "gte", "value": 670}],
        }

    def test_same_logic_different_id_is_flagged(self, client: TestClient) -> None:
        assert client.post("/rules", json=self._base("first")).status_code == 201
        response = client.post("/rules", json=self._base("second_wording"))
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["kind"] == "duplicate"
        assert detail["existing"][0]["id"] == "first"

    def test_same_logic_opposite_outcome_is_a_conflict(self, client: TestClient) -> None:
        assert client.post("/rules", json=self._base("approver")).status_code == 201
        response = client.post("/rules", json=self._base("rejecter", outcome="REJECT"))
        assert response.status_code == 409
        assert response.json()["detail"]["kind"] == "conflict"

    def test_force_overrides(self, client: TestClient) -> None:
        assert client.post("/rules", json=self._base("first")).status_code == 201
        assert client.post("/rules", json=self._base("second")).status_code == 409
        assert (
            client.post("/rules?force=true", json=self._base("second")).status_code
            == 201
        )

    def test_cosmetic_differences_still_match(self, client: TestClient) -> None:
        assert client.post("/rules", json=self._base("first")).status_code == 201
        cosmetic = {
            "id": "cosmetic",
            "outcome": "APPROVE",
            "conditions": [
                # Same logic: padded field name, float instead of int.
                {"field": " credit_score ", "operator": "gte", "value": 670.0}
            ],
        }
        assert client.post("/rules", json=cosmetic).status_code == 409

    def test_genuinely_different_rule_is_allowed(self, client: TestClient) -> None:
        assert client.post("/rules", json=self._base("first")).status_code == 201
        different = {
            "id": "different",
            "outcome": "APPROVE",
            "conditions": [{"field": "credit_score", "operator": "gte", "value": 800}],
        }
        assert client.post("/rules", json=different).status_code == 201

    def test_condition_order_does_not_matter(self, client: TestClient) -> None:
        two_conditions = {
            "id": "ordered",
            "outcome": "APPROVE",
            "conditions": [
                {"field": "age", "operator": "gte", "value": 18},
                {"field": "credit_score", "operator": "gte", "value": 670},
            ],
        }
        assert client.post("/rules", json=two_conditions).status_code == 201
        reversed_order = {
            **two_conditions,
            "id": "reversed",
            "conditions": list(reversed(two_conditions["conditions"])),
        }
        assert client.post("/rules", json=reversed_order).status_code == 409

    def test_updating_a_rule_to_itself_is_not_a_duplicate(
        self, client: TestClient
    ) -> None:
        assert client.post("/rules", json=self._base("only")).status_code == 201
        # Re-saving the same rule (e.g. tweaking its priority) must not
        # report the rule as a duplicate of itself.
        response = client.put(
            "/rules/only", json={**self._base("only"), "priority": 99}
        )
        assert response.status_code == 200
        assert response.json()["priority"] == 99

    def test_editing_a_rule_into_a_copy_of_another_is_flagged(
        self, client: TestClient
    ) -> None:
        assert client.post("/rules", json=self._base("first")).status_code == 201
        other = {
            "id": "other",
            "outcome": "APPROVE",
            "conditions": [{"field": "income", "operator": "gte", "value": 50000}],
        }
        assert client.post("/rules", json=other).status_code == 201
        response = client.put("/rules/other", json=self._base("other"))
        assert response.status_code == 409


class TestRuleVocabularyValidation:
    """Regression coverage for a live incident: an AI-authored rule saved
    with outcome "disapprove_loan" and type "loan_eligibility" — neither is
    something the rest of the system understands (badge colors, gated
    worst-wins severity, or the engine's evaluator registry) — but nothing
    stopped it from being persisted. The bad type rule then broke /decide
    for every request. Both are now rejected at write time (create, update,
    and AI-authored), before they ever reach the database."""

    def test_unknown_outcome_is_rejected_on_create(self, client: TestClient) -> None:
        payload = make_rule(outcome="disapprove_loan")
        response = client.post("/rules", json=payload)
        assert response.status_code == 422
        assert "outcome" in response.json()["detail"]

    def test_unknown_type_is_rejected_on_create(self, client: TestClient) -> None:
        payload = {**make_rule(), "type": "loan_eligibility"}
        response = client.post("/rules", json=payload)
        assert response.status_code == 422
        assert "type" in response.json()["detail"]

    def test_lowercase_outcome_is_normalized_not_rejected(self, client: TestClient) -> None:
        payload = make_rule(outcome="approve")
        response = client.post("/rules", json=payload)
        assert response.status_code == 201
        assert response.json()["outcome"] == "APPROVE"

    def test_unknown_outcome_is_rejected_on_update(self, client: TestClient) -> None:
        payload = make_rule()
        assert client.post("/rules", json=payload).status_code == 201
        response = client.put(
            f"/rules/{payload['id']}", json={**payload, "outcome": "decline"}
        )
        assert response.status_code == 422
        # The bad update must not have overwritten the good rule.
        assert client.get(f"/rules/{payload['id']}").json()["outcome"] == "APPROVE"
        body = response.json()
        assert "detail" in body
        assert isinstance(body["detail"], str)

    def test_duplicate_rule(self, client: TestClient) -> None:
        payload = make_rule()
        assert client.post("/rules", json=payload).status_code == 201

        response = client.post("/rules", json=payload)

        assert response.status_code == 409
        body = response.json()
        assert "detail" in body
        assert "already exists" in body["detail"]
