# ============================================================
# FILE   : app/services/llm/base.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: Provider-agnostic LLM interface. Concrete vendors (Anthropic,
#          OpenAI, ...) implement this Protocol; routers depend only on it.
# ============================================================
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class LLMProvider(Protocol):
    """A chat-completion backend. Implementations must be stateless/thread-safe."""

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
        json_mode: bool = False,
    ) -> str:
        """Return the model's raw text response for a single-turn prompt.

        If `schema` is given, the response is guaranteed-valid JSON matching
        that Pydantic model where the provider supports structured output
        (e.g. Gemini's response_schema) — no markdown fences, no prose.

        If the result is JSON-shaped but the keys aren't known ahead of time
        (so a fixed `schema` doesn't fit — e.g. extracting arbitrary request
        fields from free text), pass `json_mode=True` instead: the provider
        should still enforce valid, unfenced JSON syntax without constraining
        the object's shape. `schema` implies `json_mode`; pass at most one.

        Providers that can't enforce either should still return their
        best-effort JSON/text.
        """
        ...
