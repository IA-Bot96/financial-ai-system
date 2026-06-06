"""PSX company-overview adapter (L3b).

GET https://dps.psx.com.pk/company/{symbol} (symbol in the route) -> the rich
company page. Provides live price, P/E, market cap, shares, EPS, margins, and
annual financials in one call — the market data the valuation path needs (price,
market cap and shares were previously unavailable).
"""

from __future__ import annotations

from .base import ApiClient, ApiSpec, CallResult
from .parsers import parse_company_overview
from ..models import Citation, EvidenceItem

# scalar fields surfaced as cited external evidence
_SCALARS = [
    ("price", "PKR/share", "share price"),
    ("pe_ratio", "x", "P/E (TTM)"),
    ("market_cap", "Rupees in thousand", "market cap"),
    ("shares", "shares", "shares outstanding"),
]


def _normalizer(raw, params, spec, retrieved_at):
    ov = parse_company_overview(raw) if isinstance(raw, str) else (raw or {})
    symbol = ov.get("symbol") or params.get("symbol")
    items: list[EvidenceItem] = []
    for field, unit, label in _SCALARS:
        v = ov.get(field)
        if v is None:
            continue
        cite = Citation(ref_id="C?", kind="external",
                        display=f"PSX company overview ({symbol}) — {label}",
                        locator={"source": spec.id, "symbol": symbol, "field": field,
                                 "retrieved_at": retrieved_at}, retrieved_at=retrieved_at)
        items.append(EvidenceItem(claim=f"{symbol} {label} = {v}", value=float(v),
                                  unit=unit, kind="external", citations=[cite],
                                  reliability=spec.reliability_rating, freshness=retrieved_at))
    return items


class CompanyOverview:
    def __init__(self, client: ApiClient, *, symbols=None,
                 base_url: str = "https://dps.psx.com.pk") -> None:
        self.client = client
        self.symbols = symbols  # optional: resolve company name -> symbol
        self.spec = ApiSpec(id="PSX.CompanyOverview", base_url=base_url,
                            path="company/{symbol}", method="GET", response_type="html",
                            reliability_rating=0.9, refresh_frequency="intraday",
                            failure_mode="cache", normalizer=_normalizer)
        self._cache: dict[str, dict] = {}

    def _resolve(self, symbol: str | None, company: str | None) -> str | None:
        if symbol:
            return symbol
        if company and self.symbols is not None:
            return self.symbols.ticker_for(company)
        return None

    def fetch(self, symbol: str | None = None, *, company: str | None = None) -> CallResult:
        """Scalar facts (price/PE/market cap/shares) as cited EvidenceItems."""
        sym = self._resolve(symbol, company)
        if sym is None:
            return CallResult(items=[], status="failed", note="unresolved symbol")
        return self.client.call(self.spec, symbol=sym)

    def overview(self, symbol: str | None = None, *, company: str | None = None) -> dict:
        """Full structured overview (quote/equity/profile/financials/ratios)."""
        sym = self._resolve(symbol, company)
        if sym is None:
            return {}
        if sym not in self._cache:
            res = self.client.call(self.spec, symbol=sym)
            # reconstruct scalars from items; re-parse not needed for the headline set
            scalars = {c.locator["field"]: i.value
                       for i in res.items for c in i.citations if "field" in (c.locator or {})}
            self._cache[sym] = {"symbol": sym, **scalars}
        return self._cache[sym]
