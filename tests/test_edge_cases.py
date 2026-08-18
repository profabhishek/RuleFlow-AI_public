# ============================================================
# FILE   : tests/test_edge_cases.py
# OWNER  : ROLE 1 — Rule Engine
# PURPOSE: Comprehensive edge-case coverage for models, operators,
#          the evaluator contract, the decision engine, and the loader.
#          Complements test_engine.py / test_evaluators.py with the
#          boundary, error, and degenerate cases.
# ============================================================
"""Exhaustive edge-case tests for the rule engine core.

These target the tricky corners: schema-validation failures, numeric/date
coercion, strict boolean identity, sentinel score thresholds, confidence
math (agreement ratios, zero-weight ties), dotted-path field resolution,
fail-safe evaluation, and loader robustness.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from rule_engine import (
    Condition,
    ConditionResult,
    Decision,
    DecisionPolicy,
    Rule,
    RuleResult,
    apply_operator,
    evaluate,
    get_evaluator,
    load_rules_from_string,
    operator,
    register_evaluator,
    rules_to_dicts,
)
from rule_engine.engine import _score_confidence
from rule_engine.evaluators import (
    OPERATORS,
    RULE_EVALUATORS,
    UNARY_OPERATORS,
    RuleEvaluator,
    _MISSING,
    _read_field,
)
from rule_engine.exceptions import (
    OperatorError,
    RuleValidationError,
    UnknownRuleTypeError,
)

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def cond(field: str, op: str, value: object = None) -> Condition:
    return Condition(field=field, operator=op, value=value)


def rule(
    rid: str,
    conditions: list[Condition],
    outcome: str,
    *,
    logic: str = "AND",
    priority: int = 0,
    weight: float = 0.0,
    enabled: bool = True,
    rtype: str = "conditional",
) -> Rule:
    return Rule(
        id=rid,
        conditions=tuple(conditions),
        outcome=outcome,
        logic=logic,
        priority=priority,
        weight=weight,
        enabled=enabled,
        type=rtype,
    )


# ============================================================================
# 1. Condition.from_dict — schema validation
# ============================================================================


class TestConditionFromDict:
    def test_non_dict_rejected(self) -> None:
        with pytest.raises(RuleValidationError):
            Condition.from_dict(["not", "a", "dict"], "r1", 0)

    @pytest.mark.parametrize("bad_field", [None, "", 123, {}])
    def test_bad_field_rejected(self, bad_field: object) -> None:
        with pytest.raises(RuleValidationError):
            Condition.from_dict(
                {"field": bad_field, "operator": "gt", "value": 1}, "r1", 0
            )

    @pytest.mark.parametrize("bad_op", [None, "", 5])
    def test_bad_operator_rejected(self, bad_op: object) -> None:
        with pytest.raises(RuleValidationError):
            Condition.from_dict({"field": "x", "operator": bad_op}, "r1", 0)

    def test_value_defaults_to_none(self) -> None:
        c = Condition.from_dict({"field": "x", "operator": "is_true"}, "r1", 0)
        assert c.value is None
        assert c.field == "x" and c.operator == "is_true"


# ============================================================================
# 2. Rule.from_dict — schema validation, coercion, defaults
# ============================================================================


class TestRuleFromDict:
    def _base(self, **over: object) -> dict:
        data = {
            "id": "r1",
            "outcome": "APPROVE",
            "conditions": [{"field": "x", "operator": "gt", "value": 1}],
        }
        data.update(over)
        return data

    def test_non_dict_rejected(self) -> None:
        with pytest.raises(RuleValidationError):
            Rule.from_dict("nope")

    @pytest.mark.parametrize("bad_id", [None, "", 42])
    def test_bad_id_rejected(self, bad_id: object) -> None:
        with pytest.raises(RuleValidationError):
            Rule.from_dict(self._base(id=bad_id))

    @pytest.mark.parametrize("bad_outcome", [None, "", 3.14])
    def test_bad_outcome_rejected(self, bad_outcome: object) -> None:
        with pytest.raises(RuleValidationError):
            Rule.from_dict(self._base(outcome=bad_outcome))

    @pytest.mark.parametrize(
        "raw,expected", [("and", "AND"), ("Or", "OR"), ("OR", "OR")]
    )
    def test_logic_normalized(self, raw: str, expected: str) -> None:
        assert Rule.from_dict(self._base(logic=raw)).logic == expected

    def test_invalid_logic_rejected(self) -> None:
        with pytest.raises(RuleValidationError):
            Rule.from_dict(self._base(logic="XOR"))

    @pytest.mark.parametrize("conds", [None, "x", [], {}])
    def test_conditions_must_be_nonempty_list(self, conds: object) -> None:
        with pytest.raises(RuleValidationError):
            Rule.from_dict(self._base(conditions=conds))

    @pytest.mark.parametrize("bad_enabled", [1, 0, "true", None])
    def test_enabled_must_be_bool(self, bad_enabled: object) -> None:
        with pytest.raises(RuleValidationError):
            Rule.from_dict(self._base(enabled=bad_enabled))

    def test_enabled_false_accepted(self) -> None:
        assert Rule.from_dict(self._base(enabled=False)).enabled is False

    @pytest.mark.parametrize("field_name", ["weight", "priority", "version"])
    @pytest.mark.parametrize("bad_value", ["ten", None, True, [1]])
    def test_numeric_fields_reject_non_numbers_and_bools(
        self, field_name: str, bad_value: object
    ) -> None:
        with pytest.raises(RuleValidationError):
            Rule.from_dict(self._base(**{field_name: bad_value}))

    def test_priority_and_version_coerced_to_int(self) -> None:
        r = Rule.from_dict(self._base(priority=3.9, version=2.7))
        assert r.priority == 3 and r.version == 2
        assert isinstance(r.priority, int) and isinstance(r.version, int)

    def test_weight_kept_as_float(self) -> None:
        r = Rule.from_dict(self._base(weight=2))
        assert r.weight == 2.0 and isinstance(r.weight, float)

    def test_defaults_applied(self) -> None:
        r = Rule.from_dict(self._base())
        assert r.description == ""
        assert r.type == "conditional"
        assert r.logic == "AND"
        assert r.weight == 0.0
        assert r.priority == 0
        assert r.version == 1
        assert r.enabled is True

    def test_non_string_type_coerced_to_str(self) -> None:
        assert Rule.from_dict(self._base(type=123)).type == "123"


# ============================================================================
# 3. Model serialization (to_dict)
# ============================================================================


class TestModelSerialization:
    def test_condition_result_to_dict(self) -> None:
        cr = ConditionResult("x", "gt", 1, 5, True, "note")
        assert cr.to_dict() == {
            "field": "x",
            "operator": "gt",
            "expected": 1,
            "actual": 5,
            "passed": True,
            "note": "note",
        }

    def test_rule_result_nests_conditions(self) -> None:
        rr = RuleResult(
            rule_id="r",
            outcome="A",
            matched=True,
            weight=1.0,
            priority=2,
            conditions=[ConditionResult("x", "gt", 1, 5, True)],
        )
        payload = rr.to_dict()
        assert payload["rule_id"] == "r"
        assert payload["conditions"][0]["passed"] is True

    def test_decision_rounds_confidence_to_four_places(self) -> None:
        d = Decision(
            decision="A",
            confidence=0.6666666666,
            score=1.0,
            rules_evaluated=["r"],
            rules_matched=["r"],
            rules_rejected=[],
            explanation="",
        )
        assert d.to_dict()["confidence"] == 0.6667


# ============================================================================
# 4. Numeric operators & coercion
# ============================================================================


class TestNumericOperatorEdges:
    def test_boundaries(self) -> None:
        assert apply_operator("gte", 5, 5) is True
        assert apply_operator("lte", 5, 5) is True
        assert apply_operator("gt", 5, 5) is False
        assert apply_operator("lt", 5, 5) is False

    def test_negative_and_float(self) -> None:
        assert apply_operator("lt", -10, -5) is True
        assert apply_operator("gt", 1.5, 1.4999) is True

    def test_numeric_string_coercion(self) -> None:
        assert apply_operator("gte", "700", "700") is True
        assert apply_operator("between", "5", ["1", "10"]) is True

    def test_bool_is_not_a_number(self) -> None:
        with pytest.raises(OperatorError):
            apply_operator("gt", True, 1)

    @pytest.mark.parametrize("bad", ["not a list", [1], [1, 2, 3], 5])
    def test_between_malformed_range(self, bad: object) -> None:
        with pytest.raises(OperatorError):
            apply_operator("between", 5, bad)

    def test_between_inclusive_edges(self) -> None:
        assert apply_operator("between", 1, [1, 10]) is True
        assert apply_operator("between", 10, [1, 10]) is True
        assert apply_operator("between", 0, [1, 10]) is False

    def test_non_numeric_string_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_operator("gt", "abc", 5)


# ============================================================================
# 5. Equality / inequality (dual numeric + fallback path)
# ============================================================================


class TestEqualityEdges:
    def test_cross_type_numeric_equality(self) -> None:
        assert apply_operator("eq", "42", 42) is True
        assert apply_operator("eq", 42.0, 42) is True

    def test_fallback_string_equality(self) -> None:
        assert apply_operator("eq", "hello", "hello") is True
        assert apply_operator("eq", "hello", "world") is False

    def test_fallback_list_equality(self) -> None:
        assert apply_operator("eq", [1, 2], [1, 2]) is True

    def test_bool_equality_via_fallback(self) -> None:
        assert apply_operator("eq", True, True) is True

    def test_ne_is_negation(self) -> None:
        assert apply_operator("ne", "a", "b") is True
        assert apply_operator("ne", 5, 5) is False


# ============================================================================
# 6. Boolean unary operators (strict identity)
# ============================================================================


class TestBooleanUnaryEdges:
    def test_is_true_strict(self) -> None:
        assert apply_operator("is_true", True) is True
        assert apply_operator("is_true", 1) is False
        assert apply_operator("is_true", "true") is False

    def test_is_false_strict(self) -> None:
        assert apply_operator("is_false", False) is True
        assert apply_operator("is_false", 0) is False


# ============================================================================
# 7. String operators
# ============================================================================


class TestStringOperatorEdges:
    def test_contains_coerces_to_string(self) -> None:
        assert apply_operator("contains", 12345, "234") is True
        assert apply_operator("not_contains", "hello", "z") is True

    def test_affixes(self) -> None:
        assert apply_operator("starts_with", "foobar", "foo") is True
        assert apply_operator("ends_with", "foobar", "bar") is True
        assert apply_operator("starts_with", "foobar", "bar") is False

    def test_eq_ci_strips_and_lowercases(self) -> None:
        assert apply_operator("eq_ci", "  India ", "india") is True
        assert apply_operator("eq_ci", "US", "us") is True

    def test_regex_is_search_not_fullmatch(self) -> None:
        assert apply_operator("regex", "abc123def", r"\d+") is True
        assert apply_operator("matches", "user@gmail.com", r"@gmail\.com$") is True
        assert apply_operator("regex", "nope", r"\d+") is False

    def test_invalid_regex_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_operator("regex", "abc", "(")


# ============================================================================
# 8. Membership operators
# ============================================================================


class TestMembershipEdges:
    def test_in_accepts_list_tuple_set(self) -> None:
        assert apply_operator("in", "US", ["US", "IN"]) is True
        assert apply_operator("in", "US", ("US", "IN")) is True
        assert apply_operator("in", "US", {"US", "IN"}) is True

    def test_in_absent(self) -> None:
        assert apply_operator("in", "XX", ["US", "IN"]) is False

    @pytest.mark.parametrize("op", ["in", "not_in"])
    def test_membership_requires_collection(self, op: str) -> None:
        with pytest.raises(OperatorError):
            apply_operator(op, "US", "not a list")

    def test_not_in(self) -> None:
        assert apply_operator("not_in", "XX", ["US", "IN"]) is True
        assert apply_operator("not_in", "US", ["US", "IN"]) is False


# ============================================================================
# 9. Emptiness operators
# ============================================================================


class TestEmptinessEdges:
    @pytest.mark.parametrize("empty", [None, "", [], {}, (), set()])
    def test_empty_values(self, empty: object) -> None:
        assert apply_operator("is_empty", empty) is True

    @pytest.mark.parametrize("non_empty", [0, False, "x", [1], {"a": 1}])
    def test_non_empty_values(self, non_empty: object) -> None:
        assert apply_operator("is_empty", non_empty) is False

    def test_is_not_empty_inverse(self) -> None:
        assert apply_operator("is_not_empty", "x") is True
        assert apply_operator("is_not_empty", "") is False


# ============================================================================
# 10. Date operators & coercion
# ============================================================================


class TestDateOperatorEdges:
    def test_ordering_and_boundaries(self) -> None:
        assert apply_operator("before", "2023-01-01", "2024-01-01") is True
        assert apply_operator("after", "2025-06-01", "2024-01-01") is True
        assert apply_operator("on_or_before", "2024-01-01", "2024-01-01") is True
        assert apply_operator("on_or_after", "2024-01-01", "2024-01-01") is True

    def test_aliases(self) -> None:
        assert apply_operator("date_lt", "2023-01-01", "2024-01-01") is True
        assert apply_operator("date_gt", "2025-01-01", "2024-01-01") is True
        assert apply_operator("date_lte", "2024-01-01", "2024-01-01") is True
        assert apply_operator("date_gte", "2024-01-01", "2024-01-01") is True

    def test_datetime_coerced_to_date(self) -> None:
        assert (
            apply_operator("before", datetime(2023, 1, 1, 23, 59), "2024-01-01") is True
        )

    def test_date_object_accepted(self) -> None:
        assert apply_operator("after", date(2025, 1, 1), date(2024, 1, 1)) is True

    def test_bad_date_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_operator("before", "not-a-date", "2024-01-01")


# ============================================================================
# 11. Operator registry mechanics
# ============================================================================


class TestOperatorRegistry:
    def test_unknown_operator_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_operator("does_not_exist", 1, 2)

    def test_aliases_share_one_function(self) -> None:
        assert OPERATORS["gt"] is OPERATORS[">"]
        assert OPERATORS["eq"] is OPERATORS["=="] is OPERATORS["equals"]

    def test_unary_operators_registered(self) -> None:
        for name in ("is_true", "is_false", "is_empty", "is_not_empty"):
            assert name in UNARY_OPERATORS

    def test_custom_operator_registration(self) -> None:
        name = "_edgecase_divisible_by"
        try:

            @operator(name)
            def _divisible(actual: object, expected: object) -> bool:
                return int(actual) % int(expected) == 0  # type: ignore[arg-type]

            assert apply_operator(name, 10, 5) is True
            assert apply_operator(name, 10, 3) is False
        finally:
            OPERATORS.pop(name, None)


# ============================================================================
# 12. Field resolution (_read_field)
# ============================================================================


class TestReadField:
    def test_direct_key(self) -> None:
        assert _read_field({"x": 5}, "x") == 5

    def test_nested_dotted_path(self) -> None:
        assert _read_field({"company": {"size": 250}}, "company.size") == 250

    def test_literal_dotted_key_takes_precedence(self) -> None:
        assert _read_field({"a.b": 1, "a": {"b": 2}}, "a.b") == 1

    def test_missing_key_returns_sentinel(self) -> None:
        assert _read_field({"x": 1}, "y") is _MISSING

    def test_broken_nested_path_returns_sentinel(self) -> None:
        assert _read_field({"a": 5}, "a.b") is _MISSING
        assert _read_field({"a": {"b": 1}}, "a.b.c") is _MISSING


# ============================================================================
# 13. Conditional evaluator — fail-safe behaviour
# ============================================================================


class TestConditionalEvaluatorEdges:
    def _ev(self):
        return get_evaluator("conditional")

    def test_and_requires_all(self) -> None:
        r = rule("r", [cond("a", "gt", 0), cond("b", "gt", 0)], "A", logic="AND")
        assert self._ev().evaluate(r, {"a": 1, "b": 0}).matched is False

    def test_or_requires_any(self) -> None:
        r = rule("r", [cond("a", "gt", 0), cond("b", "gt", 0)], "A", logic="OR")
        assert self._ev().evaluate(r, {"a": 1, "b": 0}).matched is True

    def test_missing_field_is_non_match_with_note(self) -> None:
        r = rule("r", [cond("credit", "gt", 600)], "A")
        result = self._ev().evaluate(r, {"age": 30})
        assert result.matched is False
        assert result.conditions[0].note == "field absent from request"
        assert result.conditions[0].actual is None

    def test_missing_field_with_is_empty_passes(self) -> None:
        r = rule("r", [cond("notes", "is_empty")], "A")
        result = self._ev().evaluate(r, {"age": 30})
        assert result.matched is True
        assert "treated as empty" in result.conditions[0].note

    def test_operator_error_is_non_match_with_note(self) -> None:
        r = rule("r", [cond("name", "gt", 5)], "A")
        result = self._ev().evaluate(r, {"name": "Alice"})
        assert result.matched is False
        assert "operator error" in result.conditions[0].note

    def test_unknown_operator_is_non_match_with_note(self) -> None:
        r = rule("r", [cond("x", "no_such_operator", 1)], "A")
        result = self._ev().evaluate(r, {"x": 5})
        assert result.matched is False
        assert "operator error" in result.conditions[0].note

    def test_condition_order_preserved(self) -> None:
        r = rule("r", [cond("a", "gt", 0), cond("b", "gt", 0), cond("c", "gt", 0)], "A")
        result = self._ev().evaluate(r, {"a": 1, "b": 1, "c": 1})
        assert [c.field for c in result.conditions] == ["a", "b", "c"]


# ============================================================================
# 14. Evaluator registry
# ============================================================================


class TestEvaluatorRegistry:
    def test_unknown_rule_type_raises(self) -> None:
        with pytest.raises(UnknownRuleTypeError):
            get_evaluator("ghost_type")

    def test_register_and_lookup_custom_evaluator(self) -> None:
        rtype = "_edgecase_always_match"
        try:

            @register_evaluator
            class _AlwaysMatch(RuleEvaluator):
                rule_type = rtype

                def evaluate(self, rule_obj, request):  # type: ignore[override]
                    return RuleResult(
                        rule_id=rule_obj.id,
                        outcome=rule_obj.outcome,
                        matched=True,
                        weight=rule_obj.weight,
                        priority=rule_obj.priority,
                    )

            assert isinstance(get_evaluator(rtype), _AlwaysMatch)
        finally:
            RULE_EVALUATORS.pop(rtype, None)


# ============================================================================
# 15. DecisionPolicy validation
# ============================================================================


class TestDecisionPolicy:
    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(RuleValidationError):
            DecisionPolicy(mode="majority")

    def test_priority_defaults(self) -> None:
        p = DecisionPolicy()
        assert p.mode == "priority" and p.default == "REVIEW"

    def test_score_mode_fills_default_threshold(self) -> None:
        p = DecisionPolicy(mode="score", default="REJECT")
        assert p.thresholds == [(0.0, "REJECT")]

    def test_score_mode_keeps_explicit_thresholds(self) -> None:
        bands = [(60, "APPROVE"), (20, "REVIEW")]
        assert DecisionPolicy(mode="score", thresholds=bands).thresholds == bands


# ============================================================================
# 16. Engine — priority mode
# ============================================================================


class TestEvaluatePriorityMode:
    def test_request_must_be_dict(self) -> None:
        with pytest.raises(RuleValidationError):
            evaluate(["not", "a", "dict"], [])  # type: ignore[arg-type]

    def test_no_rules_returns_default(self) -> None:
        d = evaluate({"x": 1}, [], DecisionPolicy(default="REVIEW"))
        assert d.decision == "REVIEW"
        assert d.confidence == 0.0
        assert d.rules_evaluated == [] and d.rules_matched == []

    def test_none_policy_defaults_to_priority(self) -> None:
        rules = [rule("r", [cond("x", "gt", 0)], "APPROVE", priority=1, weight=5)]
        assert evaluate({"x": 5}, rules).decision == "APPROVE"

    def test_highest_priority_wins(self) -> None:
        rules = [
            rule("approve", [cond("x", "gt", 0)], "APPROVE", priority=10, weight=50),
            rule("reject", [cond("x", "gt", 0)], "REJECT", priority=100, weight=-100),
        ]
        assert evaluate({"x": 1}, rules).decision == "REJECT"

    def test_tie_broken_by_input_order(self) -> None:
        rules = [
            rule("first", [cond("x", "gt", 0)], "APPROVE", priority=5),
            rule("second", [cond("x", "gt", 0)], "REJECT", priority=5),
        ]
        assert evaluate({"x": 1}, rules).decision == "APPROVE"

    def test_confidence_agreement_ratio(self) -> None:
        rules = [
            rule("a", [cond("x", "gt", 0)], "APPROVE", priority=5, weight=30),
            rule("b", [cond("x", "gt", 0)], "APPROVE", priority=3, weight=10),
            rule("c", [cond("x", "gt", 0)], "REJECT", priority=1, weight=20),
        ]
        d = evaluate({"x": 1}, rules)
        assert d.decision == "APPROVE"
        assert d.confidence == pytest.approx(40 / 60)

    def test_confidence_uses_absolute_weights(self) -> None:
        rules = [
            rule("r1", [cond("x", "gt", 0)], "REJECT", priority=10, weight=-100),
            rule("r2", [cond("x", "gt", 0)], "REJECT", priority=5, weight=-60),
            rule("r3", [cond("x", "gt", 0)], "APPROVE", priority=1, weight=40),
        ]
        d = evaluate({"x": 1}, rules)
        assert d.decision == "REJECT"
        assert d.confidence == pytest.approx(160 / 200)

    def test_confidence_is_one_when_all_weights_zero(self) -> None:
        rules = [
            rule("a", [cond("x", "gt", 0)], "APPROVE", priority=5, weight=0),
            rule("b", [cond("x", "gt", 0)], "REJECT", priority=3, weight=0),
        ]
        assert evaluate({"x": 1}, rules).confidence == 1.0

    def test_disabled_rule_not_evaluated(self) -> None:
        rules = [
            rule("on", [cond("x", "gt", 0)], "APPROVE", priority=1),
            rule("off", [cond("x", "gt", 0)], "REJECT", priority=9, enabled=False),
        ]
        d = evaluate({"x": 1}, rules)
        assert "off" not in d.rules_evaluated
        assert d.decision == "APPROVE"

    def test_evaluated_matched_rejected_partition(self) -> None:
        rules = [
            rule("hit", [cond("x", "gt", 0)], "APPROVE", priority=1),
            rule("miss", [cond("x", "lt", 0)], "REJECT", priority=1),
        ]
        d = evaluate({"x": 5}, rules)
        assert set(d.rules_evaluated) == {"hit", "miss"}
        assert d.rules_matched == ["hit"]
        assert d.rules_rejected == ["miss"]

    def test_score_field_sums_matched_weights_even_in_priority_mode(self) -> None:
        rules = [
            rule("a", [cond("x", "gt", 0)], "APPROVE", priority=1, weight=30),
            rule("b", [cond("x", "gt", 0)], "APPROVE", priority=2, weight=12),
        ]
        assert evaluate({"x": 1}, rules).score == 42

    def test_unknown_rule_type_degrades_to_safe_non_match(self) -> None:
        # Was: raises UnknownRuleTypeError, crashing evaluate() for every
        # rule in the batch over one bad rule's type. Changed after a live
        # incident where an AI-authored rule with an invented type
        # ("loan_eligibility") broke /decide for every request, not just
        # ones touching that rule. Unknown type now degrades the same way
        # a missing field or operator/type mismatch already does: a safe,
        # explained non-match — never a crash. See rule_engine/engine.py's
        # _evaluate_one() and TestUnknownRuleTypeIsFailSafe in
        # test_engine.py for the full regression coverage.
        bad = rule("ghost", [cond("x", "gt", 0)], "A", rtype="does_not_exist")
        d = evaluate({"x": 1}, [bad])
        assert "ghost" in d.rules_rejected
        assert "does_not_exist" in d.trace[0].conditions[0].note


# ============================================================================
# 17. Engine — score mode
# ============================================================================


SCORE_POLICY = DecisionPolicy(
    mode="score",
    default="REJECT",
    thresholds=[(60, "APPROVE"), (20, "REVIEW"), (-1e9, "REJECT")],
)


class TestEvaluateScoreMode:
    def test_band_selection(self) -> None:
        rules = [
            rule("big", [cond("x", "gt", 0)], "APPROVE", weight=60),
            rule("small", [cond("y", "gt", 0)], "APPROVE", weight=15),
        ]
        assert evaluate({"x": 1, "y": 1}, rules, SCORE_POLICY).decision == "APPROVE"
        assert evaluate({"y": 1}, rules, SCORE_POLICY).decision == "REJECT"

    def test_threshold_boundary_is_inclusive(self) -> None:
        rules = [rule("edge", [cond("x", "gt", 0)], "APPROVE", weight=60)]
        assert evaluate({"x": 1}, rules, SCORE_POLICY).decision == "APPROVE"

    def test_review_band(self) -> None:
        rules = [rule("mid", [cond("x", "gt", 0)], "REVIEW", weight=30)]
        assert evaluate({"x": 1}, rules, SCORE_POLICY).decision == "REVIEW"

    def test_no_match_uses_score_default(self) -> None:
        rules = [rule("r", [cond("x", "gt", 999)], "APPROVE", weight=100)]
        assert evaluate({"x": 1}, rules, SCORE_POLICY).decision == "REJECT"

    def test_confidence_bounded(self) -> None:
        rules = [rule("r", [cond("x", "gt", 0)], "APPROVE", weight=85)]
        d = evaluate({"x": 1}, rules, SCORE_POLICY)
        assert 0.0 <= d.confidence <= 1.0


# ============================================================================
# 18. _score_confidence — sentinel handling & boundary math
# ============================================================================


class TestScoreConfidence:
    THRESHOLDS = [(60, "APPROVE"), (20, "REVIEW"), (-1e9, "REJECT")]

    def test_sentinel_bounds_excluded(self) -> None:
        # Real boundaries are [20, 60]; span 40. score 85 -> min(65,25)=25.
        assert _score_confidence(85, self.THRESHOLDS) == pytest.approx(25 / 40)

    def test_on_boundary_is_zero(self) -> None:
        assert _score_confidence(60, self.THRESHOLDS) == 0.0

    def test_all_sentinel_bounds_returns_one(self) -> None:
        assert _score_confidence(5, [(1e9, "A"), (-1e9, "R")]) == 1.0

    def test_no_thresholds_returns_one(self) -> None:
        assert _score_confidence(5, []) == 1.0

    def test_single_boundary_span_fallback(self) -> None:
        # Single real boundary 50 -> span falls back to max(|50|, 1) = 50.
        assert _score_confidence(60, [(50, "A"), (-1e9, "R")]) == pytest.approx(10 / 50)

    def test_far_outside_clamped_to_one(self) -> None:
        assert _score_confidence(10_000, self.THRESHOLDS) == 1.0


# ============================================================================
# 19. Determinism
# ============================================================================


class TestDeterminism:
    def test_repeated_priority_eval_identical(self) -> None:
        rules = [
            rule("a", [cond("x", "gt", 1)], "APPROVE", priority=5, weight=10),
            rule("b", [cond("x", "gt", 1)], "REJECT", priority=5, weight=10),
        ]
        results = {evaluate({"x": 5}, rules).decision for _ in range(50)}
        assert results == {"APPROVE"}

    def test_repeated_score_eval_identical(self) -> None:
        rules = [rule("a", [cond("x", "gt", 0)], "APPROVE", weight=61)]
        confs = {evaluate({"x": 1}, rules, SCORE_POLICY).confidence for _ in range(50)}
        assert len(confs) == 1


# ============================================================================
# 20. Loader — robustness & round-trips
# ============================================================================


class TestLoaderEdges:
    def test_top_level_list_accepted(self) -> None:
        text = (
            '[{"id": "r1", "outcome": "A", '
            '"conditions": [{"field": "x", "operator": "gt", "value": 1}]}]'
        )
        assert len(load_rules_from_string(text)) == 1

    def test_wrapped_object_accepted(self) -> None:
        text = (
            '{"rules": [{"id": "r1", "outcome": "A", '
            '"conditions": [{"field": "x", "operator": "gt", "value": 1}]}]}'
        )
        assert load_rules_from_string(text)[0].id == "r1"

    def test_empty_list_is_valid(self) -> None:
        assert load_rules_from_string("[]") == []

    @pytest.mark.parametrize(
        "text",
        [
            "{not valid json",
            '{"rules": "not a list"}',
            "42",
            '"a string"',
        ],
    )
    def test_malformed_documents_rejected(self, text: str) -> None:
        with pytest.raises(RuleValidationError):
            load_rules_from_string(text)

    def test_duplicate_ids_rejected(self) -> None:
        text = (
            '[{"id": "dup", "outcome": "A", '
            '"conditions": [{"field": "x", "operator": "gt", "value": 1}]},'
            '{"id": "dup", "outcome": "B", '
            '"conditions": [{"field": "y", "operator": "gt", "value": 1}]}]'
        )
        with pytest.raises(RuleValidationError):
            load_rules_from_string(text)

    def test_round_trip_preserves_fields(self) -> None:
        text = (
            '[{"id": "r1", "outcome": "APPROVE", "priority": 3, "weight": 2.5, '
            '"logic": "OR", "description": "d", '
            '"conditions": [{"field": "x", "operator": "gt", "value": 1},'
            '{"field": "y", "operator": "lt", "value": 9}]}]'
        )
        rules = load_rules_from_string(text)
        dumped = rules_to_dicts(rules)[0]
        assert dumped["id"] == "r1"
        assert dumped["priority"] == 3
        assert dumped["weight"] == 2.5
        assert dumped["logic"] == "OR"
        assert len(dumped["conditions"]) == 2

    def test_load_from_file(self, tmp_path) -> None:
        from rule_engine import load_rules

        p = tmp_path / "rules.json"
        p.write_text(
            '[{"id": "r1", "outcome": "A", '
            '"conditions": [{"field": "x", "operator": "gt", "value": 1}]}]',
            encoding="utf-8",
        )
        rules = load_rules(str(p))
        assert len(rules) == 1 and rules[0].id == "r1"
