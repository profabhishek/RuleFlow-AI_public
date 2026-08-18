# ============================================================
# FILE   : app/services/llm/stub.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: Offline, deterministic LLMProvider. Default when no LLM_API_KEY
#          is configured, so NL endpoints work without network/vendor keys
#          for local dev, tests, and demo fallback.
# ============================================================
from __future__ import annotations

import json
import re

from pydantic import BaseModel

#: Last-resort pattern: an identifier followed closely by a number. Only
#: used when the prompt doesn't tell us which fields actually exist.
_NUMBER_RE = re.compile(r"(?P<field>[a-zA-Z_][a-zA-Z0-9_]*)\D{0,5}?(?P<value>-?\d+(?:\.\d+)?)")

#: The router appends "Known fields: a, b, c." to the extraction prompt.
_KNOWN_FIELDS_RE = re.compile(r"Known fields:\s*(?P<fields>[^\n.]+)")

#: Words that negate a boolean mention, e.g. "not on a watchlist",
#: "identity is unverified", "no fraud flags".
_NEGATIONS = ("not ", "n't ", "no ", "never ", "un", "isn't", "without ")

#: Phrasings that imply age without naming the field, e.g. "16 year old".
_AGE_PHRASE_RE = re.compile(
    r"(?P<value>\d{1,3})\s*[-\s]?\s*year[s]?[-\s]?old", re.IGNORECASE
)


class StubProvider:
    """Naive keyword/regex extraction. No network calls, no external deps.

    Good enough to exercise the NL endpoints end-to-end in dev/CI; swap in a
    real LLMProvider (same interface) once a vendor is chosen.
    """

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
        json_mode: bool = False,
    ) -> str:
        # schema/json_mode unused: this provider already emits clean JSON text.
        if "Return a rule extracted" in system:
            return self._rule_from_text(user)
        if "Return a JSON object of request fields" in system:
            return self._request_from_text(user, system)
        return self._explain(user)

    def _rule_from_text(self, text: str) -> str:
        fields = {m.group("field"): float(m.group("value")) for m in _NUMBER_RE.finditer(text)}
        conditions = [
            {"field": field, "operator": ">=", "value": value}
            for field, value in fields.items()
        ]
        outcome = "APPROVE" if "approve" in text.lower() else "REVIEW"
        rule = {
            "id": "stub-" + re.sub(r"\W+", "-", text.strip().lower())[:40],
            "description": text.strip(),
            "conditions": conditions or [{"field": "value", "operator": ">=", "value": 0}],
            "logic": "AND",
            "outcome": outcome,
        }
        return json.dumps(rule)

    def _request_from_text(self, text: str, system: str = "") -> str:
        """Extract request fields, guided by the known field names if given.

        The naive "identifier immediately before a number" heuristic is a
        poor fit for real sentences: "A 16 year old ... credit score 720
        ... fraud score 12" yielded `{"A": 16, "score": 12}` — the leading
        article became a field, and the two "... score" phrases collided so
        the later one silently overwrote the earlier. Since the router now
        tells us which fields actually exist, match those directly and only
        fall back to the old heuristic when the list isn't available.
        """
        known = self._known_fields(system)
        if not known:
            fields = {
                m.group("field"): float(m.group("value"))
                for m in _NUMBER_RE.finditer(text)
            }
            return json.dumps(fields)

        extracted: dict[str, object] = {}
        for field in known:
            value = self._extract_field(field, text)
            if value is not None:
                extracted[field] = value
        return json.dumps(extracted)

    @staticmethod
    def _known_fields(system: str) -> list[str]:
        match = _KNOWN_FIELDS_RE.search(system)
        if not match:
            return []
        return [f.strip() for f in match.group("fields").split(",") if f.strip()]

    @staticmethod
    def _field_pattern(field: str, *, reverse: bool = False) -> str:
        """Regex matching a field name written as prose or as an identifier.

        Tolerates small connecting words between the parts, so
        ``on_watchlist`` matches "on a watchlist" and ``identity_verified``
        matches "identity has not been verified".
        """
        words = [w for w in field.split("_") if w]
        if reverse:
            words = list(reversed(words))
        joiner = r"[\s_]+(?:(?:a|an|the|any|is|are|was|were|been|has|have|not|no|of)[\s_]+){0,3}"
        return r"\b" + joiner.join(re.escape(w) for w in words) + r"\b"

    def _extract_field(self, field: str, text: str) -> object | None:
        """Find one known field's value in free text, or None if absent.

        Accepts the field written either as an identifier (`credit_score`)
        or in prose (`credit score`), and understands negated boolean
        mentions ("not on a watchlist" -> False).
        """
        pattern = self._field_pattern(field)
        # Numbers can sit on either side of the field name ("credit score of
        # 720" / "a 720 credit score"), so try both and keep whichever sits
        # CLOSER to the mention. Without the distance check, "a 720 credit
        # score and a 28 dti" wrongly read forward past "and a" and gave
        # credit_score = 28. The gaps exclude , ; . so a value from the next
        # clause is never captured at all.
        gap = r"[^\d,;.]{0,15}?"
        candidates: list[tuple[int, float]] = []

        forward = re.search(rf"{pattern}({gap})(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
        if forward:
            candidates.append((len(forward.group(1)), float(forward.group(2))))

        backward = re.search(rf"(-?\d+(?:\.\d+)?)({gap}){pattern}", text, re.IGNORECASE)
        if backward:
            candidates.append((len(backward.group(2)), float(backward.group(1))))

        if candidates:
            return min(candidates)[1]

        # 3. Age has a very common prose form that never names the field.
        if field == "age":
            age_phrase = _AGE_PHRASE_RE.search(text)
            if age_phrase:
                return float(age_phrase.group("value"))

        # 4. Boolean mention: the field's words appear with no number, so
        #    treat it as a flag and let negation set the polarity. Only for
        #    multi-word fields — a bare "age" with no number shouldn't
        #    become `age: true`.
        if "_" not in field:
            return None
        mentioned = re.search(pattern, text, re.IGNORECASE) or re.search(
            self._field_pattern(field, reverse=True), text, re.IGNORECASE
        )
        if mentioned:
            return not self._is_negated(text, mentioned.start(), mentioned.group(0))
        return None

    @staticmethod
    def _is_negated(text: str, position: int, matched: str) -> bool:
        """True if the mention is negated, before it or within it.

        Both placements occur in natural phrasing: "**not** on a watchlist"
        (before) and "identity has **not** been verified" (inside).
        """
        window = text[max(0, position - 22) : position].lower()
        return any(n in window for n in _NEGATIONS) or any(
            n in matched.lower() for n in _NEGATIONS
        )

    def _explain(self, decision_json: str) -> str:
        try:
            decision = json.loads(decision_json)
        except (json.JSONDecodeError, TypeError):
            return "Unable to summarize decision."
        matched = ", ".join(decision.get("rules_matched", [])) or "none"
        return (
            f"Decision: {decision.get('decision', 'UNKNOWN')} "
            f"(confidence {decision.get('confidence', 0)}). "
            f"Matched rules: {matched}."
        )
