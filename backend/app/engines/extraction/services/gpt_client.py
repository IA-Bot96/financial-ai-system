"""OpenAI (GPT) client wrapper. Model + key are read from settings (.env).

Provides JSON and pydantic-structured completions. Tries the SDK's structured
parse helper first, then falls back to JSON mode + pydantic validation so it
works across model/SDK versions.
"""
from __future__ import annotations

import base64
import json
from typing import Type, TypeVar

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


def _user_content(user: str, images: list[bytes] | None, detail: str = "high"):
    """Build the user message content. Text-only -> a plain string (unchanged behaviour);
    with images -> a multimodal content-block list (text first, then each PNG as a data
    URL). Keeping the text alongside the image lets GPT use the image to resolve OCR
    ambiguity while still reading exact figures from the text."""
    if not images:
        return user
    blocks: list[dict] = [{"type": "text", "text": user}]
    for png in images:
        b64 = base64.b64encode(png).decode("ascii")
        blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": detail},
        })
    return blocks


class GPTClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.openai_api_key:
            logger.warning("OPENAI_API_KEY is empty — set it in backend/.env before interpretation.")
        from openai import OpenAI

        self.client = OpenAI(
            api_key=self.settings.openai_api_key,
            timeout=self.settings.openai_timeout,
            max_retries=self.settings.openai_max_retries,
        )
        self.model = self.settings.openai_model

    def complete_json(self, system: str, user: str, images: list[bytes] | None = None) -> dict:
        resp = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": _user_content(user, images, self.settings.vision_detail)},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            logger.error("GPT returned non-JSON content: %s", content[:500])
            return {}

    def complete_structured(self, system: str, user: str, schema: Type[T],
                            images: list[bytes] | None = None) -> T:
        # Preferred: native structured parse.
        try:
            resp = self.client.beta.chat.completions.parse(
                model=self.model,
                response_format=schema,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": _user_content(user, images, self.settings.vision_detail)},
                ],
            )
            parsed = resp.choices[0].message.parsed
            if parsed is not None:
                return parsed
        except Exception as exc:  # noqa: BLE001
            logger.debug("Structured parse unavailable (%s); using JSON mode.", exc)

        # Fallback: JSON mode + schema hint + validation (image passes through too).
        schema_hint = json.dumps(schema.model_json_schema())
        data = self.complete_json(
            system, f"{user}\n\nReturn JSON matching this schema:\n{schema_hint}", images=images)
        return schema.model_validate(data)
