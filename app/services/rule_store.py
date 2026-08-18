# ============================================================
# FILE   : app/services/rule_store.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: Rule persistence via SQLAlchemy+SQLite           [R2]
# ============================================================
"""SQLite-backed rule storage.

One row per rule, storing the same dict shape `Rule.from_dict` /
`rules_to_dicts` already use (per rule_engine/loader.py's documented
storage contract) — no field-by-field column mapping, no drift risk
between the engine's schema and this table.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from rule_engine import RULE_EVALUATORS, Rule, load_rules, rules_to_dicts
from rule_engine.exceptions import RuleValidationError


class RuleAlreadyExistsError(Exception):
    """Raised by add_rule() when the rule id already exists."""


class RuleNotFoundError(Exception):
    """Raised by update_rule()/delete_rule() when the rule id doesn't exist."""


#: The engine's operator registry accepts several aliases per operator (see
#: the @operator(...) registrations in rule_engine/evaluators.py, e.g.
#: @operator("gt", ">")) so evaluation works with either spelling. But a
#: fixed-vocabulary UI control (the frontend's operator <select>) only lists
#: canonical names — an alias that slips through renders as the dropdown's
#: first option instead of the real one. Canonicalize here so every
#: persisted/returned rule uses the same spelling regardless of how it was
#: authored (manual POST, PUT, or AI-generated).
_OPERATOR_ALIASES: dict[str, str] = {
    ">": "gt",
    ">=": "gte",
    "<": "lt",
    "<=": "lte",
    "==": "eq",
    "equals": "eq",
    "!=": "ne",
    "not_equals": "ne",
    "matches": "regex",
    "date_lt": "before",
    "date_gt": "after",
    "date_lte": "on_or_before",
    "date_gte": "on_or_after",
}


#: The three verdicts every other layer already assumes: the frontend's
#: badge colors, the gated /decide/gated worst-wins severity ordering
#: (APPROVE < REVIEW < REJECT), and the audit trail's "decision" field.
#: `rule_engine.Rule.outcome` is deliberately a free string at the engine
#: level (extensibility — new outcome vocabularies shouldn't need engine
#: changes), so this is a product-level guard rail enforced at the API
#: boundary, not a core engine restriction.
VALID_OUTCOMES = frozenset({"APPROVE", "REJECT", "REVIEW"})


def validate_authorable_rule(data: dict) -> dict:
    """Reject rules with an outcome or rule type the rest of the system
    can't actually handle, before they're ever persisted.

    Two real incidents motivated this: (1) an AI-authored rule saved with
    outcome "disapprove_loan" instead of REJECT — it persisted fine, then
    silently rendered as a gray "neutral" badge and got treated as
    REVIEW-severity by /decide/gated's worst-wins aggregation, instead of
    REJECT. (2) an AI-authored rule saved with type "loan_eligibility" (no
    such evaluator exists) — it persisted fine, then broke `/decide` for
    *every* request, not just ones touching that rule, because the engine
    had no fallback for an unregistered type. Both are now caught here,
    at write time, with a clear 422 instead of a working-until-it-isn't
    rule silently entering the system.
    """
    outcome = data.get("outcome")
    if isinstance(outcome, str):
        canonical = outcome.strip().upper()
        if canonical not in VALID_OUTCOMES:
            raise RuleValidationError(
                f"'outcome' must be one of {sorted(VALID_OUTCOMES)}, got '{outcome}'."
            )
        if canonical != outcome:
            data = {**data, "outcome": canonical}

    rule_type = data.get("type", "conditional")
    if isinstance(rule_type, str) and rule_type not in RULE_EVALUATORS:
        raise RuleValidationError(
            f"'type' must be one of {sorted(RULE_EVALUATORS)} (the registered "
            f"rule-type evaluators), got '{rule_type}'."
        )
    return data


def _value_key(value: object) -> str:
    """Hashable, type-normalized form of a condition value.

    Normalizes so cosmetic differences don't read as different logic:
    ``670`` and ``670.0`` match, ``" gold "`` and ``"gold"`` match.
    Falls back to ``repr`` for anything not JSON-serializable so this can
    never raise on unusual rule data.
    """
    if isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return json.dumps(float(value))
    if isinstance(value, str):
        return json.dumps(value.strip())
    try:
        return json.dumps(value, sort_keys=True)
    except TypeError:
        return repr(value)


def condition_signature(rule: Rule) -> tuple:
    """Fingerprint of *what a rule actually matches*, ignoring cosmetics.

    Two rules with the same signature fire on exactly the same requests,
    no matter how differently they were worded, what ids they were given,
    or which category they landed in. Conditions are compared as a set
    because AND/OR evaluation is order-independent.

    Deliberately excludes: id, description, category, priority, weight,
    enabled, version — none of which change *when* a rule matches.
    """
    conditions = frozenset(
        (c.field.strip(), c.operator, _value_key(c.value)) for c in rule.conditions
    )
    return (rule.logic, conditions)


def find_equivalent_rules(rule: Rule, *, exclude_id: str | None = None) -> list[Rule]:
    """Existing rules whose matching logic is identical to ``rule``'s.

    Powers the "this already exists — create anyway?" confirmation. Users
    describe rules in plain English, so the same intent easily arrives
    twice with different wording and a different generated id ("credit
    score 670+ clears underwriting" vs "approve if credit score above
    670"). Structural id/uniqueness checks can't catch that; this can.

    Known limitation: equivalence is structural, so logically-equal but
    differently-expressed conditions (``gte 670`` vs ``gt 669``) are not
    detected. That's a deliberate trade-off — deterministic and instant,
    with no extra LLM round-trip, and it catches the common case.
    """
    signature = condition_signature(rule)
    skip = exclude_id if exclude_id is not None else rule.id
    return [
        existing
        for existing in list_rules()
        if existing.id != skip and condition_signature(existing) == signature
    ]


def normalize_rule_dict(data: dict) -> dict:
    """Canonicalize condition operator aliases (e.g. "==" -> "eq") in a rule dict."""
    conditions = data.get("conditions")
    if not isinstance(conditions, list):
        return data
    normalized = dict(data)
    normalized["conditions"] = [
        {**c, "operator": _OPERATOR_ALIASES.get(c["operator"], c["operator"])}
        if isinstance(c, dict) and isinstance(c.get("operator"), str)
        else c
        for c in conditions
    ]
    return normalized


_metadata = MetaData()
_rules_table = Table(
    "rules",
    _metadata,
    Column("id", String, primary_key=True),
    Column("data", Text, nullable=False),
)

_engine: Engine | None = None


def _get_engine() -> Engine:
    # not thread-safe on cold start: concurrent first calls can race and
    # create the engine/seed twice. Fine for a single-worker dev server.
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(settings.database_url, future=True)
        _metadata.create_all(_engine)
        _seed_if_empty(_engine, settings.rules_path)
    return _engine


def _seed_if_empty(engine: Engine, rules_path: str) -> None:
    with engine.connect() as conn:
        existing = conn.execute(select(_rules_table.c.id).limit(1)).first()
        if existing is not None or not Path(rules_path).exists():
            return
        for rule in load_rules(rules_path):
            _upsert(conn, rule)
        conn.commit()


def _upsert(conn, rule: Rule) -> None:
    data = json.dumps(rules_to_dicts([rule])[0])
    stmt = sqlite_insert(_rules_table).values(id=rule.id, data=data)
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={"data": data})
    conn.execute(stmt)


def list_rules(category: str | None = None) -> list[Rule]:
    """Return all rules, or only those in `category` when given.

    Filtering is in Python (rule count is small); category travels inside the
    stored JSON blob via rule_engine's rules_to_dicts contract, so there is no
    separate column to keep in sync.
    """
    engine = _get_engine()
    with engine.connect() as conn:
        rows = conn.execute(select(_rules_table.c.data)).fetchall()
    rules = [Rule.from_dict(normalize_rule_dict(json.loads(row.data))) for row in rows]
    if category is not None:
        rules = [r for r in rules if r.category == category]
    return rules


def get_rule(rule_id: str) -> Rule | None:
    engine = _get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(_rules_table.c.data).where(_rules_table.c.id == rule_id)
        ).first()
    return Rule.from_dict(normalize_rule_dict(json.loads(row.data))) if row else None


def add_rule(rule: Rule) -> None:
    """Insert a new rule. Raises RuleAlreadyExistsError if the id is taken."""
    engine = _get_engine()
    data = json.dumps(rules_to_dicts([rule])[0])
    try:
        with engine.connect() as conn:
            conn.execute(_rules_table.insert().values(id=rule.id, data=data))
            conn.commit()
    except IntegrityError as exc:
        raise RuleAlreadyExistsError(f"Rule '{rule.id}' already exists.") from exc


def update_rule(rule_id: str, rule: Rule) -> None:
    """Replace an existing rule. Raises RuleNotFoundError if it doesn't exist."""
    engine = _get_engine()
    data = json.dumps(rules_to_dicts([rule])[0])
    with engine.connect() as conn:
        result = conn.execute(
            _rules_table.update().where(_rules_table.c.id == rule_id).values(data=data)
        )
        conn.commit()
    if result.rowcount == 0:
        raise RuleNotFoundError(f"Rule '{rule_id}' not found.")


def delete_rule(rule_id: str) -> None:
    """Delete a rule. Raises RuleNotFoundError if it doesn't exist."""
    engine = _get_engine()
    with engine.connect() as conn:
        result = conn.execute(_rules_table.delete().where(_rules_table.c.id == rule_id))
        conn.commit()
    if result.rowcount == 0:
        raise RuleNotFoundError(f"Rule '{rule_id}' not found.")
