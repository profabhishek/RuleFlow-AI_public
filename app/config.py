# ============================================================
# FILE   : app/config.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: Settings from env vars (no internal deps)      [R2]
# ============================================================
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./ruleflow.db"
    log_level: str = "INFO"
    rules_path: str = "rules/rules.json"
    decision_mode: str = "priority"
    default_outcome: str = "REVIEW"
    api_key: str = ""

    # NL layer (Role 2).
    # llm_provider is a comma-separated chain tried in order on failure, e.g.
    # "gemini,openrouter" (Gemini primary, OpenRouter backup for Gemini's hard
    # daily limits). A single value ("gemini", "stub") works as before. Add
    # "stub" last for an always-available offline terminal fallback.
    llm_provider: str = "stub"
    llm_model: str = ""  # Gemini model (LLM_MODEL); openrouter has its own below

    # Gemini: llm_api_key is kept as a backward-compatible alias for the key.
    llm_api_key: str = ""
    gemini_api_key: str = ""

    # OpenRouter (backup). Own key/model so both providers are usable at once.
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct"

    #: Ask the LLM to rewrite each decision in plain English.
    #: Off by default: it doubles LLM calls per /decide/query (one to
    #: extract fields, one to narrate the result) and the engine already
    #: returns a precise, deterministic explanation naming the deciding
    #: rule and every condition that passed. On a 20-requests/day free
    #: tier that halving is the difference between ~10 and ~20 demo
    #: queries. Set NL_EXPLANATIONS=true to enable the nicer prose.
    nl_explanations: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
