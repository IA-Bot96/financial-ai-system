"""PSX market-data adapter (L3b) — Phase 4.

Provides share price and EPS for a ticker, normalized to EvidenceItems. Demonstrates
unit/scale reconciliation: PSX reports absolute PKR, the workbook uses "Rupees in
thousand", so financial magnitudes are divided by 1000 (price/EPS are per-share and
kept as-is).
"""

from __future__ import annotations

from .base import ApiClient, ApiSpec, external_evidence


def _quote_normalizer(raw: dict, params: dict, spec: ApiSpec, retrieved_at: str):
    ticker = params.get("ticker", "?")
    items = []
    if raw.get("price") is not None:
        items.append(external_evidence(
            f"{ticker} share price = {raw['price']} PKR",
            float(raw["price"]), spec=spec, retrieved_at=retrieved_at,
            unit="PKR/share", extra_loc={"ticker": ticker, "field": "price"}))
    if raw.get("eps") is not None:
        items.append(external_evidence(
            f"{ticker} EPS = {raw['eps']} PKR",
            float(raw["eps"]), spec=spec, retrieved_at=retrieved_at,
            unit="PKR/share", extra_loc={"ticker": ticker, "field": "eps"}))
    return items


class PSX:
    def __init__(self, client: ApiClient, base_url: str = "https://psx.example/api") -> None:
        self.client = client
        self.spec = ApiSpec(
            id="PSX.Quote", base_url=base_url, path="companies/{ticker}/quote",
            reliability_rating=0.9, refresh_frequency="intraday",
            failure_mode="cache", normalizer=_quote_normalizer,
        )

    def quote(self, ticker: str):
        return self.client.call(self.spec, ticker=ticker)
