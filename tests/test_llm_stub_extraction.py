# ============================================================
# FILE   : tests/test_llm_stub_extraction.py
# OWNER  : ROLE 2 — API & Data (added by R1, flagged cross-role)
# PURPOSE: Lock in schema-aware field extraction in the offline stub.
# ============================================================
"""Tests for StubProvider's request-field extraction.

The stub is what runs whenever the real model is unavailable — no API key,
no SDK, or (the common case) the daily free-tier quota is spent. That makes
its accuracy a demo-reliability concern, not a test-only detail.

The original heuristic took "the identifier immediately before a number",
which on a real sentence produced:

    "A 16 year old ... credit score 720 ... fraud score 12, dti 28"
    -> {"A": 16, "score": 12, "dti": 28}

...so the leading article became a field, the two "... score" phrases
collided, and an under-age applicant came back APPROVED because only `dti`
survived. Extraction is now driven by the field names the router supplies.
"""

from __future__ import annotations

import json

import pytest

from app.services.llm.stub import StubProvider

FIELDS = "age, credit_score, dti, fraud_score, identity_verified, on_watchlist"
SYSTEM = (
    "Return a JSON object of request fields (field name -> value).\n"
    f"Known fields: {FIELDS}.\n"
)
#: No "Known fields:" line — exercises the legacy fallback path.
SYSTEM_NO_SCHEMA = "Return a JSON object of request fields (field name -> value)."


def extract(text: str, system: str = SYSTEM) -> dict:
    return json.loads(StubProvider().complete(system=system, user=text))


class TestKnownFieldExtraction:
    def test_the_original_failing_sentence(self) -> None:
        # Verbatim regression: this exact input produced
        # {"A": 16, "score": 12, "dti": 28} and a wrong APPROVE.
        result = extract(
            "A 16 year old with verified identity, credit score 720, "
            "dti 28, fraud score 12, not on a watchlist."
        )
        assert result == {
            "age": 16,
            "credit_score": 720,
            "dti": 28,
            "fraud_score": 12,
            "identity_verified": True,
            "on_watchlist": False,
        }

    def test_no_invented_field_names(self) -> None:
        result = extract("A 16 year old with credit score 720.")
        assert set(result) <= {
            "age",
            "credit_score",
            "dti",
            "fraud_score",
            "identity_verified",
            "on_watchlist",
        }
        assert "A" not in result

    def test_similar_field_names_do_not_collide(self) -> None:
        # "credit score" and "fraud score" both end in "score"; the old
        # heuristic mapped both to `score` and lost one.
        result = extract("credit score 720 and fraud score 12")
        assert result["credit_score"] == 720
        assert result["fraud_score"] == 12

    def test_prose_and_identifier_spellings_both_work(self) -> None:
        assert extract("credit score 700")["credit_score"] == 700
        assert extract("credit_score 700")["credit_score"] == 700

    def test_number_before_the_field_name(self) -> None:
        result = extract("An applicant with a 720 credit score and a 28 dti.")
        assert result["credit_score"] == 720  # not 28 from the next clause
        assert result["dti"] == 28

    def test_value_from_the_next_clause_is_not_captured(self) -> None:
        # "identity verified, credit score 550" must not read 550 as the
        # value of identity_verified.
        result = extract("Age 34, identity verified, credit score 550.")
        assert result["identity_verified"] is True
        assert result["credit_score"] == 550

    def test_filler_words_between_field_and_value(self) -> None:
        assert extract("a credit score of 720")["credit_score"] == 720


class TestBooleanFields:
    def test_positive_mention(self) -> None:
        assert extract("identity verified")["identity_verified"] is True

    def test_reversed_word_order(self) -> None:
        assert extract("verified identity")["identity_verified"] is True

    def test_negation_before_the_mention(self) -> None:
        assert extract("not on a watchlist")["on_watchlist"] is False

    def test_negation_inside_the_mention(self) -> None:
        result = extract("whose identity has NOT been verified")
        assert result["identity_verified"] is False

    def test_filler_words_inside_the_field_name(self) -> None:
        assert extract("on the watchlist")["on_watchlist"] is True

    def test_single_word_field_never_becomes_a_bare_boolean(self) -> None:
        # A bare "age" with no number must be omitted, not set to True.
        assert "age" not in extract("we should consider their age carefully")


class TestDegenerateInput:
    @pytest.mark.parametrize("text", ["", "   ", "what is the weather in mumbai"])
    def test_nothing_extractable_yields_an_empty_object(self, text: str) -> None:
        assert extract(text) == {}

    def test_partial_information_is_preserved(self) -> None:
        assert extract("A 30 year old applicant.") == {"age": 30}

    def test_unknown_fields_in_text_are_ignored(self) -> None:
        result = extract("A 30 year old with a favourite colour of 7.")
        assert result == {"age": 30}


class TestLegacyFallback:
    def test_without_a_schema_the_old_heuristic_still_runs(self) -> None:
        # Back-compat: callers that don't advertise their fields must still
        # get best-effort extraction rather than an empty object.
        result = extract("age 30 credit_score 700", system=SYSTEM_NO_SCHEMA)
        assert result["age"] == 30
        assert result["credit_score"] == 700
