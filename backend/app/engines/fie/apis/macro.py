"""Macro adapter (L3b) — policy rate, inflation, FX, GDP growth.

Same spec-driven, injectable-parser pattern as the PSX/News adapters. Each
indicator is normalized to a cited external EvidenceItem (percent / rate units).
Real endpoints/parsers are wired per-source; offline-testable via a fake transport.
"""

from __future__ import annotations

from typing import Callable

from .base import ApiClient, ApiSpec
from ..models import Citation, EvidenceItem

_UNIT = {"policy_rate": "percent", "inflation": "percent",
         "fx_usd_pkr": "PKR/USD", "gdp_growth": "percent"}


def _default_parser(raw):
    """Default: transport returned {indicator: value, ...} or {'indicators': {...}}."""
    if isinstance(raw, dict):
        return raw.get("indicators", raw)
    return {}


def _make_normalizer(parser: Callable, source_id: str):
    def _norm(raw, params, spec, retrieved_at):
        items = []
        for ind, val in (parser(raw) or {}).items():
            if val is None:
                continue
            cite = Citation(ref_id="C?", kind="external",
                            display=f"{source_id}: {ind}={val} (retrieved {retrieved_at})",
                            locator={"source": source_id, "indicator": ind,
                                     "retrieved_at": retrieved_at}, retrieved_at=retrieved_at)
            items.append(EvidenceItem(
                claim=f"{ind} = {val}", value=float(val), unit=_UNIT.get(ind, "value"),
                kind="external", citations=[cite], reliability=spec.reliability_rating,
                freshness=retrieved_at, as_of=retrieved_at))
        return items
    return _norm


class Macro:
    def __init__(self, client: ApiClient, *, parser: Callable | None = None,
                 base_url: str = "https://macro.example/api") -> None:
        self.client = client
        self.spec = ApiSpec(
            id="Macro.Indicators", base_url=base_url, path="indicators",
            reliability_rating=0.8, refresh_frequency="monthly", failure_mode="degrade",
            normalizer=_make_normalizer(parser or _default_parser, "Macro.Indicators"))

    def indicators(self, country: str = "PK"):
        return self.client.call(self.spec, country=country)
