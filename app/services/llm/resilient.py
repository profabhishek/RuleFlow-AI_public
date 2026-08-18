# ============================================================
# FILE   : app/services/llm/resilient.py
# OWNER  : ROLE 2 — API & Data
# NOTE   : Added by R1 (Abhishek) as a flagged cross-role fix after the
#          Gemini free tier (20 requests/day) was exhausted mid-testing.
#          See AI_LOG R1.md.
# PURPOSE: Wrap any LLMProvider with a prompt cache and automatic
#          degradation to the offline stub when the vendor is rate-limited,
#          so a quota wall can never kill a live demo.
# ============================================================
"""Quota-resilient LLM provider wrapper.

Two problems this solves, both discovered in real use:

1. **Repeated prompts burn quota.** Rehearsing a demo or re-running a test
   script sends the same text over and over. Identical prompts are
   deterministic inputs, so the first response is cached and replayed for
   free.

2. **Running out of quota kills the demo.** Gemini's free tier is 20
   requests/day; the NL endpoints previously surfaced a 429 as a wall of
   vendor JSON and stopped working entirely. Now a rate-limit error falls
   back to the offline stub so the feature keeps responding, and the
   degraded state is reported to the caller so the UI can say so plainly
   rather than passing regex output off as AI output.

Deliberately vendor-agnostic: rate limiting is detected from the error's
text (status code / status name), not by importing a vendor exception
class, so this keeps working if the provider behind it is swapped.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict

from pydantic import BaseModel

from .base import LLMProvider
from .stub import StubProvider

logger = logging.getLogger("app.services.llm.resilient")

#: Substrings that identify a rate-limit / quota-exhaustion failure.
_QUOTA_MARKERS = (
    "429",
    "resource_exhausted",
    "quota",
    "rate limit",
    "rate_limit",
    "too many requests",
)

#: How long to stop calling the vendor after a quota error before trying
#: again. Long enough to avoid hammering a per-minute limit, short enough
#: that a per-minute limit recovers on its own during a demo.
_DEFAULT_COOLDOWN_SECONDS = 45.0

#: Cached prompts. Small: a demo/test run repeats a handful of prompts.
_DEFAULT_CACHE_SIZE = 256


def is_quota_error(exc: BaseException) -> bool:
    """True if `exc` looks like a vendor rate-limit / quota rejection."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


class ResilientProvider:
    """An LLMProvider that caches responses and survives rate limiting.

    Not thread-safe by design intent — the degraded flag and cache are
    plain instance state, which is correct for the single-worker dev/demo
    server this targets. A multi-worker deployment should move the cache
    to a shared store and the degraded flag to a per-request value.
    """

    def __init__(
        self,
        primary: LLMProvider,
        *,
        fallback: LLMProvider | None = None,
        cache_size: int = _DEFAULT_CACHE_SIZE,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self._primary = primary
        self._fallback = fallback if fallback is not None else StubProvider()
        self._cache: OrderedDict[tuple, str] = OrderedDict()
        self._cache_size = cache_size
        self._cooldown = cooldown_seconds
        self._cooldown_until = 0.0
        #: True once a quota failure has forced fallback in this process.
        self.degraded = False
        #: Human-readable reason, surfaced to the UI.
        self.degradation_note = ""

    # -- introspection used by /health and the NL routers --------------------

    @property
    def primary_name(self) -> str:
        return type(self._primary).__name__

    @property
    def in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
        json_mode: bool = False,
    ) -> str:
        key = (system, user, getattr(schema, "__name__", None), json_mode)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            logger.info("LLM cache hit — no API call made")
            return cached

        if self.in_cooldown:
            logger.warning(
                "LLM in quota cooldown for another %.0fs; using offline stub",
                self._cooldown_until - time.monotonic(),
            )
            return self._fallback.complete(
                system=system, user=user, schema=schema, json_mode=json_mode
            )

        try:
            result = self._primary.complete(
                system=system, user=user, schema=schema, json_mode=json_mode
            )
        except Exception as exc:
            if not is_quota_error(exc):
                raise  # a real failure — let the router turn it into a 502
            self._enter_cooldown(exc)
            return self._fallback.complete(
                system=system, user=user, schema=schema, json_mode=json_mode
            )

        self._remember(key, result)
        return result

    # -- internals -----------------------------------------------------------

    def _enter_cooldown(self, exc: BaseException) -> None:
        self._cooldown_until = time.monotonic() + self._cooldown
        self.degraded = True
        self.degradation_note = (
            f"{self.primary_name} is rate-limited (quota exhausted); "
            "using offline extraction instead. Free-tier quotas reset daily."
        )
        logger.error(
            "LLM QUOTA EXHAUSTED on %s — falling back to the offline stub for "
            "the next %.0fs. Natural-language results are NOT AI-generated "
            "while degraded. Error: %s",
            self.primary_name,
            self._cooldown,
            exc,
        )

    def _remember(self, key: tuple, value: str) -> None:
        # Only successful primary responses are cached. Fallback output is
        # never cached, so the real model is retried once quota recovers.
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
