# ============================================================
# FILE   : app/schemas.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: Pydantic API request/response models           [R2]
# ============================================================
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class AIRuleCondition(BaseModel):
    field: str
    operator: str
    value: Any = None


class AIGeneratedRule(BaseModel):
    """Shape the AI must fill in for `POST /rules/from-text`.

    Passed as Gemini's `response_schema` so the model returns exactly this
    JSON shape — no markdown fences, no prose to parse defensively. Mirrors
    what `rule_engine.Rule.from_dict` expects; that's still the source of
    truth for business-rule validation (non-empty id, valid logic, etc.).
    """

    id: str
    description: str = ""
    type: str = "conditional"
    category: str = "general"
    conditions: list[AIRuleCondition]
    logic: str = "AND"
    outcome: str
    weight: float = 0.0
    priority: int = 0
    enabled: bool = True


class NLRuleCreateRequest(BaseModel):
    text: str


class NLRuleCreateResponse(BaseModel):
    rule: dict[str, Any]
    raw_ai_output: str
    #: True when the configured model was unavailable (typically quota
    #: exhausted) and offline extraction produced this instead. Surfaced so
    #: the UI never passes regex output off as AI output.
    ai_degraded: bool = False
    ai_note: str = ""


class NLQueryRequest(BaseModel):
    text: str


class NLQueryResponse(BaseModel):
    decision: dict[str, Any]
    extracted_request: dict[str, Any]
    #: AI-written summary. Empty when NL_EXPLANATIONS is off (the default) —
    #: callers should fall back to the engine's own `decision.explanation`.
    explanation: str = ""
    #: See NLRuleCreateResponse.ai_degraded.
    ai_degraded: bool = False
    ai_note: str = ""


class GateResult(BaseModel):
    """One category's evaluation within a gated decision."""

    category: str
    decision: dict[str, Any]  # the engine Decision.to_dict() for this category


class GatedDecisionResponse(BaseModel):
    """Result of running each rule category as an independent gate.

    `final_decision` is the worst outcome across gates (REJECT > REVIEW >
    APPROVE); a request clears only if every gate approves. `gates` carries
    each category's full engine Decision for explainability.
    """

    final_decision: str
    gates: list[GateResult]
