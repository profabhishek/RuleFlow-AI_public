"""Extended end-to-end and edge-case coverage for the public HTTP API.

Every test exercises the real FastAPI router, SQLite rule store, rule engine,
and response serialization. External LLM calls are replaced through FastAPI's
dependency-injection seam so the suite remains deterministic and offline.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.audit_log import AuditLogError, AuditService

APPROVE_REQUEST = {
    "age": 35,
    "identity_verified": True,
    "credit_score": 760,
    "dti": 28,
    "fraud_score": 12,
    "on_watchlist": False,
}

VALID_AI_RULE = {
    "id": "ai-high-income",
    "description": "Approve applicants with income of at least 100000.",
    "type": "conditional",
    "category": "underwriting",
    "conditions": [{"field": "income", "operator": ">=", "value": 100000}],
    "logic": "AND",
    "outcome": "APPROVE",
    "weight": 25,
    "priority": 30,
    "enabled": True,
}

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


class FakeLLM:
    """Return queued responses or raise a configured provider failure."""

    def __init__(self, *responses: str, error: Exception | None = None) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise RuntimeError("FakeLLM has no response configured")
        return self.responses.pop(0)


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """Create a fully isolated API with seeded rules and a temporary audit."""
    repo_root = Path(__file__).resolve().parents[1]
    environment = {
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'e2e.db').as_posix()}",
        "RULES_PATH": str(repo_root / "rules" / "rules.json"),
        "AUDIT_LOG_PATH": str(tmp_path / "audit.jsonl"),
        "LLM_PROVIDER": "stub",
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)

    from app.config import get_settings
    from app.dependencies import get_llm_provider
    from app.main import app
    from app.routers.audit import get_audit_service
    from app.services import rule_store

    get_settings.cache_clear()
    get_llm_provider.cache_clear()
    get_audit_service.cache_clear()
    if rule_store._engine is not None:
        rule_store._engine.dispose()
    rule_store._engine = None
    app.dependency_overrides.clear()

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    if rule_store._engine is not None:
        rule_store._engine.dispose()
    rule_store._engine = None
    get_audit_service.cache_clear()
    get_llm_provider.cache_clear()
    get_settings.cache_clear()
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def override_llm(client: TestClient, provider: FakeLLM) -> None:
    from app.dependencies import get_llm_provider

    client.app.dependency_overrides[get_llm_provider] = lambda: provider


def assert_decision(body: object, expected: str | None = None) -> None:
    assert isinstance(body, dict)
    assert DECISION_KEYS <= set(body)
    if expected is not None:
        assert body["decision"] == expected
    assert 0 <= body["confidence"] <= 1
    assert isinstance(body["trace"], list)


def enable_ai_explanations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the optional AI-written decision summary back on for a test.

    It is off by default: it costs a second LLM call per query (Gemini's
    free tier is 20/day) and the engine already returns a precise
    deterministic explanation. Tests that assert on AI prose must opt in.
    """
    from app.config import get_settings

    monkeypatch.setenv("NL_EXPLANATIONS", "true")
    get_settings.cache_clear()


class TestApplicationContract:
    def test_health_endpoint(self, client: TestClient) -> None:
        response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        # Health also reports which LLM is really in use. Without this a
        # misconfigured provider silently degrades to offline extraction
        # and looks like "the AI is just bad", which cost real debugging
        # time — so the contract is asserted, not just the status.
        assert body["llm_provider"] == "stub"
        assert body["llm_active"] == "StubProvider"
        assert body["llm_degraded"] is False

    def test_cors_preflight_allows_static_frontend(self, client: TestClient) -> None:
        response = client.options(
            "/rules",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        assert "GET" in response.headers["access-control-allow-methods"]

    def test_frontend_calls_only_documented_api_routes(
        self, client: TestClient
    ) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        index = (repo_root / "frontend" / "index.html").read_text(encoding="utf-8")
        script = (repo_root / "frontend" / "app.js").read_text(encoding="utf-8")

        assert 'href="styles.css"' in index
        assert 'src="app.js"' in index
        frontend_paths = set(re.findall(r'api\("[A-Z]+", "([^"]+)', script))
        static_paths = {
            path
            for path in frontend_paths
            if "+" not in path and not path.endswith("/")
        }
        openapi_paths = set(client.get("/openapi.json").json()["paths"])
        assert static_paths <= openapi_paths


class TestSingleDecisionEdges:
    @pytest.mark.parametrize("payload", [[], None, "request", 1])
    def test_non_object_request_is_rejected(
        self, client: TestClient, payload: object
    ) -> None:
        response = client.post("/decide", json=payload)

        assert response.status_code == 422

    def test_audit_write_failure_does_not_block_decision(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.routers import decisions

        class BrokenAudit:
            def append(self, record: object) -> None:
                raise AuditLogError("disk unavailable")

        monkeypatch.setattr(decisions, "get_audit_service", lambda: BrokenAudit())

        response = client.post("/decide", json=APPROVE_REQUEST)

        assert response.status_code == 200
        assert_decision(response.json(), "APPROVE")

    def test_configured_default_is_used_when_no_rules_match(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import get_settings

        for rule in client.get("/rules").json():
            assert client.delete(f"/rules/{rule['id']}").status_code == 204
        monkeypatch.setenv("DEFAULT_OUTCOME", "REJECT")
        get_settings.cache_clear()

        response = client.post("/decide", json={})

        assert response.status_code == 200
        assert_decision(response.json(), "REJECT")


class TestBulkDecisions:
    def test_mixed_bulk_decisions_preserve_order(self, client: TestClient) -> None:
        requests = [
            APPROVE_REQUEST,
            {**APPROVE_REQUEST, "age": 16},
            {},
        ]

        response = client.post("/decide/bulk", json=requests)

        assert response.status_code == 200
        body = response.json()
        assert [item["decision"] for item in body] == [
            "APPROVE",
            "REJECT",
            "REVIEW",
        ]
        for item in body:
            assert_decision(item)

    def test_empty_bulk_request_returns_empty_list(self, client: TestClient) -> None:
        response = client.post("/decide/bulk", json=[])

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.parametrize("payload", [{}, "not-a-list", [1], [None]])
    def test_invalid_bulk_shapes_are_rejected(
        self, client: TestClient, payload: object
    ) -> None:
        response = client.post("/decide/bulk", json=payload)

        assert response.status_code == 422

    def test_each_bulk_decision_is_audited(self, client: TestClient) -> None:
        response = client.post(
            "/decide/bulk",
            json=[APPROVE_REQUEST, {**APPROVE_REQUEST, "age": 16}],
        )
        assert response.status_code == 200

        audit = client.get("/audit", params={"event_type": "decision_evaluation"})
        assert audit.status_code == 200
        assert [record["decision"] for record in audit.json()] == [
            "APPROVE",
            "REJECT",
        ]


class TestGatedDecisions:
    def test_all_gates_approve(self, client: TestClient) -> None:
        response = client.post("/decide/gated", json=APPROVE_REQUEST)

        assert response.status_code == 200
        body = response.json()
        assert body["final_decision"] == "APPROVE"
        assert [gate["category"] for gate in body["gates"]] == [
            "kyc",
            "fraud",
            "underwriting",
            "affordability",
        ]
        for gate in body["gates"]:
            assert_decision(gate["decision"], "APPROVE")

    def test_worst_gate_wins(self, client: TestClient) -> None:
        response = client.post(
            "/decide/gated", json={**APPROVE_REQUEST, "on_watchlist": True}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["final_decision"] == "REJECT"
        decisions = {
            gate["category"]: gate["decision"]["decision"] for gate in body["gates"]
        }
        assert decisions["fraud"] == "REJECT"
        assert decisions["kyc"] == "APPROVE"

    def test_missing_fields_fail_safe_to_review(self, client: TestClient) -> None:
        response = client.post("/decide/gated", json={})

        assert response.status_code == 200
        assert response.json()["final_decision"] == "REVIEW"

    def test_no_rules_returns_default_and_no_gates(self, client: TestClient) -> None:
        for rule in client.get("/rules").json():
            assert client.delete(f"/rules/{rule['id']}").status_code == 204

        response = client.post("/decide/gated", json=APPROVE_REQUEST)

        assert response.status_code == 200
        assert response.json() == {"final_decision": "REVIEW", "gates": []}

    def test_non_object_request_is_rejected(self, client: TestClient) -> None:
        response = client.post("/decide/gated", json=[])

        assert response.status_code == 422


class TestNaturalLanguageDecision:
    def test_successful_query_runs_full_decision_flow(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enable_ai_explanations(monkeypatch)
        provider = FakeLLM(
            json.dumps(APPROVE_REQUEST),
            "The applicant clears every decision gate.",
        )
        override_llm(client, provider)

        response = client.post(
            "/decide/query", json={"text": "A low-risk qualified applicant"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["extracted_request"] == APPROVE_REQUEST
        assert body["explanation"] == "The applicant clears every decision gate."
        assert_decision(body["decision"], "APPROVE")
        assert len(provider.calls) == 2
        assert provider.calls[0]["json_mode"] is True

    def test_explanations_are_off_by_default_and_cost_one_call(
        self, client: TestClient
    ) -> None:
        # Quota guard: the default path must make exactly ONE LLM call and
        # leave `explanation` empty, so callers fall back to the engine's
        # own deterministic explanation.
        provider = FakeLLM(json.dumps(APPROVE_REQUEST))
        override_llm(client, provider)

        response = client.post("/decide/query", json={"text": "A qualified applicant"})

        assert response.status_code == 200
        body = response.json()
        assert body["explanation"] == ""
        assert len(provider.calls) == 1
        assert body["decision"]["explanation"]  # the engine still explains itself
        assert_decision(body["decision"], "APPROVE")

    def test_extraction_prompt_names_the_known_fields(
        self, client: TestClient
    ) -> None:
        # The model must be told which fields exist, or it invents
        # plausible-but-wrong names (`verified_identity` for
        # `identity_verified`), silently breaking the KYC gate.
        provider = FakeLLM(json.dumps(APPROVE_REQUEST))
        override_llm(client, provider)

        assert (
            client.post("/decide/query", json={"text": "an applicant"}).status_code
            == 200
        )

        system_prompt = provider.calls[0]["system"]
        assert "Known fields:" in system_prompt
        for field in ("age", "credit_score", "identity_verified", "on_watchlist"):
            assert field in system_prompt

    def test_offline_stub_query_end_to_end(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enable_ai_explanations(monkeypatch)
        response = client.post(
            "/decide/query",
            json={"text": ("age 35, credit_score 760, dti 28, fraud_score 12")},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["extracted_request"]["age"] == 35
        assert body["extracted_request"]["credit_score"] == 760
        assert_decision(body["decision"], "APPROVE")
        assert body["explanation"].startswith("Decision: APPROVE")

    def test_offline_stub_reads_natural_prose(self, client: TestClient) -> None:
        # Regression: the old stub heuristic turned "A 16 year old ...
        # credit score 720 ... fraud score 12" into {"A": 16, "score": 12}
        # and wrongly APPROVED an under-age applicant.
        response = client.post(
            "/decide/query",
            json={
                "text": (
                    "A 16 year old with verified identity, credit score 720, "
                    "dti 28, fraud score 12, not on a watchlist."
                )
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["extracted_request"]["age"] == 16
        assert body["extracted_request"]["on_watchlist"] is False
        assert_decision(body["decision"], "REJECT")
        assert "kyc_reject_underage" in body["decision"]["rules_matched"]

    def test_invalid_ai_json_is_rejected(self, client: TestClient) -> None:
        override_llm(client, FakeLLM("not-json"))

        response = client.post("/decide/query", json={"text": "applicant"})

        assert response.status_code == 422
        assert "not valid JSON" in response.json()["detail"]

    @pytest.mark.parametrize("ai_output", ["[]", "null", '"text"', "42"])
    def test_non_object_ai_output_is_rejected(
        self, client: TestClient, ai_output: str
    ) -> None:
        override_llm(client, FakeLLM(ai_output))

        response = client.post("/decide/query", json={"text": "applicant"})

        assert response.status_code == 422
        assert "must be a JSON object" in response.json()["detail"]

    def test_extraction_provider_failure_returns_502(self, client: TestClient) -> None:
        override_llm(client, FakeLLM(error=RuntimeError("offline")))

        response = client.post("/decide/query", json={"text": "applicant"})

        assert response.status_code == 502
        assert "LLM provider failed" in response.json()["detail"]

    def test_explanation_provider_failure_returns_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only reachable with explanations enabled — that second call is
        # what can fail. With the default (off) there is nothing to fail.
        enable_ai_explanations(monkeypatch)
        provider = FakeLLM(json.dumps(APPROVE_REQUEST))
        override_llm(client, provider)

        response = client.post("/decide/query", json={"text": "applicant"})

        assert response.status_code == 502
        assert "LLM provider failed" in response.json()["detail"]

    @pytest.mark.parametrize("payload", [{}, {"text": None}, {"text": 123}])
    def test_invalid_query_body_is_rejected(
        self, client: TestClient, payload: object
    ) -> None:
        response = client.post("/decide/query", json=payload)

        assert response.status_code == 422


class TestNaturalLanguageRuleCreation:
    def test_generated_rule_is_normalized_and_persisted(
        self, client: TestClient
    ) -> None:
        raw_rule = json.dumps(VALID_AI_RULE)
        provider = FakeLLM(raw_rule)
        override_llm(client, provider)

        response = client.post(
            "/rules/from-text",
            json={"text": "Approve income greater than or equal to 100000"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["raw_ai_output"] == raw_rule
        assert body["rule"]["id"] == "ai-high-income"
        assert body["rule"]["conditions"][0]["operator"] == "gte"
        persisted = client.get("/rules/ai-high-income")
        assert persisted.status_code == 200
        assert persisted.json() == body["rule"]

    def test_offline_stub_rule_creation_end_to_end(self, client: TestClient) -> None:
        response = client.post(
            "/rules/from-text",
            json={"text": "Approve when income is 100000"},
        )

        assert response.status_code == 201
        rule = response.json()["rule"]
        assert rule["id"].startswith("stub-")
        assert rule["outcome"] == "APPROVE"
        assert rule["conditions"] == [
            {"field": "income", "operator": "gte", "value": 100000.0}
        ]
        assert client.get(f"/rules/{rule['id']}").status_code == 200

    def test_invalid_ai_json_is_rejected(self, client: TestClient) -> None:
        override_llm(client, FakeLLM("```json\n{}\n```"))

        response = client.post("/rules/from-text", json={"text": "Create a rule"})

        assert response.status_code == 422
        assert "not valid JSON" in response.json()["detail"]

    @pytest.mark.parametrize("ai_output", ["[]", "null", '"rule"', "10"])
    def test_non_object_ai_output_is_rejected(
        self, client: TestClient, ai_output: str
    ) -> None:
        override_llm(client, FakeLLM(ai_output))

        response = client.post("/rules/from-text", json={"text": "Create a rule"})

        assert response.status_code == 422

    def test_invalid_generated_rule_is_rejected(self, client: TestClient) -> None:
        override_llm(client, FakeLLM(json.dumps({"id": "incomplete"})))

        response = client.post("/rules/from-text", json={"text": "Create a rule"})

        assert response.status_code == 422
        assert "failed rule validation" in response.json()["detail"]
        assert client.get("/rules/incomplete").status_code == 404

    def test_generated_duplicate_id_returns_conflict(self, client: TestClient) -> None:
        raw_rule = json.dumps(VALID_AI_RULE)
        provider = FakeLLM(raw_rule, raw_rule)
        override_llm(client, provider)
        assert (
            client.post("/rules/from-text", json={"text": "first"}).status_code == 201
        )

        response = client.post("/rules/from-text", json={"text": "again"})

        assert response.status_code == 409
        assert "collides" in response.json()["detail"]

    def test_provider_failure_returns_502(self, client: TestClient) -> None:
        override_llm(client, FakeLLM(error=RuntimeError("quota exceeded")))

        response = client.post("/rules/from-text", json={"text": "Create a rule"})

        assert response.status_code == 502
        assert "LLM provider failed" in response.json()["detail"]

    @pytest.mark.parametrize("payload", [{}, {"text": None}, {"text": []}])
    def test_invalid_request_body_is_rejected(
        self, client: TestClient, payload: object
    ) -> None:
        response = client.post("/rules/from-text", json=payload)

        assert response.status_code == 422


class TestRuleCrudEdges:
    @pytest.mark.parametrize("method", ["get", "put", "delete"])
    def test_missing_rule_returns_404(self, client: TestClient, method: str) -> None:
        if method == "put":
            response = client.put("/rules/missing", json=VALID_AI_RULE)
        else:
            response = getattr(client, method)("/rules/missing")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_path_id_overrides_update_body_id(self, client: TestClient) -> None:
        created = client.post("/rules", json=VALID_AI_RULE)
        assert created.status_code == 201

        response = client.put(
            "/rules/ai-high-income",
            json={**VALID_AI_RULE, "id": "attempted-id-change", "priority": 99},
        )

        assert response.status_code == 200
        assert response.json()["id"] == "ai-high-income"
        assert response.json()["priority"] == 99
        assert client.get("/rules/attempted-id-change").status_code == 404

    def test_operator_alias_is_canonicalized(self, client: TestClient) -> None:
        response = client.post("/rules", json=VALID_AI_RULE)

        assert response.status_code == 201
        assert response.json()["conditions"][0]["operator"] == "gte"

    def test_store_filters_seed_rules_by_category(self, client: TestClient) -> None:
        from app.services import rule_store

        assert client.get("/rules").status_code == 200
        kyc_rules = rule_store.list_rules(category="kyc")

        assert len(kyc_rules) == 3
        assert {rule.category for rule in kyc_rules} == {"kyc"}

    @pytest.mark.parametrize("payload", [[], None, "rule", 1])
    def test_non_object_rule_payload_is_rejected(
        self, client: TestClient, payload: object
    ) -> None:
        response = client.post("/rules", json=payload)

        assert response.status_code == 422

    def test_malformed_json_request_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/rules",
            content=b'{"id":',
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 422


class FailingAuditService(AuditService):
    def __init__(self) -> None:
        pass

    def get_all(self):
        raise AuditLogError("private storage failure")

    def get_by_event(self, event_type):
        raise AuditLogError("private storage failure")


class TestAuditEndpointEdges:
    def test_filter_returns_only_requested_event(self, client: TestClient) -> None:
        assert client.post("/decide", json=APPROVE_REQUEST).status_code == 200

        response = client.get("/audit", params={"event_type": "decision_evaluation"})

        assert response.status_code == 200
        records = response.json()
        assert len(records) == 1
        assert {record["event_type"] for record in records} == {"decision_evaluation"}

    def test_invalid_filter_is_rejected(self, client: TestClient) -> None:
        response = client.get("/audit", params={"event_type": "unknown"})

        assert response.status_code == 422

    @pytest.mark.parametrize("event_type", [None, "decision_evaluation"])
    def test_storage_failure_is_sanitized(
        self, client: TestClient, event_type: str | None
    ) -> None:
        from app.main import app
        from app.routers.audit import get_audit_service

        app.dependency_overrides[get_audit_service] = FailingAuditService
        params = {"event_type": event_type} if event_type is not None else None

        response = client.get("/audit", params=params)

        assert response.status_code == 500
        assert response.json()["detail"] == "Could not read the audit trail."
        assert "private" not in response.text
