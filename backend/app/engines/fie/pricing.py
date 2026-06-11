"""Token -> USD cost estimation for the LLM client.

The OpenAI API returns token *counts* (usage) but never a USD figure, so the cost a query incurs
is computed here from a per-model rate table. Rates are USD per 1,000,000 tokens, split into input
(prompt) and output (completion) — the standard OpenAI billing shape.

The table below holds best-effort defaults; override or extend it without code changes via the
``FIE_MODEL_PRICING`` environment variable (a JSON object, e.g.
``{"gpt-5.4-mini": {"input": 0.25, "output": 2.0}}``) which is merged over the defaults. Because
these are rate *estimates*, every cost this module returns is flagged ``estimated`` for the UI.
"""

from __future__ import annotations

import json
import logging
import os

_log = logging.getLogger("app.engines.fie")

# USD per 1,000,000 tokens. ESTIMATES — confirm against your OpenAI billing and override via the
# FIE_MODEL_PRICING env var (JSON) rather than editing here, so deploys can correct rates freely.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.4-mini": {"input": 0.25, "output": 2.00},
    "gpt-5-mini":   {"input": 0.25, "output": 2.00},
    "gpt-4o-mini":  {"input": 0.15, "output": 0.60},
    "gpt-4o":       {"input": 2.50, "output": 10.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
}

# Used when a model name matches no table entry (exact or prefix) — a mini-tier rate so an unknown
# model still yields a plausible, non-zero estimate rather than silently billing $0.
_FALLBACK = {"input": 0.25, "output": 2.00}

_cache: dict[str, dict[str, float]] | None = None


def _table() -> dict[str, dict[str, float]]:
    """The effective rate table = defaults overlaid with any FIE_MODEL_PRICING override (cached)."""
    global _cache
    if _cache is not None:
        return _cache
    table = {k: dict(v) for k, v in DEFAULT_PRICING.items()}
    raw = os.getenv("FIE_MODEL_PRICING")
    if raw:
        try:
            override = json.loads(raw)
            if isinstance(override, dict):
                for model, rates in override.items():
                    if isinstance(rates, dict) and "input" in rates and "output" in rates:
                        table[model] = {"input": float(rates["input"]),
                                        "output": float(rates["output"])}
        except Exception as exc:  # noqa: BLE001 — bad override must not break answering
            _log.warning("FIE_MODEL_PRICING ignored (not valid JSON): %s", exc,
                         extra={"component": "LLM"})
    _cache = table
    return table


_warned_models: set[str] = set()


def _rates_for(model: str) -> dict[str, float]:
    """Look up a model's rate: exact match, else the longest table key that prefixes the model
    name (so ``gpt-4o-2024-08-06`` resolves to ``gpt-4o``), else the mini-tier fallback."""
    table = _table()
    if model in table:
        return table[model]
    prefix = [k for k in table if model.startswith(k)]
    if prefix:
        return table[max(prefix, key=len)]
    # No rate for this model — the cost chip will use a guessed mini-tier rate. Warn ONCE per
    # model so an unrecognized model's estimate is traceable (add it to DEFAULT_PRICING /
    # FIE_MODEL_PRICING to fix). Cost never blocks answering, so this is a warning, not an error.
    if model not in _warned_models:
        _warned_models.add(model)
        _log.warning("no pricing for model %r — cost estimated at the fallback rate "
                     "($%s/1M in, $%s/1M out); add it to FIE_MODEL_PRICING to correct",
                     model, _FALLBACK["input"], _FALLBACK["output"], extra={"component": "LLM"})
    return _FALLBACK


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> dict:
    """Estimate the USD cost of a call from its token counts and the model's rate table entry.

    Returns the split + total in USD plus the rates used (so the UI can show its working)."""
    rates = _rates_for(model)
    input_usd = (max(0, prompt_tokens) / 1_000_000) * rates["input"]
    output_usd = (max(0, completion_tokens) / 1_000_000) * rates["output"]
    return {
        "input_usd": round(input_usd, 6),
        "output_usd": round(output_usd, 6),
        "total_usd": round(input_usd + output_usd, 6),
        "input_rate_per_1m": rates["input"],
        "output_rate_per_1m": rates["output"],
    }


def usage_snapshot(llm) -> dict:
    """Cumulative token counters off an LLM client (the _RecordingLLM wrapper forwards them to the
    inner client). Empty dict for NullLLM, which has no usage to bill."""
    snap = getattr(llm, "usage_snapshot", None)
    if callable(snap):
        try:
            return snap()
        except Exception:  # noqa: BLE001
            pass
    u = getattr(llm, "usage", None)
    return dict(u) if isinstance(u, dict) else {}


def usage_cost(llm, before: dict):
    """Build a query's UsageCost from the DELTA in the client's cumulative counters (snapshot
    `before` the query vs now). Returns None when no LLM is present (NullLLM / deterministic path)
    so the UI shows no cost chip; a fully-cached query still yields a $0.00 chip (cached_calls>0,
    tokens=0). Returns a models.UsageCost."""
    from .models import UsageCost
    after = usage_snapshot(llm)
    if not after:
        return None  # NullLLM — nothing was billed, no chip
    pt = max(0, after.get("prompt_tokens", 0) - before.get("prompt_tokens", 0))
    ct = max(0, after.get("completion_tokens", 0) - before.get("completion_tokens", 0))
    calls = max(0, after.get("calls", 0) - before.get("calls", 0))
    cached = max(0, after.get("cached_calls", 0) - before.get("cached_calls", 0))
    if calls == 0 and cached == 0:
        return None  # the LLM was never consulted for this query
    model = getattr(llm, "model", "?")
    cost = estimate_cost(model, pt, ct)
    return UsageCost(
        model=model, prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct,
        api_calls=calls, cached_calls=cached,
        input_usd=cost["input_usd"], output_usd=cost["output_usd"], total_usd=cost["total_usd"],
        input_rate_per_1m=cost["input_rate_per_1m"], output_rate_per_1m=cost["output_rate_per_1m"],
        source="estimated",
    )
