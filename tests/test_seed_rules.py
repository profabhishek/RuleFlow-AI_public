# ============================================================
# FILE   : tests/test_seed_rules.py
# OWNER  : ROLE 1 — Rule Engine
# PURPOSE: End-to-end tests that load the real seed rules
#          (rules/rules.json) and exercise realistic fintech
#          loan-decision scenarios through evaluate() in both
#          decision policies, flat and per-category (gated).
# ============================================================
"""Integration tests against the shipped fintech seed rules.

The seeds model four independent gates a real lender runs:

    kyc           — legal age + verified identity (AML/KYC)
    fraud         — watchlist screening + fraud risk score
    underwriting  — FICO-style credit bands (<580 / 580-669 / 670+)
    affordability — debt-to-income bands (>43% / 36-43% / <36%)

These validate the whole pipeline — load_rules -> evaluate -> Decision —
using the exact rules Role 2 serves, so a regression in any layer
(loader, models, evaluators, engine) surfaces as a wrong loan decision.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rule_engine import DecisionPolicy, evaluate, load_rules, rules_to_dicts
from rule_engine.exceptions import RuleValidationError
from rule_engine.loader import load_rules_from_string

SEED_PATH = Path(__file__).resolve().parents[1] / "rules" / "rules.json"

#: A profile that passes every gate. Tests copy and break one field at a time.
CLEAN_APPLICANT = {
    "age": 34,
    "identity_verified": True,
    "credit_score": 720,
    "dti": 28,
    "fraud_score": 12,
    "on_watchlist": False,
}


@pytest.fixture(scope="module")
def seed_rules():
    return load_rules(str(SEED_PATH))


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------


class TestSeedLoading:
    def test_all_rules_load(self, seed_rules) -> None:
        assert len(seed_rules) == 12

    def test_expected_ids_present(self, seed_rules) -> None:
        ids = {r.id for r in seed_rules}
        assert ids == {
            "kyc_reject_underage",
            "kyc_reject_unverified_identity",
            "kyc_pass",
            "fraud_reject_watchlist",
            "fraud_review_high_score",
            "fraud_pass",
            "uw_reject_poor_credit",
            "uw_review_fair_credit",
            "uw_approve_good_credit",
            "afford_reject_high_dti",
            "afford_review_borderline_dti",
            "afford_approve_healthy_dti",
        }

    def test_four_gates_present(self, seed_rules) -> None:
        assert {r.category for r in seed_rules} == {
            "kyc",
            "fraud",
            "underwriting",
            "affordability",
        }

    def test_every_gate_has_an_approve_path(self, seed_rules) -> None:
        # A gate with no APPROVE rule could never clear — it would always
        # fall to the policy default and jam the gated flow at REVIEW.
        for category in {r.category for r in seed_rules}:
            outcomes = {r.outcome for r in seed_rules if r.category == category}
            assert "APPROVE" in outcomes, f"gate '{category}' cannot approve"

    def test_round_trip_reloads_identically(self, seed_rules) -> None:
        dumped = rules_to_dicts(seed_rules)
        reloaded = load_rules_from_string(json.dumps({"rules": dumped}))
        assert [r.id for r in reloaded] == [r.id for r in seed_rules]
        assert [r.priority for r in reloaded] == [r.priority for r in seed_rules]
        assert [r.category for r in reloaded] == [r.category for r in seed_rules]


# ----------------------------------------------------------------------------
# Priority mode (default) — realistic applicants, one broken field at a time
# ----------------------------------------------------------------------------


class TestSeedPriorityScenarios:
    def test_clean_profile_is_approved(self, seed_rules) -> None:
        d = evaluate(dict(CLEAN_APPLICANT), seed_rules)
        assert d.decision == "APPROVE"
        assert {
            "kyc_pass",
            "fraud_pass",
            "uw_approve_good_credit",
            "afford_approve_healthy_dti",
        } <= set(d.rules_matched)
        # Every matched rule agrees on APPROVE -> full confidence.
        assert d.confidence == pytest.approx(1.0)

    def test_underage_is_rejected(self, seed_rules) -> None:
        d = evaluate({**CLEAN_APPLICANT, "age": 16}, seed_rules)
        assert d.decision == "REJECT"
        assert "kyc_reject_underage" in d.rules_matched

    def test_unverified_identity_is_rejected(self, seed_rules) -> None:
        d = evaluate({**CLEAN_APPLICANT, "identity_verified": False}, seed_rules)
        assert d.decision == "REJECT"
        assert "kyc_reject_unverified_identity" in d.rules_matched

    def test_watchlist_hit_is_rejected(self, seed_rules) -> None:
        d = evaluate({**CLEAN_APPLICANT, "on_watchlist": True}, seed_rules)
        assert d.decision == "REJECT"
        assert "fraud_reject_watchlist" in d.rules_matched

    def test_high_fraud_score_routes_to_review(self, seed_rules) -> None:
        d = evaluate({**CLEAN_APPLICANT, "fraud_score": 85}, seed_rules)
        assert d.decision == "REVIEW"
        assert "fraud_review_high_score" in d.rules_matched

    def test_poor_credit_is_rejected(self, seed_rules) -> None:
        d = evaluate({**CLEAN_APPLICANT, "credit_score": 550}, seed_rules)
        assert d.decision == "REJECT"
        assert "uw_reject_poor_credit" in d.rules_matched

    def test_fair_credit_routes_to_review(self, seed_rules) -> None:
        d = evaluate({**CLEAN_APPLICANT, "credit_score": 620}, seed_rules)
        assert d.decision == "REVIEW"
        assert "uw_review_fair_credit" in d.rules_matched

    def test_high_dti_is_rejected(self, seed_rules) -> None:
        d = evaluate({**CLEAN_APPLICANT, "dti": 45}, seed_rules)
        assert d.decision == "REJECT"
        assert "afford_reject_high_dti" in d.rules_matched

    def test_borderline_dti_routes_to_review(self, seed_rules) -> None:
        d = evaluate({**CLEAN_APPLICANT, "dti": 40}, seed_rules)
        assert d.decision == "REVIEW"
        assert "afford_review_borderline_dti" in d.rules_matched

    def test_two_rejections_highest_priority_explains(self, seed_rules) -> None:
        # Underage (120) AND watchlisted (115): both fire, underage explains.
        d = evaluate(
            {**CLEAN_APPLICANT, "age": 16, "on_watchlist": True}, seed_rules
        )
        assert d.decision == "REJECT"
        assert {"kyc_reject_underage", "fraud_reject_watchlist"} <= set(
            d.rules_matched
        )
        assert "kyc_reject_underage" in d.explanation


# ----------------------------------------------------------------------------
# Bad / hostile input — must degrade safely, never crash, stay deterministic
# ----------------------------------------------------------------------------


class TestSeedBadInput:
    def test_empty_request_falls_to_default_review(self, seed_rules) -> None:
        d = evaluate({}, seed_rules)
        assert d.decision == "REVIEW"  # policy default — route unknowns to a human
        assert d.rules_matched == []

    def test_missing_fields_never_crash(self, seed_rules) -> None:
        d = evaluate({"age": 45}, seed_rules)
        assert d.decision in {"APPROVE", "REJECT", "REVIEW"}

    def test_wrong_types_degrade_to_safe_non_match(self, seed_rules) -> None:
        junk = {
            "age": "twenty",
            "credit_score": None,
            "dti": [1, 2],
            "fraud_score": {"nested": True},
            "identity_verified": "yes",
            "on_watchlist": "no",
        }
        d = evaluate(junk, seed_rules)  # must not raise
        assert d.decision == "REVIEW"  # nothing legitimately matched
        assert d.rules_matched == []

    def test_non_dict_request_raises_validation_error(self, seed_rules) -> None:
        with pytest.raises(RuleValidationError):
            evaluate("age=16", seed_rules)  # type: ignore[arg-type]

    def test_extra_unknown_fields_are_ignored(self, seed_rules) -> None:
        d = evaluate({**CLEAN_APPLICANT, "favourite_color": "teal"}, seed_rules)
        assert d.decision == "APPROVE"

    def test_determinism_same_input_same_output(self, seed_rules) -> None:
        a = evaluate(dict(CLEAN_APPLICANT), seed_rules)
        b = evaluate(dict(CLEAN_APPLICANT), seed_rules)
        assert a.to_dict() == b.to_dict()


# ----------------------------------------------------------------------------
# Per-category (gated) — each gate evaluated independently, like /decide/gated
# ----------------------------------------------------------------------------


class TestSeedGatedByCategory:
    def _gate(self, seed_rules, category: str, request: dict) -> str:
        rules = [r for r in seed_rules if r.category == category]
        return evaluate(request, rules).decision

    def test_clean_profile_clears_every_gate(self, seed_rules) -> None:
        for cat in ("kyc", "fraud", "underwriting", "affordability"):
            assert self._gate(seed_rules, cat, dict(CLEAN_APPLICANT)) == "APPROVE"

    def test_one_bad_gate_fails_only_that_gate(self, seed_rules) -> None:
        applicant = {**CLEAN_APPLICANT, "credit_score": 550}
        assert self._gate(seed_rules, "underwriting", applicant) == "REJECT"
        # The other gates still clear — the failure is isolated.
        for cat in ("kyc", "fraud", "affordability"):
            assert self._gate(seed_rules, cat, applicant) == "APPROVE"

    def test_missing_gate_data_defaults_to_review(self, seed_rules) -> None:
        # No fraud fields at all: the fraud gate can't clear or reject —
        # it falls to the default (REVIEW). Fail-safe, not fail-open.
        applicant = {k: v for k, v in CLEAN_APPLICANT.items()
                     if k not in ("fraud_score", "on_watchlist")}
        assert self._gate(seed_rules, "fraud", applicant) == "REVIEW"


# ----------------------------------------------------------------------------
# Score mode — weighted aggregation over the same seed rules
# ----------------------------------------------------------------------------


SCORE_POLICY = DecisionPolicy(
    mode="score",
    default="REJECT",
    thresholds=[(80, "APPROVE"), (20, "REVIEW"), (-1e9, "REJECT")],
)


class TestSeedScoreScenarios:
    def test_clean_profile_scores_into_approve(self, seed_rules) -> None:
        d = evaluate(dict(CLEAN_APPLICANT), seed_rules, SCORE_POLICY)
        # kyc_pass 20 + fraud_pass 20 + uw_approve 40 + afford_approve 35 = 115.
        assert d.score == pytest.approx(115)
        assert d.decision == "APPROVE"

    def test_poor_credit_drags_score_below_review(self, seed_rules) -> None:
        d = evaluate({**CLEAN_APPLICANT, "credit_score": 550}, seed_rules, SCORE_POLICY)
        # 20 + 20 + 35 - 60 = 15, under the REVIEW band (>=20) -> REJECT.
        assert d.score == pytest.approx(15)
        assert d.decision == "REJECT"

    def test_watchlist_hit_scores_deep_negative(self, seed_rules) -> None:
        d = evaluate({**CLEAN_APPLICANT, "on_watchlist": True}, seed_rules, SCORE_POLICY)
        assert d.score < 0
        assert d.decision == "REJECT"
