# ============================================================
# FILE   : tests/test_llm_resilient.py
# OWNER  : ROLE 2 — API & Data (added by R1, flagged cross-role)
# PURPOSE: Cover the prompt cache and the quota-degradation path that
#          keeps NL features alive when the vendor rate-limits us.
# ============================================================
"""Tests for ResilientProvider.

Motivated by a real incident: the Gemini free tier (20 requests/day) ran
out mid-testing and every natural-language endpoint returned a wall of
vendor JSON. These lock in that (a) repeated prompts don't spend quota and
(b) a rate-limited vendor degrades to offline extraction instead of
failing, while still telling the caller it happened.
"""

from __future__ import annotations

import pytest

from app.services.llm.resilient import ResilientProvider, is_quota_error

#: Verbatim shape of the error Gemini returns when the daily free-tier
#: quota is gone (trimmed).
_REAL_QUOTA_ERROR = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded "
    "your current quota, please check your plan and billing details.', "
    "'status': 'RESOURCE_EXHAUSTED'}}"
)


class _CountingProvider:
    """Records every call; optionally raises a chosen exception."""

    def __init__(self, response: str = "primary", raises: Exception | None = None):
        self.calls = 0
        self._response = response
        self._raises = raises

    def complete(self, *, system, user, schema=None, json_mode=False) -> str:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._response


class _FallbackProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system, user, schema=None, json_mode=False) -> str:
        self.calls += 1
        return "fallback"


class TestQuotaDetection:
    def test_recognises_the_real_gemini_quota_error(self) -> None:
        assert is_quota_error(RuntimeError(_REAL_QUOTA_ERROR)) is True

    @pytest.mark.parametrize(
        "message",
        [
            "429 Too Many Requests",
            "RESOURCE_EXHAUSTED",
            "Quota exceeded for metric ...",
            "rate limit reached",
        ],
    )
    def test_recognises_common_phrasings(self, message: str) -> None:
        assert is_quota_error(RuntimeError(message)) is True

    @pytest.mark.parametrize(
        "message",
        ["401 unauthorized", "connection reset", "invalid schema", "boom"],
    )
    def test_ignores_unrelated_failures(self, message: str) -> None:
        assert is_quota_error(RuntimeError(message)) is False


class TestCaching:
    def test_identical_prompt_is_served_from_cache(self) -> None:
        primary = _CountingProvider()
        provider = ResilientProvider(primary)
        first = provider.complete(system="s", user="u")
        second = provider.complete(system="s", user="u")
        assert first == second == "primary"
        assert primary.calls == 1  # the second call spent no quota

    def test_different_prompts_are_not_conflated(self) -> None:
        primary = _CountingProvider()
        provider = ResilientProvider(primary)
        provider.complete(system="s", user="one")
        provider.complete(system="s", user="two")
        assert primary.calls == 2

    def test_json_mode_is_part_of_the_cache_key(self) -> None:
        primary = _CountingProvider()
        provider = ResilientProvider(primary)
        provider.complete(system="s", user="u", json_mode=False)
        provider.complete(system="s", user="u", json_mode=True)
        assert primary.calls == 2

    def test_cache_evicts_oldest_beyond_capacity(self) -> None:
        primary = _CountingProvider()
        provider = ResilientProvider(primary, cache_size=2)
        for text in ("a", "b", "c"):
            provider.complete(system="s", user=text)
        assert primary.calls == 3
        provider.complete(system="s", user="a")  # evicted, must re-call
        assert primary.calls == 4


class TestQuotaDegradation:
    def _rate_limited(self) -> ResilientProvider:
        return ResilientProvider(
            _CountingProvider(raises=RuntimeError(_REAL_QUOTA_ERROR)),
            fallback=_FallbackProvider(),
        )

    def test_quota_error_falls_back_instead_of_raising(self) -> None:
        provider = self._rate_limited()
        assert provider.complete(system="s", user="u") == "fallback"

    def test_degraded_state_is_reported(self) -> None:
        provider = self._rate_limited()
        assert provider.degraded is False
        provider.complete(system="s", user="u")
        assert provider.degraded is True
        assert "rate-limited" in provider.degradation_note

    def test_cooldown_stops_hammering_the_vendor(self) -> None:
        primary = _CountingProvider(raises=RuntimeError(_REAL_QUOTA_ERROR))
        provider = ResilientProvider(primary, fallback=_FallbackProvider())
        for index in range(5):
            provider.complete(system="s", user=f"different-{index}")
        # Only the first attempt reaches the vendor; the rest are short-
        # circuited by the cooldown.
        assert primary.calls == 1
        assert provider.in_cooldown is True

    def test_cooldown_expires_and_the_vendor_is_retried(self) -> None:
        primary = _CountingProvider(raises=RuntimeError(_REAL_QUOTA_ERROR))
        provider = ResilientProvider(
            primary, fallback=_FallbackProvider(), cooldown_seconds=0
        )
        provider.complete(system="s", user="one")
        provider.complete(system="s", user="two")
        assert primary.calls == 2  # retried once the cooldown lapsed

    def test_fallback_output_is_never_cached(self) -> None:
        # Otherwise a single quota blip would poison the cache with regex
        # output and the real model would never be used for that prompt.
        primary = _CountingProvider(raises=RuntimeError(_REAL_QUOTA_ERROR))
        provider = ResilientProvider(
            primary, fallback=_FallbackProvider(), cooldown_seconds=0
        )
        provider.complete(system="s", user="u")
        primary._raises = None  # vendor recovers
        assert provider.complete(system="s", user="u") == "primary"

    def test_non_quota_errors_still_propagate(self) -> None:
        # A genuine bug must not be silently masked as "degraded".
        provider = ResilientProvider(
            _CountingProvider(raises=ValueError("malformed schema")),
            fallback=_FallbackProvider(),
        )
        with pytest.raises(ValueError):
            provider.complete(system="s", user="u")
        assert provider.degraded is False
