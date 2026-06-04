"""OpenAI (GPT) client wrapper. Model + key are read from settings (.env).

Provides JSON and pydantic-structured completions. Tries the SDK's structured
parse helper first, then falls back to JSON mode + pydantic validation so it
works across model/SDK versions.
"""
from __future__ import annotations

import json
from typing import Type, TypeVar

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


class GPTClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.openai_api_key:
            logger.warning("OPENAI_API_KEY is empty — set it in backend/.env before running Layer 3.")
        from openai import OpenAI

        self.client = OpenAI(
            api_key=self.settings.openai_api_key,
            timeout=self.settings.openai_timeout,
            max_retries=self.settings.openai_max_retries,
        )
        self.model = self.settings.openai_model

    def complete_json(self, system: str, user: str) -> dict:
        resp = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            logger.error("GPT returned non-JSON content: %s", content[:500])
            return {}

    def complete_structured(self, system: str, user: str, schema: Type[T]) -> T:
        # Preferred: native structured parse.
        try:
            resp = self.client.beta.chat.completions.parse(
                model=self.model,
                response_format=schema,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            parsed = resp.choices[0].message.parsed
            if parsed is not None:
                return parsed
        except Exception as exc:  # noqa: BLE001
            logger.debug("Structured parse unavailable (%s); using JSON mode.", exc)

        # Fallback: JSON mode + schema hint + validation.
        schema_hint = json.dumps(schema.model_json_schema())
        data = self.complete_json(system, f"{user}\n\nReturn JSON matching this schema:\n{schema_hint}")
        return schema.model_validate(data)
