# ============================================================
# FILE   : tests/test_engine.py
# OWNER  : ROLE 1 — Rule Engine
# PURPOSE: Unit tests for the decision engine and the JSON loader.
# ============================================================
"""Tests for evaluate(), decision policies, determinism, and rule loading."""

import pytest

from rule_engine import (
    Condition,
    DecisionPolicy,
    Rule,
    evaluate,
    load_rules_from_string,
    rules_to_dicts,
)
from rule_engine.exceptions import RuleValidationError


def make_rule(
    rid: str,
    field: str,
    op: str,
    value: object,
    outcome: str,
    priority: int = 0,
    weight: float = 0.0,
    logic: str = "AND",
) -> Rule:
    return Rule(
        id=rid,
        conditions=(Condition(field=field, operator=op, value=value),),
        outcome=outcome,
        priority=priority,
        weight=weight,
        logic=logic,
    )


# ----------------------------------------------------------------------------
# Priority mode
# ----------------------------------------------------------------------------


class TestPriorityMode:
    def test_highest_priority_matched_rule_wins(self) -> None:
        rules = [
            make_rule(
                "approve", "score", "gte", 700, "APPROVE", priority=10, weight=50
            ),
            make_rule("reject", "age", "lt", 18, "REJECT", priority=100, weight=-100),
        ]
        decision = evaluate({"score": 750, "age": 16}, rules)
        assert decision.decision == "REJECT"
        assert set(decision.rules_matched) == {"approve", "reject"}

    def test_no_match_returns_default(self) -> None:
        rules = [make_rule("r", "score", "gte", 999, "APPROVE", priority=1)]
        decision = evaluate({"score": 100}, rules, DecisionPolicy(default="REVIEW"))
        assert decision.decision == "REVIEW"
        assert decision.rules_matched == []
        assert decision.confidence == 0.0

    def test_explanation_names_winning_rule(self) -> None:
        rules = [make_rule("winner", "x", "gt", 1, "APPROVE", priority=5, weight=10)]
        decision = evaluate({"x": 5}, rules)
        assert "winner" in decision.explanation

    def test_priority_tie_broken_by_input_order(self) -> None:
        rules = [
            make_rule("first", "x", "gt", 0, "APPROVE", priority=5, weight=1),
            make_rule("second", "x", "gt", 0, "REJECT", priority=5, weight=1),
        ]
        assert evaluate({"x": 1}, rules).decision == "APPROVE"


# ----------------------------------------------------------------------------
# Score mode
# ----------------------------------------------------------------------------


class TestScoreMode:
    POLICY = DecisionPolicy(
        mode="score",
        default="REJECT",
        thresholds=[(60, "APPROVE"), (20, "REVIEW"), (-1e9, "REJECT")],
    )

    def test_band_selection(self) -> None:
        rules = [
            make_rule("big", "x", "gt", 0, "APPROVE", weight=60),
            make_rule("small", "y", "gt", 0, "APPROVE", weight=15),
        ]
        assert evaluate({"x": 1, "y": 1}, rules, self.POLICY).decision == "APPROVE"
        assert evaluate({"y": 1}, rules, self.POLICY).decision == "REJECT"

    def test_confidence_bounded_and_nonzero_inside_band(self) -> None:
        rules = [make_rule("big", "x", "gt", 0, "APPROVE", weight=85)]
        decision = evaluate({"x": 1}, rules, self.POLICY)
        assert 0.0 < decision.confidence <= 1.0

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(RuleValidationError):
            DecisionPolicy(mode="majority")


# ----------------------------------------------------------------------------
# Determinism & robustness
# ----------------------------------------------------------------------------


class TestUnknownRuleTypeIsFailSafe:
    """Regression coverage for a live incident: an AI-authored rule saved
    with an unregistered `type` (e.g. "loan_eligibility" instead of
    "conditional") used to make get_evaluator() raise, which crashed
    evaluate() for EVERY request — not just ones touching that rule. A
    single bad rule anywhere in the rule set took down all decisioning.
    evaluate() must instead treat it as a safe non-match, same as a missing
    field or an operator/type mismatch, and keep evaluating every other
    rule normally."""

    def test_unregistered_type_does_not_raise(self) -> None:
        bad = Rule(
            id="bad_type_rule",
            conditions=(Condition(field="age", operator="gt", value=60),),
            outcome="REJECT",
            type="loan_eligibility",
            priority=999,
        )
        d = evaluate({"age": 70}, [bad])  # must not raise
        assert d.decision in {"APPROVE", "REJECT", "REVIEW"}
        assert "bad_type_rule" in d.rules_rejected
        assert "bad_type_rule" not in d.rules_matched

    def test_other_rules_still_evaluate_normally(self) -> None:
        bad = Rule(
            id="bad_type_rule",
            conditions=(Condition(field="age", operator="gt", value=60),),
            outcome="REJECT",
            type="loan_eligibility",
            priority=999,
        )
        good = make_rule("good", "score", "gte", 700, "APPROVE", priority=1, weight=10)
        d = evaluate({"age": 70, "score": 800}, [bad, good])
        # The bad-typed rule is skipped as a non-match; the good rule still
        # fires normally despite the bad rule having a higher priority.
        assert d.decision == "APPROVE"
        assert "good" in d.rules_matched
        assert "bad_type_rule" in d.rules_rejected

    def test_bad_rule_trace_explains_why(self) -> None:
        bad = Rule(
            id="bad_type_rule",
            conditions=(Condition(field="age", operator="gt", value=60),),
            outcome="REJECT",
            type="loan_eligibility",
        )
        d = evaluate({"age": 70}, [bad])
        trace = next(t for t in d.trace if t.rule_id == "bad_type_rule")
        assert trace.matched is False
        assert "loan_eligibility" in trace.conditions[0].note


class TestDeterminismAndRobustness:
    def test_same_input_same_output(self) -> None:
        rules = [
            make_rule("a", "x", "gt", 1, "APPROVE", priority=5, weight=10),
            make_rule("b", "x", "gt", 1, "REJECT", priority=5, weight=10),
        ]
        outcomes = {evaluate({"x": 5}, rules).decision for _ in range(50)}
        assert len(outcomes) == 1

    def test_missing_field_never_crashes(self) -> None:
        rules = [make_rule("r", "credit_score", "gt", 600, "APPROVE", priority=1)]
        decision = evaluate({"age": 30}, rules)
        assert decision.rules_matched == []

    def test_non_dict_request_rejected(self) -> None:
        with pytest.raises(RuleValidationError):
            evaluate(["not", "a", "dict"], [])  # type: ignore[arg-type]

    def test_disabled_rules_skipped(self) -> None:
        rule = Rule(
            id="off",
            conditions=(Condition("x", "gt", 0),),
            outcome="APPROVE",
            priority=1,
            enabled=False,
        )
        decision = evaluate({"x": 5}, [rule])
        assert "off" not in decision.rules_evaluated

    def test_dotted_field_path(self) -> None:
        rules = [make_rule("r", "company.size", "gte", 100, "APPROVE", priority=1)]
        assert evaluate({"company": {"size": 250}}, rules).decision == "APPROVE"

    def test_decision_serializes_to_dict(self) -> None:
        rules = [make_rule("r", "x", "gt", 0, "APPROVE", priority=1, weight=5)]
        payload = evaluate({"x": 1}, rules).to_dict()
        assert payload["decision"] == "APPROVE"
        assert "trace" in payload and payload["rules_matched"] == ["r"]


# ----------------------------------------------------------------------------
# Loader
# ----------------------------------------------------------------------------


class TestLoader:
    def test_parses_valid_rules(self) -> None:
        text = (
            '{"rules": [{"id": "r1", "outcome": "APPROVE", '
            '"conditions": [{"field": "x", "operator": "gt", "value": 1}]}]}'
        )
        rules = load_rules_from_string(text)
        assert len(rules) == 1 and rules[0].id == "r1"

    def test_round_trip_serialization(self) -> None:
        text = (
            '[{"id": "r1", "outcome": "APPROVE", "priority": 3, "weight": 2.5, '
            '"conditions": [{"field": "x", "operator": "gt", "value": 1}]}]'
        )
        rules = load_rules_from_string(text)
        dumped = rules_to_dicts(rules)
        assert dumped[0]["id"] == "r1" and dumped[0]["priority"] == 3

    @pytest.mark.parametrize(
        "text",
        [
            "{not json",
            '{"rules": "not a list"}',
            '[{"outcome": "A", "conditions": [{"field": "x", "operator": "gt"}]}]',
            '[{"id": "r", "outcome": "A", "conditions": []}]',
            (
                '[{"id": "r", "outcome": "A", "logic": "XOR", '
                '"conditions": [{"field": "x", "operator": "gt", "value": 1}]}]'
            ),
            (
                '[{"id": "dup", "outcome": "A", "conditions": '
                '[{"field": "x", "operator": "gt", "value": 1}]}, '
                '{"id": "dup", "outcome": "B", "conditions": '
                '[{"field": "y", "operator": "gt", "value": 1}]}]'
            ),
        ],
        ids=[
            "bad-json",
            "not-a-list",
            "missing-id",
            "empty-conditions",
            "bad-logic",
            "duplicate-ids",
        ],
    )
    def test_invalid_documents_rejected(self, text: str) -> None:
        with pytest.raises(RuleValidationError):
            load_rules_from_string(text)
