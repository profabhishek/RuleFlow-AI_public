# ============================================================
# FILE   : rule_engine/__init__.py
# OWNER  : ROLE 1 — Rule Engine
# PURPOSE: Public API surface for the engine package.
# ============================================================
"""rule_engine — configurable, framework-independent decision engine.

Zero third-party dependencies by design. The API layer (Role 2) consumes:

    from rule_engine import evaluate, DecisionPolicy, load_rules
    decision = evaluate(request_dict, rules, DecisionPolicy(mode="priority"))
    payload = decision.to_dict()
"""

from .engine import DecisionPolicy, evaluate
from .evaluators import (
    OPERATORS,
    RULE_EVALUATORS,
    RuleEvaluator,
    apply_operator,
    get_evaluator,
    operator,
    register_evaluator,
)
from .exceptions import (
    OperatorError,
    RuleEngineError,
    RuleValidationError,
    UnknownRuleTypeError,
)
from .loader import load_rules, load_rules_from_string, rules_to_dicts
from .models import Condition, ConditionResult, Decision, Rule, RuleResult

__all__ = [
    "OPERATORS",
    "RULE_EVALUATORS",
    "Condition",
    "ConditionResult",
    "Decision",
    "DecisionPolicy",
    "OperatorError",
    "Rule",
    "RuleEngineError",
    "RuleEvaluator",
    "RuleResult",
    "RuleValidationError",
    "UnknownRuleTypeError",
    "apply_operator",
    "evaluate",
    "get_evaluator",
    "load_rules",
    "load_rules_from_string",
    "operator",
    "register_evaluator",
    "rules_to_dicts",
]
