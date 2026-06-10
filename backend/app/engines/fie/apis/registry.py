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
    endpoint: str
    method: str = "GET"
    content_type: str = "json"         # POST encoding when method == POST
    response_type: str = "json"        # json | html | xlsx
    params: dict = field(default_factory=dict)        # default/static params
    dynamic_params: tuple[str, ...] = ()              # filled from the query (symbol/query/year/dates)
    parser: str = ""                   # key into parsers.PARSERS
    scope: str = "market"              # company | sector | market — selection hint
    returns: tuple[str, ...] = ()      # parsed output-model fields — sent to the planner so it
    #                                    selects an API by the metrics it ACTUALLY returns
    use_for: str = ""                  # what WE use this API for (purpose), e.g. "fetch a company's
    #                                    symbol, sector, competitors" — sent to the planner as intent

    @property
    def parser_fn(self):
        return parsers.PARSERS.get(self.parser)


REGISTRY: list[ApiInfo] = [
    ApiInfo(
        name="symbols_master", category="reference",
        description="Complete PSX symbol registry: a JSON array of EVERY listed security. Per "
                    "record — symbol (ticker), name (company/security name), sector (from "
                    "sectorName), and three boolean flags: is_etf, is_debt (TFC/sukuk/bond), "
                    "is_gem (GEM board). Resolves a company name <-> ticker, a symbol's sector, "
                    "and same-sector peers/competitors. Carries NO prices and NO financials.",
        returns=("symbol", "name", "sector", "is_etf", "is_debt", "is_gem"),
        use_for="fetch a company's symbol (ticker), its sector, and its competitors (same-sector "
                "peers)",
        endpoint="https://dps.psx.com.pk/symbols", method="GET", response_type="json",
        parser="symbols_master"),
    ApiInfo(
        name="company_announcements", category="disclosures", scope="company",
        description="Recent official PSX disclosures filed BY a specific company (by symbol): "
                    "EOGM/board-meeting notices, dividend/corporate-action announcements, "
                    "material-information filings. Each item is a titled, dated filing with a PDF.",
        returns=("title", "date", "symbol", "pdf_url", "doc_id"),
        endpoint="https://dps.psx.com.pk/announcements", method="POST", content_type="form",
        response_type="html",
        params={"type": "C", "count": 50, "offset": 0, "page": "annc"},
        dynamic_params=("symbol", "date_from", "date_to"),
        parser="company_announcements"),
    ApiInfo(
        name="sector_announcements", category="disclosures", scope="sector",
        description="Recent PSX disclosures matched by keyword/sector (no single company): titled, "
                    "dated filings with PDFs.",
        returns=("title", "date", "symbol", "pdf_url", "doc_id"),
        endpoint="https://dps.psx.com.pk/announcements", method="POST", content_type="form",
        response_type="html",
        params={"type": "C", "count": 50, "offset": 0, "page": "annc"},
        dynamic_params=("query", "date_from", "date_to"),
        parser="sector_announcements"),
    ApiInfo(
        name="secp_notices", category="disclosures", scope="company",
        description="Recent SECP regulatory notices/orders (by symbol): titled, dated items with "
                    "a PDF — compliance, governance, enforcement.",
        returns=("title", "date", "pdf_url", "doc_id"),
        endpoint="https://dps.psx.com.pk/announcements", method="POST", content_type="form",
        response_type="html",
        params={"type": "B", "count": 50, "offset": 0, "page": "annc"},
        dynamic_params=("symbol", "date_from", "date_to"),
        parser="secp_notices"),
    ApiInfo(
        name="sector_secp_notices", category="disclosures", scope="sector",
        description="Recent SECP regulatory notices matched by keyword/sector: titled, dated items "
                    "with a PDF.",
        returns=("title", "date", "pdf_url", "doc_id"),
        endpoint="https://dps.psx.com.pk/announcements", method="POST", content_type="form",
        response_type="html",
        params={"type": "B", "count": 50, "offset": 0, "page": "annc"},
        dynamic_params=("query", "date_from", "date_to"),
        parser="sector_secp_notices"),
    ApiInfo(
        name="company_overview", category="fundamentals", scope="company",
        description="A company's profile header (by symbol): its sector and latest traded price. "
                    "Does NOT return financial statements, ratios, or payouts.",
        returns=("symbol", "name", "sector", "price"),
        endpoint="https://dps.psx.com.pk/company/{symbol}", method="GET", response_type="html",
        dynamic_params=("symbol",), parser="company_overview"),
    ApiInfo(
        name="company_payouts", category="fundamentals", scope="company",
        description="A company's recent payout EVENTS (by symbol): the date of each dividend/bonus "
                    "and its book-closure date. Flags whether each event was a dividend/bonus — "
                    "does NOT return the dividend amount or rate.",
        returns=("date", "dividend", "bonus", "book_closure"),
        endpoint="https://dps.psx.com.pk/company/payouts", method="POST", content_type="form",
        response_type="html", dynamic_params=("symbol",), parser="company_payouts"),
    ApiInfo(
        name="debt_performers", category="market",
        description="PSX's official daily DEBT performer lists: TOP ACTIVE SECURITIES (by volume) "
                    "and TOP ADVANCERS (top gainers by %) among debt instruments (GoP sukuk/bonds). "
                    "Each row: symbol, instrument name, price, change, change %, volume.",
        returns=("symbol", "name", "price", "change", "change_pct", "volume"),
        endpoint="https://dps.psx.com.pk/debt-performers", method="GET", response_type="html",
        parser="debt_performers"),
    ApiInfo(
        name="debt_market_watch", category="market",
        description="Live quote board for DEBT instruments (GoP sukuk/bonds): per-security price, "
                    "YIELD %, daily change, change %, volume. Symbols are debt codes (P01GHS...).",
        returns=("symbol", "name", "price", "yield_pct", "change", "change_pct", "volume"),
        endpoint="https://dps.psx.com.pk/market-watch-debt", method="GET", response_type="html",
        parser="debt_market_watch"),
    ApiInfo(
        name="performers", category="market",
        description="PSX's official daily performer lists: TOP ACTIVE STOCKS (by volume), TOP "
                    "ADVANCERS (top gainers by %) and TOP DECLINERS (top losers by %). Each row: "
                    "symbol, price, change, change %, volume.",
        returns=("symbol", "name", "price", "change", "change_pct", "volume"),
        endpoint="https://dps.psx.com.pk/performers", method="GET", response_type="html",
        parser="performers"),
    ApiInfo(
        name="market_watch", category="market",
        description="Live quote board for the whole market: per-security price, daily change, "
                    "and trading volume.",
        returns=("symbol", "name", "sector", "price", "change_pct", "volume"),
        endpoint="https://dps.psx.com.pk/market-watch", method="GET", response_type="html",
        parser="market_watch"),
    ApiInfo(
        name="sector_market_watch", category="market", scope="sector",
        description="Live quote board narrowed to one sector (by sector): per-security price, "
                    "daily change, volume.",
        returns=("symbol", "name", "sector", "price", "change_pct", "volume"),
        endpoint="https://dps.psx.com.pk/market-watch", method="GET", response_type="html",
        dynamic_params=("sector",), parser="sector_market_watch"),
    ApiInfo(
        name="deliverable_futures_market_watch", category="market",
        description="Live deliverable-futures quote board: per-contract price, change, volume.",
        returns=("symbol", "price", "change", "change_pct", "volume"),
        endpoint="https://dps.psx.com.pk/market-watch-futures", method="GET",
        response_type="html", parser="deliverable_futures_market_watch"),
    ApiInfo(
        name="company_deliverable_futures_market_watch", category="market", scope="company",
        description="Deliverable-futures contracts for one company (by base symbol): per-contract "
                    "price, change, volume.",
        returns=("symbol", "price", "change", "change_pct", "volume"),
        endpoint="https://dps.psx.com.pk/market-watch-futures", method="GET",
        response_type="html", dynamic_params=("symbol",),
        parser="company_deliverable_futures_market_watch"),
    ApiInfo(
        name="cash_settled_futures_market_watch", category="market",
        description="Live cash-settled-futures quote board: per-contract price, change, volume "
                    "(often empty outside active CSF sessions).",
        returns=("symbol", "price", "change", "change_pct", "volume"),
        endpoint="https://dps.psx.com.pk/market-watch-csf", method="GET",
        response_type="html", parser="cash_settled_futures_market_watch"),
    ApiInfo(
        name="daily_market_summary", category="market",
        description="Today's whole-exchange summary: exchange open/closed status, index level, "
                    "advancers/decliners breadth, and total turnover.",
        returns=("timestamp", "exchange_status", "index", "breadth", "turnover"),
        endpoint="https://www.psx.com.pk/market-summary/", method="GET",
        response_type="html", parser="daily_market_summary"),
    ApiInfo(
        name="analysis_reports", category="fundamentals",
        description="The PSX yearly fundamentals dataset (by year) for EVERY listed company, in "
                    "Rs. MILLION: sales, profit-before-tax, profit-after-tax, equity, total "
                    "assets, financial charges, dividends, plus the company's sector — enabling "
                    "sector aggregates (e.g. net/PBT margin). Has NO cost-of-sales / gross profit.",
        returns=("symbol", "name", "sector", "fiscal_year", "sales", "pbt", "pat", "equity",
                 "total_assets", "financial_charges", "cash_dividend_pct", "stock_dividend_pct",
                 "shareholders"),
        endpoint="https://dps.psx.com.pk/download/analysis_report/year-{year}.xlsx",
        method="GET", response_type="xlsx", dynamic_params=("year",),
        parser="analysis_reports"),
    ApiInfo(
        name="sector_summary", category="market",
        description="Per-SECTOR trading activity (whole market, one row per sector): advancing/"
                    "declining counts, turnover and market capitalization. No profitability.",
        returns=("sector", "sector_code", "advance", "decline", "turnover", "market_cap_b"),
        endpoint="https://dps.psx.com.pk/sector-summary/sectorwise", method="GET",
        response_type="html", parser="sector_summary"),
    ApiInfo(
        name="stock_screener", category="market", scope="company",
        description="Per-company VALUATION & liquidity snapshot (by symbol): P/E (TTM), dividend "
                    "yield %, market cap, free float, 1-year return %, 30-day avg volume, price. "
                    "Use this for a company's P/E or dividend yield. Market-derived (~5-min delay).",
        returns=("symbol", "name", "sector", "market_cap", "price", "change_pct", "change_1y_pct",
                 "pe_ratio_ttm", "dividend_yield_pct", "free_float", "volume_30d_avg"),
        endpoint="https://dps.psx.com.pk/screener", method="GET", response_type="html",
        dynamic_params=("symbol",), parser="stock_screener"),
    ApiInfo(
        name="sector_stock_screener", category="market", scope="sector",
        description="VALUATION & liquidity snapshot for every company in a SECTOR (by sector) — "
                    "for peer comparison: P/E (TTM), dividend yield %, market cap, free float, "
                    "1-year return %, 30-day avg volume, price.",
        returns=("symbol", "name", "sector", "market_cap", "price", "change_pct", "change_1y_pct",
                 "pe_ratio_ttm", "dividend_yield_pct", "free_float", "volume_30d_avg"),
        endpoint="https://dps.psx.com.pk/screener", method="GET", response_type="html",
        dynamic_params=("sector",), parser="sector_stock_screener"),
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
    """Rank APIs by relevance to the query text (+ optional intent), scored on each API's actual
    RETURN fields (weighted, the ground-truth signal), its description, and its name. The old
    `provides` tags were removed — they oversold (e.g. company_overview claimed financials/ratios
    it never returns), so selection is now based on what the API truly yields."""
    q = _tokens(text) | _tokens(intent or "")
    scored: list[tuple[ApiInfo, float]] = []
    for api in REGISTRY:
        hay_returns = _tokens(" ".join(api.returns).replace("_", " "))
        hay_all = hay_returns | _tokens(api.description) | _tokens(api.name.replace("_", " "))
        # return-field hits weigh more (they're the API's true capability)
        score = 2.0 * len(q & hay_returns) + 1.0 * len(q & hay_all)
        if score > 0:
            scored.append((api, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]
