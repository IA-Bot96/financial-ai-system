"""Named deterministic tools (L3b) — the rule-base's TYPED function menu.

Each tool is a small deterministic function with an explicit signature (name, what it does, inputs,
outputs). The planner SELECTS a tool and FILLS its arguments — exactly like it picks a formula —
and the rule-base EVALUATES it. This is far more reliable on a small model than choosing an opaque
data source by index: the model picks a verb with named arguments and never reasons about an API.

A tool internally owns its own data source + parser + filter, and returns the usual
PrimitiveResult — ``(summary dict, evidence, calcs)`` — so its output flows through citation
binding and the verify gates unchanged. Reference tools (symbol/sector/competitors) carry NO
numbers, so their evidence values are None; they're cited to PSX.Symbols as the source.

Every PSX endpoint the planner can reach is wrapped as a named tool here; the planner selects
tools only (there is no longer an opaque API-catalog path). Tools resolve their endpoint via the
registry (apis.registry.BY_NAME) and fetch through the shared RegistryFetcher.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

from .models import Citation, EvidenceItem

_log = logging.getLogger("app.engines.fie")

# Result tuple, same contract as primitives: (summary dict for the LLM, evidence, calcs)
ToolResult = tuple[dict, list, list]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str          # what the tool DOES
    inputs: tuple[dict, ...]  # [{name, type, desc}]
    outputs: str              # what it returns
    fn: Callable              # (engine, **args) -> ToolResult


# --------------------------------------------------------------------------- shared helpers
def _syms(engine):
    """The cached PSX Symbols adapter (full listed-security registry)."""
    from .external import _adapters
    _ar, syms = _adapters(engine)
    return syms


def _cite(symbol: str | None = None):
    return Citation(ref_id="C?", kind="external", display="PSX symbol registry (dps.psx.com.pk/symbols)",
                    locator={"source": "PSX.Symbols", **({"symbol": symbol} if symbol else {})})


_SYMBOL_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)?$")


def _looks_like_symbol(text: str) -> bool:
    """Decide whether a single user-supplied token is a PSX TICKER rather than a company NAME.
    PSX tickers are short, spaceless, all-uppercase alphanumerics (optionally a futures suffix) —
    e.g. MTL, AGTL, 786, TPLRF1, AICL-JUNB; company names carry spaces / lowercase / length
    (e.g. 'Millat Tractors Limited'). Pure heuristic, used only when the registry can't confirm."""
    t = (text or "").strip()
    return bool(t) and " " not in t and len(t) <= 12 and bool(_SYMBOL_RE.match(t))


def _resolve(syms, text: str):
    """(symbol, official_name) from ONE input that may be EITHER a PSX ticker OR a company name —
    the tool decides which. An exact ticker hit in the registry always wins; otherwise the input
    is fuzzy-matched as a company name; failing both, a ticker-shaped token is accepted as-is (so
    it still resolves offline / for symbols not in the cached registry)."""
    if not text:
        return None, None
    s = text.strip()
    up = s.upper()
    if syms is not None:
        for r in syms.records():                   # exact ticker match -> definitely a symbol
            if (r.get("symbol") or "").upper() == up:
                return r.get("symbol"), r.get("name")
        sym = syms.ticker_for(s)                    # else fuzzy company-name -> ticker
        if sym:
            return sym, syms.name_for(sym)
    if _looks_like_symbol(s):                       # registry miss but looks like a ticker
        return up, None
    return None, None


# ------------------------------------------------------------------------------------- tools
def get_company_symbol(engine, company: str = "", **_) -> ToolResult:
    syms = _syms(engine)
    if syms is None:
        return {"tool": "getCompanySymbol", "company": company, "note": "symbol registry unavailable"}, [], []
    sym, name = _resolve(syms, company)
    if not sym:
        return {"tool": "getCompanySymbol", "company": company, "symbol": None,
                "note": "no matching listed company"}, [], []
    res = {"tool": "getCompanySymbol", "company": company, "name": name, "symbol": sym}
    ev = [EvidenceItem(claim=f"{name or company} trades on PSX as {sym}", kind="external",
                       citations=[_cite(sym)], reliability=0.95)]
    return res, ev, []


def get_company_sector(engine, company: str = "", **_) -> ToolResult:
    syms = _syms(engine)
    if syms is None:
        return {"tool": "getCompanySector", "company": company, "note": "symbol registry unavailable"}, [], []
    sym, name = _resolve(syms, company)
    sector = syms.sector_for(sym) if sym else None
    if not sector:
        return {"tool": "getCompanySector", "company": company, "symbol": sym, "sector": None,
                "note": "company not found in registry"}, [], []
    res = {"tool": "getCompanySector", "company": company, "name": name, "symbol": sym, "sector": sector}
    ev = [EvidenceItem(claim=f"{name or company} ({sym}) is in the {sector} sector", kind="external",
                       citations=[_cite(sym)], reliability=0.95)]
    return res, ev, []


def get_company_competitors(engine, company: str = "", **_) -> ToolResult:
    """Same-sector peers of a company (the company itself excluded)."""
    syms = _syms(engine)
    if syms is None:
        return {"tool": "getCompanyCompetitors", "company": company, "note": "symbol registry unavailable"}, [], []
    sym, name = _resolve(syms, company)
    sector = syms.sector_for(sym) if sym else None
    if not sector:
        return {"tool": "getCompanyCompetitors", "company": company, "symbol": sym,
                "competitors": [], "note": "company/sector not found in registry"}, [], []
    peers = [{"symbol": r.get("symbol"), "name": r.get("name")}
             for r in syms.records()
             if (r.get("sector") or "") == sector and (r.get("symbol") or "").upper() != (sym or "").upper()]
    res = {"tool": "getCompanyCompetitors", "company": company, "name": name, "symbol": sym,
           "sector": sector, "competitors": peers, "count": len(peers)}
    ev = [EvidenceItem(claim=f"{sector} sector has {len(peers)} other listed companies besides {sym}",
                       kind="external", citations=[_cite(sym)], reliability=0.95)]
    return res, ev, []


def get_sector_companies(engine, sector: str = "", **_) -> ToolResult:
    syms = _syms(engine)
    if syms is None:
        return {"tool": "getSectorCompanies", "sector": sector, "note": "symbol registry unavailable"}, [], []
    s = (sector or "").strip().lower()
    companies = [{"symbol": r.get("symbol"), "name": r.get("name")}
                 for r in syms.records() if (r.get("sector") or "").lower() == s]
    if not companies:
        return {"tool": "getSectorCompanies", "sector": sector, "companies": [],
                "note": "no listed companies for that sector name"}, [], []
    # `listing` is a ready-to-render "Full Name (SYMBOL)" form so the composer surfaces full names
    # (not just tickers) when asked to "list them" / "names" / "full names" — both fields are kept.
    listing = [f"{c['name']} ({c['symbol']})" if c.get("name") else (c.get("symbol") or "")
               for c in companies]
    res = {"tool": "getSectorCompanies", "sector": sector, "companies": companies,
           "count": len(companies), "listing": listing}
    ev = [EvidenceItem(claim=f"The {sector} sector has {len(companies)} listed companies",
                       kind="external", citations=[_cite()], reliability=0.95)]
    return res, ev, []


# ---------------------------------------------------------- filings: announcements + SECP notices
# The company-announcements feed (type=C) and the SECP-notices feed (type=B) share the SAME
# endpoint, table markup, and parser — they differ ONLY in the registry entry (the `type` param).
# So one generic core drives both; the four public tools are thin wrappers naming the api + label.
_FILING_FIELDS = ("date", "time", "symbol", "name", "title", "status", "pdf_url", "doc_id")


def _filings_fetcher(engine, limit: int):
    """A RegistryFetcher over the shared ApiClient with this tool's own record cap (the default
    fetcher caps at 8 — too few for a filings feed)."""
    from .external import _registry_fetcher
    from .apis.fetch import RegistryFetcher
    rf = _registry_fetcher(engine)               # ensures a cached ApiClient (shared call budget)
    return RegistryFetcher(rf.client, max_records_per_api=max(1, int(limit)))


def _filing_record(item) -> dict:
    loc = (item.citations[0].locator if getattr(item, "citations", None) else {}) or {}
    return {k: loc.get(k) for k in _FILING_FIELDS if loc.get(k) is not None}


def _fetch_filings(engine, symbol: str, *, api_name: str, limit: int) -> list:
    """ONE POST to /announcements (type=C for announcements, type=B for SECP — set by the named
    registry entry) filtered to `symbol`, parsed via the precise announcements parser. Returns the
    parsed EvidenceItems (each locator carries the full record)."""
    from .apis.registry import BY_NAME
    api = BY_NAME.get(api_name)
    if api is None:
        return []
    res = _filings_fetcher(engine, limit).fetch(api, symbol=symbol)
    return list(res.items)


def _company_filings(engine, *, company, symbol, api_name, tool_name, item_key, limit) -> ToolResult:
    syms = _syms(engine)
    sym, name = _resolve(syms, symbol or company) if syms else (symbol or None, None)
    if not sym:
        return {"tool": tool_name, "company": company or symbol,
                "note": "could not resolve a PSX symbol"}, [], []
    items = _fetch_filings(engine, sym, api_name=api_name, limit=limit)
    recs = [_filing_record(i) for i in items]
    res = {"tool": tool_name, "company": company or sym, "name": name, "symbol": sym,
           "count": len(recs), item_key: recs}
    if not recs:
        res["note"] = "none found"
    return res, items, []


def _sector_filings(engine, *, company, symbol, sector, api_name, tool_name, item_key,
                    max_companies, per_company) -> ToolResult:
    syms = _syms(engine)
    if syms is None:
        return {"tool": tool_name, "note": "symbol registry unavailable"}, [], []
    target = (sector or "").strip()
    if not target:
        sym, _n = _resolve(syms, company or symbol)
        target = syms.sector_for(sym) if sym else None
    if not target:
        return {"tool": tool_name, "company": company or symbol or sector,
                "note": "could not resolve a sector"}, [], []

    members = [r for r in syms.records() if (r.get("sector") or "").lower() == target.lower()]
    total = len(members)
    selected = members[: max(1, int(max_companies))]

    all_items, per = [], []
    for r in selected:
        items = _fetch_filings(engine, r["symbol"], api_name=api_name, limit=per_company)
        all_items += items
        per.append({"symbol": r["symbol"], "name": r["name"], "count": len(items)})

    truncated = total > len(selected)
    if truncated:
        _log.info("%s: queried %d/%d %s companies (capped at max_companies=%d)",
                  tool_name, len(selected), total, target, max_companies, extra={"component": "Fetch"})
    res = {"tool": tool_name, "sector": target, "companies_total": total,
           "companies_queried": len(selected), "truncated": truncated, "per_company": per,
           "count": len(all_items), item_key: [_filing_record(i) for i in all_items]}
    return res, all_items, []


# --- announcements (type=C) ---
def get_company_announcements(engine, company: str = "", symbol: str = "", limit: int = 15, **_) -> ToolResult:
    return _company_filings(engine, company=company, symbol=symbol, api_name="company_announcements",
                            tool_name="getCompanyAnnouncements", item_key="announcements", limit=limit)


def get_sector_announcements(engine, company: str = "", symbol: str = "", sector: str = "",
                             max_companies: int = 8, per_company: int = 5, **_) -> ToolResult:
    return _sector_filings(engine, company=company, symbol=symbol, sector=sector,
                           api_name="company_announcements", tool_name="getSectorAnnouncements",
                           item_key="announcements", max_companies=max_companies, per_company=per_company)


# --- SECP regulatory notices (type=B) ---
# CRITICAL difference from announcements: the SECP feed is MARKET-WIDE and has NO symbol/name
# column — the company appears only as free text in the TITLE. It cannot be filtered by symbol.
# It CAN be filtered server-side by the `query` param (a loose title search — e.g. "Dewan Cement"
# also returns "Dewan Automotive"), so we query by company NAME and then post-filter titles by the
# company's distinctive name tokens for precision.
_SECP_STOP = {"limited", "ltd", "company", "co", "the", "pakistan", "mills", "industries", "and",
              "corp", "corporation", "group", "holdings", "enterprises", "services", "plc",
              "pvt", "private", "modaraba", "fund", "funds"}


def _name_tokens(name: str) -> list[str]:
    """Distinctive lowercase tokens of a company name for matching it inside a SECP title."""
    return [t for t in re.split(r"[^a-z0-9]+", (name or "").lower())
            if len(t) >= 4 and t not in _SECP_STOP]


def _title_of(item) -> str:
    return ((item.citations[0].locator.get("title") if getattr(item, "citations", None) else "") or "").lower()


def _fetch_secp(engine, query: str, *, limit: int) -> list:
    """SECP notices (type=B) filtered server-side by a title `query`. Uses the query-capable
    registry entry (the symbol-based one can't filter this feed)."""
    from .apis.registry import BY_NAME
    api = BY_NAME.get("sector_secp_notices")
    if api is None or not query:
        return []
    res = _filings_fetcher(engine, limit).fetch(api, query=query)
    return list(res.items)


def get_company_secp_notices(engine, company: str = "", symbol: str = "", limit: int = 15, **_) -> ToolResult:
    """SECP regulatory orders concerning ONE company. Resolves the official name, queries the SECP
    title-search, then keeps only titles that actually mention the company (the feed has no symbol;
    matching is by name text, so a clean/large company often has NONE)."""
    syms = _syms(engine)
    sym, name = _resolve(syms, symbol or company) if syms else (None, None)
    tokens = _name_tokens(name or company or "")
    # query the DISTINCTIVE tokens, not the full "X Limited" — the loose server search would
    # otherwise match the common "Limited" token and flood with unrelated orders.
    q = " ".join(tokens) or (name or company or symbol)
    if not q:
        return {"tool": "getCompanySECPNotices", "note": "need a company name or symbol"}, [], []
    items = _fetch_secp(engine, q, limit=limit)
    matched = [it for it in items if not tokens or any(t in _title_of(it) for t in tokens)]
    recs = [_filing_record(i) for i in matched]
    res = {"tool": "getCompanySECPNotices", "company": company or sym or q, "name": name,
           "symbol": sym, "count": len(recs), "raw_matches": len(items), "notices": recs}
    if not recs:
        res["note"] = (f"no SECP regulatory orders clearly referring to {name or company or q}"
                       + (f"; the feed returned {len(items)} loose match(es) for that name"
                          if items else " (SECP is a market-wide feed; clean companies often have none)"))
    return res, matched, []


def get_sector_secp_notices(engine, company: str = "", symbol: str = "", sector: str = "",
                            max_companies: int = 8, per_company: int = 5, **_) -> ToolResult:
    """SECP regulatory orders across a sector / a company's competitors. SECP has no sector key, so
    this resolves the sector's companies and runs the SECP title-search PER COMPANY (by name),
    post-filtering each, then concatenates. Most listed companies have no orders, so results are
    typically sparse."""
    syms = _syms(engine)
    if syms is None:
        return {"tool": "getSectorSECPNotices", "note": "symbol registry unavailable"}, [], []
    target = (sector or "").strip()
    if not target:
        sym, _n = _resolve(syms, company or symbol)
        target = syms.sector_for(sym) if sym else None
    if not target:
        return {"tool": "getSectorSECPNotices", "company": company or symbol or sector,
                "note": "could not resolve a sector"}, [], []

    members = [r for r in syms.records() if (r.get("sector") or "").lower() == target.lower()]
    total = len(members)
    selected = members[: max(1, int(max_companies))]

    all_items, per = [], []
    for r in selected:
        tokens = _name_tokens(r.get("name") or "")
        q = " ".join(tokens) or (r.get("name") or r["symbol"])
        items = _fetch_secp(engine, q, limit=per_company)
        matched = [it for it in items if not tokens or any(t in _title_of(it) for t in tokens)]
        all_items += matched
        per.append({"symbol": r["symbol"], "name": r["name"], "count": len(matched)})

    truncated = total > len(selected)
    if truncated:
        _log.info("getSectorSECPNotices: queried %d/%d %s companies (capped at max_companies=%d)",
                  len(selected), total, target, max_companies, extra={"component": "Fetch"})
    res = {"tool": "getSectorSECPNotices", "sector": target, "companies_total": total,
           "companies_queried": len(selected), "truncated": truncated, "per_company": per,
           "count": len(all_items), "notices": [_filing_record(i) for i in all_items]}
    return res, all_items, []


# ---------------------------------------------------------- company overview (PSX /company/{sym})
def _ov_cite(sym: str):
    url = f"https://dps.psx.com.pk/company/{sym}"
    return Citation(ref_id="C?", kind="external", display=f"PSX company page ({sym})",
                    locator={"source": "PSX.CompanyOverview", "url": url, "link": url, "symbol": sym})


def _label_val(metrics: dict, *subs: str):
    """First value whose label contains ANY of the substrings (case-insensitive)."""
    for lab, v in (metrics or {}).items():
        ll = lab.lower()
        if any(s in ll for s in subs):
            return v
    return None


def get_company_overview(engine, company: str = "", symbol: str = "", **_) -> ToolResult:
    """Full PSX company profile for ANY listed company: live quote (price, change, P/E (TTM),
    day open/high/low/volume, 52-week range), equity (market cap, shares, free float), business
    profile (description, key people, auditor, registrar, fiscal year), and per-YEAR financials
    (sales, profit-after-tax, EPS) + ratios (GROSS & net profit margin, EPS growth, PEG)."""
    syms = _syms(engine)
    sym, name = _resolve(syms, symbol or company) if syms else (symbol or None, None)
    if not sym:
        return {"tool": "getCompanyOverview", "company": company or symbol,
                "note": "could not resolve a PSX symbol"}, [], []
    from .external import _registry_fetcher
    from .apis.registry import BY_NAME
    api = BY_NAME.get("company_overview")
    res = _registry_fetcher(engine).fetch(api, symbol=sym) if api else None
    data = (res.items[0].citations[0].locator if (res and res.items) else {}) or {}
    if not (data.get("name") or data.get("price")):
        return {"tool": "getCompanyOverview", "company": company or sym, "symbol": sym,
                "note": "company page returned no data"}, [], []

    qs = data.get("quote_stats") or {}
    ratios = data.get("ratios") or {}
    fin = data.get("financials_annual") or {}
    prof = data.get("profile") or {}
    summary = {
        "tool": "getCompanyOverview", "symbol": data.get("symbol") or sym,
        "name": data.get("name") or name, "sector": data.get("sector"),
        "price": data.get("price"), "change": data.get("change"), "change_pct": data.get("change_pct"),
        "pe_ratio_ttm": data.get("pe_ratio"), "market_cap_000s": data.get("market_cap"),
        "shares": data.get("shares"), "free_float_shares": data.get("free_float"),
        "free_float_pct": data.get("free_float_pct"),
        "day_open": qs.get("Open"), "day_high": qs.get("High"), "day_low": qs.get("Low"),
        "volume": qs.get("Volume"), "week_52_range": _label_val(qs, "52-week"),
        "change_1y_pct": _label_val(qs, "1-year"), "ytd_change_pct": _label_val(qs, "ytd"),
        "business_description": prof.get("business_description"), "key_people": prof.get("key_people"),
        "auditor": prof.get("auditor"), "registrar": prof.get("registrar"),
        "address": prof.get("address"), "website": prof.get("website"),
        "fiscal_year_end": prof.get("fiscal_year_end"),
        "financials_annual": fin, "financials_quarterly": data.get("financials_quarterly"),
        "ratios": ratios,
    }

    # number-rich evidence so the numeric guard backs every figure the answer may quote.
    ev = [EvidenceItem(
        claim=(f"{summary['name']} ({sym}) — price Rs.{data.get('price')}, "
               f"P/E(TTM) {data.get('pe_ratio')}, market cap {data.get('market_cap')} (000s), "
               f"shares {data.get('shares')}, free float {data.get('free_float')} "
               f"({data.get('free_float_pct')}%); 52-week {_label_val(qs, '52-week')}"),
        kind="external", citations=[_ov_cite(sym)], reliability=0.9)]
    for yr, m in ratios.items():
        ev.append(EvidenceItem(
            claim=(f"{sym} {yr}: gross margin {_label_val(m, 'gross')}%, "
                   f"net margin {_label_val(m, 'net profit', 'net margin')}%, "
                   f"EPS growth {_label_val(m, 'eps growth')}%, PEG {_label_val(m, 'peg')}"),
            kind="external", citations=[_ov_cite(sym)], reliability=0.9))
    for yr, m in fin.items():
        ev.append(EvidenceItem(
            claim=f"{sym} {yr}: sales {m.get('sales')}, profit after tax {m.get('pat')}, EPS {m.get('eps')}",
            kind="external", citations=[_ov_cite(sym)], reliability=0.9))
    return summary, ev, []


# ------------------------------------------------------------------- company payouts (dividends)
def _payout_cite(sym: str):
    url = f"https://dps.psx.com.pk/company/{sym}"
    return Citation(ref_id="C?", kind="external", display=f"PSX payouts ({sym})",
                    locator={"source": "PSX.CompanyPayouts", "url": url, "link": url, "symbol": sym})


def get_company_payouts(engine, company: str = "", symbol: str = "", limit: int = 20, **_) -> ToolResult:
    """A company's dividend/payout history: each entry has the announcement date, the result period,
    the payout (percent + whether interim/final and cash-dividend/bonus/right), and the book-closure
    window. Resolves the company name -> symbol, then queries the payouts feed for that symbol."""
    syms = _syms(engine)
    sym, name = _resolve(syms, symbol or company) if syms else (symbol or None, None)
    if not sym:
        return {"tool": "getCompanyPayouts", "company": company or symbol,
                "note": "could not resolve a PSX symbol"}, [], []
    from .apis.registry import BY_NAME
    api = BY_NAME.get("company_payouts")
    items = _filings_fetcher(engine, limit).fetch(api, symbol=sym).items if api else []
    fields = ("date", "financial_results", "details", "book_closure", "payout_pct",
              "interim", "final", "dividend", "bonus", "right")
    payouts, ev = [], []
    for it in items:
        loc = (it.citations[0].locator if getattr(it, "citations", None) else {}) or {}
        rec = {k: loc.get(k) for k in fields if loc.get(k) is not None}
        if not rec:
            continue
        payouts.append(rec)
        tag = "interim" if rec.get("interim") else ("final" if rec.get("final") else "")
        ev.append(EvidenceItem(
            claim=(f"{sym} payout {rec.get('payout_pct')}% {tag} "
                   f"{', '.join(l for k, l in (('dividend','cash dividend'),('bonus','bonus shares'),('right','right shares')) if rec.get(k))} "
                   f"— announced {rec.get('date')}, period {rec.get('financial_results')}, "
                   f"book closure {rec.get('book_closure')}").strip(),
            kind="external", citations=[_payout_cite(sym)], reliability=0.9))
    res = {"tool": "getCompanyPayouts", "company": company or sym, "name": name, "symbol": sym,
           "count": len(payouts), "payouts": payouts}
    if not payouts:
        res["note"] = "no payout history found"
    return res, ev, []


# --------------------------------------------------------------- market watch (live quote board)
_MW_FIELDS = ("symbol", "name", "sector", "sector_code", "price", "ldcp", "open", "high", "low",
              "change", "change_pct", "volume", "status", "is_etf", "listed_in")


def _mw_cite():
    url = "https://dps.psx.com.pk/market-watch"
    return Citation(ref_id="C?", kind="external", display="PSX market watch",
                    locator={"source": "PSX.MarketWatch", "url": url, "link": url})


def _mw_record(item) -> dict:
    loc = (item.citations[0].locator if getattr(item, "citations", None) else {}) or {}
    return {k: loc.get(k) for k in _MW_FIELDS if loc.get(k) is not None}


def _mw_claim(r: dict) -> str:
    return (f"{r.get('symbol')} ({r.get('sector')}) — price {r.get('price')}, "
            f"change {r.get('change')} ({r.get('change_pct')}%), open {r.get('open')}, "
            f"high {r.get('high')}, low {r.get('low')}, volume {r.get('volume')}")


def get_company_market_watch(engine, company: str = "", symbol: str = "", **_) -> ToolResult:
    """Today's live PSX trading line for one company: current price, day open/high/low, change,
    change %, volume, LDCP, and any board status tag (e.g. XD/XB/NC)."""
    syms = _syms(engine)
    sym, name = _resolve(syms, symbol or company) if syms else (symbol or None, None)
    if not sym:
        return {"tool": "getCompanyMarketWatch", "company": company or symbol,
                "note": "could not resolve a PSX symbol"}, [], []
    from .apis.registry import BY_NAME
    api = BY_NAME.get("market_watch")
    items = _filings_fetcher(engine, 5).fetch(api, symbol=sym).items if api else []
    if not items:
        return {"tool": "getCompanyMarketWatch", "company": company or sym, "symbol": sym,
                "note": "not on today's market-watch board (may be suspended or untraded today)"}, [], []
    rec = _mw_record(items[0])
    res = {"tool": "getCompanyMarketWatch", "company": company or sym, "name": name, "symbol": sym,
           **rec}
    ev = [EvidenceItem(claim=_mw_claim(rec), kind="external", citations=[_mw_cite()], reliability=0.9)]
    return res, ev, []


def get_sector_market_watch(engine, company: str = "", symbol: str = "", sector: str = "",
                            limit: int = 100, **_) -> ToolResult:
    """Today's live PSX quotes for EVERY company in a sector (price/change/volume per name) — the
    basis for sector movers/laggards. Give a sector name, or a company/symbol whose sector is used.
    Sector is matched by its PSX sector CODE (resolved from the name via the baked-in code table)."""
    syms = _syms(engine)
    target = (sector or "").strip()
    if not target:
        sym, _n = _resolve(syms, company or symbol)
        target = syms.sector_for(sym) if (syms and sym) else None
    if not target:
        return {"tool": "getSectorMarketWatch", "company": company or symbol or sector,
                "note": "could not resolve a sector"}, [], []
    from .apis.parsers import resolve_sector_code, PSX_SECTORS
    code = resolve_sector_code(target)
    if not code:
        return {"tool": "getSectorMarketWatch", "sector": target,
                "note": "unknown sector name/code"}, [], []
    sector_name = PSX_SECTORS.get(code, target)
    from .apis.registry import BY_NAME
    api = BY_NAME.get("market_watch")
    items = _filings_fetcher(engine, limit).fetch(api, sector=code).items if api else []
    rows = [_mw_record(it) for it in items]
    ev = [EvidenceItem(claim=_mw_claim(r), kind="external", citations=[_mw_cite()], reliability=0.9)
          for r in rows]
    res = {"tool": "getSectorMarketWatch", "sector": sector_name, "sector_code": code,
           "count": len(rows), "companies": rows}
    if not rows:
        res["note"] = "no companies trading in that sector on today's board"
    return res, ev, []


# ----------------------------------------------------------- deliverable futures (market-watch-futures)
_FUT_FIELDS = ("symbol", "base_symbol", "contract", "price", "ldcp", "open", "high", "low",
               "change", "change_pct", "volume", "status")


def _fut_cite():
    url = "https://dps.psx.com.pk/market-watch-futures"
    return Citation(ref_id="C?", kind="external", display="PSX deliverable futures",
                    locator={"source": "PSX.DeliverableFutures", "url": url, "link": url})


def get_company_futures(engine, company: str = "", symbol: str = "", **_) -> ToolResult:
    """Today's deliverable-futures contracts for one company (one row per contract month, e.g.
    MTL-JUN / MTL-JUL): price, day open/high/low, change, change %, volume. Resolves the company's
    base symbol, then pulls all its contracts off the futures board."""
    syms = _syms(engine)
    sym, name = _resolve(syms, symbol or company) if syms else (symbol or None, None)
    if not sym:
        return {"tool": "getCompanyFutures", "company": company or symbol,
                "note": "could not resolve a PSX symbol"}, [], []
    from .apis.registry import BY_NAME
    api = BY_NAME.get("deliverable_futures_market_watch")
    items = _filings_fetcher(engine, 20).fetch(api, symbol=sym).items if api else []
    rows = []
    for it in items:
        loc = (it.citations[0].locator if getattr(it, "citations", None) else {}) or {}
        rows.append({k: loc.get(k) for k in _FUT_FIELDS if loc.get(k) is not None})
    ev = [EvidenceItem(
        claim=(f"{r.get('symbol')} (deliverable future) — price {r.get('price')}, "
               f"change {r.get('change')} ({r.get('change_pct')}%), open {r.get('open')}, "
               f"high {r.get('high')}, low {r.get('low')}, volume {r.get('volume')}"),
        kind="external", citations=[_fut_cite()], reliability=0.9) for r in rows]
    res = {"tool": "getCompanyFutures", "company": company or sym, "name": name, "symbol": sym,
           "count": len(rows), "contracts": rows}
    if not rows:
        res["note"] = "no deliverable-futures contracts trading for this company today"
    return res, ev, []


# ----------------------------------------------------- whole-board snapshots (market + futures)
def _num(x):
    return x if isinstance(x, (int, float)) else None


def _movers(rows: list[dict], n: int):
    """Top gainers / losers (by change %) and most-active (by volume) from a parsed board."""
    by_pct = [r for r in rows if _num(r.get("change_pct")) is not None]
    by_vol = [r for r in rows if _num(r.get("volume")) is not None]
    gainers = sorted(by_pct, key=lambda r: r["change_pct"], reverse=True)[:n]
    losers = sorted(by_pct, key=lambda r: r["change_pct"])[:n]
    active = sorted(by_vol, key=lambda r: r["volume"], reverse=True)[:n]
    return gainers, losers, active


def get_market_watch(engine, limit: int = 10, **_) -> ToolResult:
    """Whole-market snapshot from today's PSX board: the top gainers and top losers (by % change)
    and the most-active stocks (by volume). Use for 'market overview / today's top gainers /
    biggest losers / most active stocks' across the WHOLE exchange (no company or sector filter)."""
    from .apis.registry import BY_NAME
    api = BY_NAME.get("market_watch")
    items = _filings_fetcher(engine, 1000).fetch(api).items if api else []
    rows = [_mw_record(it) for it in items]
    gainers, losers, active = _movers(rows, max(1, int(limit)))
    seen, ev = set(), []
    for r in [*gainers, *losers, *active]:
        if r.get("symbol") in seen:
            continue
        seen.add(r.get("symbol"))
        ev.append(EvidenceItem(claim=_mw_claim(r), kind="external", citations=[_mw_cite()], reliability=0.9))
    res = {"tool": "getMarketWatch", "board_size": len(rows), "top_n": int(limit),
           "top_gainers": gainers, "top_losers": losers, "most_active": active}
    if not rows:
        res["note"] = "market-watch board is empty (market may be closed)"
    return res, ev, []


def _fut_record(item) -> dict:
    loc = (item.citations[0].locator if getattr(item, "citations", None) else {}) or {}
    return {k: loc.get(k) for k in _FUT_FIELDS if loc.get(k) is not None}


def get_futures(engine, limit: int = 10, **_) -> ToolResult:
    """Whole-board snapshot of today's DELIVERABLE FUTURES: top gaining and top losing contracts
    (by % change) and the most-active contracts (by volume), across all listed futures. Use for
    'futures market overview / which futures are moving / most-active futures'."""
    from .apis.registry import BY_NAME
    api = BY_NAME.get("deliverable_futures_market_watch")
    items = _filings_fetcher(engine, 1000).fetch(api).items if api else []
    rows = [_fut_record(it) for it in items]
    gainers, losers, active = _movers(rows, max(1, int(limit)))

    def _fc(r):
        return (f"{r.get('symbol')} (future) — price {r.get('price')}, change {r.get('change')} "
                f"({r.get('change_pct')}%), volume {r.get('volume')}")
    seen, ev = set(), []
    for r in [*gainers, *losers, *active]:
        if r.get("symbol") in seen:
            continue
        seen.add(r.get("symbol"))
        ev.append(EvidenceItem(claim=_fc(r), kind="external", citations=[_fut_cite()], reliability=0.9))
    res = {"tool": "getFutures", "board_size": len(rows), "top_n": int(limit),
           "top_gainers": gainers, "top_losers": losers, "most_active": active}
    if not rows:
        res["note"] = "deliverable-futures board is empty (market may be closed)"
    return res, ev, []


# ----------------------------------------------------- cash-settled futures (market-watch-csf)
# CSF shares the deliverable-futures parser + record shape (_FUT_FIELDS/_fut_record); only the
# board endpoint differs (market-watch-csf vs market-watch-futures), so these mirror the
# getCompanyFutures / getFutures pair against the cash_settled_futures_market_watch api.
def _csf_cite():
    url = "https://dps.psx.com.pk/market-watch-csf"
    return Citation(ref_id="C?", kind="external", display="PSX cash-settled futures",
                    locator={"source": "PSX.CashSettledFutures", "url": url, "link": url})


def get_company_cash_settled_futures(engine, company: str = "", symbol: str = "", **_) -> ToolResult:
    """Today's CASH-SETTLED FUTURES (CSF) contracts for one company (one row per contract month):
    price, day open/high/low, change, change %, volume. Resolves the company's base symbol, then
    pulls all its contracts off the cash-settled-futures board."""
    syms = _syms(engine)
    sym, name = _resolve(syms, symbol or company) if syms else (symbol or None, None)
    if not sym:
        return {"tool": "getCompanyCashSettledFutures", "company": company or symbol,
                "note": "could not resolve a PSX symbol"}, [], []
    from .apis.registry import BY_NAME
    api = BY_NAME.get("cash_settled_futures_market_watch")
    items = _filings_fetcher(engine, 20).fetch(api, symbol=sym).items if api else []
    rows = []
    for it in items:
        loc = (it.citations[0].locator if getattr(it, "citations", None) else {}) or {}
        rows.append({k: loc.get(k) for k in _FUT_FIELDS if loc.get(k) is not None})
    ev = [EvidenceItem(
        claim=(f"{r.get('symbol')} (cash-settled future) — price {r.get('price')}, "
               f"change {r.get('change')} ({r.get('change_pct')}%), open {r.get('open')}, "
               f"high {r.get('high')}, low {r.get('low')}, volume {r.get('volume')}"),
        kind="external", citations=[_csf_cite()], reliability=0.9) for r in rows]
    res = {"tool": "getCompanyCashSettledFutures", "company": company or sym, "name": name,
           "symbol": sym, "count": len(rows), "contracts": rows}
    if not rows:
        res["note"] = "no cash-settled-futures contracts trading for this company today"
    return res, ev, []


def get_cash_settled_futures(engine, limit: int = 10, **_) -> ToolResult:
    """Whole-board snapshot of today's CASH-SETTLED FUTURES (CSF): top gaining and top losing
    contracts (by % change) and the most-active contracts (by volume), across all listed CSF
    contracts. Use for 'cash-settled futures overview / which CSF are moving / most-active CSF'."""
    from .apis.registry import BY_NAME
    api = BY_NAME.get("cash_settled_futures_market_watch")
    items = _filings_fetcher(engine, 1000).fetch(api).items if api else []
    rows = [_fut_record(it) for it in items]
    gainers, losers, active = _movers(rows, max(1, int(limit)))

    def _fc(r):
        return (f"{r.get('symbol')} (cash-settled future) — price {r.get('price')}, change "
                f"{r.get('change')} ({r.get('change_pct')}%), volume {r.get('volume')}")
    seen, ev = set(), []
    for r in [*gainers, *losers, *active]:
        if r.get("symbol") in seen:
            continue
        seen.add(r.get("symbol"))
        ev.append(EvidenceItem(claim=_fc(r), kind="external", citations=[_csf_cite()], reliability=0.9))
    res = {"tool": "getCashSettledFutures", "board_size": len(rows), "top_n": int(limit),
           "top_gainers": gainers, "top_losers": losers, "most_active": active}
    if not rows:
        res["note"] = "cash-settled-futures board is empty (market may be closed)"
    return res, ev, []


# --------------------------------------------------------- top performers (PSX official lists)
def _perf_cite():
    url = "https://dps.psx.com.pk/performers"
    return Citation(ref_id="C?", kind="external", display="PSX top performers",
                    locator={"source": "PSX.Performers", "url": url, "link": url})


def _perf_data(engine, api_name: str = "performers") -> dict:
    """A performers payload ({active, advancers, decliners}) for the equity (/performers) or debt
    (/debt-performers) page — parser returns one dict, carried whole in the fetched item locator."""
    from .apis.registry import BY_NAME
    api = BY_NAME.get(api_name)
    items = _filings_fetcher(engine, 5).fetch(api).items if api else []
    return (items[0].citations[0].locator if items else {}) or {}


def _perf_ev(rows: list[dict], label: str) -> list:
    return [EvidenceItem(
        claim=(f"{r.get('symbol')} ({label}) — price {r.get('price')}, change {r.get('change')} "
               f"({r.get('change_pct')}%), volume {r.get('volume')}"),
        kind="external", citations=[_perf_cite()], reliability=0.9) for r in rows]


def get_top_active_stocks(engine, n: int = 10, **_) -> ToolResult:
    """PSX's official TOP ACTIVE STOCKS list (most traded by volume today), top N."""
    rows = (_perf_data(engine).get("active") or [])[:max(1, int(n))]
    res = {"tool": "getTopActiveStocks", "count": len(rows), "stocks": rows}
    if not rows:
        res["note"] = "no performer data (market may be closed)"
    return res, _perf_ev(rows, "most active"), []


def get_top_advancers(engine, n: int = 10, **_) -> ToolResult:
    """PSX's official TOP ADVANCERS list (biggest gainers by % change today), top N."""
    rows = (_perf_data(engine).get("advancers") or [])[:max(1, int(n))]
    res = {"tool": "getTopAdvancers", "count": len(rows), "advancers": rows}
    if not rows:
        res["note"] = "no performer data (market may be closed)"
    return res, _perf_ev(rows, "top advancer"), []


def get_top_decliners(engine, n: int = 10, **_) -> ToolResult:
    """PSX's official TOP DECLINERS list (biggest losers by % change today), top N."""
    rows = (_perf_data(engine).get("decliners") or [])[:max(1, int(n))]
    res = {"tool": "getTopDecliners", "count": len(rows), "decliners": rows}
    if not rows:
        res["note"] = "no performer data (market may be closed)"
    return res, _perf_ev(rows, "top decliner"), []


# ----------------------------------------------------- DEBT market (sukuk/bonds: performers + board)
def get_top_active_debt_securities(engine, n: int = 10, **_) -> ToolResult:
    """PSX's official TOP ACTIVE DEBT SECURITIES (GoP sukuk/bonds, most traded by volume), top N."""
    rows = (_perf_data(engine, "debt_performers").get("active") or [])[:max(1, int(n))]
    res = {"tool": "getTopActiveDebtSecurities", "count": len(rows), "securities": rows}
    if not rows:
        res["note"] = "no debt-performer data (market may be closed)"
    return res, _perf_ev(rows, "most active debt security"), []


def get_top_debt_advancers(engine, n: int = 10, **_) -> ToolResult:
    """PSX's official TOP DEBT ADVANCERS (debt instruments gaining most by % today), top N."""
    rows = (_perf_data(engine, "debt_performers").get("advancers") or [])[:max(1, int(n))]
    res = {"tool": "getTopDebtAdvancers", "count": len(rows), "advancers": rows}
    if not rows:
        res["note"] = "no debt-performer data (market may be closed)"
    return res, _perf_ev(rows, "top debt advancer"), []


_DEBT_FIELDS = ("symbol", "name", "price", "yield_pct", "ldcp", "open", "high", "low",
                "change", "change_pct", "volume", "sector_code", "status")


def _debt_cite():
    url = "https://dps.psx.com.pk/market-watch-debt"
    return Citation(ref_id="C?", kind="external", display="PSX debt market watch",
                    locator={"source": "PSX.DebtMarketWatch", "url": url, "link": url})


def get_debt_market_watch(engine, limit: int = 50, **_) -> ToolResult:
    """Live board for DEBT instruments (GoP sukuk/bonds): per-security price, YIELD %, change,
    change %, volume. The whole debt board (it is small), capped at `limit`."""
    from .apis.registry import BY_NAME
    api = BY_NAME.get("debt_market_watch")
    items = _filings_fetcher(engine, 1000).fetch(api).items if api else []
    rows = []
    for it in items[:max(1, int(limit))]:
        loc = (it.citations[0].locator if getattr(it, "citations", None) else {}) or {}
        rows.append({k: loc.get(k) for k in _DEBT_FIELDS if loc.get(k) is not None})
    ev = [EvidenceItem(
        claim=(f"{r.get('symbol')} ({r.get('name')}) — price {r.get('price')}, "
               f"yield {r.get('yield_pct')}%, change {r.get('change')} ({r.get('change_pct')}%), "
               f"volume {r.get('volume')}"),
        kind="external", citations=[_debt_cite()], reliability=0.9) for r in rows]
    res = {"tool": "getDebtMarketWatch", "count": len(rows), "securities": rows}
    if not rows:
        res["note"] = "debt board is empty (market may be closed)"
    return res, ev, []


# ------------------------------------------------- market summary (www.psx.com.pk/market-summary)
def _summary_cite():
    url = "https://www.psx.com.pk/market-summary/"
    return Citation(ref_id="C?", kind="external", display="PSX market summary",
                    locator={"source": "PSX.MarketSummary", "url": url, "link": url})


def _summary_data(engine) -> dict:
    """The whole parsed market-summary page (one dict carried in the fetched item locator):
    timestamp, exchange (status/volume/value/trades), breadth (advanced/declined/unchanged/
    total), indices[], and sectors[] (per-sector OHLC tables)."""
    from .apis.registry import BY_NAME
    api = BY_NAME.get("daily_market_summary")
    items = _filings_fetcher(engine, 5).fetch(api).items if api else []
    return (items[0].citations[0].locator if items else {}) or {}


def get_market_summary(engine, **_) -> ToolResult:
    """Today's whole-exchange SUMMARY from PSX: exchange open/closed status, total volume / value /
    trades, market breadth (advanced / declined / unchanged / total), and the index board (KSE-100,
    KSE-30, KMI-30, ALLSHR, etc. with level, change and change %). Use for 'how is the market today /
    is the market up or down / KSE-100 level / advance-decline / total turnover'."""
    data = _summary_data(engine)
    ex = data.get("exchange") or {}
    br = data.get("breadth") or {}
    indices = data.get("indices") or []
    if not (ex or indices):
        return {"tool": "getMarketSummary",
                "note": "market summary unavailable (page returned no data)"}, [], []
    res = {"tool": "getMarketSummary", "timestamp": data.get("timestamp"),
           "exchange": ex, "breadth": br, "indices": indices}
    ev = [EvidenceItem(
        claim=(f"PSX exchange status {ex.get('status')}: volume {ex.get('volume')}, "
               f"value {ex.get('value')}, trades {ex.get('trades')}; advanced {br.get('advanced')}, "
               f"declined {br.get('declined')}, unchanged {br.get('unchanged')} of {br.get('total')} "
               f"(as of {data.get('timestamp')})"),
        kind="external", citations=[_summary_cite()], reliability=0.9)]
    for ix in indices:
        ev.append(EvidenceItem(
            claim=(f"{ix.get('name')} index {ix.get('value')}, change {ix.get('change')} "
                   f"({ix.get('change_pct')}%)"),
            kind="external", citations=[_summary_cite()], reliability=0.9))
    return res, ev, []


def _match_sector(sectors: list[dict], target: str) -> dict | None:
    """Find the market-summary sector block whose heading matches `target` (a sector name or code).
    Matches the page title case-insensitively, also trying the canonical PSX sector name resolved
    from a code/keyword, then a loose substring either direction."""
    t = (target or "").strip().lower()
    if not t:
        return None
    cands = {t}
    try:
        from .apis.parsers import resolve_sector_code, PSX_SECTORS
        code = resolve_sector_code(target)
        if code and PSX_SECTORS.get(code):
            cands.add(PSX_SECTORS[code].strip().lower())
    except Exception:  # noqa: BLE001
        pass
    for blk in sectors:                                # exact title match first
        if (blk.get("sector") or "").strip().lower() in cands:
            return blk
    for blk in sectors:                                # else loose substring
        title = (blk.get("sector") or "").strip().lower()
        if any(c and (c in title or title in c) for c in cands):
            return blk
    return None


def get_sector_market_summary(engine, sector: str = "", company: str = "", symbol: str = "",
                              limit: int = 200, **_) -> ToolResult:
    """Today's per-company OHLC quote table for ONE sector, from the PSX market-summary board: each
    company's LDCP, day open/high/low, current price, change, change % (vs LDCP) and volume. Give a
    sector name (e.g. 'CEMENT'), or a company/symbol whose sector is used."""
    syms = _syms(engine)
    target = (sector or "").strip()
    if not target:
        sym, _n = _resolve(syms, company or symbol) if syms else (None, None)
        target = syms.sector_for(sym) if (syms and sym) else None
    if not target:
        return {"tool": "getSectorMarketSummary", "company": company or symbol or sector,
                "note": "could not resolve a sector"}, [], []
    data = _summary_data(engine)
    sectors = data.get("sectors") or []
    block = _match_sector(sectors, target)
    if not block:
        return {"tool": "getSectorMarketSummary", "sector": target,
                "note": "sector not found on the market-summary board",
                "available_sectors": [s.get("sector") for s in sectors]}, [], []
    rows = (block.get("rows") or [])[:max(1, int(limit))]
    ev = [EvidenceItem(
        claim=(f"{r.get('symbol')} ({r.get('name')}) — price {r.get('price')}, "
               f"change {r.get('change')} ({r.get('change_pct')}%), open {r.get('open')}, "
               f"high {r.get('high')}, low {r.get('low')}, LDCP {r.get('ldcp')}, "
               f"volume {r.get('volume')}"),
        kind="external", citations=[_summary_cite()], reliability=0.9) for r in rows]
    res = {"tool": "getSectorMarketSummary", "sector": block.get("sector"),
           "count": len(rows), "companies": rows}
    if not rows:
        res["note"] = "no companies in that sector block"
    return res, ev, []


# --------------------------------------------------- sector turnover (sector-summary/sectorwise)
def _sector_turnover_cite():
    url = "https://dps.psx.com.pk/sector-summary"
    return Citation(ref_id="C?", kind="external", display="PSX sector summary",
                    locator={"source": "PSX.SectorSummary", "url": url, "link": url})


_TURN_FIELDS = ("sector_code", "sector", "advance", "decline", "unchange", "turnover",
                "market_cap_b")


def get_sector_turnover(engine, **_) -> ToolResult:
    """Today's SECTOR-WISE turnover board for the whole market: one row per PSX sector with its
    advancing / declining / unchanged scrip counts, total TURNOVER (shares traded) and market
    capitalization (Rs billion). Use for 'which sector traded most / sector turnover / sector
    market cap / sector breadth / where was the activity today'."""
    from .apis.registry import BY_NAME
    api = BY_NAME.get("sector_summary")
    items = _filings_fetcher(engine, 1000).fetch(api).items if api else []
    rows = []
    for it in items:
        loc = (it.citations[0].locator if getattr(it, "citations", None) else {}) or {}
        rec = {k: loc.get(k) for k in _TURN_FIELDS if loc.get(k) is not None}
        if rec.get("sector"):
            rows.append(rec)
    rows.sort(key=lambda r: (r.get("turnover") or 0), reverse=True)
    ev = [EvidenceItem(
        claim=(f"{r.get('sector')} ({r.get('sector_code')}) — turnover {r.get('turnover')} shares, "
               f"market cap {r.get('market_cap_b')}B; {r.get('advance')} advancing, "
               f"{r.get('decline')} declining, {r.get('unchange')} unchanged"),
        kind="external", citations=[_sector_turnover_cite()], reliability=0.9) for r in rows]
    res = {"tool": "getSectorTurnover", "count": len(rows), "sectors": rows}
    if not rows:
        res["note"] = "sector summary board is empty (market may be closed)"
    return res, ev, []


# ------------------------------------------------ stock screener (valuation / liquidity board)
def _screener_cite():
    url = "https://dps.psx.com.pk/screener"
    return Citation(ref_id="C?", kind="external", display="PSX stock screener",
                    locator={"source": "PSX.Screener", "url": url, "link": url})


_SCREENER_FIELDS = ("symbol", "name", "sector_code", "sector", "listed_in", "status",
                    "market_cap", "price", "change_pct", "change_1y_pct", "pe_ratio_ttm",
                    "dividend_yield_pct", "free_float", "volume_30d_avg")


def _screener_rows(engine) -> list[dict]:
    """Every screener row (one GET = whole market), each rebuilt from its EvidenceItem locator.
    Cap is set high so the full board survives (the default fetcher caps at 8)."""
    from .apis.registry import BY_NAME
    api = BY_NAME.get("stock_screener")
    items = _filings_fetcher(engine, 4000).fetch(api).items if api else []
    rows: list[dict] = []
    for it in items:
        loc = (it.citations[0].locator if getattr(it, "citations", None) else {}) or {}
        rec = {k: loc.get(k) for k in _SCREENER_FIELDS if loc.get(k) is not None}
        if rec.get("symbol"):
            rows.append(rec)
    return rows


def _screener_claim(r: dict) -> str:
    return (f"{r.get('symbol')} ({r.get('name')}) — price {r.get('price')}, "
            f"market cap {r.get('market_cap')}, P/E (TTM) {r.get('pe_ratio_ttm')}, "
            f"dividend yield {r.get('dividend_yield_pct')}%, 1-year return "
            f"{r.get('change_1y_pct')}%, free float {r.get('free_float')}, "
            f"30-day avg volume {r.get('volume_30d_avg')}")


def get_company_screener(engine, company: str = "", **_) -> ToolResult:
    """ONE company's VALUATION & liquidity snapshot from the PSX stock screener: P/E (TTM),
    dividend yield %, market cap, free float, 1-year return %, 30-day average volume and price.
    Market-derived (~5-min delay; the 1-year return is NOT payout-adjusted)."""
    syms = _syms(engine)
    sym, _name = _resolve(syms, company) if syms else (None, None)
    if not sym:
        sym = (company or "").strip().upper() or None
    if not sym:
        return {"tool": "getCompanyScreener", "company": company,
                "note": "could not resolve a company"}, [], []
    row = next((r for r in _screener_rows(engine)
                if (r.get("symbol") or "").upper() == sym), None)
    if not row:
        return {"tool": "getCompanyScreener", "company": company, "symbol": sym,
                "note": "company not found on the screener"}, [], []
    ev = [EvidenceItem(claim=_screener_claim(row), kind="external",
                       citations=[_screener_cite()], reliability=0.9)]
    return {"tool": "getCompanyScreener", "company": company or sym, **row}, ev, []


def get_sector_screener(engine, sector: str = "", limit: int = 60, **_) -> ToolResult:
    """VALUATION & liquidity snapshot for EVERY company in one sector from the PSX stock screener —
    P/E (TTM), dividend yield %, market cap, free float, 1-year return %, 30-day avg volume, price —
    for peer comparison. Give a PSX sector name (e.g. 'CEMENT')."""
    target = (sector or "").strip()
    if not target:
        return {"tool": "getSectorScreener", "note": "need a sector name (e.g. 'CEMENT')"}, [], []
    cands = {target.lower()}
    try:
        from .apis.parsers import resolve_sector_code, PSX_SECTORS
        code = resolve_sector_code(target)
        if code and PSX_SECTORS.get(code):
            cands.add(PSX_SECTORS[code].strip().lower())
            cands.add(code.strip().lower())
    except Exception:  # noqa: BLE001
        pass
    rows = _screener_rows(engine)
    members = [r for r in rows
               if (r.get("sector") or "").strip().lower() in cands
               or (r.get("sector_code") or "").strip().lower() in cands]
    if not members:
        return {"tool": "getSectorScreener", "sector": sector,
                "note": "no companies for that sector on the screener",
                "available_sectors": sorted({r.get("sector") for r in rows if r.get("sector")})}, [], []
    sect_name = next((m.get("sector") for m in members if m.get("sector")), target)
    shown = members[:max(1, int(limit))]
    ev = [EvidenceItem(claim=_screener_claim(r), kind="external",
                       citations=[_screener_cite()], reliability=0.9) for r in shown]
    res = {"tool": "getSectorScreener", "sector": sect_name, "count": len(members),
           "companies": shown}
    if len(members) > len(shown):
        res["truncated"] = True
    return res, ev, []


# ----------------------------------------------------- composite tools (chain existing tools)
# These bake common multi-step questions into ONE deterministic call so the planner never has to
# fire several tools and combine/rank/limit the JSON itself (the weak model is unreliable at that).
# Each reuses the same fetch helpers as the primitives — no new endpoints.
_SCREEN_METRICS = {
    "pe_ratio_ttm": "P/E (TTM)", "dividend_yield_pct": "dividend yield %",
    "market_cap": "market cap", "change_1y_pct": "1-year return %",
    "price": "price", "volume_30d_avg": "30-day avg volume", "free_float": "free float",
}
_METRIC_ALIASES = {
    "pe": "pe_ratio_ttm", "p/e": "pe_ratio_ttm", "pe_ratio": "pe_ratio_ttm",
    "p/e (ttm)": "pe_ratio_ttm", "pe_ttm": "pe_ratio_ttm",
    "yield": "dividend_yield_pct", "dividend_yield": "dividend_yield_pct",
    "dividend": "dividend_yield_pct", "div_yield": "dividend_yield_pct",
    "mcap": "market_cap", "marketcap": "market_cap", "market cap": "market_cap",
    "1y": "change_1y_pct", "1yr": "change_1y_pct", "1-year": "change_1y_pct",
    "return": "change_1y_pct", "1y_return": "change_1y_pct", "yearly_return": "change_1y_pct",
    "volume": "volume_30d_avg", "vol": "volume_30d_avg", "freefloat": "free_float",
}


def _norm_metric(metric: str, default: str = "pe_ratio_ttm") -> str:
    m = (metric or "").strip().lower()
    if m in _SCREEN_METRICS:
        return m
    return _METRIC_ALIASES.get(m, default)


def _median(xs: list[float]):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    mid = n // 2
    return s[mid] if n % 2 else round((s[mid - 1] + s[mid]) / 2, 4)


def _screen_pick(r: dict, metric: str) -> dict:
    """A compact comparison row: identity + the ranked metric + the usual valuation columns."""
    keys = ("symbol", "name", "market_cap", "price", "pe_ratio_ttm", "dividend_yield_pct",
            "change_1y_pct", "free_float", "volume_30d_avg")
    out = {k: r.get(k) for k in keys if r.get(k) is not None}
    out["_metric_value"] = r.get(metric)
    return out


def get_company_peer_comparison(engine, company: str = "", metric: str = "pe_ratio_ttm",
                                limit: int = 20, **_) -> ToolResult:
    """Rank a company against its SAME-SECTOR peers on one valuation metric (P/E TTM, dividend
    yield %, market cap, 1-year return %, price, 30-day volume), using the PSX screener board. Marks
    the company's rank, percentile and the peer median/average so 'is X cheap/expensive/high-yield
    vs its peers' is answered in one call. metric defaults to P/E; higher value ranks first (so for
    P/E rank-1 is the MOST expensive — read the company's value vs the peer median for cheap/dear)."""
    m = _norm_metric(metric)
    syms = _syms(engine)
    sym, name = _resolve(syms, company) if syms else (None, None)
    if not sym:
        sym = (company or "").strip().upper() or None
    if not sym:
        return {"tool": "getCompanyPeerComparison", "company": company,
                "note": "could not resolve a company"}, [], []
    rows = _screener_rows(engine)
    comp = next((r for r in rows if (r.get("symbol") or "").upper() == sym), None)
    if not comp:
        return {"tool": "getCompanyPeerComparison", "company": company, "symbol": sym,
                "note": "company not found on the screener"}, [], []
    code = comp.get("sector_code")
    sect_name = comp.get("sector")
    members = [r for r in rows if r.get("sector_code") == code] if code else []
    valued = [r for r in members if isinstance(r.get(m), (int, float)) and r.get(m) not in (None, 0)]
    valued.sort(key=lambda r: r.get(m), reverse=True)
    cv = comp.get(m)
    peer_vals = [r.get(m) for r in valued if (r.get("symbol") or "").upper() != sym]
    rank = next((i + 1 for i, r in enumerate(valued)
                 if (r.get("symbol") or "").upper() == sym), None)
    below = sum(1 for v in peer_vals if isinstance(cv, (int, float)) and v < cv)
    pctile = round(below / len(peer_vals) * 100, 1) if peer_vals and isinstance(cv, (int, float)) else None
    ranked = [{**_screen_pick(r, m), "rank": i + 1,
               "is_query": (r.get("symbol") or "").upper() == sym}
              for i, r in enumerate(valued[:max(1, int(limit))])]
    res = {"tool": "getCompanyPeerComparison", "company": company or sym, "symbol": sym,
           "name": name or comp.get("name"), "sector": sect_name, "metric": m,
           "metric_label": _SCREEN_METRICS.get(m, m), "company_value": cv,
           "rank": rank, "of_companies": len(valued),
           "percentile": pctile, "peer_median": _median(peer_vals),
           "peer_average": round(sum(peer_vals) / len(peer_vals), 4) if peer_vals else None,
           "ranked": ranked}
    label = _SCREEN_METRICS.get(m, m)
    ev = [EvidenceItem(
        claim=(f"{sym} {label} {cv} ranks {rank} of {len(valued)} in {sect_name} "
               f"(peer median {res['peer_median']}, peer average {res['peer_average']}; "
               f"{sym} is at the {pctile} percentile)"),
        kind="external", citations=[_screener_cite()], reliability=0.9)]
    for r in ranked:
        ev.append(EvidenceItem(
            claim=f"{r.get('symbol')} ({r.get('name')}) {label} {r.get('_metric_value')}",
            kind="external", citations=[_screener_cite()], reliability=0.9))
    return res, ev, []


def get_company_vs_sector_fundamentals(engine, company: str = "", year: int = 0, **_) -> ToolResult:
    """Compare ONE company's AUDITED profitability to its SECTOR aggregate for a year, from the PSX
    analysis report (Rs MILLION): the company's net & PBT margin vs the sector's net & PBT margin,
    with the gap (company minus sector) and whether it is above/below. Answers 'is X more/less
    profitable than its sector / how does its margin compare to peers'."""
    try:
        y = int(year)
    except (TypeError, ValueError):
        y = 0
    if not y:
        return {"tool": "getCompanyVsSectorFundamentals", "company": company,
                "note": "need a fiscal year (e.g. 2025)"}, [], []
    crow, _cev, _ = get_analysis_report(engine, year=y, company=company)
    if crow.get("note") or not crow.get("sales"):
        return {"tool": "getCompanyVsSectorFundamentals", "company": company, "year": y,
                "note": crow.get("note") or "company fundamentals not found for that year"}, [], []
    sales = crow.get("sales") or 0
    c_net = round((crow.get("pat") or 0) / sales * 100, 2) if sales else None
    c_pbt = round((crow.get("pbt") or 0) / sales * 100, 2) if sales else None
    sector = crow.get("sector")
    srow, _sev, _ = get_analysis_report(engine, year=y, sector=sector) if sector else ({}, [], [])
    s_net = srow.get("net_margin_pct")
    s_pbt = srow.get("pbt_margin_pct")
    d_net = round(c_net - s_net, 2) if (c_net is not None and s_net is not None) else None
    d_pbt = round(c_pbt - s_pbt, 2) if (c_pbt is not None and s_pbt is not None) else None
    res = {"tool": "getCompanyVsSectorFundamentals", "year": y,
           "company": company or crow.get("company"), "symbol": crow.get("symbol"),
           "name": crow.get("name"), "sector": sector,
           "company_net_margin_pct": c_net, "company_pbt_margin_pct": c_pbt,
           "sector_net_margin_pct": s_net, "sector_pbt_margin_pct": s_pbt,
           "net_margin_gap_pp": d_net, "pbt_margin_gap_pp": d_pbt,
           "net_margin_vs_sector": (None if d_net is None else ("above" if d_net >= 0 else "below")),
           "sector_companies": srow.get("companies_total"),
           "company_sales": crow.get("sales"), "company_pat": crow.get("pat"),
           "company_pbt": crow.get("pbt")}
    if s_net is None:
        res["note"] = "sector aggregate unavailable; showing the company's own margins only"
    ev = [EvidenceItem(
        claim=(f"{crow.get('symbol')} FY{y} net margin {c_net}% vs {sector} sector {s_net}% "
               f"(gap {d_net} pp); PBT margin {c_pbt}% vs sector {s_pbt}% (gap {d_pbt} pp)"),
        kind="external", citations=[_ar_cite(y)], reliability=0.9)]
    return res, ev, []


def get_company_snapshot(engine, company: str = "", **_) -> ToolResult:
    """One-shot company dossier merging THREE feeds: identity (symbol + sector), today's LIVE quote
    (price, open/high/low, change %, volume, board status) and the valuation snapshot (market cap,
    P/E TTM, dividend yield %, 1-year return %, free float, 30-day avg volume). Use for an
    open-ended 'tell me about / give me a rundown on / snapshot of <company>'."""
    syms = _syms(engine)
    sym, name = _resolve(syms, company) if syms else (None, None)
    if not sym:
        sym = (company or "").strip().upper() or None
    if not sym:
        return {"tool": "getCompanySnapshot", "company": company,
                "note": "could not resolve a company"}, [], []
    sector = syms.sector_for(sym) if syms else None
    quote, qev, _ = get_company_market_watch(engine, company=company or sym)
    val_row = next((r for r in _screener_rows(engine)
                    if (r.get("symbol") or "").upper() == sym), None)
    live = {k: quote.get(k) for k in ("price", "open", "high", "low", "change", "change_pct",
                                      "volume", "status") if quote.get(k) is not None}
    valuation = {}
    if val_row:
        valuation = {k: val_row.get(k) for k in ("market_cap", "price", "pe_ratio_ttm",
                     "dividend_yield_pct", "change_1y_pct", "free_float", "volume_30d_avg")
                     if val_row.get(k) is not None}
        sector = sector or val_row.get("sector")
        name = name or val_row.get("name")
    res = {"tool": "getCompanySnapshot", "company": company or sym, "symbol": sym,
           "name": name, "sector": sector, "live": live, "valuation": valuation}
    missing = [lbl for lbl, d in (("live quote", live), ("valuation", valuation)) if not d]
    if missing:
        res["note"] = "unavailable: " + ", ".join(missing)
    ev = [EvidenceItem(
        claim=(f"{sym} ({name}) — sector {sector}; price {live.get('price')}, change "
               f"{live.get('change_pct')}%, volume {live.get('volume')}; market cap "
               f"{valuation.get('market_cap')}, P/E (TTM) {valuation.get('pe_ratio_ttm')}, "
               f"dividend yield {valuation.get('dividend_yield_pct')}%, 1-year return "
               f"{valuation.get('change_1y_pct')}%"),
        kind="external", citations=[_screener_cite()], reliability=0.9)]
    ev += list(qev or [])
    return res, ev, []


def screen_stocks(engine, metric: str = "dividend_yield_pct", sector: str = "", limit: int = 15,
                  order: str = "desc", **_) -> ToolResult:
    """Rank PSX stocks across the WHOLE market (or within one sector) by a single screener metric —
    dividend yield %, P/E (TTM), 1-year return %, market cap, price or 30-day volume. order='desc'
    (default) for the highest (top yield / biggest gainers / largest cap); order='asc' for the
    lowest (cheapest P/E). Zero / blank values are dropped so the ranking is meaningful. Use for
    'which stocks have the highest dividend yield / lowest P/E / biggest 1-year gain on PSX'."""
    m = _norm_metric(metric, default="dividend_yield_pct")
    asc = str(order or "").strip().lower() in ("asc", "ascending", "low", "lowest", "up")
    rows = _screener_rows(engine)
    scope = "PSX"
    if sector and sector.strip():
        cands = {sector.strip().lower()}
        try:
            from .apis.parsers import resolve_sector_code, PSX_SECTORS
            code = resolve_sector_code(sector)
            if code and PSX_SECTORS.get(code):
                cands.add(PSX_SECTORS[code].strip().lower())
                cands.add(code.strip().lower())
        except Exception:  # noqa: BLE001
            pass
        rows = [r for r in rows if (r.get("sector") or "").strip().lower() in cands
                or (r.get("sector_code") or "").strip().lower() in cands]
        scope = next((r.get("sector") for r in rows if r.get("sector")), sector)
    valued = [r for r in rows if isinstance(r.get(m), (int, float)) and r.get(m) not in (None, 0)]
    valued.sort(key=lambda r: r.get(m), reverse=not asc)
    shown = valued[:max(1, int(limit))]
    label = _SCREEN_METRICS.get(m, m)
    ranked = [{**_screen_pick(r, m), "rank": i + 1} for i, r in enumerate(shown)]
    res = {"tool": "screenStocks", "metric": m, "metric_label": label,
           "order": "asc" if asc else "desc", "scope": scope,
           "universe_size": len(valued), "count": len(ranked), "ranked": ranked}
    if not ranked:
        res["note"] = "no stocks with a usable value for that metric"
    ev = [EvidenceItem(
        claim=f"{r.get('symbol')} ({r.get('name')}) {label} {r.get('_metric_value')}",
        kind="external", citations=[_screener_cite()], reliability=0.9) for r in ranked]
    return res, ev, []


# ------------------------------------------------- analysis report (yearly fundamentals xlsx)
def _ar_cite(year):
    url = f"https://dps.psx.com.pk/download/analysis_report/year-{year}.xlsx"
    return Citation(ref_id="C?", kind="external", display=f"PSX analysis report {year}",
                    locator={"source": "PSX.AnalysisReports", "url": url, "link": url, "year": year})


_AR_FIELDS = ("symbol", "name", "sector", "fiscal_year", "year_end", "sales", "pbt", "pat",
              "equity", "total_assets", "financial_charges", "cash_dividend_pct",
              "stock_dividend_pct", "shareholders")


def _ar_row(rec: dict) -> dict:
    return {k: rec.get(k) for k in _AR_FIELDS if rec.get(k) is not None}


def get_analysis_report(engine, year: int = 0, company: str = "", symbol: str = "",
                        sector: str = "", limit: int = 60, **_) -> ToolResult:
    """PSX yearly fundamentals dataset for a given YEAR (Rs MILLION): per listed company — sales,
    profit-before-tax, profit-after-tax, shareholders' equity, total assets, financial charges,
    dividend %s and shareholder count, plus the company's sector. Pass a company/symbol for that
    one company's audited fundamentals; pass a sector for every company in it PLUS the sector
    aggregate (net & PBT margin); pass neither for the whole-market totals. NOTE: figures are Rs
    MILLION (the workbook is Rs thousand) — reconcile scale before mixing."""
    try:
        y = int(year)
    except (TypeError, ValueError):
        y = 0
    if not y:
        return {"tool": "getAnalysisReport", "note": "need a fiscal year (e.g. 2025)"}, [], []
    try:
        from .external import _adapters, _load_report
        ar, syms = _adapters(engine)
        recs = _load_report(ar, y)            # {symbol: full record}, process-cached
    except Exception as e:  # noqa: BLE001
        return {"tool": "getAnalysisReport", "year": y, "note": f"report unavailable: {e}"}, [], []
    if not recs:
        return {"tool": "getAnalysisReport", "year": y,
                "note": f"no PSX analysis report published for {y}"}, [], []

    # --- one company ---
    if company or symbol:
        sym, name = _resolve(syms, symbol or company) if syms else (symbol or None, None)
        rec = recs.get(sym) if sym else None
        if not rec:
            return {"tool": "getAnalysisReport", "year": y, "company": company or symbol,
                    "note": "company not found in that year's report"}, [], []
        row = _ar_row(rec)
        ev = [EvidenceItem(
            claim=(f"{rec.get('symbol')} ({rec.get('name')}) FY{rec.get('fiscal_year') or y} "
                   f"(Rs million): sales {rec.get('sales')}, PBT {rec.get('pbt')}, "
                   f"PAT {rec.get('pat')}, equity {rec.get('equity')}, total assets "
                   f"{rec.get('total_assets')}, financial charges {rec.get('financial_charges')}; "
                   f"cash dividend {rec.get('cash_dividend_pct')}%"),
            kind="external", citations=[_ar_cite(y)], reliability=0.9)]
        return {"tool": "getAnalysisReport", "year": y, "company": company or sym, **row}, ev, []

    # --- one sector (members + aggregate) ---
    if sector:
        t = sector.strip().lower()
        cands = {t}
        try:
            from .apis.parsers import resolve_sector_code, PSX_SECTORS
            code = resolve_sector_code(sector)
            if code and PSX_SECTORS.get(code):
                cands.add(PSX_SECTORS[code].strip().lower())
        except Exception:  # noqa: BLE001
            pass
        members = [r for r in recs.values()
                   if any(c and (c == (r.get("sector") or "").strip().lower()
                                 or c in (r.get("sector") or "").strip().lower()) for c in cands)]
        if not members:
            return {"tool": "getAnalysisReport", "year": y, "sector": sector,
                    "note": "no companies for that sector in the report",
                    "available_sectors": sorted({r.get("sector") for r in recs.values() if r.get("sector")})}, [], []
        ssales = sum(r.get("sales") or 0 for r in members)
        spat = sum(r.get("pat") or 0 for r in members)
        spbt = sum(r.get("pbt") or 0 for r in members)
        net = round(spat / ssales * 100, 2) if ssales else None
        pbt = round(spbt / ssales * 100, 2) if ssales else None
        sect_name = members[0].get("sector")
        rows = [_ar_row(r) for r in members[:max(1, int(limit))]]
        ev = [EvidenceItem(
            claim=(f"{sect_name} sector FY{y} (Rs million, {len(members)} companies): total sales "
                   f"{ssales}, total PAT {spat}, total PBT {spbt}; net margin {net}%, PBT margin {pbt}%"),
            kind="external", citations=[_ar_cite(y)], reliability=0.9)]
        for r in rows:
            ev.append(EvidenceItem(
                claim=(f"{r.get('symbol')} FY{y} (Rs million): sales {r.get('sales')}, "
                       f"PAT {r.get('pat')}, PBT {r.get('pbt')}, equity {r.get('equity')}"),
                kind="external", citations=[_ar_cite(y)], reliability=0.9))
        res = {"tool": "getAnalysisReport", "year": y, "sector": sect_name,
               "companies_total": len(members), "total_sales": ssales, "total_pat": spat,
               "total_pbt": spbt, "net_margin_pct": net, "pbt_margin_pct": pbt,
               "companies": rows}
        if len(members) > len(rows):
            res["truncated"] = True
        return res, ev, []

    # --- whole market totals ---
    allr = list(recs.values())
    ssales = sum(r.get("sales") or 0 for r in allr)
    spat = sum(r.get("pat") or 0 for r in allr)
    spbt = sum(r.get("pbt") or 0 for r in allr)
    net = round(spat / ssales * 100, 2) if ssales else None
    ev = [EvidenceItem(
        claim=(f"PSX all listed companies FY{y} (Rs million, {len(allr)} companies): total sales "
               f"{ssales}, total PAT {spat}, total PBT {spbt}; aggregate net margin {net}%"),
        kind="external", citations=[_ar_cite(y)], reliability=0.9)]
    res = {"tool": "getAnalysisReport", "year": y, "companies_total": len(allr),
           "total_sales": ssales, "total_pat": spat, "total_pbt": spbt, "net_margin_pct": net,
           "note": "whole-market totals; pass company/symbol or sector for detail"}
    return res, ev, []


def get_company_analysis_report(engine, company: str = "", year: int = 0, **_) -> ToolResult:
    """One company's audited yearly fundamentals (Rs million) from the PSX analysis report."""
    res, ev, calcs = get_analysis_report(engine, year=year, company=company)
    res["tool"] = "getCompanyAnalysisReport"
    return res, ev, calcs


def get_sector_analysis_report(engine, sector: str = "", year: int = 0, limit: int = 60, **_) -> ToolResult:
    """A sector's audited yearly fundamentals + aggregate net/PBT margin (Rs million) from the PSX
    analysis report. With no sector, returns the whole-market totals for the year."""
    res, ev, calcs = get_analysis_report(engine, year=year, sector=sector, limit=limit)
    res["tool"] = "getSectorAnalysisReport"
    return res, ev, calcs


TOOLS: dict[str, Tool] = {
    "getCompanyOverview": Tool(
        name="getCompanyOverview",
        description="Full PSX profile for ANY listed company: live quote (price, change, P/E TTM, "
                    "day open/high/low/volume, 52-week range), equity (market cap, shares, free "
                    "float), business profile (description, key people, auditor, registrar, fiscal "
                    "year-end), and per-YEAR financials (sales, profit-after-tax, EPS) and ratios "
                    "(GROSS profit margin, net profit margin, EPS growth, PEG). The go-to tool for "
                    "'tell me about X', valuation, margins, EPS, market cap, or company profile.",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX ticker (auto-detected)"},),
        outputs="dict: quote, equity, profile, financials_annual/quarterly (sales/PAT/EPS), "
                "ratios (gross & net margin %, EPS growth, PEG)",
        fn=get_company_overview),
    "getMarketWatch": Tool(
        name="getMarketWatch",
        description="WHOLE-MARKET snapshot from today's PSX board: top gainers, top losers (by % "
                    "change) and most-active stocks (by volume) across the entire exchange — no "
                    "company/sector filter. Use for 'market overview / today's gainers / biggest "
                    "losers / most active stocks'.",
        inputs=({"name": "limit", "type": "integer", "desc": "how many per list (default 10)"},),
        outputs="dict: board_size, top_gainers[], top_losers[], most_active[] (each {symbol, price, change_pct, volume})",
        fn=get_market_watch),
    "getTopActiveStocks": Tool(
        name="getTopActiveStocks",
        description="PSX's official TOP ACTIVE STOCKS list — the most-traded stocks by volume "
                    "today. Returns the top N.",
        inputs=({"name": "n", "type": "integer", "desc": "how many to return (e.g. 3, 5, 10)"},),
        outputs="list of {symbol, name, price, change, change_pct, volume}",
        fn=get_top_active_stocks),
    "getTopAdvancers": Tool(
        name="getTopAdvancers",
        description="PSX's official TOP ADVANCERS list — the biggest gainers by % change today "
                    "(top gainers). Returns the top N.",
        inputs=({"name": "n", "type": "integer", "desc": "how many to return (e.g. 3, 5, 10)"},),
        outputs="list of {symbol, name, price, change, change_pct, volume}",
        fn=get_top_advancers),
    "getTopDecliners": Tool(
        name="getTopDecliners",
        description="PSX's official TOP DECLINERS list — the biggest losers by % change today "
                    "(top losers). Returns the top N.",
        inputs=({"name": "n", "type": "integer", "desc": "how many to return (e.g. 3, 5, 10)"},),
        outputs="list of {symbol, name, price, change, change_pct, volume}",
        fn=get_top_decliners),
    "getTopActiveDebtSecurities": Tool(
        name="getTopActiveDebtSecurities",
        description="PSX's official TOP ACTIVE DEBT SECURITIES list — most-traded debt instruments "
                    "(GoP sukuk / bonds) by volume today. Returns the top N.",
        inputs=({"name": "n", "type": "integer", "desc": "how many to return (e.g. 3, 5, 10)"},),
        outputs="list of {symbol, name, price, change, change_pct, volume}",
        fn=get_top_active_debt_securities),
    "getTopDebtAdvancers": Tool(
        name="getTopDebtAdvancers",
        description="PSX's official TOP DEBT ADVANCERS list — debt instruments (GoP sukuk / bonds) "
                    "gaining most by % change today. Returns the top N.",
        inputs=({"name": "n", "type": "integer", "desc": "how many to return (e.g. 3, 5, 10)"},),
        outputs="list of {symbol, name, price, change, change_pct, volume}",
        fn=get_top_debt_advancers),
    "getDebtMarketWatch": Tool(
        name="getDebtMarketWatch",
        description="Live quote board for DEBT instruments (GoP sukuk / bonds): per-security price, "
                    "YIELD %, change, change %, volume. Use for debt/bond/sukuk market or yield "
                    "questions.",
        inputs=({"name": "limit", "type": "integer", "desc": "max securities (default 50)"},),
        outputs="list of {symbol, name, price, yield_pct, ldcp, open, high, low, change, change_pct, volume}",
        fn=get_debt_market_watch),
    "getMarketSummary": Tool(
        name="getMarketSummary",
        description="Today's whole-exchange SUMMARY from PSX: exchange open/closed status, total "
                    "volume / value / number of trades, market breadth (advanced / declined / "
                    "unchanged / total scrips), and the index board (KSE-100, KSE-30, KMI-30, "
                    "ALLSHR and other indices with level, change and change %). Use for 'how did "
                    "the market do today / is the market up or down / KSE-100 level / "
                    "advance-decline / total turnover'.",
        inputs=(),
        outputs="dict: timestamp, exchange{status,volume,value,trades}, "
                "breadth{advanced,declined,unchanged,total}, indices[{name,value,change,change_pct}]",
        fn=get_market_summary),
    "getSectorMarketSummary": Tool(
        name="getSectorMarketSummary",
        description="Today's per-company OHLC quote table for ONE sector from the PSX "
                    "market-summary board: each company's LDCP, day open/high/low, current price, "
                    "change, change % (vs LDCP) and volume. Use when the user wants a whole "
                    "sector's prices/OHLC. Give a PSX sector name (e.g. 'CEMENT'); for a single "
                    "company use getCompanyMarketWatch.",
        inputs=({"name": "sector", "type": "string", "desc": "PSX sector name, e.g. 'CEMENT'"},
                {"name": "limit", "type": "integer", "desc": "max companies (default 200)"}),
        outputs="list of {symbol, name, ldcp, open, high, low, price, change, change_pct, volume} for the sector",
        fn=get_sector_market_summary),
    "getSectorTurnover": Tool(
        name="getSectorTurnover",
        description="Today's SECTOR-WISE turnover board for the whole market: one row per PSX "
                    "sector with its advancing / declining / unchanged scrip counts, total "
                    "TURNOVER (shares traded) and market capitalization (Rs billion). Use for "
                    "'which sector traded the most / sector turnover / sector market cap / sector "
                    "breadth / where was today's activity'.",
        inputs=(),
        outputs="list of {sector_code, sector, advance, decline, unchange, turnover, market_cap_b} "
                "(sorted by turnover desc)",
        fn=get_sector_turnover),
    "getCompanyScreener": Tool(
        name="getCompanyScreener",
        description="ONE company's VALUATION & liquidity snapshot from the PSX stock screener: "
                    "P/E (TTM), dividend yield %, market cap, free float, 1-year return %, 30-day "
                    "average volume and price. The source for a company's P/E or dividend yield. "
                    "Market-derived (~5-min delay; 1-year return NOT payout-adjusted).",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX ticker (auto-detected)"},),
        outputs="dict: {symbol, name, sector, market_cap, price, change_pct, change_1y_pct, "
                "pe_ratio_ttm, dividend_yield_pct, free_float, volume_30d_avg, listed_in, status}",
        fn=get_company_screener),
    "getSectorScreener": Tool(
        name="getSectorScreener",
        description="VALUATION & liquidity snapshot for EVERY company in a SECTOR from the PSX "
                    "stock screener — P/E (TTM), dividend yield %, market cap, free float, 1-year "
                    "return %, 30-day avg volume, price — for peer comparison. Give a PSX sector "
                    "name; for ONE company use getCompanyScreener.",
        inputs=({"name": "sector", "type": "string", "desc": "PSX sector name, e.g. 'CEMENT'"},
                {"name": "limit", "type": "integer", "desc": "max companies listed (default 60)"}),
        outputs="dict: {sector, count, companies[] of {symbol, name, market_cap, price, "
                "change_1y_pct, pe_ratio_ttm, dividend_yield_pct, free_float, volume_30d_avg}}",
        fn=get_sector_screener),
    "getCompanyPeerComparison": Tool(
        name="getCompanyPeerComparison",
        description="Rank a company against its SAME-SECTOR peers on one valuation metric (P/E TTM, "
                    "dividend yield %, market cap, 1-year return %, price, 30-day volume) from the "
                    "PSX screener — returns the company's rank, percentile and the peer "
                    "median/average. Use for 'is X cheap/expensive/high-yield vs its peers / how "
                    "does X rank in its sector'. One call; no need to fetch the sector separately.",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX ticker (auto-detected)"},
                {"name": "metric", "type": "string", "desc": "pe_ratio_ttm (default) | dividend_yield_pct | market_cap | change_1y_pct | price | volume_30d_avg"},
                {"name": "limit", "type": "integer", "desc": "max peers listed (default 20)"}),
        outputs="dict: {symbol, sector, metric, company_value, rank, of_companies, percentile, "
                "peer_median, peer_average, ranked[] of {symbol, name, _metric_value, rank, is_query}}",
        fn=get_company_peer_comparison),
    "getCompanyVsSectorFundamentals": Tool(
        name="getCompanyVsSectorFundamentals",
        description="Compare ONE company's AUDITED profitability to its SECTOR aggregate for a year "
                    "(PSX analysis report, Rs MILLION): company net & PBT margin vs sector net & "
                    "PBT margin, with the gap (company minus sector) and above/below. Use for 'is X "
                    "more profitable than its sector / how does its margin compare to peers'.",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX ticker (auto-detected)"},
                {"name": "year", "type": "integer", "desc": "fiscal year, e.g. 2025"}),
        outputs="dict: {symbol, sector, company_net_margin_pct, company_pbt_margin_pct, "
                "sector_net_margin_pct, sector_pbt_margin_pct, net_margin_gap_pp, pbt_margin_gap_pp, "
                "net_margin_vs_sector, sector_companies}",
        fn=get_company_vs_sector_fundamentals),
    "getCompanySnapshot": Tool(
        name="getCompanySnapshot",
        description="One-shot company dossier merging THREE feeds: identity (symbol + sector), "
                    "today's LIVE quote (price, open/high/low, change %, volume, status) and the "
                    "valuation snapshot (market cap, P/E TTM, dividend yield %, 1-year return %, "
                    "free float, 30-day avg volume). Use for open-ended 'tell me about / give me a "
                    "rundown on / snapshot of <company>'.",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX ticker (auto-detected)"},),
        outputs="dict: {symbol, name, sector, live:{price, open, high, low, change, change_pct, "
                "volume, status}, valuation:{market_cap, pe_ratio_ttm, dividend_yield_pct, "
                "change_1y_pct, free_float, volume_30d_avg}}",
        fn=get_company_snapshot),
    "screenStocks": Tool(
        name="screenStocks",
        description="Rank PSX stocks across the WHOLE market (or within one sector) by a single "
                    "screener metric: dividend yield %, P/E (TTM), 1-year return %, market cap, "
                    "price or 30-day volume. order='desc' for the highest (top yield / biggest "
                    "gainers / largest cap), 'asc' for the lowest (cheapest P/E). Zero/blank values "
                    "are dropped. Use for 'highest dividend-yield / lowest-P/E / biggest 1-year "
                    "gainer stocks on PSX (or in a sector)'.",
        inputs=({"name": "metric", "type": "string", "desc": "dividend_yield_pct (default) | pe_ratio_ttm | change_1y_pct | market_cap | price | volume_30d_avg"},
                {"name": "sector", "type": "string", "desc": "optional PSX sector name to scope to, e.g. 'CEMENT'"},
                {"name": "order", "type": "string", "desc": "'desc' (highest, default) or 'asc' (lowest, e.g. cheapest P/E)"},
                {"name": "limit", "type": "integer", "desc": "how many to return (default 15)"}),
        outputs="dict: {metric, order, scope, universe_size, count, ranked[] of {symbol, name, "
                "_metric_value, market_cap, price, pe_ratio_ttm, dividend_yield_pct, change_1y_pct, rank}}",
        fn=screen_stocks),
    "getCompanyAnalysisReport": Tool(
        name="getCompanyAnalysisReport",
        description="ONE company's AUDITED yearly fundamentals for a given YEAR (Rs MILLION): "
                    "sales, profit-before-tax, profit-after-tax, shareholders' equity, total "
                    "assets, financial charges, dividend %s and shareholder count, plus its "
                    "sector. The source for a company's prior-year sales / profit / equity / "
                    "assets. Figures are Rs MILLION (the workbook is Rs thousand).",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX ticker (auto-detected)"},
                {"name": "year", "type": "integer", "desc": "fiscal year, e.g. 2025"}),
        outputs="dict: {symbol, name, sector, fiscal_year, sales, pbt, pat, equity, total_assets, "
                "financial_charges, cash_dividend_pct, stock_dividend_pct, shareholders}",
        fn=get_company_analysis_report),
    "getSectorAnalysisReport": Tool(
        name="getSectorAnalysisReport",
        description="A SECTOR's AUDITED yearly fundamentals for a given YEAR (Rs MILLION): every "
                    "company in the sector plus the sector AGGREGATE — total sales / PAT / PBT and "
                    "the sector NET and PBT margin. The source for sector profitability and peer "
                    "comparison. With no sector, returns whole-market totals. Figures are Rs "
                    "MILLION (the workbook is Rs thousand).",
        inputs=({"name": "sector", "type": "string", "desc": "PSX sector name, e.g. 'CEMENT'"},
                {"name": "year", "type": "integer", "desc": "fiscal year, e.g. 2025"},
                {"name": "limit", "type": "integer", "desc": "max companies listed (default 60)"}),
        outputs="dict: {sector, companies_total, total_sales, total_pat, total_pbt, "
                "net_margin_pct, pbt_margin_pct, companies[]}",
        fn=get_sector_analysis_report),
    "getFutures": Tool(
        name="getFutures",
        description="WHOLE-BOARD snapshot of today's DELIVERABLE FUTURES: top gaining / losing "
                    "contracts (by % change) and most-active contracts (by volume), across all "
                    "listed futures. Use for 'futures market overview / which futures are moving'.",
        inputs=({"name": "limit", "type": "integer", "desc": "how many per list (default 10)"},),
        outputs="dict: board_size, top_gainers[], top_losers[], most_active[] (each {symbol, contract, price, change_pct, volume})",
        fn=get_futures),
    "getCompanyMarketWatch": Tool(
        name="getCompanyMarketWatch",
        description="Today's LIVE PSX trading line for one company: current price, day "
                    "open/high/low, change, change %, volume, LDCP, board status tag. Use for "
                    "'today's price / how is X trading now / today's volume'.",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX ticker (auto-detected)"},),
        outputs="dict: symbol, name, sector, price, open, high, low, change, change_pct, volume, status",
        fn=get_company_market_watch),
    "getSectorMarketWatch": Tool(
        name="getSectorMarketWatch",
        description="Today's LIVE PSX quotes for EVERY company in a sector (price/change/volume per "
                    "name) — the basis for sector movers/gainers/losers. Give a PSX sector name; "
                    "for a single company use getCompanyMarketWatch.",
        inputs=({"name": "sector", "type": "string", "desc": "PSX sector name, e.g. 'CEMENT'"},),
        outputs="list of {symbol, name, sector, price, change, change_pct, volume, status} for the sector",
        fn=get_sector_market_watch),
    "getCompanyFutures": Tool(
        name="getCompanyFutures",
        description="Today's DELIVERABLE FUTURES contracts for one company — one row per contract "
                    "month (e.g. MTL-JUN, MTL-JUL): price, day open/high/low, change, change %, "
                    "volume. Use for futures / deliverable futures contract questions.",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX base ticker (auto-detected)"},),
        outputs="list of {symbol, base_symbol, contract, price, open, high, low, change, change_pct, volume}",
        fn=get_company_futures),
    "getCashSettledFutures": Tool(
        name="getCashSettledFutures",
        description="WHOLE-BOARD snapshot of today's CASH-SETTLED FUTURES (CSF): top gaining / "
                    "losing contracts (by % change) and most-active contracts (by volume), across "
                    "all listed CSF. Use for 'cash-settled futures overview / which CSF are moving'. "
                    "For DELIVERABLE futures use getFutures instead.",
        inputs=({"name": "limit", "type": "integer", "desc": "how many per list (default 10)"},),
        outputs="dict: board_size, top_gainers[], top_losers[], most_active[] (each {symbol, contract, price, change_pct, volume})",
        fn=get_cash_settled_futures),
    "getCompanyCashSettledFutures": Tool(
        name="getCompanyCashSettledFutures",
        description="Today's CASH-SETTLED FUTURES (CSF) contracts for one company — one row per "
                    "contract month: price, day open/high/low, change, change %, volume. Use for "
                    "cash-settled futures contract questions about a company. For DELIVERABLE "
                    "futures use getCompanyFutures instead.",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX base ticker (auto-detected)"},),
        outputs="list of {symbol, base_symbol, contract, price, open, high, low, change, change_pct, volume}",
        fn=get_company_cash_settled_futures),
    "getCompanyPayouts": Tool(
        name="getCompanyPayouts",
        description="A company's dividend / payout history — each entry: announcement date, result "
                    "period, payout percent, whether interim or final and cash-dividend / bonus / "
                    "right, and the book-closure window. Use for dividend, payout, bonus, or "
                    "book-closure questions about any listed company.",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX ticker (auto-detected)"},
                {"name": "limit", "type": "integer", "desc": "max payout entries (default 20)"}),
        outputs="list of {date, financial_results, details, book_closure, payout_pct, interim, "
                "final, dividend, bonus, right}",
        fn=get_company_payouts),
    "getCompanySymbol": Tool(
        name="getCompanySymbol",
        description="Resolve a company's PSX ticker symbol from its name.",
        inputs=({"name": "company", "type": "string", "desc": "company name as written (or a ticker)"},),
        outputs="symbol (ticker) + official listed name",
        fn=get_company_symbol),
    "getCompanySector": Tool(
        name="getCompanySector",
        description="Look up which PSX sector a company belongs to.",
        inputs=({"name": "company", "type": "string", "desc": "company name as written (or a ticker)"},),
        outputs="sector name (+ the company's symbol)",
        fn=get_company_sector),
    "getCompanyCompetitors": Tool(
        name="getCompanyCompetitors",
        description="List a company's competitors — the other listed companies in its same PSX sector.",
        inputs=({"name": "company", "type": "string", "desc": "company name as written (or a ticker)"},),
        outputs="list of competitor {symbol, name} + their shared sector",
        fn=get_company_competitors),
    "getSectorCompanies": Tool(
        name="getSectorCompanies",
        description="List every PSX-listed company in a given sector.",
        inputs=({"name": "sector", "type": "string", "desc": "PSX sector name, e.g. 'CEMENT'"},),
        outputs="list of {symbol, name} in that sector",
        fn=get_sector_companies),
    "getCompanyAnnouncements": Tool(
        name="getCompanyAnnouncements",
        description="Recent official PSX announcements/disclosures filed BY one company "
                    "(board meetings, dividends/corporate actions, material info, EOGM notices).",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX ticker (auto-detected)"},
                {"name": "limit", "type": "integer", "desc": "max announcements (default 15)"}),
        outputs="list of {date, time, symbol, name, title, status, pdf_url, doc_id}",
        fn=get_company_announcements),
    "getSectorAnnouncements": Tool(
        name="getSectorAnnouncements",
        description="Recent PSX announcements ACROSS a whole sector / a company's competitors — "
                    "resolves the input to a sector, lists that sector's companies (i.e. the "
                    "company's same-sector peers/competitors), calls the announcements feed once "
                    "per company, and concatenates. Give a PSX sector name; for ONE company's "
                    "announcements use getCompanyAnnouncements.",
        inputs=({"name": "sector", "type": "string", "desc": "PSX sector name, e.g. 'CEMENT'"},
                {"name": "max_companies", "type": "integer", "desc": "cap on companies queried (default 8)"},
                {"name": "per_company", "type": "integer", "desc": "announcements per company (default 5)"}),
        outputs="concatenated {date, time, symbol, name, title, status, pdf_url, doc_id} + per_company counts",
        fn=get_sector_announcements),
    "getCompanySECPNotices": Tool(
        name="getCompanySECPNotices",
        description="SECP regulatory orders/notices concerning ONE company (enforcement, "
                    "show-cause orders, sanctions, auditor/governance actions). NOTE: the SECP feed "
                    "is market-wide with no ticker field — matching is by the company NAME in the "
                    "order title, so a clean/large listed company often has NONE.",
        inputs=({"name": "company", "type": "string", "desc": "company name OR PSX ticker (auto-detected; used to search titles)"},
                {"name": "limit", "type": "integer", "desc": "max notices to scan (default 15)"}),
        outputs="list of {date, time, title, status, pdf_url, doc_id} (+ raw_matches count)",
        fn=get_company_secp_notices),
    "getSectorSECPNotices": Tool(
        name="getSectorSECPNotices",
        description="SECP regulatory orders across a sector / a company's competitors. SECP has no "
                    "sector key, so this lists the sector's companies and runs the SECP title-search "
                    "by NAME per company, then concatenates. Results are typically sparse (most "
                    "listed companies have no SECP orders). Give a PSX sector name; for ONE "
                    "company's notices use getCompanySECPNotices.",
        inputs=({"name": "sector", "type": "string", "desc": "PSX sector name, e.g. 'CEMENT'"},
                {"name": "max_companies", "type": "integer", "desc": "cap on companies queried (default 8)"},
                {"name": "per_company", "type": "integer", "desc": "notices scanned per company (default 5)"}),
        outputs="concatenated {date, time, title, status, pdf_url, doc_id} + per_company counts",
        fn=get_sector_secp_notices),
}


def list_tools() -> list[dict]:
    """The tool menu sent to the planner — name, what it does, inputs, outputs (no fn)."""
    return [{"name": t.name, "description": t.description,
             "inputs": [dict(i) for i in t.inputs], "outputs": t.outputs}
            for t in TOOLS.values()]


def run_tool(engine, name: str, args: dict | None) -> ToolResult:
    """Evaluate a planner-selected tool by name with its filled args."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"tool": name, "note": f"unknown tool '{name}'"}, [], []
    try:
        return tool.fn(engine, **(args or {}))
    except TypeError as e:  # bad/missing args from the planner
        return {"tool": name, "note": f"bad arguments: {e}"}, [], []
    except Exception as e:  # noqa: BLE001 — never let a tool crash the controller
        return {"tool": name, "note": f"error: {e}"}, [], []
