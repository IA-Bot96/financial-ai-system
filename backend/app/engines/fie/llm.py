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

_MISS = object()  # cache sentinel (distinguish "no entry" from a cached None)


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

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None,
                 *, max_input_chars: int = 24_000, max_output_tokens: int = 800,
                 cache_max: int = 256) -> None:
        self.model = model
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._client = None
        # cost guards: truncate oversized prompts, cap completion length
        self.max_input_chars = max_input_chars
        self.max_output_tokens = max_output_tokens
        # bounded in-process response cache: identical prompts (json is temperature=0,
        # i.e. deterministic) reuse the prior completion instead of re-billing the API.
        self.cache_max = cache_max
        self._cache: dict[tuple, object] = {}

    def _clip(self, text: str) -> str:
        if text and len(text) > self.max_input_chars:
            return text[:self.max_input_chars]
        return text

    def _cache_get(self, key: tuple):
        return self._cache.get(key, _MISS)

    def _cache_put(self, key: tuple, value) -> None:
        if value is None:
            return                                  # don't cache failures (allow retry)
        if len(self._cache) >= self.cache_max:
            self._cache.pop(next(iter(self._cache)))  # simple FIFO eviction
        self._cache[key] = value

    def _ensure(self):
        if self._client is None:
            from openai import OpenAI  # imported lazily
            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def complete_json(self, system: str, user: str, schema: dict) -> Optional[dict]:
        key = ("json", system, user, repr(sorted((schema or {}).items())))
        hit = self._cache_get(key)
        if hit is not _MISS:
            return hit
        try:
            client = self._ensure()
            resp = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": self._clip(system)},
                          {"role": "user", "content": self._clip(user)}],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=self.max_output_tokens,
            )
            out = json.loads(resp.choices[0].message.content)
            self._cache_put(key, out)
            return out
        except Exception:
            return None

    def complete_text(self, system: str, user: str) -> Optional[str]:
        key = ("text", system, user)
        hit = self._cache_get(key)
        if hit is not _MISS:
            return hit
        try:
            client = self._ensure()
            resp = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": self._clip(system)},
                          {"role": "user", "content": self._clip(user)}],
                temperature=0.2,
                max_tokens=self.max_output_tokens,
            )
            out = resp.choices[0].message.content
            self._cache_put(key, out)
            return out
        except Exception:
            return None
