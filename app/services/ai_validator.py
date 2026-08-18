# ============================================================
# FILE   : app/services/ai_validator.py
# OWNER  : ROLE 3 — Quality & Audit
# PURPOSE: Trust layer for AI-generated schema/rule changes. Validates the
#          structured JSON an LLM produces from a natural-language prompt
#          BEFORE anything is applied. It only inspects and explains —
#          it never mutates the schema, the rules, or the database.
# ============================================================
"""Validation of AI-generated schema and rule operations.

Business users describe changes in natural language; an LLM turns that into a
structured JSON *operation*. That JSON is untrusted and must be checked before
it is ever applied. This module is that gate.

Supported operations
---------------------
Schema:  ``add_field``, ``update_field``, ``delete_field``, ``rename_field``
Rules:   ``add_rule``, ``update_rule``, ``delete_rule``

Expected payload shapes (a single JSON object with an ``operation`` key)::

    {"operation": "add_field",    "field": "salary", "datatype": "number"}
    {"operation": "update_field", "field": "salary", "datatype": "integer"}
    {"operation": "delete_field", "field": "salary"}
    {"operation": "rename_field", "field": "salary", "new_name": "base_salary"}
    {"operation": "add_rule",     "rule": { ...full rule object... }}
    {"operation": "update_rule",  "rule": { ...full rule object... }}
    {"operation": "delete_rule",  "rule_id": "rule_reject_underage"}

What is checked
---------------
required properties, malformed JSON, invalid/unsupported operations, duplicate
fields, supported datatypes, supported operators, protected fields, schema
compatibility (does the target field exist?), and rule compatibility (valid
rule structure, known operators/type, referenced fields present).

Design
------
* **Read-only**: the validator never writes. Current state (existing fields,
  rule ids, field-usage) is supplied per call via :class:`ValidationContext`,
  so the validator has no coupling to any store or database.
* **Modular / open-closed**: each operation is a small handler registered in a
  dispatch table. New operations are added with :meth:`AIValidator.register_operation`
  — no existing handler changes.
* **Dependency injection**: policy inputs (protected fields, supported
  datatypes/operators/rule-types) are injected via the constructor, defaulting
  to Role 1's live registries so the trust layer and the engine never drift.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from rule_engine import OPERATORS, RULE_EVALUATORS, Rule, RuleValidationError
from rule_engine.evaluators import UNARY_OPERATORS

logger = logging.getLogger("app.services.ai_validator")

#: Datatypes an AI-proposed field may declare.
DEFAULT_SUPPORTED_DATATYPES: frozenset[str] = frozenset(
    {"string", "number", "integer", "boolean", "date", "datetime", "array", "object"}
)

#: Fields that may never be created, renamed, retyped, or deleted by AI.
DEFAULT_PROTECTED_FIELDS: frozenset[str] = frozenset({"id"})

SCHEMA_OPERATIONS: frozenset[str] = frozenset(
    {"add_field", "update_field", "delete_field", "rename_field"}
)
RULE_OPERATIONS: frozenset[str] = frozenset({"add_rule", "update_rule", "delete_rule"})

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_REQUIRED_RULE_PROPERTIES: frozenset[str] = frozenset(
    {
        "id",
        "description",
        "type",
        "logic",
        "conditions",
        "outcome",
        "weight",
        "priority",
        "version",
        "enabled",
    }
)
_NUMERIC_OPERATORS: frozenset[str] = frozenset(
    {"gt", ">", "gte", ">=", "lt", "<", "lte", "<=", "between"}
)
_STRING_OPERATORS: frozenset[str] = frozenset(
    {
        "contains",
        "not_contains",
        "starts_with",
        "ends_with",
        "eq_ci",
        "regex",
        "matches",
    }
)
_DATE_OPERATORS: frozenset[str] = frozenset(
    {
        "before",
        "date_lt",
        "after",
        "date_gt",
        "on_or_before",
        "date_lte",
        "on_or_after",
        "date_gte",
    }
)
_MEMBERSHIP_OPERATORS: frozenset[str] = frozenset({"in", "not_in"})
_BOOLEAN_OPERATORS: frozenset[str] = frozenset({"is_true", "is_false"})

__all__ = [
    "AIValidator",
    "ValidationContext",
    "ValidationIssue",
    "ValidationResponse",
    "DEFAULT_SUPPORTED_DATATYPES",
    "DEFAULT_PROTECTED_FIELDS",
]


# ----------------------------------------------------------------------------
# Structured response models (Pydantic — this is the API-facing contract)
# ----------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    """A single problem or caution about an operation.

    ``field`` names the schema field / rule id the issue is about, or ``None``
    for operation-level issues (malformed JSON, unknown operation, ...).
    """

    field: str | None = None
    message: str


class ValidationResponse(BaseModel):
    """The verdict returned to callers.

    ``valid`` is ``True`` only when there are no errors; warnings never block.
    """

    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)


# ----------------------------------------------------------------------------
# Current-state snapshot (injected per call; keeps the validator store-agnostic)
# ----------------------------------------------------------------------------


@dataclass
class ValidationContext:
    """Read-only snapshot of current state the validator checks against.

    Attributes:
        fields:      Existing field name -> declared datatype. Empty means the
                     schema is unknown, so existence/duplicate checks are
                     skipped (nothing to compare against).
        rule_ids:    Ids of rules that currently exist.
        field_usage: Field name -> ids of rules that reference it. Powers the
                     rule-compatibility warnings on delete/rename/retype.
    """

    fields: dict[str, str] = field(default_factory=dict)
    rule_ids: set[str] = field(default_factory=set)
    field_usage: dict[str, list[str]] = field(default_factory=dict)


class _Result:
    """Mutable accumulator a handler fills; converted to a response at the end."""

    def __init__(self) -> None:
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []

    def error(self, message: str, field: str | None = None) -> None:
        self.errors.append(ValidationIssue(field=field, message=message))

    def warning(self, message: str, field: str | None = None) -> None:
        self.warnings.append(ValidationIssue(field=field, message=message))

    def to_response(self) -> ValidationResponse:
        return ValidationResponse(
            valid=not self.errors, errors=self.errors, warnings=self.warnings
        )


#: A handler validates one operation, appending findings to the result.
OperationHandler = Callable[[Mapping[str, Any], ValidationContext, _Result], None]


class AIValidator:
    """Validates AI-generated schema/rule operations. Inspect-only, never applies.

    All policy inputs are injected; by default they mirror Role 1's live
    operator and rule-type registries so the trust layer stays in lockstep with
    the engine that will ultimately run the rules.
    """

    def __init__(
        self,
        *,
        protected_fields: Iterable[str] = DEFAULT_PROTECTED_FIELDS,
        supported_datatypes: Iterable[str] = DEFAULT_SUPPORTED_DATATYPES,
        supported_operators: Iterable[str] | None = None,
        supported_rule_types: Iterable[str] | None = None,
    ) -> None:
        self._protected_fields = frozenset(protected_fields)
        self._supported_datatypes = frozenset(supported_datatypes)
        self._supported_operators = frozenset(
            supported_operators if supported_operators is not None else OPERATORS.keys()
        )
        self._supported_rule_types = frozenset(
            supported_rule_types
            if supported_rule_types is not None
            else RULE_EVALUATORS.keys()
        )
        self._handlers: dict[str, OperationHandler] = {
            "add_field": self._add_field,
            "update_field": self._update_field,
            "delete_field": self._delete_field,
            "rename_field": self._rename_field,
            "add_rule": self._add_rule,
            "update_rule": self._update_rule,
            "delete_rule": self._delete_rule,
        }

    # -- public API ----------------------------------------------------------

    def register_operation(self, name: str, handler: OperationHandler) -> None:
        """Register a handler for a new operation (open-closed extension point)."""
        self._handlers[name] = handler

    def validate(
        self,
        payload: str | bytes | Mapping[str, Any],
        context: ValidationContext | None = None,
    ) -> ValidationResponse:
        """Validate one AI-generated operation.

        Accepts a parsed object or a raw JSON string/bytes. Always returns a
        structured :class:`ValidationResponse`; it never raises for bad input.
        """
        ctx = context if context is not None else ValidationContext()
        result = _Result()

        if isinstance(payload, (str, bytes, bytearray)):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, ValueError) as exc:
                result.error(f"Malformed JSON: {exc}")
                return result.to_response()

        if not isinstance(payload, Mapping):
            result.error("Payload must be a JSON object describing an operation.")
            return result.to_response()

        operation = payload.get("operation")
        if not isinstance(operation, str) or not operation:
            result.error("Missing required property 'operation'.", field="operation")
            return result.to_response()

        handler = self._handlers.get(operation)
        if handler is None:
            result.error(
                f"Unsupported operation '{operation}'. "
                f"Supported: {sorted(self._handlers)}.",
                field="operation",
            )
            return result.to_response()

        if context is None and operation in SCHEMA_OPERATIONS | RULE_OPERATIONS:
            result.error(
                "Validation context is required for schema and rule operations.",
                field="operation",
            )
            return result.to_response()

        try:
            handler(payload, ctx, result)
        except Exception:  # trust-layer boundary must never crash callers
            logger.exception("unexpected error validating operation '%s'", operation)
            result.error("Internal validation error.")

        return result.to_response()

    # -- schema operation handlers ------------------------------------------

    def _add_field(
        self, payload: Mapping[str, Any], ctx: ValidationContext, result: _Result
    ) -> None:
        name = self._require_field_name(payload, result)
        if name is None:
            return
        if name in self._protected_fields:
            result.error("Field is protected and cannot be created.", field=name)
        if name in ctx.fields:
            result.error("Field already exists.", field=name)
        self._check_datatype(payload, name, result, required=True)
        self._warn_if_unconventional(name, result)

    def _update_field(
        self, payload: Mapping[str, Any], ctx: ValidationContext, result: _Result
    ) -> None:
        name = self._require_field_name(payload, result)
        if name is None:
            return
        if name in self._protected_fields:
            result.error("Field is protected and cannot be modified.", field=name)
        if name not in ctx.fields:
            result.error("Field does not exist.", field=name)

        has_datatype = "datatype" in payload
        self._check_datatype(payload, name, result, required=False)
        if not has_datatype and "description" not in payload:
            result.error("No updatable properties provided.", field=name)
        if has_datatype and ctx.field_usage.get(name):
            users = ctx.field_usage[name]
            result.error(
                f"Field is used by rules {users}; changing its datatype may "
                "break their evaluation.",
                field=name,
            )

    def _delete_field(
        self, payload: Mapping[str, Any], ctx: ValidationContext, result: _Result
    ) -> None:
        name = self._require_field_name(payload, result)
        if name is None:
            return
        if name in self._protected_fields:
            result.error("Field is protected and cannot be deleted.", field=name)
        if name not in ctx.fields:
            result.error("Field does not exist.", field=name)
        if ctx.field_usage.get(name):
            users = ctx.field_usage[name]
            result.error(
                f"Field is used by rules {users}; it cannot be deleted safely.",
                field=name,
            )

    def _rename_field(
        self, payload: Mapping[str, Any], ctx: ValidationContext, result: _Result
    ) -> None:
        old = self._require_field_name(payload, result)
        new = payload.get("new_name")
        if not isinstance(new, str) or not new.strip():
            result.error("Missing required property 'new_name'.", field="new_name")
        if old is None:
            return

        if old in self._protected_fields:
            result.error("Field is protected and cannot be renamed.", field=old)
        if old not in ctx.fields:
            result.error("Field does not exist.", field=old)

        if isinstance(new, str) and new:
            if new in self._protected_fields:
                result.error("Target name is protected/reserved.", field=new)
            if new in ctx.fields:
                result.error("Field already exists.", field=new)
            self._warn_if_unconventional(new, result)
        if ctx.field_usage.get(old):
            users = ctx.field_usage[old]
            result.error(
                f"Field is used by rules {users}; it cannot be renamed safely.",
                field=old,
            )

    # -- rule operation handlers --------------------------------------------

    def _add_rule(
        self, payload: Mapping[str, Any], ctx: ValidationContext, result: _Result
    ) -> None:
        self._validate_rule(payload, ctx, result, must_exist=False)

    def _update_rule(
        self, payload: Mapping[str, Any], ctx: ValidationContext, result: _Result
    ) -> None:
        self._validate_rule(payload, ctx, result, must_exist=True)

    def _delete_rule(
        self, payload: Mapping[str, Any], ctx: ValidationContext, result: _Result
    ) -> None:
        rule_id = payload.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id:
            result.error("Missing required property 'rule_id'.", field="rule_id")
            return
        if rule_id not in ctx.rule_ids:
            result.error("Rule does not exist.", field=rule_id)

    # -- shared rule validation ---------------------------------------------

    def _validate_rule(
        self,
        payload: Mapping[str, Any],
        ctx: ValidationContext,
        result: _Result,
        *,
        must_exist: bool,
    ) -> None:
        rule_dict = payload.get("rule")
        if not isinstance(rule_dict, Mapping):
            result.error("Missing or invalid required property 'rule'.", field="rule")
            return

        missing = sorted(_REQUIRED_RULE_PROPERTIES - rule_dict.keys())
        for property_name in missing:
            result.error(
                f"Missing required rule property '{property_name}'.",
                field=f"rule.{property_name}",
            )
        if missing:
            return

        self._validate_raw_rule_types(rule_dict, result)
        self._validate_condition_completeness(rule_dict, result)
        if result.errors:
            return

        # Reuse Role 1's schema validation for structure / required properties.
        try:
            rule = Rule.from_dict(dict(rule_dict))
        except RuleValidationError as exc:
            result.error(f"Invalid rule: {exc}", field="rule")
            return

        if must_exist and rule.id not in ctx.rule_ids:
            result.error("Rule does not exist.", field=rule.id)
        if not must_exist and rule.id in ctx.rule_ids:
            result.error("Rule already exists.", field=rule.id)

        if rule.type not in self._supported_rule_types:
            result.error(f"Unsupported rule type '{rule.type}'.", field="type")

        for condition in rule.conditions:
            if condition.operator not in self._supported_operators:
                result.error(
                    f"Unsupported operator '{condition.operator}'.",
                    field=condition.field,
                )
            base = condition.field.split(".", 1)[0]
            if base not in ctx.fields:
                result.error(
                    f"Field '{base}' is not defined in the current schema; "
                    "the rule is incompatible with the current schema.",
                    field=condition.field,
                )
                continue
            if "." not in condition.field:
                self._validate_operator_compatibility(
                    field_name=condition.field,
                    datatype=ctx.fields[base],
                    operator=condition.operator,
                    value=condition.value,
                    result=result,
                )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _require_field_name(payload: Mapping[str, Any], result: _Result) -> str | None:
        name = payload.get("field")
        if not isinstance(name, str) or not name.strip():
            result.error("Missing required property 'field'.", field="field")
            return None
        return name

    def _check_datatype(
        self,
        payload: Mapping[str, Any],
        name: str,
        result: _Result,
        *,
        required: bool,
    ) -> None:
        datatype = payload.get("datatype")
        if datatype is None:
            if required:
                result.error("Missing required property 'datatype'.", field=name)
            return
        if not isinstance(datatype, str):
            result.error("Datatype must be a string.", field=name)
            return
        if datatype not in self._supported_datatypes:
            result.error(
                f"Unsupported datatype '{datatype}'. "
                f"Supported: {sorted(self._supported_datatypes)}.",
                field=name,
            )

    @staticmethod
    def _validate_raw_rule_types(rule: Mapping[str, Any], result: _Result) -> None:
        for property_name in ("id", "description", "type", "logic", "outcome"):
            value = rule[property_name]
            requires_content = property_name != "description"
            if not isinstance(value, str) or (requires_content and not value):
                qualifier = " non-empty" if requires_content else ""
                result.error(
                    f"Rule property '{property_name}' must be a{qualifier} string.",
                    field=f"rule.{property_name}",
                )

        for property_name in ("priority", "version"):
            value = rule[property_name]
            if isinstance(value, bool) or not isinstance(value, int):
                result.error(
                    f"Rule property '{property_name}' must be an integer.",
                    field=f"rule.{property_name}",
                )

        weight = rule["weight"]
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            result.error(
                "Rule property 'weight' must be a number.", field="rule.weight"
            )
        if not isinstance(rule["enabled"], bool):
            result.error(
                "Rule property 'enabled' must be a boolean.", field="rule.enabled"
            )

    @staticmethod
    def _validate_condition_completeness(
        rule: Mapping[str, Any], result: _Result
    ) -> None:
        conditions = rule.get("conditions")
        if not isinstance(conditions, list):
            return
        for index, condition in enumerate(conditions):
            if not isinstance(condition, Mapping):
                continue
            operator = condition.get("operator")
            if "value" not in condition and operator not in UNARY_OPERATORS:
                result.error(
                    "Missing required condition property 'value'.",
                    field=f"rule.conditions[{index}].value",
                )

    @staticmethod
    def _validate_operator_compatibility(
        *,
        field_name: str,
        datatype: str,
        operator: str,
        value: Any,
        result: _Result,
    ) -> None:
        if operator in _NUMERIC_OPERATORS and datatype not in {"number", "integer"}:
            result.error(
                f"Operator '{operator}' is incompatible with datatype '{datatype}'.",
                field=field_name,
            )
            return
        if operator in _STRING_OPERATORS and datatype != "string":
            result.error(
                f"Operator '{operator}' is incompatible with datatype '{datatype}'.",
                field=field_name,
            )
            return
        if operator in _DATE_OPERATORS and datatype not in {"date", "datetime"}:
            result.error(
                f"Operator '{operator}' is incompatible with datatype '{datatype}'.",
                field=field_name,
            )
            return
        if operator in _BOOLEAN_OPERATORS and datatype != "boolean":
            result.error(
                f"Operator '{operator}' is incompatible with datatype '{datatype}'.",
                field=field_name,
            )
            return

        if operator in _NUMERIC_OPERATORS:
            if operator == "between":
                valid = (
                    isinstance(value, (list, tuple))
                    and len(value) == 2
                    and all(AIValidator._is_number(item) for item in value)
                )
            else:
                valid = AIValidator._is_number(value)
            if not valid:
                suffix = "s [low, high]" if operator == "between" else ""
                result.error(
                    f"Operator '{operator}' requires numeric value{suffix}.",
                    field=field_name,
                )
        elif operator in {"regex", "matches"}:
            if not isinstance(value, str):
                result.error(
                    "Regex operator requires a string pattern.", field=field_name
                )
                return
            try:
                re.compile(value)
            except re.error:
                result.error("Regex pattern is invalid.", field=field_name)
        elif operator in _MEMBERSHIP_OPERATORS and not isinstance(
            value, (list, tuple, set)
        ):
            result.error(
                f"Operator '{operator}' requires a list of values.", field=field_name
            )
        elif operator in _DATE_OPERATORS and not AIValidator._is_iso_date(value):
            result.error(
                f"Operator '{operator}' requires a valid ISO date.", field=field_name
            )

    @staticmethod
    def _is_number(value: Any) -> bool:
        return not isinstance(value, bool) and isinstance(value, (int, float))

    @staticmethod
    def _is_iso_date(value: Any) -> bool:
        if isinstance(value, (date, datetime)):
            return True
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return False
        return True

    @staticmethod
    def _warn_if_unconventional(name: str, result: _Result) -> None:
        if not _IDENTIFIER_RE.fullmatch(name):
            result.warning(
                "Field name is not a conventional identifier "
                "(letters, digits, underscore; not starting with a digit).",
                field=name,
            )
