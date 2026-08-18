# ============================================================
# FILE   : app/services/llm/gemini.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: Google Gemini-backed LLMProvider (google-genai SDK).
# ============================================================
from __future__ import annotations

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import get_settings

_DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiProvider:
    """LLMProvider backed by the Gemini API.

    Selected via LLM_PROVIDER=gemini; requires LLM_API_KEY. LLM_MODEL
    overrides the default model if set.
    """

    def __init__(self) -> None:
        settings = get_settings()
        # GEMINI_API_KEY is preferred; LLM_API_KEY stays a back-compat alias.
        api_key = settings.gemini_api_key or settings.llm_api_key
        if not api_key:
            raise ValueError(
                "gemini provider requires GEMINI_API_KEY (or LLM_API_KEY) "
                "to be set (see .env.example)."
            )
        self._client = genai.Client(api_key=api_key)
        self._model = settings.llm_model or _DEFAULT_MODEL

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
        json_mode: bool = False,
    ) -> str:
        config = types.GenerateContentConfig(system_instruction=system)
        if schema is not None:
            # Structured output: Gemini returns JSON matching `schema` exactly,
            # no markdown fences, no prose — nothing to strip/parse defensively.
            config.response_mime_type = "application/json"
            config.response_schema = schema
        elif json_mode:
            # Shape isn't fixed (e.g. arbitrary extracted field names), but
            # the result must still be valid, unfenced JSON.
            config.response_mime_type = "application/json"
        response = self._client.models.generate_content(
            model=self._model,
            contents=user,
            config=config,
        )
        return (response.text or "").strip()
