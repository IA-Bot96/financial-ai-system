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

    def web_search(self, query: str) -> Optional[dict]:
        """Hosted open-web search. Return {'text': str, 'sources': [{url,title,snippet}]} or None."""


class NullLLM:
    """Default no-op client — forces the deterministic path."""

    def complete_json(self, system: str, user: str, schema: dict) -> Optional[dict]:
        return None

    def complete_text(self, system: str, user: str) -> Optional[str]:
        return None

    def web_search(self, query: str) -> Optional[dict]:
        return None


class OpenAILLM:
    """Thin, lazy OpenAI-backed client. Not exercised in tests (no network).

    Uses structured outputs for JSON and plain completions for text. Any failure
    degrades to None so the engine falls back to deterministic behavior.
    """

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None,
                 *, max_input_chars: int = 24_000, max_output_tokens: int = 800,
                 json_temperature: float = 0.0, text_temperature: float = 0.2,
                 seed: int | None = 7, cache_max: int = 256) -> None:
        self.model = model
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._client = None
        # cost guards: truncate oversized prompts, cap completion length
        self.max_input_chars = max_input_chars
        self.max_output_tokens = max_output_tokens
        # sampling temperature (config-driven, not hardcoded): json = structured decisions,
        # text = answer phrasing. Models that reject custom temperature fall back to default.
        self.json_temperature = json_temperature
        self.text_temperature = text_temperature
        # bounded in-process response cache: identical prompts (json is temperature=0,
        # i.e. deterministic) reuse the prior completion instead of re-billing the API.
        self.cache_max = cache_max
        self._cache: dict[tuple, object] = {}
        # last caught error — readable by the debug recorder so the dump shows the actual
        # error message instead of bare null when complete_json/complete_text fail.
        self.last_error: str | None = None
        # A fixed seed makes outputs reproducible run-to-run (the main determinism lever for a
        # model forced to temperature=1, e.g. gpt-5-mini). Sent alongside temperature; both are
        # param-tolerant — a model that rejects either gets it stripped + a one-time retry.
        self.seed = seed
        self._omit_temperature = False
        self._omit_seed = False
        # cumulative token usage across this client's lifetime. The controller snapshots the
        # delta around a single query to bill that query's cost (see pricing.estimate_cost).
        # Cache hits cost nothing, so they bump `cached_calls` only — never the token counts.
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "cached_calls": 0}
        _log.info(
            "OpenAILLM ready: model=%s key_set=%s max_input_chars=%d max_output_tokens=%d",
            self.model, bool(self._api_key), self.max_input_chars, self.max_output_tokens,
            extra={"component": "LLM"},
        )

    def _clip(self, text: str) -> str:
        if text and len(text) > self.max_input_chars:
            return text[:self.max_input_chars]
        return text

    def _record_usage(self, usage) -> None:
        """Accumulate one API response's token usage. Normalizes both shapes: Chat Completions
        (`prompt_tokens`/`completion_tokens`) and the Responses API (`input_tokens`/`output_tokens`)."""
        if not usage:
            return
        prompt = getattr(usage, "prompt_tokens", None)
        if prompt is None:
            prompt = getattr(usage, "input_tokens", 0)
        completion = getattr(usage, "completion_tokens", None)
        if completion is None:
            completion = getattr(usage, "output_tokens", 0)
        self.usage["prompt_tokens"] += int(prompt or 0)
        self.usage["completion_tokens"] += int(completion or 0)
        self.usage["calls"] += 1

    def usage_snapshot(self) -> dict:
        """A copy of the cumulative usage counters (so callers can diff start vs end)."""
        return dict(self.usage)

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

    def _create(self, *, messages, temperature, response_format=None):
        """chat.completions.create with PARAM tolerance: send temperature (determinism) and a
        fixed seed (reproducibility), but if the model 400s rejecting either, strip the offending
        one and retry. Sticky flags mean we strip it for the rest of the run — so the same code
        works on gpt-4o*, gpt-5.4-mini, gpt-5-mini, … without ever failing on an unsupported param."""
        client = self._ensure()

        def _kwargs():
            kw = {"model": self.model, "messages": messages,
                  "max_completion_tokens": self.max_output_tokens}
            if response_format is not None:
                kw["response_format"] = response_format
            if temperature is not None and not self._omit_temperature:
                kw["temperature"] = temperature
            if self.seed is not None and not self._omit_seed:
                kw["seed"] = self.seed
            return kw

        for _ in range(3):  # at most: strip temperature, strip seed, then succeed
            try:
                return client.chat.completions.create(**_kwargs())
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if "temperature" in msg and not self._omit_temperature:
                    self._omit_temperature = True
                    _log.info("OpenAI: model %s rejects a custom temperature — using the default",
                              self.model, extra={"component": "LLM"})
                    continue
                if "seed" in msg and not self._omit_seed:
                    self._omit_seed = True
                    _log.info("OpenAI: model %s rejects a seed — dropping it (less reproducible)",
                              self.model, extra={"component": "LLM"})
                    continue
                raise
        # all tolerable params stripped — final attempt (let any real error propagate)
        return client.chat.completions.create(**_kwargs())

    def complete_json(self, system: str, user: str, schema: dict) -> Optional[dict]:
        key = ("json", system, user, repr(sorted((schema or {}).items())))
        hit = self._cache_get(key)
        if hit is not _MISS:
            self.usage["cached_calls"] += 1
            _log.debug("OpenAI complete_json: cache hit", extra={"component": "LLM"})
            return hit
        try:
            resp = self._create(
                messages=[{"role": "system", "content": self._clip(system)},
                          {"role": "user", "content": self._clip(user)}],
                response_format={"type": "json_object"},
                temperature=self.json_temperature,
            )
            out = _loads_lenient(resp.choices[0].message.content)
            usage = resp.usage
            self._record_usage(usage)
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

    def web_search(self, query: str) -> Optional[dict]:
        """TERMINAL last-resort open-web search via the Responses API's hosted `web_search` tool
        (verified to work on gpt-5.4-mini). Returns the model's grounded summary plus the cited
        sources (url + title + the surrounding text), so the caller can register them as external
        EvidenceItems and the numeric guard can admit any figure quoted verbatim from a source.
        Degrades to None on any failure so the controller falls back to an honest 'not found'."""
        key = ("web", query)
        hit = self._cache_get(key)
        if hit is not _MISS:
            self.usage["cached_calls"] += 1
            _log.debug("OpenAI web_search: cache hit", extra={"component": "LLM"})
            return hit
        try:
            client = self._ensure()
            resp = client.responses.create(
                model=self.model,
                tools=[{"type": "web_search"}],
                input=("Search the web and answer concisely with figures and dates where available. "
                       f"Query: {self._clip(query)}"),
            )
            text = getattr(resp, "output_text", None) or ""
            sources: list[dict] = []
            seen: set[str] = set()
            for item in getattr(resp, "output", []) or []:
                if getattr(item, "type", None) != "message":
                    continue
                for chunk in getattr(item, "content", []) or []:
                    snippet = getattr(chunk, "text", None) or ""
                    for ann in getattr(chunk, "annotations", []) or []:
                        url = getattr(ann, "url", None)
                        if not url or url in seen:
                            continue
                        seen.add(url)
                        sources.append({"url": url, "title": getattr(ann, "title", None) or url,
                                        "snippet": snippet})
            out = {"text": text, "sources": sources}
            self._record_usage(getattr(resp, "usage", None))
            _log.info("OpenAI web_search ok: model=%s sources=%d", self.model, len(sources),
                      extra={"component": "LLM"})
            self._cache_put(key, out)
            return out
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            _log.warning("OpenAI web_search failed: %s  [model=%s]", self.last_error, self.model,
                         extra={"component": "LLM"})
            return None

    def complete_text(self, system: str, user: str) -> Optional[str]:
        key = ("text", system, user)
        hit = self._cache_get(key)
        if hit is not _MISS:
            self.usage["cached_calls"] += 1
            _log.debug("OpenAI complete_text: cache hit", extra={"component": "LLM"})
            return hit
        try:
            resp = self._create(
                messages=[{"role": "system", "content": self._clip(system)},
                          {"role": "user", "content": self._clip(user)}],
                temperature=self.text_temperature,
            )
            out = resp.choices[0].message.content
            usage = resp.usage
            self._record_usage(usage)
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
