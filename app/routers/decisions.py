# ============================================================
# FILE   : app/routers/decisions.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: POST /decide, POST /decide/bulk                 [R2]
# NOTE   : Audit wiring + LLM error handling added by R1 (Abhishek) as a
#          flagged cross-role fix — see AI_LOG R1.md.
# ============================================================
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.config import get_settings
from app.dependencies import get_llm_provider
from app.routers.audit import get_audit_service
from app.schemas import (
    GatedDecisionResponse,
    GateResult,
    NLQueryRequest,
    NLQueryResponse,
)
from app.services import rule_store
from app.services.audit_log import AuditRecord
from app.services.llm import LLMProvider
from rule_engine import Decision, DecisionPolicy, evaluate

logger = logging.getLogger("app.routers.decisions")

router = APIRouter(prefix="/decide", tags=["decisions"])

#: Kept as a stable prefix: the offline stub dispatches on this phrase, so
#: it must survive any rewording of the rest of the prompt.
_REQUEST_PROMPT_PREFIX = (
    "Return a JSON object of request fields (field name -> value) extracted "
    'from the user\'s scenario, e.g. {"age": 35, "credit_score": 700}. '
    "No prose, JSON only."
)


def _request_prompt() -> str:
    """Extraction prompt naming the exact fields the current rules use.

    Without this the model invents plausible-but-wrong names — a live
    example produced `verified_identity` when every rule reads
    `identity_verified`, so the KYC gate silently never matched and an
    under-age applicant came back APPROVED. The field list is derived from
    the rules themselves rather than hardcoded, so it stays correct as the
    ruleset changes.
    """
    fields = sorted({c.field for r in rule_store.list_rules() for c in r.conditions})
    if not fields:
        return _REQUEST_PROMPT_PREFIX
    return (
        f"{_REQUEST_PROMPT_PREFIX}\n"
        f"Known fields: {', '.join(fields)}.\n"
        "Use EXACTLY these field names when the scenario mentions them — "
        "never invent variations, reorderings, or synonyms. Booleans must be "
        "true/false. Omit any field the scenario does not mention rather "
        "than guessing a value."
    )

#: Outcome severity for cross-gate aggregation (worst wins). Anything not
#: listed is treated as REVIEW-level: unknown outcomes should route to a human,
#: never silently approve or auto-reject.
_SEVERITY = {"APPROVE": 1, "REVIEW": 2, "REJECT": 3}
_REVIEW_SEVERITY = _SEVERITY["REVIEW"]


def _policy() -> DecisionPolicy:
    settings = get_settings()
    return DecisionPolicy(mode=settings.decision_mode, default=settings.default_outcome)


def _audit(decision: Decision) -> None:
    """Append the decision to the audit trail; never block the decision path.

    Auditing is best-effort by design: a broken/locked audit file must not
    turn a valid decision into a 500. Failures are logged, not raised.
    """
    try:
        get_audit_service().append(AuditRecord.for_decision(decision))
    except Exception:
        logger.warning("audit append failed; decision still returned", exc_info=True)


def _llm(llm: LLMProvider, *, system: str, user: str, **kwargs: Any) -> str:
    """Call the LLM provider, mapping runtime failures to a clear 502.

    A bad/expired API key or vendor outage should read as 'LLM provider
    failed', not as an anonymous 500 from the decision API.
    """
    try:
        return llm.complete(system=system, user=user, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"LLM provider failed: {exc}"
        ) from exc


@router.post("")
def decide(request: dict[str, Any]) -> dict:
    rules = rule_store.list_rules()
    decision = evaluate(request, rules, _policy())
    _audit(decision)
    return decision.to_dict()


@router.post("/bulk")
def decide_bulk(requests: list[dict[str, Any]]) -> list[dict]:
    rules = rule_store.list_rules()
    policy = _policy()
    decisions = [evaluate(request, rules, policy) for request in requests]
    for decision in decisions:
        _audit(decision)
    return [decision.to_dict() for decision in decisions]


@router.post("/gated", response_model=GatedDecisionResponse)
def decide_gated(request: dict[str, Any]) -> GatedDecisionResponse:
    """Evaluate the request against each rule category as an independent gate.

    Each category is run through R1's `evaluate()` on its own (engine stays a
    pure single-policy function — this only orchestrates above it). Outcomes
    are combined worst-wins (REJECT > REVIEW > APPROVE): the request clears
    only if every gate approves. Per-gate Decisions are returned for the trail.
    """
    policy = _policy()
    all_rules = rule_store.list_rules()

    # Preserve first-seen category order for stable, explainable output.
    categories: list[str] = []
    for rule in all_rules:
        if rule.category not in categories:
            categories.append(rule.category)

    gates: list[GateResult] = []
    # Falls back to the policy default only when there are no rules/gates at
    # all; -1 ensures the first real gate always overrides the seed, so an
    # all-APPROVE run yields APPROVE rather than sticking at the default.
    worst_outcome = policy.default
    worst_severity = -1

    for category in categories:
        category_rules = [r for r in all_rules if r.category == category]
        decision = evaluate(request, category_rules, policy)
        _audit(decision)
        gates.append(GateResult(category=category, decision=decision.to_dict()))
        severity = _SEVERITY.get(decision.decision, _REVIEW_SEVERITY)
        if severity > worst_severity:
            worst_severity = severity
            worst_outcome = decision.decision

    return GatedDecisionResponse(final_decision=worst_outcome, gates=gates)


@router.post("/query", response_model=NLQueryResponse)
def decide_from_text(
    body: NLQueryRequest,
    llm: LLMProvider = Depends(get_llm_provider),
) -> NLQueryResponse:
    """NL scenario -> structured request -> R1's evaluate() -> NL explanation."""
    raw_request = _llm(llm, system=_request_prompt(), user=body.text, json_mode=True)
    try:
        extracted_request = json.loads(raw_request)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422, detail=f"AI output was not valid JSON: {raw_request}"
        ) from exc
    if not isinstance(extracted_request, dict):
        raise HTTPException(
            status_code=422,
            detail=f"AI output must be a JSON object of fields: {raw_request}",
        )

    rules = rule_store.list_rules()
    decision = evaluate(extracted_request, rules, _policy())
    _audit(decision)

    # Second LLM call, off by default — see Settings.nl_explanations. The
    # engine's own explanation already names the deciding rule and every
    # condition that passed, so this only buys nicer prose, at the cost of
    # doubling quota usage. Empty string means "use the engine's".
    explanation = ""
    if get_settings().nl_explanations:
        explanation = _llm(
            llm,
            system="Summarize this decision in plain, natural language for an end user.",
            user=json.dumps(decision.to_dict()),
        )

    return NLQueryResponse(
        decision=decision.to_dict(),
        extracted_request=extracted_request,
        explanation=explanation,
        ai_degraded=getattr(llm, "degraded", False),
        ai_note=getattr(llm, "degradation_note", ""),
    )
