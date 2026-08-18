# ============================================================
# FILE   : rule_engine/evaluators.py
# OWNER  : ROLE 1 — Rule Engine
# PURPOSE: Two-level plugin registry: operators + rule-type evaluators.
#          Every evaluator implements evaluate(rule, request) -> RuleResult.
# ============================================================
"""Evaluators for the rule engine.

Two plugin registries, per the team guideline "use the plugin/registry
pattern" and the evaluator contract ``evaluate(rule, request) -> RuleResult``:

1. **Operator registry** (low level). Each operator is a pure function
   ``(actual, expected) -> bool`` registered by name via ``@operator``.
   Adding a comparison (regex, between, ...) is ~5 lines here, nothing else.

2. **Rule-type evaluator registry** (high level). Each rule ``type`` maps to
   a class implementing ``evaluate(rule, request) -> RuleResult``. Adding a
   whole new *kind* of rule (e.g. a scripted rule or an external-lookup rule)
   means: create an evaluator class, register it — **no engine changes**.

The engine never hardcodes either level; it only does registry lookups.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, ClassVar

from .exceptions import OperatorError, UnknownRuleTypeError
from .models import Condition, ConditionResult, Rule, RuleResult

logger = logging.getLogger("rule_engine.evaluators")

# ----------------------------------------------------------------------------
# Level 1 — operator registry
# ----------------------------------------------------------------------------

OPERATORS: dict[str, Callable[[Any, Any], bool]] = {}
UNARY_OPERATORS: set[str] = set()


def operator(*names: str, unary: bool = False) -> Callable:
    """Register a comparison function under one or more operator names."""

    def register(func: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
        for name in names:
            OPERATORS[name] = func
            if unary:
                UNARY_OPERATORS.add(name)
        return func

    return register


def apply_operator(name: str, actual: Any, expected: Any = None) -> bool:
    """Look up and run an operator; unknown names raise ``OperatorError``."""
    func = OPERATORS.get(name)
    if func is None:
        raise OperatorError(
            f"Unknown operator '{name}'. Registered: {sorted(OPERATORS)}."
        )
    return func(actual, expected)


def _as_number(value: Any) -> float:
    if isinstance(value, bool):
        raise OperatorError("Expected a number, got a boolean.")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise OperatorError(f"Value '{value}' is not numeric.") from exc
    raise OperatorError(f"Value of type {type(value).__name__} is not numeric.")


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError as exc:
            raise OperatorError(f"Value '{value}' is not an ISO date.") from exc
    raise OperatorError(f"Value of type {type(value).__name__} is not a date.")


# Numeric ---------------------------------------------------------------------


@operator("gt", ">")
def _gt(actual: Any, expected: Any) -> bool:
    return _as_number(actual) > _as_number(expected)


@operator("gte", ">=")
def _gte(actual: Any, expected: Any) -> bool:
    return _as_number(actual) >= _as_number(expected)


@operator("lt", "<")
def _lt(actual: Any, expected: Any) -> bool:
    return _as_number(actual) < _as_number(expected)


@operator("lte", "<=")
def _lte(actual: Any, expected: Any) -> bool:
    return _as_number(actual) <= _as_number(expected)


@operator("between")
def _between(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, (list, tuple)) or len(expected) != 2:
        raise OperatorError("'between' expects a [low, high] pair.")
    low, high = _as_number(expected[0]), _as_number(expected[1])
    return low <= _as_number(actual) <= high


# Equality / boolean ----------------------------------------------------------


@operator("eq", "==", "equals")
def _eq(actual: Any, expected: Any) -> bool:
    try:
        return _as_number(actual) == _as_number(expected)
    except OperatorError:
        return bool(actual == expected)


@operator("ne", "!=", "not_equals")
def _ne(actual: Any, expected: Any) -> bool:
    return not _eq(actual, expected)


@operator("is_true", unary=True)
def _is_true(actual: Any, expected: Any = None) -> bool:
    return actual is True


@operator("is_false", unary=True)
def _is_false(actual: Any, expected: Any = None) -> bool:
    return actual is False


# String ----------------------------------------------------------------------


@operator("contains")
def _contains(actual: Any, expected: Any) -> bool:
    return str(expected) in str(actual)


@operator("not_contains")
def _not_contains(actual: Any, expected: Any) -> bool:
    return str(expected) not in str(actual)


@operator("starts_with")
def _starts_with(actual: Any, expected: Any) -> bool:
    return str(actual).startswith(str(expected))


@operator("ends_with")
def _ends_with(actual: Any, expected: Any) -> bool:
    return str(actual).endswith(str(expected))


@operator("eq_ci")
def _eq_ci(actual: Any, expected: Any) -> bool:
    return str(actual).strip().lower() == str(expected).strip().lower()


@operator("regex", "matches")
def _regex(actual: Any, expected: Any) -> bool:
    try:
        pattern = re.compile(str(expected))
    except re.error as exc:
        raise OperatorError(f"Invalid regex '{expected}': {exc}") from exc
    return pattern.search(str(actual)) is not None


# Membership ------------------------------------------------------------------


@operator("in")
def _in(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, (list, tuple, set)):
        raise OperatorError("'in' expects a list of allowed values.")
    return actual in expected


@operator("not_in")
def _not_in(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, (list, tuple, set)):
        raise OperatorError("'not_in' expects a list of disallowed values.")
    return actual not in expected


# Emptiness -------------------------------------------------------------------


@operator("is_empty", unary=True)
def _is_empty(actual: Any, expected: Any = None) -> bool:
    if actual is None:
        return True
    if isinstance(actual, (str, list, dict, tuple, set)):
        return len(actual) == 0
    return False


@operator("is_not_empty", unary=True)
def _is_not_empty(actual: Any, expected: Any = None) -> bool:
    return not _is_empty(actual)


# Dates -----------------------------------------------------------------------


@operator("before", "date_lt")
def _before(actual: Any, expected: Any) -> bool:
    return _as_date(actual) < _as_date(expected)


@operator("after", "date_gt")
def _after(actual: Any, expected: Any) -> bool:
    return _as_date(actual) > _as_date(expected)


@operator("on_or_before", "date_lte")
def _on_or_before(actual: Any, expected: Any) -> bool:
    return _as_date(actual) <= _as_date(expected)


@operator("on_or_after", "date_gte")
def _on_or_after(actual: Any, expected: Any) -> bool:
    return _as_date(actual) >= _as_date(expected)


# ----------------------------------------------------------------------------
# Level 2 — rule-type evaluator registry
# ----------------------------------------------------------------------------


class RuleEvaluator(ABC):
    """Contract every rule-type evaluator must implement.

    Team guideline: ``evaluate(rule, request) -> RuleResult``.
    Evaluators must be stateless and deterministic.
    """

    rule_type: ClassVar[str]

    @abstractmethod
    def evaluate(self, rule: Rule, request: dict[str, Any]) -> RuleResult:
        """Evaluate one rule against one request. Never raises for data
        problems — they become non-matches with an explanatory note."""


RULE_EVALUATORS: dict[str, RuleEvaluator] = {}


def register_evaluator(cls: type[RuleEvaluator]) -> type[RuleEvaluator]:
    """Class decorator: registers an evaluator instance under its rule_type."""
    RULE_EVALUATORS[cls.rule_type] = cls()
    return cls


def get_evaluator(rule_type: str) -> RuleEvaluator:
    evaluator = RULE_EVALUATORS.get(rule_type)
    if evaluator is None:
        raise UnknownRuleTypeError(
            f"No evaluator registered for rule type '{rule_type}'. "
            f"Registered: {sorted(RULE_EVALUATORS)}."
        )
    return evaluator


_MISSING = object()


def _read_field(request: dict[str, Any], field_name: str) -> Any:
    """Read a field, supporting dotted paths like ``company.size``."""
    if field_name in request:
        return request[field_name]
    if "." in field_name:
        cursor: Any = request
        for part in field_name.split("."):
            if isinstance(cursor, dict) and part in cursor:
                cursor = cursor[part]
            else:
                return _MISSING
        return cursor
    return _MISSING


@register_evaluator
class ConditionalRuleEvaluator(RuleEvaluator):
    """Default evaluator: AND/OR over a list of operator conditions.

    Fail-safe by design: a missing field or a type mismatch makes the
    condition fail with a recorded note — it never crashes the run.
    """

    rule_type: ClassVar[str] = "conditional"

    def evaluate(self, rule: Rule, request: dict[str, Any]) -> RuleResult:
        condition_results = [
            self._check(condition, request) for condition in rule.conditions
        ]
        flags = [c.passed for c in condition_results]
        matched = all(flags) if rule.logic == "AND" else any(flags)

        logger.debug("rule=%s matched=%s conditions=%s", rule.id, matched, len(flags))
        return RuleResult(
            rule_id=rule.id,
            outcome=rule.outcome,
            matched=matched,
            weight=rule.weight,
            priority=rule.priority,
            conditions=condition_results,
        )

    @staticmethod
    def _check(condition: Condition, request: dict[str, Any]) -> ConditionResult:
        actual = _read_field(request, condition.field)

        if actual is _MISSING:
            if condition.operator == "is_empty":
                return ConditionResult(
                    field=condition.field,
                    operator=condition.operator,
                    expected=condition.value,
                    actual=None,
                    passed=True,
                    note="field absent -> treated as empty",
                )
            return ConditionResult(
                field=condition.field,
                operator=condition.operator,
                expected=condition.value,
                actual=None,
                passed=False,
                note="field absent from request",
            )

        try:
            passed = apply_operator(condition.operator, actual, condition.value)
            note = ""
        except OperatorError as exc:
            passed = False
            note = f"operator error: {exc}"

        return ConditionResult(
            field=condition.field,
            operator=condition.operator,
            expected=condition.value,
            actual=actual,
            passed=passed,
            note=note,
        )
