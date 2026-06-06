"""LLM client boundary (L1/L5/L8b).

The LLM is always behind this typed interface and is **optional**: with no client
(``NullLLM``) the engine runs the fully deterministic path. The LLM may only
(a) enrich query understanding (paraphrase the rules miss) and (b) write prose
constrained to facts already produced by the deterministic layers — never
introduce numbers (enforced by the numeric guard, see safety.py).
"""

from __future__ import annotations

import json
import os
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, schema: dict) -> Optional[dict]:
        """Return a JSON object constrained to ``schema``, or None on failure."""

    def complete_text(self, system: str, user: str) -> Optional[str]:
        """Return free text, or None on failure."""


class NullLLM:
    """Default no-op client — forces the deterministic path."""

    def complete_json(self, system: str, user: str, schema: dict) -> Optional[dict]:
        return None

    def complete_text(self, system: str, user: str) -> Optional[str]:
        return None


class OpenAILLM:
    """Thin, lazy OpenAI-backed client. Not exercised in tests (no network).

    Uses structured outputs for JSON and plain completions for text. Any failure
    degrades to None so the engine falls back to deterministic behavior.
    """

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None) -> None:
        self.model = model
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._client = None

    def _ensure(self):
        if self._client is None:
            from openai import OpenAI  # imported lazily
            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def complete_json(self, system: str, user: str, schema: dict) -> Optional[dict]:
        try:
            client = self._ensure()
            resp = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                temperature=0,
            )
            return json.loads(resp.choices[0].message.content)
        except Exception:
            return None

    def complete_text(self, system: str, user: str) -> Optional[str]:
        try:
            client = self._ensure()
            resp = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=0.2,
            )
            return resp.choices[0].message.content
        except Exception:
            return None
