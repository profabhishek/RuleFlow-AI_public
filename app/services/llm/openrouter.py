# ============================================================
# FILE   : app/services/llm/openrouter.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: OpenRouter-backed LLMProvider (OpenAI-compatible API). Backup for
#          Gemini's hard daily limits.
# ============================================================
from __future__ import annotations

from openai import OpenAI
from pydantic import BaseModel

from app.config import get_settings

_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    """LLMProvider backed by OpenRouter's OpenAI-compatible chat API.

    Selected via LLM_PROVIDER (e.g. "gemini,openrouter"); requires
    OPENROUTER_API_KEY. OPENROUTER_MODEL overrides the default model.
    """

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.openrouter_api_key:
            raise ValueError(
                "openrouter provider requires OPENROUTER_API_KEY (see .env.example)."
            )
        self._client = OpenAI(
            base_url=_BASE_URL, api_key=settings.openrouter_api_key
        )
        self._model = settings.openrouter_model

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
        json_mode: bool = False,
    ) -> str:
        system_prompt = system
        response_format = None
        if schema is not None:
            # Model support for strict json_schema varies across OpenRouter
            # models; the portable path is json_object mode plus the schema
            # spelled out in the prompt. Both prompts already say "JSON".
            response_format = {"type": "json_object"}
            system_prompt = (
                f"{system}\n\nReturn JSON matching this schema exactly:\n"
                f"{schema.model_json_schema()}"
            )
        elif json_mode:
            response_format = {"type": "json_object"}

        completion = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user},
            ],
            response_format=response_format,
        )
        return (completion.choices[0].message.content or "").strip()
