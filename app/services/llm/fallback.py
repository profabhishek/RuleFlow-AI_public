# ============================================================
# FILE   : app/services/llm/fallback.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: Chain LLMProviders so a failing primary (e.g. Gemini hitting its
#          hard daily limit) automatically falls through to the next.
# ============================================================
from __future__ import annotations

import logging

from pydantic import BaseModel

from .base import LLMProvider

logger = logging.getLogger("app.services.llm.fallback")


class FallbackProvider:
    """Try each provider in order; on any failure, fall through to the next.

    Built by the factory when LLM_PROVIDER lists more than one provider (e.g.
    "gemini,openrouter"). Falls back on *any* exception — a 429 daily-limit, a
    vendor outage, a timeout — so the NL endpoints keep working as long as one
    provider in the chain responds. If every provider fails, the last error is
    re-raised (surfaced as a 502 by the router's _llm wrapper).
    """

    def __init__(self, providers: list[LLMProvider]) -> None:
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider.")
        self._providers = providers

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
        json_mode: bool = False,
    ) -> str:
        last_exc: Exception | None = None
        for index, provider in enumerate(self._providers):
            name = type(provider).__name__
            try:
                return provider.complete(
                    system=system, user=user, schema=schema, json_mode=json_mode
                )
            except Exception as exc:  # noqa: BLE001 — fall through to next provider
                last_exc = exc
                is_last = index == len(self._providers) - 1
                logger.warning(
                    "LLM provider %s failed (%s)%s",
                    name,
                    exc,
                    "" if is_last else "; falling back to next provider",
                    exc_info=True,
                )
        assert last_exc is not None  # providers is non-empty, so a failure was set
        raise last_exc
