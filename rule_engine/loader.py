# ============================================================
# FILE   : rule_engine/loader.py
# OWNER  : ROLE 1 — Rule Engine
# PURPOSE: Parse & validate rules from JSON. Storage-agnostic by design:
#          Role 2's SQLAlchemy store builds Rule objects from DB rows via
#          the same Rule.from_dict contract.
# ============================================================
"""Rule loading and validation.

The engine consumes ``list[Rule]`` and does not care where rules came from.
This module covers the JSON path (seed file, tests, import/export). The
repository layer (Role 2) persists rules in SQLite and reuses
``Rule.from_dict`` / ``rules_to_dicts`` as the conversion contract, keeping
the engine free of any database dependency.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .exceptions import RuleValidationError
from .models import Rule

logger = logging.getLogger("rule_engine.loader")


def load_rules_from_string(text: str) -> list[Rule]:
    """Parse and validate rules from a JSON string.

    Accepts either a top-level list of rule objects, or an object of the
    form ``{"rules": [...]}``. Guarantees unique rule ids.

    Raises:
        RuleValidationError: on malformed JSON or any invalid rule.
    """
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuleValidationError(f"Rules document is not valid JSON: {exc}") from exc

    if isinstance(data, dict) and "rules" in data:
        data = data["rules"]
    if not isinstance(data, list):
        raise RuleValidationError(
            "Rules must be a JSON list, or an object with a 'rules' list."
        )

    rules = [Rule.from_dict(item) for item in data]

    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise RuleValidationError(f"Duplicate rule id '{rule.id}'.")
        seen.add(rule.id)

    logger.info("loaded %d rules", len(rules))
    return rules


def load_rules(path: str) -> list[Rule]:
    """Load and validate rules from a JSON file on disk."""
    with open(path, "r", encoding="utf-8") as fh:
        return load_rules_from_string(fh.read())


def rules_to_dicts(rules: list[Rule]) -> list[dict[str, Any]]:
    """Serialize rules back to plain dicts (export / persistence contract)."""
    return [
        {
            "id": r.id,
            "description": r.description,
            "type": r.type,
            "logic": r.logic,
            "outcome": r.outcome,
            "weight": r.weight,
            "priority": r.priority,
            "version": r.version,
            "enabled": r.enabled,
            "category": r.category,
            "conditions": [
                {"field": c.field, "operator": c.operator, "value": c.value}
                for c in r.conditions
            ],
        }
        for r in rules
    ]
