# ============================================================
# FILE   : rule_engine/engine.py
# OWNER  : ROLE 1 — Rule Engine
# PURPOSE: evaluate() pure function + priority/score decision policies.
# ============================================================
"""The decision engine.

``evaluate(request, rules, policy)`` is a **pure function**: identical inputs
always produce an identical ``Decision``. No randomness, no wall clock in the
decision path, no global mutable state. Ties resolve by a documented, stable
order (priority descending, then input order — Python's sort is stable).

Policies:
    * ``priority`` (default) — the matched rule with the highest priority
      sets the outcome. Intuitive for approve/reject/review flows.
    * ``score`` — weights of matched rules are summed and mapped to an
      outcome band via thresholds. Suits many-weak-signals scoring.

The engine imports neither FastAPI nor any database — per the architecture
rule, it depends only on models and the evaluator registries.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .evaluators import get_evaluator
from .exceptions import RuleValidationError, UnknownRuleTypeError
from .models import ConditionResult, Decision, Rule, RuleResult

logger = logging.getLogger("rule_engine.engine")

#: Threshold magnitudes at/above this are treated as ±infinity sentinels
#: (catch-all bands) and excluded from confidence normalization.
_SENTINEL = 1e6


@dataclass(slots=True)
class DecisionPolicy:
    """How matched rules combine into one decision.

    Attributes:
        mode:       "priority" or "score".
        default:    Outcome when no rule matches.
        thresholds: SCORE mode only — (min_score, outcome) bands, checked
                    high-to-low, e.g. [(80, "APPROVE"), (50, "REVIEW"),
                    (-1e9, "REJECT")].
    """

    mode: str = "priority"
    default: str = "REVIEW"
    thresholds: list[tuple[float, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in {"priority", "score"}:
            raise RuleValidationError(
                f"DecisionPolicy.mode must be 'priority' or 'score', got '{self.mode}'."
            )
        if self.mode == "score" and not self.thresholds:
            self.thresholds = [(0.0, self.default)]


def evaluate(
    request: dict[str, Any],
    rules: list[Rule],
    policy: DecisionPolicy | None = None,
) -> Decision:
    """Evaluate a request against all enabled rules and return a Decision.

    Raises:
        RuleValidationError: if the request is not a dict.
    """
    if policy is None:
        policy = DecisionPolicy()
    if not isinstance(request, dict):
        raise RuleValidationError(
            f"Request must be an object (dict), got {type(request).__name__}."
        )

    started = time.perf_counter()

    results: list[RuleResult] = [
        _evaluate_one(rule, request) for rule in rules if rule.enabled
    ]
    matched = [r for r in results if r.matched]
    rejected = [r for r in results if not r.matched]

    if policy.mode == "priority":
        outcome, confidence, explanation = _decide_priority(matched, policy)
    else:
        outcome, confidence, explanation = _decide_score(matched, policy)

    score = sum(r.weight for r in matched)
    elapsed_ms = (time.perf_counter() - started) * 1000

    logger.info(
        "decision=%s confidence=%.3f score=%g matched=%d/%d elapsed_ms=%.2f",
        outcome,
        confidence,
        score,
        len(matched),
        len(results),
        elapsed_ms,
    )

    return Decision(
        decision=outcome,
        confidence=confidence,
        score=score,
        rules_evaluated=[r.rule_id for r in results],
        rules_matched=[r.rule_id for r in matched],
        rules_rejected=[r.rule_id for r in rejected],
        explanation=explanation,
        trace=results,
    )


def _evaluate_one(rule: Rule, request: dict[str, Any]) -> RuleResult:
    """Evaluate one rule, degrading to a safe non-match if its ``type`` isn't
    a registered evaluator, instead of raising.

    Rule data can come from anywhere — a hand-edited JSON file, a manual API
    call, or an AI-authored rule that invented its own rule type. The engine
    already treats missing fields and operator/type mismatches as safe
    non-matches rather than crashing (see evaluators.py); an unregistered
    rule *type* deserves the same fail-safe treatment, not a 500/400 that
    takes every other rule's decision down with it. The bad rule shows up in
    the trace as a rejected rule with an explanatory note, so it's still
    visible and fixable — it just can't block anyone else's decision.
    """
    try:
        return get_evaluator(rule.type).evaluate(rule, request)
    except UnknownRuleTypeError as exc:
        logger.warning(
            "rule '%s' has unregistered type '%s'; skipping as a safe "
            "non-match instead of failing the whole decision: %s",
            rule.id,
            rule.type,
            exc,
        )
        return RuleResult(
            rule_id=rule.id,
            outcome=rule.outcome,
            matched=False,
            weight=rule.weight,
            priority=rule.priority,
            conditions=[
                ConditionResult(
                    field="<rule type>",
                    operator="type",
                    expected="a registered evaluator",
                    actual=rule.type,
                    passed=False,
                    note=str(exc),
                )
            ],
        )


# ----------------------------------------------------------------------------
# Combination strategies
# ----------------------------------------------------------------------------


def _decide_priority(
    matched: list[RuleResult], policy: DecisionPolicy
) -> tuple[str, float, str]:
    if not matched:
        return policy.default, 0.0, "No rule matched; returning the policy default."

    ranked = sorted(matched, key=lambda r: r.priority, reverse=True)
    winner = ranked[0]

    agree = sum(abs(r.weight) for r in matched if r.outcome == winner.outcome)
    total = sum(abs(r.weight) for r in matched)
    confidence = 1.0 if total == 0 else agree / total

    explanation = (
        f"Decision '{winner.outcome}' set by rule '{winner.rule_id}' "
        f"(priority {winner.priority}), the highest-priority matching rule."
        f"{_winning_reasons(winner)}"
    )
    return winner.outcome, confidence, explanation


def _decide_score(
    matched: list[RuleResult], policy: DecisionPolicy
) -> tuple[str, float, str]:
    score = sum(r.weight for r in matched)

    outcome = policy.default
    for min_score, band_outcome in sorted(policy.thresholds, reverse=True):
        if score >= min_score:
            outcome = band_outcome
            break

    confidence = _score_confidence(score, policy.thresholds)
    matched_ids = ", ".join(r.rule_id for r in matched) or "none"
    explanation = (
        f"Total score {score:g} from matched rules [{matched_ids}] "
        f"falls in the '{outcome}' band."
    )
    return outcome, confidence, explanation


def _score_confidence(score: float, thresholds: list[tuple[float, str]]) -> float:
    """Distance from the nearest *real* band edge, normalized to [0, 1].

    Sentinel catch-all bounds (|bound| >= _SENTINEL) are ignored so an
    unbounded floor band cannot drag confidence toward zero.
    """
    boundaries = sorted(b for b, _ in thresholds if abs(b) < _SENTINEL)
    if not boundaries:
        return 1.0
    nearest = min(abs(score - b) for b in boundaries)
    span = (max(boundaries) - min(boundaries)) or max(abs(min(boundaries)), 1.0)
    return max(0.0, min(1.0, nearest / span))


def _winning_reasons(winner: RuleResult) -> str:
    reasons = [
        f"{c.field} {c.operator} {c.expected!r} (actual {c.actual!r})"
        for c in winner.conditions
        if c.passed
    ]
    return (" Matched because: " + "; ".join(reasons) + ".") if reasons else ""
