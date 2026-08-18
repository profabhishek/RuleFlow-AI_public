# ============================================================
# FILE   : rule_engine/exceptions.py
# OWNER  : ROLE 1 — Rule Engine
# PURPOSE: Custom exception hierarchy for the engine.
# ============================================================
"""Custom exceptions raised by the rule engine.

All engine errors derive from ``RuleEngineError`` so upper layers can catch
one base class. ``RuleValidationError`` additionally derives from
``ValueError`` so the API layer can map it to an HTTP 400 cleanly.
"""


class RuleEngineError(Exception):
    """Base class for every error raised by the rule engine."""


class RuleValidationError(RuleEngineError, ValueError):
    """A rule or request is structurally invalid (bad schema, bad field, ...)."""


class OperatorError(RuleEngineError, ValueError):
    """An operator is unknown or was applied to incompatible values."""


class UnknownRuleTypeError(RuleEngineError, ValueError):
    """A rule references a rule type with no registered evaluator."""
