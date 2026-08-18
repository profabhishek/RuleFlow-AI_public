# ============================================================
# FILE   : app/routers/rules.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: GET/POST/PUT/DELETE /rules                       [R2]
# ============================================================
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_llm_provider
from app.schemas import AIGeneratedRule, NLRuleCreateRequest, NLRuleCreateResponse
from app.services import rule_store
from app.services.llm import LLMProvider
from rule_engine import Rule, rules_to_dicts
from rule_engine.exceptions import RuleValidationError

router = APIRouter(prefix="/rules", tags=["rules"])

_RULE_PROMPT = """Return a rule extracted from the user's description: an id, \
description, conditions (field/operator/value), AND/OR logic, and outcome. \
The 'type' field must always be exactly "conditional" -- it is the only \
rule type this engine supports; never invent a new one. The 'outcome' must \
be exactly one of APPROVE, REJECT, or REVIEW (uppercase, no other words) -- \
never a custom verb like "disapprove_loan" or "decline"."""


def _equivalence_conflict(rule: Rule, *, exclude_id: str | None = None) -> None:
    """Raise 409 if an existing rule already matches the same requests.

    Distinguishes two cases the user needs to tell apart:
      * **duplicate** — same conditions AND same outcome: harmless but
        redundant; the ruleset gets noisier and harder to reason about.
      * **conflict**  — same conditions, DIFFERENT outcome: actively
        dangerous. Both rules fire on every matching request and the
        winner is decided by priority, so an approve and a reject can sit
        side by side and whichever has the higher number silently wins.

    Not a hard block: callers pass ``force=true`` to create it anyway,
    so the user stays in control (an intentional override is fine — an
    accidental one shouldn't be silent).
    """
    matches = rule_store.find_equivalent_rules(rule, exclude_id=exclude_id)
    if not matches:
        return
    contradictory = [m for m in matches if m.outcome != rule.outcome]
    kind = "conflict" if contradictory else "duplicate"
    message = (
        "An existing rule already matches exactly the same requests but "
        f"produces '{contradictory[0].outcome}' instead of '{rule.outcome}'. "
        "Both would fire together and priority alone would decide the winner."
        if contradictory
        else "An existing rule already matches exactly the same requests "
        "with the same outcome."
    )
    raise HTTPException(
        status_code=409,
        detail={
            "kind": kind,
            "message": message,
            "existing": [
                {
                    "id": m.id,
                    "outcome": m.outcome,
                    "category": m.category,
                    "priority": m.priority,
                    "description": m.description,
                }
                for m in matches
            ],
            "hint": "Resend with force=true to create it anyway.",
        },
    )


@router.get("")
def list_rules() -> list[dict]:
    return rules_to_dicts(rule_store.list_rules())


@router.get("/{rule_id}")
def get_rule(rule_id: str) -> dict:
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found.")
    return rules_to_dicts([rule])[0]


@router.post("", status_code=201)
def create_rule(rule_data: dict, force: bool = False) -> dict:
    try:
        normalized = rule_store.validate_authorable_rule(
            rule_store.normalize_rule_dict(rule_data)
        )
        rule = Rule.from_dict(normalized)
    except RuleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not force:
        _equivalence_conflict(rule)
    try:
        rule_store.add_rule(rule)
    except rule_store.RuleAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return rules_to_dicts([rule])[0]


@router.put("/{rule_id}")
def update_rule(rule_id: str, rule_data: dict, force: bool = False) -> dict:
    try:
        normalized = rule_store.validate_authorable_rule(
            rule_store.normalize_rule_dict({**rule_data, "id": rule_id})
        )
        rule = Rule.from_dict(normalized)
    except RuleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Editing a rule until it matches the same requests as a different
    # rule is the same problem arriving by another route.
    if not force:
        _equivalence_conflict(rule, exclude_id=rule_id)
    try:
        rule_store.update_rule(rule_id, rule)
    except rule_store.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return rules_to_dicts([rule])[0]


@router.delete("/{rule_id}", status_code=204)
def delete_rule(rule_id: str) -> None:
    try:
        rule_store.delete_rule(rule_id)
    except rule_store.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/from-text", response_model=NLRuleCreateResponse, status_code=201)
def create_rule_from_text(
    body: NLRuleCreateRequest,
    force: bool = False,
    llm: LLMProvider = Depends(get_llm_provider),
) -> NLRuleCreateResponse:
    """AI-authored rule: free text -> structured Rule, validated via R1's
    own Rule.from_dict before it's ever persisted."""
    try:
        raw = llm.complete(system=_RULE_PROMPT, user=body.text, schema=AIGeneratedRule)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"LLM provider failed: {exc}"
        ) from exc
    try:
        normalized = rule_store.validate_authorable_rule(
            rule_store.normalize_rule_dict(_parse_json(raw))
        )
        rule = Rule.from_dict(normalized)
    except RuleValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"AI output failed rule validation: {exc}. Raw output: {raw}",
        ) from exc
    # Plain-English authoring is exactly where accidental duplicates come
    # from: the same intent described twice in different words produces a
    # different id, so the id-uniqueness check alone never catches it.
    if not force:
        _equivalence_conflict(rule)
    try:
        rule_store.add_rule(rule)
    except rule_store.RuleAlreadyExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"{exc} AI generated an id that collides with an existing rule; "
            "retry or specify a more distinct description.",
        ) from exc
    return NLRuleCreateResponse(
        rule=rules_to_dicts([rule])[0],
        raw_ai_output=raw,
        ai_degraded=getattr(llm, "degraded", False),
        ai_note=getattr(llm, "degradation_note", ""),
    )


def _parse_json(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422, detail=f"AI output was not valid JSON: {raw}"
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=422,
            detail="AI output must be a JSON object describing one rule.",
        )
    return parsed
