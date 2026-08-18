# ============================================================
# FILE   : app/dependencies.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: Shared DI: rule store, audit log               [R2]
# ============================================================
from __future__ import annotations

import logging
from functools import lru_cache

from app.config import get_settings
from app.services.llm import (
    LLMProvider,
    ResilientProvider,
    StubProvider,
    get_provider,
)

logger = logging.getLogger("app.dependencies")


@lru_cache
def get_llm_provider() -> LLMProvider:
    """Resolve the configured LLM provider, degrading gracefully to the stub.

    A missing vendor SDK or misconfigured provider must not take down the
    NL endpoints — the offline stub keeps the demo functional and the
    problem is logged loudly instead. (Fallback added by R1, flagged fix.)

    "Loudly" matters: an earlier version of this fallback logged at WARNING
    and was easy to miss, so a misconfigured provider looked like "the AI
    works but is oddly bad at reading English" rather than "you are not
    talking to an AI at all". Both the happy path and the fallback now
    announce the *active* provider unmistakably.
    """
    settings = get_settings()
    configured = settings.llm_provider
    try:
        provider = get_provider(configured)
    except Exception:
        logger.exception(
            "=" * 70 + "\n"
            "LLM PROVIDER '%s' FAILED TO INITIALISE — FALLING BACK TO THE "
            "OFFLINE STUB.\nNatural-language features will use naive regex "
            "extraction, NOT a real AI.\nLikely causes: the vendor SDK is not "
            "installed (pip install google-genai), or LLM_API_KEY is missing "
            "or invalid.\n" + "=" * 70,
            configured,
        )
        return StubProvider()

    active = type(provider).__name__
    if isinstance(provider, StubProvider) and configured != "stub":
        # get_provider() falls back to the stub for unrecognised names
        # rather than raising, so this branch catches typos like
        # LLM_PROVIDER=gemeni that would otherwise pass silently.
        logger.error(
            "LLM_PROVIDER is set to '%s', which is not a recognised provider "
            "— using the OFFLINE STUB instead of a real AI. Valid values: "
            "'stub', 'gemini'.",
            configured,
        )
    else:
        logger.info("LLM provider active: %s (LLM_PROVIDER=%s)", active, configured)

    if isinstance(provider, StubProvider):
        # The stub is free, offline and deterministic — caching it or
        # guarding it against rate limits would add nothing.
        return provider
    # Wrap real vendors: cache identical prompts (rehearsing a demo or
    # re-running the test script then costs nothing) and degrade to the
    # stub instead of dying when the vendor's quota runs out.
    return ResilientProvider(provider)
