"""API register (L2/L3b) — declarative catalog of external APIs.

Each entry carries (a) shortlisting metadata — description + ``provides`` tags so the
planner can pick the right API(s) for a query, and (b) calling metadata — endpoint,
method, content type, parameters (defaults + which are filled from the query), the
response type, and the parser name. ``shortlist()`` ranks APIs by relevance.

This is reference/selection metadata; the resilient client (base.py) does the actual
calling and the named parser (parsers.py) does the actual parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import parsers


@dataclass(frozen=True)
class ApiInfo:
    name: str
    description: str
    category: str                      # market | disclosures | reference | macro | fundamentals
    provides: tuple[str, ...]          # tags used for shortlisting
    endpoint: str
    method: str = "GET"
    content_type: str = "json"         # POST encoding when method == POST
    response_type: str = "json"        # json | html | xlsx
    params: dict = field(default_factory=dict)        # default/static params
    dynamic_params: tuple[str, ...] = ()              # filled from the query (symbol/query/year/dates)
    parser: str = ""                   # key into parsers.PARSERS

    @property
    def parser_fn(self):
        return parsers.PARSERS.get(self.parser)


REGISTRY: list[ApiInfo] = [
    ApiInfo(
        name="symbols_master", category="reference",
        description="Master registry of PSX-listed securities: symbols, company names, "
                    "sector classifications, ETF/debt/GEM indicators.",
        provides=("symbol", "ticker", "company name", "sector", "listing", "registry"),
        endpoint="https://dps.psx.com.pk/symbols", method="GET", response_type="json",
        parser="symbols_master"),
    ApiInfo(
        name="company_announcements", category="disclosures",
        description="Official PSX company announcements and disclosures: operational "
                    "updates, expansions, board meetings, dividends, acquisitions, "
                    "regulatory disclosures, strategic developments. Keyword/company filter.",
        provides=("announcement", "disclosure", "dividend", "board meeting", "acquisition",
                  "material information", "corporate action", "news"),
        endpoint="https://dps.psx.com.pk/announcements", method="POST", content_type="form",
        response_type="html",
        params={"type": "C", "count": 50, "offset": 0, "page": "annc"},
        dynamic_params=("symbol", "query", "date_from", "date_to"),
        parser="company_announcements"),
    ApiInfo(
        name="secp_notices", category="disclosures",
        description="SECP regulatory notices, compliance updates, governance directives, "
                    "enforcement actions, disclosure requirements, market regulation.",
        provides=("secp", "regulatory", "compliance", "governance", "enforcement", "notice"),
        endpoint="https://dps.psx.com.pk/announcements", method="POST", content_type="form",
        response_type="html",
        params={"type": "B", "count": 50, "offset": 0, "page": "annc"},
        dynamic_params=("symbol", "query", "date_from", "date_to"),
        parser="secp_notices"),
    ApiInfo(
        name="company_overview", category="fundamentals",
        description="Company-level intelligence: market data, profile, financial "
                    "statements, ratios, announcements, equity, payouts, trading stats, "
                    "historical performance for a symbol.",
        provides=("company profile", "financials", "ratios", "market data", "payouts",
                  "equity", "overview"),
        endpoint="https://dps.psx.com.pk/company/{symbol}", method="GET", response_type="html",
        dynamic_params=("symbol",), parser="company_overview"),
    ApiInfo(
        name="company_payouts", category="fundamentals",
        description="Company payout/distribution info: dividends, bonus issues, payout "
                    "frequency, result-linked distributions, book closure dates.",
        provides=("dividend", "payout", "bonus", "distribution", "book closure"),
        endpoint="https://dps.psx.com.pk/company/payouts", method="POST", content_type="form",
        response_type="html", dynamic_params=("symbol",), parser="company_payouts"),
    ApiInfo(
        name="market_watch", category="market",
        description="Live stock market trading data for listed securities: prices, daily "
                    "movement, sector, trading volume.",
        provides=("price", "quote", "trading", "volume", "market", "live"),
        endpoint="https://dps.psx.com.pk/market-watch", method="GET", response_type="html",
        parser="market_watch"),
    ApiInfo(
        name="deliverable_futures_market_watch", category="market",
        description="Live deliverable futures market data: futures prices, movement, "
                    "sector, volume, derivatives activity.",
        provides=("futures", "deliverable", "derivatives", "price", "volume"),
        endpoint="https://dps.psx.com.pk/market-watch-futures", method="GET",
        response_type="html", parser="deliverable_futures_market_watch"),
    ApiInfo(
        name="cash_settled_futures_market_watch", category="market",
        description="Live cash-settled futures market data: futures prices, movement, "
                    "sector, volume, derivatives activity.",
        provides=("futures", "cash settled", "derivatives", "price", "volume"),
        endpoint="https://dps.psx.com.pk/market-watch-csf", method="GET",
        response_type="html", parser="cash_settled_futures_market_watch"),
    ApiInfo(
        name="daily_market_summary", category="market",
        description="Overall PSX market summary: exchange status, breadth, index "
                    "performance, sector activity, liquidity, trading statistics.",
        provides=("market summary", "index", "breadth", "sentiment", "liquidity"),
        endpoint="https://www.psx.com.pk/market-summary/", method="GET",
        response_type="html", parser="daily_market_summary"),
    ApiInfo(
        name="analysis_reports", category="fundamentals",
        description="Yearly PSX financial analysis datasets: company-level metrics, "
                    "assets, profitability, equity, sales, taxation, sectors — for "
                    "historical analysis and forecasting.",
        provides=("financial dataset", "historical", "profitability", "assets", "sales",
                  "forecasting", "sector"),
        endpoint="https://dps.psx.com.pk/download/analysis_report/year-{year}.xlsx",
        method="GET", response_type="xlsx", dynamic_params=("year",),
        parser="analysis_reports"),
    ApiInfo(
        name="sector_summary", category="market",
        description="Sector-wise market activity: advancing/declining stocks, turnover, "
                    "market capitalization, breadth, participation — for sector "
                    "performance and sentiment.",
        provides=("sector", "sentiment", "breadth", "turnover", "market cap"),
        endpoint="https://dps.psx.com.pk/sector-summary/sectorwise", method="GET",
        response_type="html", parser="sector_summary"),
]

BY_NAME = {a.name: a for a in REGISTRY}

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for t in _TOKEN.findall((text or "").lower()):
        if len(t) <= 2:
            continue
        out.add(t)
        if t.endswith("s") and len(t) > 3:  # crude singularization (symbols->symbol)
            out.add(t[:-1])
    return out


def shortlist(text: str, *, intent: str | None = None, top_k: int = 3
              ) -> list[tuple[ApiInfo, float]]:
    """Rank APIs by relevance of their description/provides/name to the query text
    (+ optional intent). Returns up to top_k (api, score) pairs with score > 0."""
    q = _tokens(text) | _tokens(intent or "")
    scored: list[tuple[ApiInfo, float]] = []
    for api in REGISTRY:
        hay_provides = _tokens(" ".join(api.provides))
        hay_all = hay_provides | _tokens(api.description) | _tokens(api.name.replace("_", " "))
        # provides-tag hits weigh more than description hits
        score = 2.0 * len(q & hay_provides) + 1.0 * len(q & hay_all)
        if score > 0:
            scored.append((api, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]
