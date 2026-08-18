# ============================================================
# FILE   : tests/test_evaluators.py
# OWNER  : ROLE 1 — Rule Engine
# PURPOSE: Unit tests for operators and the evaluator contract.
# ============================================================
"""Tests for the operator registry and the rule-type evaluator interface."""

import pytest

from rule_engine import (
    Condition,
    Rule,
    RuleResult,
    apply_operator,
    get_evaluator,
)
from rule_engine.exceptions import OperatorError, UnknownRuleTypeError

# ----------------------------------------------------------------------------
# Operator registry
# ----------------------------------------------------------------------------


class TestNumericOperators:
    def test_comparisons(self) -> None:
        assert apply_operator("gt", 10, 5) is True
        assert apply_operator("gt", 5, 10) is False
        assert apply_operator("gte", 5, 5) is True
        assert apply_operator("lt", 3, 4) is True
        assert apply_operator("lte", 4, 4) is True

    def test_aliases(self) -> None:
        assert apply_operator(">", 10, 5) is True
        assert apply_operator("<=", 4, 4) is True

    def test_numeric_strings_coerced(self) -> None:
        assert apply_operator("gt", "10", "5") is True
        assert apply_operator("eq", "42", 42) is True

    def test_between_inclusive(self) -> None:
        assert apply_operator("between", 5, [1, 10]) is True
        assert apply_operator("between", 10, [1, 10]) is True
        assert apply_operator("between", 11, [1, 10]) is False

    def test_between_malformed_range(self) -> None:
        with pytest.raises(OperatorError):
            apply_operator("between", 5, [1])

    def test_non_numeric_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_operator("gt", "hello", 5)


class TestEqualityAndBoolean:
    def test_equality(self) -> None:
        assert apply_operator("eq", "hello", "hello") is True
        assert apply_operator("ne", "a", "b") is True

    def test_boolean_identity(self) -> None:
        assert apply_operator("is_true", True) is True
        assert apply_operator("is_true", 1) is False  # strict: 1 is not True
        assert apply_operator("is_false", False) is True


class TestStringOperators:
    def test_contains(self) -> None:
        assert apply_operator("contains", "hello world", "world") is True
        assert apply_operator("not_contains", "hello", "z") is True

    def test_affixes(self) -> None:
        assert apply_operator("starts_with", "foobar", "foo") is True
        assert apply_operator("ends_with", "foobar", "bar") is True

    def test_case_insensitive_eq(self) -> None:
        assert apply_operator("eq_ci", "  India ", "india") is True

    def test_regex(self) -> None:
        assert apply_operator("regex", "user@gmail.com", r"@gmail\.com$") is True
        assert apply_operator("regex", "user@yahoo.com", r"@gmail\.com$") is False

    def test_invalid_regex_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_operator("regex", "abc", "(")


class TestMembershipAndEmptiness:
    def test_membership(self) -> None:
        assert apply_operator("in", "IN", ["IN", "US"]) is True
        assert apply_operator("not_in", "XX", ["IN", "US"]) is True

    def test_membership_requires_list(self) -> None:
        with pytest.raises(OperatorError):
            apply_operator("in", "IN", "not a list")

    def test_emptiness(self) -> None:
        assert apply_operator("is_empty", None) is True
        assert apply_operator("is_empty", "") is True
        assert apply_operator("is_empty", []) is True
        assert apply_operator("is_not_empty", "x") is True
        assert apply_operator("is_empty", 0) is False  # 0 is a value


class TestDateOperators:
    def test_ordering(self) -> None:
        assert apply_operator("before", "2023-01-01", "2024-01-01") is True
        assert apply_operator("after", "2025-06-01", "2024-01-01") is True
        assert apply_operator("on_or_before", "2024-01-01", "2024-01-01") is True

    def test_bad_date_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_operator("before", "not-a-date", "2024-01-01")


def test_unknown_operator_raises() -> None:
    with pytest.raises(OperatorError):
        apply_operator("does_not_exist", 1, 2)


# ----------------------------------------------------------------------------
# Rule-type evaluator contract: evaluate(rule, request) -> RuleResult
# ----------------------------------------------------------------------------


class TestConditionalRuleEvaluator:
    def _rule(self, logic: str = "AND") -> Rule:
        return Rule(
            id="r1",
            conditions=(
                Condition("score", "gte", 700),
                Condition("income", "gte", 50000),
            ),
            outcome="APPROVE",
            logic=logic,
            priority=5,
            weight=10.0,
        )

    def test_returns_rule_result(self) -> None:
        evaluator = get_evaluator("conditional")
        result = evaluator.evaluate(self._rule(), {"score": 720, "income": 60000})
        assert isinstance(result, RuleResult)
        assert result.rule_id == "r1"
        assert result.matched is True
        assert len(result.conditions) == 2

    def test_and_logic(self) -> None:
        evaluator = get_evaluator("conditional")
        assert (
            evaluator.evaluate(self._rule("AND"), {"score": 720, "income": 10}).matched
            is False
        )

    def test_or_logic(self) -> None:
        evaluator = get_evaluator("conditional")
        assert (
            evaluator.evaluate(self._rule("OR"), {"score": 720, "income": 10}).matched
            is True
        )

    def test_missing_field_is_safe_non_match(self) -> None:
        evaluator = get_evaluator("conditional")
        result = evaluator.evaluate(self._rule(), {"score": 720})
        assert result.matched is False
        assert any("absent" in c.note for c in result.conditions)

    def test_type_mismatch_is_safe_non_match(self) -> None:
        evaluator = get_evaluator("conditional")
        rule = Rule(
            id="bad",
            conditions=(Condition("name", "gt", 5),),
            outcome="X",
            priority=1,
        )
        result = evaluator.evaluate(rule, {"name": "Abhishek"})
        assert result.matched is False
        assert any("operator error" in c.note for c in result.conditions)


def test_unknown_rule_type_raises() -> None:
    with pytest.raises(UnknownRuleTypeError):
        get_evaluator("no_such_type")
