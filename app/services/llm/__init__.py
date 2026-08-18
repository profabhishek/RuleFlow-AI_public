# ============================================================
# FILE   : app/services/llm/__init__.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: Factory selecting an LLMProvider by LLM_PROVIDER env setting.
#          Supports a comma-separated fallback chain (e.g. "gemini,openrouter").
# ============================================================
from __future__ import annotations

import logging

from .base import LLMProvider
from .fallback import FallbackProvider
from .resilient import ResilientProvider
from .stub import StubProvider

logger = logging.getLogger("app.services.llm")


def _build_one(name: str) -> LLMProvider | None:
    """Construct a single provider by name, or None if it can't be built.

    Vendor providers are imported lazily so their SDK is only required when
    actually selected. A missing SDK or missing API key is logged and returns
    None so the rest of the chain can still run — never raises.
    """
    try:
        if name == "gemini":
            from .gemini import GeminiProvider

            return GeminiProvider()
        if name == "openrouter":
            from .openrouter import OpenRouterProvider

            return OpenRouterProvider()
        if name == "stub":
            return StubProvider()
    except Exception:  # noqa: BLE001 — a bad provider shouldn't kill the chain
        logger.warning("LLM provider '%s' unavailable; skipping", name, exc_info=True)
        return None
    logger.warning("Unknown LLM provider '%s'; skipping", name)
    return None


def get_provider(name: str) -> LLMProvider:
    """Resolve LLM_PROVIDER (a single name or comma-separated fallback chain).

    "gemini,openrouter" tries Gemini first and falls through to OpenRouter on
    failure. Providers that can't be built (no key / missing SDK) are dropped.
    If none build, the offline stub is returned so the NL endpoints still work.
    """
    names = [n.strip() for n in name.split(",") if n.strip()]
    built = [p for p in (_build_one(n) for n in names) if p is not None]
    if not built:
        return StubProvider()
    if len(built) == 1:
        return built[0]
    return FallbackProvider(built)


__all__ = [
    "FallbackProvider",
    "LLMProvider",
    "ResilientProvider",
    "StubProvider",
    "get_provider",
]
