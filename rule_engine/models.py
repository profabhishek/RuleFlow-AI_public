# ============================================================
# FILE   : rule_engine/models.py
# OWNER  : ROLE 1 — Rule Engine
# PURPOSE: Rule / Condition / RuleResult / Decision models + validation.
# ============================================================
"""Data models for the rule engine.

Plain dataclasses, zero third-party dependencies. Pydantic belongs to the API
boundary (Role 2); the engine core stays framework-free per the architecture
rule "Engine must not depend on FastAPI or the database".

Validation happens in ``Rule.from_dict`` so malformed rules fail loudly at
load time with a precise message, never silently at decision time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .exceptions import RuleValidationError

VALID_LOGIC: frozenset[str] = frozenset({"AND", "OR"})

#: Default rule type; evaluators are registered per type (see evaluators.py).
DEFAULT_RULE_TYPE = "conditional"

#: Default rule category (a.k.a. ruleset / gate). Rules are grouped by this so
#: the application layer can evaluate one category at a time (e.g. finance
#: gates: kyc, underwriting, fraud). The engine itself does not branch on it —
#: it is metadata for grouping/filtering above the engine. Rules without an
#: explicit category fall here, keeping older rules and seeds backward-compatible.
DEFAULT_CATEGORY = "general"


@dataclass(frozen=True, slots=True)
class Condition:
    """A single atomic check, e.g. ``credit_score < 600``."""

    field: str
    operator: str
    value: Any = None

    @staticmethod
    def from_dict(data: Any, rule_id: str, index: int) -> Condition:
        if not isinstance(data, dict):
            raise RuleValidationError(
                f"Rule '{rule_id}': condition #{index} must be an object, "
                f"got {type(data).__name__}."
            )
        field_name = data.get("field")
        operator = data.get("operator")
        if not isinstance(field_name, str) or not field_name:
            raise RuleValidationError(
                f"Rule '{rule_id}': condition #{index} needs a non-empty 'field'."
            )
        if not isinstance(operator, str) or not operator:
            raise RuleValidationError(
                f"Rule '{rule_id}': condition #{index} needs a non-empty 'operator'."
            )
        return Condition(field=field_name, operator=operator, value=data.get("value"))


@dataclass(frozen=True, slots=True)
class Rule:
    """A configurable business rule (data, not code)."""

    id: str
    conditions: tuple[Condition, ...]
    outcome: str
    description: str = ""
    type: str = DEFAULT_RULE_TYPE
    logic: str = "AND"
    weight: float = 0.0
    priority: int = 0
    version: int = 1
    enabled: bool = True
    category: str = DEFAULT_CATEGORY

    @staticmethod
    def from_dict(data: Any) -> Rule:
        if not isinstance(data, dict):
            raise RuleValidationError(
                f"Each rule must be an object, got {type(data).__name__}."
            )
        rule_id = data.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise RuleValidationError("Every rule must have a non-empty string 'id'.")

        outcome = data.get("outcome")
        if not isinstance(outcome, str) or not outcome:
            raise RuleValidationError(
                f"Rule '{rule_id}': 'outcome' must be a non-empty string."
            )

        logic = str(data.get("logic", "AND")).upper()
        if logic not in VALID_LOGIC:
            raise RuleValidationError(
                f"Rule '{rule_id}': 'logic' must be one of {sorted(VALID_LOGIC)}, "
                f"got '{logic}'."
            )

        raw_conditions = data.get("conditions")
        if not isinstance(raw_conditions, list) or not raw_conditions:
            raise RuleValidationError(
                f"Rule '{rule_id}': 'conditions' must be a non-empty list."
            )
        conditions = tuple(
            Condition.from_dict(c, rule_id, i) for i, c in enumerate(raw_conditions)
        )

        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RuleValidationError(
                f"Rule '{rule_id}': 'enabled' must be a boolean if present."
            )

        category = data.get("category", DEFAULT_CATEGORY)
        if not isinstance(category, str) or not category:
            raise RuleValidationError(
                f"Rule '{rule_id}': 'category' must be a non-empty string if present."
            )

        return Rule(
            id=rule_id,
            description=str(data.get("description", "")),
            type=str(data.get("type", DEFAULT_RULE_TYPE)),
            conditions=conditions,
            logic=logic,
            outcome=outcome,
            weight=_require_number(data.get("weight", 0.0), rule_id, "weight"),
            priority=int(_require_number(data.get("priority", 0), rule_id, "priority")),
            version=int(_require_number(data.get("version", 1), rule_id, "version")),
            enabled=enabled,
            category=category,
        )


def _require_number(value: Any, rule_id: str, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleValidationError(
            f"Rule '{rule_id}': '{field_name}' must be a number, "
            f"got {type(value).__name__}."
        )
    return float(value)


# ----------------------------------------------------------------------------
# Result objects (the explanation trail)
# ----------------------------------------------------------------------------


@dataclass(slots=True)
class ConditionResult:
    """Outcome of one condition check, kept for explainability."""

    field: str
    operator: str
    expected: Any
    actual: Any
    passed: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "operator": self.operator,
            "expected": self.expected,
            "actual": self.actual,
            "passed": self.passed,
            "note": self.note,
        }


@dataclass(slots=True)
class RuleResult:
    """Outcome of evaluating ONE rule against a request.

    This is the contract every evaluator must return:
        evaluate(rule, request) -> RuleResult
    """

    rule_id: str
    outcome: str
    matched: bool
    weight: float
    priority: int
    conditions: list[ConditionResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "outcome": self.outcome,
            "matched": self.matched,
            "weight": self.weight,
            "priority": self.priority,
            "conditions": [c.to_dict() for c in self.conditions],
        }


@dataclass(slots=True)
class Decision:
    """Final, fully-explained result returned by ``engine.evaluate``."""

    decision: str
    confidence: float
    score: float
    rules_evaluated: list[str]
    rules_matched: list[str]
    rules_rejected: list[str]
    explanation: str
    trace: list[RuleResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "confidence": round(self.confidence, 4),
            "score": self.score,
            "rules_evaluated": self.rules_evaluated,
            "rules_matched": self.rules_matched,
            "rules_rejected": self.rules_rejected,
            "explanation": self.explanation,
            "trace": [t.to_dict() for t in self.trace],
        }
