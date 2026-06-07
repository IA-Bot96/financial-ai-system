"""LLM client boundary (L1/L5/L8b).

The LLM is always behind this typed interface and is **optional**: with no client
(``NullLLM``) the engine runs the fully deterministic path. The LLM may only
(a) enrich query understanding (paraphrase the rules miss) and (b) write prose
constrained to facts already produced by the deterministic layers — never
introduce numbers (enforced by the numeric guard, see safety.py).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional, Protocol, runtime_checkable

_log = logging.getLogger("app.engines.fie")

_MISS = object()  # cache sentinel (distinguish "no entry" from a cached None)


def _loads_lenient(content: Optional[str]) -> Optional[dict]:
    """Parse a JSON object from an LLM reply, tolerating common malformations.

    Even under ``response_format={"type": "json_object"}`` smaller models occasionally
    emit a markdown fence, leading prose, or a *second* object on the next line — which
    makes a strict ``json.loads`` raise ``JSONDecodeError: Extra data``. Rather than drop
    the whole response (and, for the agent, abort the run with zero tool calls), salvage
    the FIRST well-formed object via ``raw_decode``.
    """
    if not content:
        return None
    s = content.strip()
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass
    # strip a leading ```json / ``` fence if present
    if s.startswith("```"):
        s = s.lstrip("`")
        if s[:4].lower() == "json":
            s = s[4:]
    i = s.find("{")
    if i == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[i:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


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
        # last caught error — readable by the debug recorder so the dump shows the actual
        # error message instead of bare null when complete_json/complete_text fail.
        self.last_error: str | None = None
        _log.info(
            "OpenAILLM ready: model=%s key_set=%s max_input_chars=%d max_output_tokens=%d",
            self.model, bool(self._api_key), self.max_input_chars, self.max_output_tokens,
            extra={"component": "LLM"},
        )

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
            _log.debug("OpenAI complete_json: cache hit", extra={"component": "LLM"})
            return hit
        try:
            client = self._ensure()
            resp = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": self._clip(system)},
                          {"role": "user", "content": self._clip(user)}],
                response_format={"type": "json_object"},
                temperature=0,
                max_completion_tokens=self.max_output_tokens,
            )
            out = _loads_lenient(resp.choices[0].message.content)
            usage = resp.usage
            _log.debug(
                "OpenAI complete_json ok: model=%s prompt_tokens=%s completion_tokens=%s",
                self.model,
                usage.prompt_tokens if usage else "?",
                usage.completion_tokens if usage else "?",
                extra={"component": "LLM"},
            )
            self._cache_put(key, out)
            return out
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            _log.warning(
                "OpenAI complete_json failed: %s  [model=%s key_set=%s]",
                self.last_error, self.model, bool(self._api_key),
                extra={"component": "LLM"},
            )
            return None

    def complete_text(self, system: str, user: str) -> Optional[str]:
        key = ("text", system, user)
        hit = self._cache_get(key)
        if hit is not _MISS:
            _log.debug("OpenAI complete_text: cache hit", extra={"component": "LLM"})
            return hit
        try:
            client = self._ensure()
            resp = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": self._clip(system)},
                          {"role": "user", "content": self._clip(user)}],
                temperature=0.2,
                max_completion_tokens=self.max_output_tokens,
            )
            out = resp.choices[0].message.content
            usage = resp.usage
            _log.debug(
                "OpenAI complete_text ok: model=%s prompt_tokens=%s completion_tokens=%s",
                self.model,
                usage.prompt_tokens if usage else "?",
                usage.completion_tokens if usage else "?",
                extra={"component": "LLM"},
            )
            self._cache_put(key, out)
            return out
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            _log.warning(
                "OpenAI complete_text failed: %s  [model=%s key_set=%s]",
                self.last_error, self.model, bool(self._api_key),
                extra={"component": "LLM"},
            )
            return None
