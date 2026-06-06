"""Availability-gated metric resolution + clarification (L1/L2 support).

Ported in spirit from the legacy query-engine metric resolver: layer two checks on top
of our keyword/LLM metric mapping —

  1. AVAILABILITY GATING  prefer a canonical metric that actually exists in the workbook;
                          refuse to silently answer with a high-confidence canonical that
                          isn't present (surface it as unavailable, with suggestions).
  2. CLARIFICATION        when the user's term is genuinely ambiguous (e.g. bare
                          "profit" → gross / operating / net) AND ≥2 of those are
                          available, ask which they meant instead of guessing.

Our keyword map (understanding._METRIC_KEYWORDS) stays the recall engine; this only
gates/clarifies its output against what the dataset holds.
"""

from __future__ import annotations

import re
from typing import Optional

from .understanding import _METRIC_KEYWORDS

# bare, unqualified "profit"/"earnings" is ambiguous across these canonicals
_PROFIT_QUALIFIED = re.compile(
    r"gross profit|operating profit|net (income|profit)|profit after tax|"
    r"profit before tax|\bpat\b|\bpbt\b|ebit", re.I)
_PROFIT_BARE = re.compile(r"\bprofit(s|ability)?\b", re.I)
_PROFIT_CANDIDATES = ("gross_profit", "operating_profit", "pat")


def _all_matched(query: str) -> list[str]:
    """Distinct canonicals (in map order) whose keyword pattern hits the query."""
    out: list[str] = []
    for pattern, metric in _METRIC_KEYWORDS:
        if pattern.search(query) and metric not in out:
            out.append(metric)
    return out


def resolve(query: str, candidate_metrics: list[str], available: set[str]) -> dict:
    """Resolve the query's metric against what the workbook actually holds.

    Returns {resolved, available, clarify, candidates, suggestions}:
      - resolved    canonical metric to use (an available synonym is preferred)
      - available   whether `resolved` exists in the dataset
      - clarify     True when the term is ambiguous with >=2 available candidates
      - candidates  the available canonicals to offer on a clarify
      - suggestions sorted available metrics (for an unavailable/not-found answer)
    """
    matched = _all_matched(query)
    primary = (candidate_metrics or matched or [None])[0]

    # ambiguity: bare 'profit'/'earnings' with no qualifier and >=2 available senses
    clarify, candidates = False, []
    if _PROFIT_BARE.search(query) and not _PROFIT_QUALIFIED.search(query):
        candidates = [m for m in _PROFIT_CANDIDATES if m in available]
        if len(candidates) >= 2:
            clarify = True

    # availability gating: prefer an available canonical
    resolved = primary
    avail = bool(primary) and primary in available
    if primary and not avail:
        alt = next((m for m in matched if m in available), None)
        if alt:
            resolved, avail = alt, True

    return {"resolved": resolved, "available": avail, "clarify": clarify,
            "candidates": candidates, "suggestions": sorted(available)}
