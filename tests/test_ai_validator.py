# ============================================================
# FILE   : tests/test_ai_validator.py
# OWNER  : ROLE 3 — Quality & Audit
# PURPOSE: Comprehensive trust-layer tests for AI-generated JSON.
# ============================================================
"""Tests for strict, read-only validation of AI-generated operations."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from app.services.ai_validator import (
    DEFAULT_SUPPORTED_DATATYPES,
    AIValidator,
    ValidationContext,
    ValidationResponse,
)


@pytest.fixture
def context() -> ValidationContext:
    return ValidationContext(
        fields={
            "id": "string",
            "age": "integer",
            "salary": "number",
            "name": "string",
            "start_date": "date",
            "active": "boolean",
            "profile": "object",
        },
        rule_ids={"existing_rule", "age_rule"},
        field_usage={"age": ["age_rule"], "salary": ["existing_rule"]},
    )


@pytest.fixture
def validator() -> AIValidator:
    return AIValidator()


def full_rule(
    rule_id: str = "new_rule",
    *,
    field: str = "salary",
    operator: str = "gte",
    value: Any = 50000,
    rule_type: str = "conditional",
) -> dict[str, Any]:
    """Return a complete AI-generated rule with no inferred defaults."""
    return {
        "id": rule_id,
        "description": "A complete generated rule.",
        "type": rule_type,
        "logic": "AND",
        "conditions": [{"field": field, "operator": operator, "value": value}],
        "outcome": "APPROVE",
        "weight": 10,
        "priority": 20,
        "version": 1,
        "enabled": True,
    }


def assert_response_schema(response: ValidationResponse) -> None:
    payload = response.model_dump()
    assert set(payload) == {"valid", "errors", "warnings"}
    assert isinstance(payload["valid"], bool)
    assert isinstance(payload["errors"], list)
    assert isinstance(payload["warnings"], list)
    for issue in [*payload["errors"], *payload["warnings"]]:
        assert set(issue) == {"field", "message"}
        assert issue["field"] is None or isinstance(issue["field"], str)
        assert isinstance(issue["message"], str) and issue["message"]


def messages(response: ValidationResponse) -> list[str]:
    return [issue.message for issue in response.errors]


# ============================================================================
# Input boundary and response contract
# ============================================================================


class TestInputBoundary:
    @pytest.mark.parametrize(
        "payload",
        [
            "{",
            '{"operation": "add_field",}',
            '{"operation": }',
            b"\xff",
        ],
        ids=["truncated", "trailing-comma", "missing-value", "invalid-utf8"],
    )
    def test_malformed_json_is_rejected(
        self, validator: AIValidator, payload: str | bytes
    ) -> None:
        response = validator.validate(payload)
        assert response.valid is False
        assert "Malformed JSON" in response.errors[0].message
        assert_response_schema(response)

    @pytest.mark.parametrize(
        "payload",
        [None, [], 42, True, 3.14, "null", "[]", '"text"'],
        ids=[
            "none",
            "list",
            "integer",
            "boolean",
            "float",
            "json-null",
            "json-array",
            "json-string",
        ],
    )
    def test_non_object_payload_is_rejected(
        self, validator: AIValidator, payload: Any
    ) -> None:
        response = validator.validate(payload)  # type: ignore[arg-type]
        assert response.valid is False
        assert "JSON object" in response.errors[0].message

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"operation": None},
            {"operation": ""},
            {"operation": 123},
            {"operation": []},
        ],
    )
    def test_missing_or_invalid_operation_is_rejected(
        self, validator: AIValidator, payload: dict[str, Any]
    ) -> None:
        response = validator.validate(payload)
        assert response.valid is False
        assert response.errors[0].field == "operation"

    def test_unknown_operation_is_rejected(self, validator: AIValidator) -> None:
        response = validator.validate({"operation": "execute_sql"})
        assert response.valid is False
        assert response.errors[0].field == "operation"
        assert "Unsupported operation" in response.errors[0].message

    def test_operation_names_are_case_sensitive(self, validator: AIValidator) -> None:
        response = validator.validate(
            {"operation": "ADD_FIELD", "field": "bonus", "datatype": "number"}
        )
        assert response.valid is False
        assert "Unsupported operation" in response.errors[0].message

    def test_raw_valid_json_is_accepted(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            '{"operation":"add_field","field":"bonus","datatype":"number"}',
            context,
        )
        assert response.valid is True
        assert_response_schema(response)

    def test_bytes_valid_json_is_accepted(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            b'{"operation":"delete_rule","rule_id":"existing_rule"}', context
        )
        assert response.valid is True

    def test_validation_does_not_mutate_payload_or_context(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        payload = {"operation": "add_rule", "rule": full_rule()}
        original_payload = deepcopy(payload)
        original_context = deepcopy(context)
        validator.validate(payload, context)
        assert payload == original_payload
        assert context == original_context

    def test_structured_response_for_error(self, validator: AIValidator) -> None:
        response = validator.validate({"operation": "delete_rule"})
        assert_response_schema(response)


# ============================================================================
# add_field
# ============================================================================


class TestAddField:
    @pytest.mark.parametrize("datatype", sorted(DEFAULT_SUPPORTED_DATATYPES))
    def test_every_supported_datatype_is_accepted(
        self,
        validator: AIValidator,
        context: ValidationContext,
        datatype: str,
    ) -> None:
        response = validator.validate(
            {
                "operation": "add_field",
                "field": f"new_{datatype}",
                "datatype": datatype,
            },
            context,
        )
        assert response.valid is True

    @pytest.mark.parametrize(
        "payload",
        [
            {"operation": "add_field", "datatype": "number"},
            {"operation": "add_field", "field": "", "datatype": "number"},
            {"operation": "add_field", "field": None, "datatype": "number"},
            {"operation": "add_field", "field": 12, "datatype": "number"},
            {"operation": "add_field", "field": "bonus"},
        ],
    )
    def test_required_properties_are_enforced(
        self,
        validator: AIValidator,
        context: ValidationContext,
        payload: dict[str, Any],
    ) -> None:
        response = validator.validate(payload, context)
        assert response.valid is False

    def test_duplicate_field_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "add_field", "field": "salary", "datatype": "number"},
            context,
        )
        assert response.valid is False
        assert response.errors[0].field == "salary"
        assert "already exists" in response.errors[0].message

    def test_protected_field_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "add_field", "field": "id", "datatype": "string"},
            context,
        )
        assert response.valid is False
        assert "protected" in response.errors[0].message

    @pytest.mark.parametrize("datatype", ["money", "NUMBER", "", None, 7, [], {}])
    def test_unsupported_or_invalid_datatype_is_rejected(
        self,
        validator: AIValidator,
        context: ValidationContext,
        datatype: Any,
    ) -> None:
        response = validator.validate(
            {"operation": "add_field", "field": "bonus", "datatype": datatype},
            context,
        )
        assert response.valid is False
        assert_response_schema(response)

    def test_unconventional_name_returns_warning(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "add_field", "field": "annual salary", "datatype": "number"},
            context,
        )
        assert response.valid is True
        assert response.warnings
        assert response.warnings[0].field == "annual salary"

    def test_custom_datatype_policy_is_injected(
        self, context: ValidationContext
    ) -> None:
        custom = AIValidator(supported_datatypes={"currency"})
        accepted = custom.validate(
            {"operation": "add_field", "field": "bonus", "datatype": "currency"},
            context,
        )
        rejected = custom.validate(
            {"operation": "add_field", "field": "other", "datatype": "number"},
            context,
        )
        assert accepted.valid is True
        assert rejected.valid is False

    def test_custom_protected_fields_are_injected(
        self, context: ValidationContext
    ) -> None:
        custom = AIValidator(protected_fields={"salary"})
        response = custom.validate(
            {"operation": "add_field", "field": "salary", "datatype": "number"},
            ValidationContext(),
        )
        assert response.valid is False
        assert "protected" in response.errors[0].message


# ============================================================================
# update_field, delete_field, rename_field
# ============================================================================


class TestUpdateField:
    def test_existing_field_can_be_updated(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "update_field", "field": "name", "datatype": "string"},
            context,
        )
        assert response.valid is True

    def test_missing_field_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "update_field", "field": "ghost", "datatype": "string"},
            context,
        )
        assert response.valid is False
        assert "does not exist" in response.errors[0].message

    def test_protected_field_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "update_field", "field": "id", "datatype": "integer"},
            context,
        )
        assert response.valid is False

    def test_unsupported_datatype_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "update_field", "field": "name", "datatype": "blob"},
            context,
        )
        assert response.valid is False

    def test_used_field_datatype_change_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "update_field", "field": "salary", "datatype": "integer"},
            context,
        )
        assert response.valid is False
        assert "used by rules" in response.errors[0].message

    def test_no_update_properties_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "update_field", "field": "name"}, context
        )
        assert response.valid is False

    def test_used_field_datatype_change_is_blocked(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "update_field", "field": "salary", "datatype": "string"},
            context,
        )
        assert response.valid is False


class TestDeleteField:
    def test_existing_field_can_be_deleted(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "delete_field", "field": "name"}, context
        )
        assert response.valid is True

    def test_unknown_field_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "delete_field", "field": "ghost"}, context
        )
        assert response.valid is False

    def test_protected_field_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "delete_field", "field": "id"}, context
        )
        assert response.valid is False

    def test_used_field_produces_blocking_conflict(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "delete_field", "field": "age"}, context
        )
        assert response.valid is False
        assert "age_rule" in response.errors[0].message

    def test_used_field_delete_is_blocked(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "delete_field", "field": "age"}, context
        )
        assert response.valid is False


class TestRenameField:
    def test_existing_field_can_be_renamed(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "rename_field", "field": "name", "new_name": "full_name"},
            context,
        )
        assert response.valid is True

    @pytest.mark.parametrize("new_name", [None, "", 12, [], {}])
    def test_new_name_is_required_and_must_be_string(
        self,
        validator: AIValidator,
        context: ValidationContext,
        new_name: Any,
    ) -> None:
        response = validator.validate(
            {"operation": "rename_field", "field": "name", "new_name": new_name},
            context,
        )
        assert response.valid is False

    def test_target_collision_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "rename_field", "field": "name", "new_name": "salary"},
            context,
        )
        assert response.valid is False
        assert response.errors[0].field == "salary"

    def test_unknown_source_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "rename_field", "field": "ghost", "new_name": "new_ghost"},
            context,
        )
        assert response.valid is False

    @pytest.mark.parametrize(
        ("old", "new"),
        [("id", "new_id"), ("name", "id")],
        ids=["protected-source", "protected-target"],
    )
    def test_protected_source_or_target_is_rejected(
        self,
        validator: AIValidator,
        context: ValidationContext,
        old: str,
        new: str,
    ) -> None:
        response = validator.validate(
            {"operation": "rename_field", "field": old, "new_name": new}, context
        )
        assert response.valid is False

    def test_used_field_rename_is_blocked(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "rename_field", "field": "age", "new_name": "years"},
            context,
        )
        assert response.valid is False


# ============================================================================
# add_rule, update_rule, delete_rule
# ============================================================================


class TestRuleOperations:
    def test_complete_rule_is_accepted(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "add_rule", "rule": full_rule()}, context
        )
        assert response.valid is True

    @pytest.mark.parametrize(
        "rule_value",
        [None, "", [], 7],
        ids=["none", "string", "list", "integer"],
    )
    def test_rule_must_be_an_object(
        self,
        validator: AIValidator,
        context: ValidationContext,
        rule_value: Any,
    ) -> None:
        response = validator.validate(
            {"operation": "add_rule", "rule": rule_value}, context
        )
        assert response.valid is False
        assert response.errors[0].field == "rule"

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda rule: rule.pop("id"),
            lambda rule: rule.update(id=""),
            lambda rule: rule.pop("outcome"),
            lambda rule: rule.update(outcome=""),
            lambda rule: rule.update(conditions=[]),
            lambda rule: rule.update(logic="XOR"),
            lambda rule: rule.update(enabled="yes"),
            lambda rule: rule.update(weight=True),
            lambda rule: rule.update(priority=None),
        ],
        ids=[
            "missing-id",
            "empty-id",
            "missing-outcome",
            "empty-outcome",
            "empty-conditions",
            "bad-logic",
            "bad-enabled",
            "boolean-weight",
            "null-priority",
        ],
    )
    def test_malformed_rule_is_rejected(
        self,
        validator: AIValidator,
        context: ValidationContext,
        mutation: Any,
    ) -> None:
        rule = full_rule()
        mutation(rule)
        response = validator.validate({"operation": "add_rule", "rule": rule}, context)
        assert response.valid is False
        assert response.errors[0].field is not None
        assert response.errors[0].field.startswith("rule")

    def test_duplicate_rule_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "add_rule", "rule": full_rule("existing_rule")}, context
        )
        assert response.valid is False
        assert "already exists" in response.errors[0].message

    def test_existing_rule_can_be_updated(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "update_rule", "rule": full_rule("existing_rule")}, context
        )
        assert response.valid is True

    def test_missing_rule_cannot_be_updated(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "update_rule", "rule": full_rule("missing_rule")}, context
        )
        assert response.valid is False
        assert "does not exist" in response.errors[0].message

    def test_existing_rule_can_be_deleted(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "delete_rule", "rule_id": "existing_rule"}, context
        )
        assert response.valid is True

    @pytest.mark.parametrize("rule_id", [None, "", 12, [], {}])
    def test_delete_rule_requires_string_id(
        self,
        validator: AIValidator,
        context: ValidationContext,
        rule_id: Any,
    ) -> None:
        response = validator.validate(
            {"operation": "delete_rule", "rule_id": rule_id}, context
        )
        assert response.valid is False

    def test_unknown_rule_cannot_be_deleted(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "delete_rule", "rule_id": "ghost"}, context
        )
        assert response.valid is False

    @pytest.mark.parametrize(
        ("field", "operator", "value"),
        [
            ("salary", "gte", 10),
            ("name", "equals", "Alice"),
            ("active", "is_true", None),
            ("name", "contains", "A"),
            ("name", "regex", r"^[A-Z]"),
            ("name", "in", ["A", "B"]),
            ("name", "is_empty", None),
            ("start_date", "before", "2030-01-01"),
        ],
    )
    def test_supported_operator_is_accepted(
        self,
        validator: AIValidator,
        context: ValidationContext,
        field: str,
        operator: str,
        value: Any,
    ) -> None:
        response = validator.validate(
            {
                "operation": "add_rule",
                "rule": full_rule(field=field, operator=operator, value=value),
            },
            context,
        )
        assert response.valid is True

    def test_unsupported_operator_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {
                "operation": "add_rule",
                "rule": full_rule(operator="approximately"),
            },
            context,
        )
        assert response.valid is False
        assert response.errors[0].field == "salary"

    def test_unsupported_rule_type_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {
                "operation": "add_rule",
                "rule": full_rule(rule_type="python_script"),
            },
            context,
        )
        assert response.valid is False
        assert response.errors[0].field == "type"

    def test_dotted_field_is_compatible_when_root_exists(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {
                "operation": "add_rule",
                "rule": full_rule(field="profile.score"),
            },
            context,
        )
        assert response.valid is True
        assert response.warnings == []

    def test_unknown_schema_field_is_reported(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "add_rule", "rule": full_rule(field="credit_score")},
            context,
        )
        assert response.valid is False
        assert response.errors[0].field == "credit_score"

    def test_operator_policy_can_be_restricted(
        self, context: ValidationContext
    ) -> None:
        custom = AIValidator(supported_operators={"eq"})
        response = custom.validate(
            {"operation": "add_rule", "rule": full_rule(operator="gte")}, context
        )
        assert response.valid is False

    def test_custom_rule_type_policy_can_be_injected(
        self, context: ValidationContext
    ) -> None:
        custom = AIValidator(supported_rule_types={"external"})
        response = custom.validate(
            {
                "operation": "add_rule",
                "rule": full_rule(rule_type="external"),
            },
            context,
        )
        assert response.valid is True


# ============================================================================
# Strict trust-layer regression tests
# ============================================================================


class TestStrictTrustContract:
    @pytest.mark.parametrize(
        "missing",
        ["description", "type", "logic", "weight", "priority", "version", "enabled"],
    )
    def test_incomplete_rule_metadata_is_rejected(
        self,
        validator: AIValidator,
        context: ValidationContext,
        missing: str,
    ) -> None:
        rule = full_rule()
        rule.pop(missing)
        response = validator.validate({"operation": "add_rule", "rule": rule}, context)
        assert response.valid is False

    def test_missing_condition_value_is_rejected(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        rule = full_rule()
        rule["conditions"][0].pop("value")
        response = validator.validate({"operation": "add_rule", "rule": rule}, context)
        assert response.valid is False

    def test_unknown_rule_field_is_blocking_error(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "add_rule", "rule": full_rule(field="credit_score")},
            context,
        )
        assert response.valid is False

    def test_delete_rule_does_not_infer_missing_rule_id(
        self, validator: AIValidator, context: ValidationContext
    ) -> None:
        response = validator.validate(
            {"operation": "delete_rule", "rule": {"id": "existing_rule"}}, context
        )
        assert response.valid is False
        assert response.errors[0].field == "rule_id"

    @pytest.mark.parametrize(
        "payload",
        [
            {"operation": "update_field", "field": "ghost", "datatype": "string"},
            {"operation": "delete_field", "field": "ghost"},
            {"operation": "delete_rule", "rule_id": "ghost"},
        ],
    )
    def test_state_dependent_operation_without_context_is_rejected(
        self, validator: AIValidator, payload: dict[str, Any]
    ) -> None:
        response = validator.validate(payload)
        assert response.valid is False

    @pytest.mark.parametrize(
        ("field", "operator", "value"),
        [
            ("name", "gt", 3),
            ("salary", "between", [10]),
            ("name", "regex", "("),
            ("name", "in", "not-a-list"),
            ("start_date", "before", "not-a-date"),
        ],
    )
    def test_operator_value_and_datatype_compatibility_is_enforced(
        self,
        validator: AIValidator,
        context: ValidationContext,
        field: str,
        operator: str,
        value: Any,
    ) -> None:
        response = validator.validate(
            {
                "operation": "add_rule",
                "rule": full_rule(field=field, operator=operator, value=value),
            },
            context,
        )
        assert response.valid is False

    def test_internal_handler_exception_is_sanitized(
        self, validator: AIValidator
    ) -> None:
        def explode(payload: Any, context: Any, result: Any) -> None:
            raise RuntimeError("secret database connection string")

        validator.register_operation("explode", explode)
        response = validator.validate({"operation": "explode"})
        assert response.valid is False
        assert "secret" not in " ".join(messages(response))


# ============================================================================
# Extension point
# ============================================================================


class TestOperationRegistry:
    def test_custom_operation_can_be_registered(self, validator: AIValidator) -> None:
        called = False

        def custom(payload: Any, context: Any, result: Any) -> None:
            nonlocal called
            called = True
            result.warning("Custom check ran.", field="custom")

        validator.register_operation("custom_operation", custom)
        response = validator.validate({"operation": "custom_operation"})
        assert called is True
        assert response.valid is True
        assert response.warnings[0].field == "custom"
