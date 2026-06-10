"""External primitives — PSX-sourced data the workbook doesn't hold.

Phase 2 ships the sector primitive: a sector-level profitability benchmark computed from the PSX
yearly analysis report. PSX publishes sales / PBT / PAT per company (NOT cost of sales), so we
can aggregate NET and PBT margin across a sector — but NOT gross margin. The primitive states
that limit in its result so the composer can say so plainly instead of fabricating a gross
figure (the demo's failure mode).

The aggregate is emitted as a CalcResult (so the numeric guard admits it) plus an external
EvidenceItem (so the grounding gate sees genuine sector/peer evidence and lets the answer stand).
"""

from __future__ import annotations

import logging

from .models import Citation, CalcResult, EvidenceItem

_log = logging.getLogger("app.engines.fie")


# Process-global caches for the PSX yearly analysis report. The report is published once a year
# and the engine is rebuilt per request, so caching here (not per-engine) avoids re-downloading
# the 155 KB file on every query. _REPORT_MISS negative-caches years with no published report yet
# (so a year=None search doesn't keep hitting 404s on the not-yet-released latest year).
_REPORT_CACHE: dict[int, dict] = {}   # year -> {symbol: record}
_REPORT_MISS: set[int] = set()        # years known to have no report (404 / empty)


def _load_report(ar, year: int) -> dict:
    y = int(year)
    if y in _REPORT_CACHE:
        return _REPORT_CACHE[y]
    if y in _REPORT_MISS:
        return {}
    try:
        recs = ar._records(y)
    except Exception:  # noqa: BLE001
        recs = {}
    if recs:
        _REPORT_CACHE[y] = recs
    else:
        _REPORT_MISS.add(y)
    return recs


def _adapters(engine):
    """Lazily build + cache an ApiClient/Symbols/AnalysisReports trio on the engine. PSX endpoints
    are public (no key); the analysis report is cached per year inside the adapter."""
    cache = engine.__dict__.setdefault("_ext", {})
    if "ar" not in cache:
        from .apis import ApiClient, HttpTransport, Symbols
        from .apis.analysis_reports import AnalysisReports
        client = ApiClient(HttpTransport())
        try:
            client.begin_request(50)  # set a per-run external-call budget if the client tracks one
        except Exception:  # noqa: BLE001
            pass
        syms = Symbols(client)
        cache["syms"] = syms
        cache["ar"] = AnalysisReports(client, symbols=syms)
    return cache["ar"], cache["syms"]


def _registry_fetcher(engine):
    """Lazily build + cache a RegistryFetcher (shares the cached ApiClient)."""
    cache = engine.__dict__.setdefault("_ext", {})
    if "rf" not in cache:
        _adapters(engine)  # ensures cache['syms'] + a client exist
        from .apis import ApiClient, HttpTransport
        from .apis.fetch import RegistryFetcher
        # reuse the same client the adapters built (it carries the call budget)
        client = cache.get("client")
        if client is None:
            client = ApiClient(HttpTransport())
            try:
                client.begin_request(50)
            except Exception:  # noqa: BLE001
                pass
            cache["client"] = client
        cache["rf"] = RegistryFetcher(client)
    return cache["rf"]


def list_apis() -> list[dict]:
    """The PSX API catalog menu the planner selects from — an opaque INDEX, what each API does,
    and the exact fields it RETURNS. The name is withheld on purpose: the planner must choose by
    the corrected description + actual return fields, not by guessing from a suggestive name. The
    chosen index maps back to the API in the rule-base (api_for_index)."""
    from .apis.registry import REGISTRY
    out = []
    for i, a in enumerate(REGISTRY):        # `i` is the canonical REGISTRY index (api_for_index)
        if a.name in _TOOL_SUPERSEDED:      # covered by a named tool (tools.py) -> not an opaque api
            continue
        entry = {"index": i, "description": a.description, "returns": list(a.returns)}
        if a.use_for:                       # what WE use it for — extra routing signal for the planner
            entry["use_for"] = a.use_for
        out.append(entry)
    return out


# APIs now exposed as named, typed tools (external.list_tools/tools.py) instead of an opaque
# catalog index — the planner picks the tool, not the raw api.
_TOOL_SUPERSEDED = {"symbols_master", "company_announcements", "sector_announcements",
                    "secp_notices", "sector_secp_notices", "company_overview", "company_payouts",
                    "market_watch", "sector_market_watch", "performers",
                    "debt_performers", "debt_market_watch",
                    "deliverable_futures_market_watch", "company_deliverable_futures_market_watch",
                    "daily_market_summary", "sector_summary", "analysis_reports",
                    "stock_screener", "sector_stock_screener"}


def api_for_index(idx) -> str | None:
    """Map a planner-selected catalog index back to the API name (rule-base side)."""
    from .apis.registry import REGISTRY
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return None
    return REGISTRY[i].name if 0 <= i < len(REGISTRY) else None


def company_identity(engine) -> dict:
    """Resolve the workbook company's PSX ticker + sector ONCE (cached), deterministically, from
    the full symbols master — NOT from a truncated API table (which previously mislabelled Millat
    as 'BILLS AND BONDS'). Goes into the workbook metadata so the planner/composer treat the
    sector as a known fact rather than guessing it from a market feed."""
    cache = engine.__dict__.setdefault("_ext", {})
    if "identity" not in cache:
        symbol = sector = None
        try:
            syms = cache.get("syms")
            if syms is None:
                from .apis import ApiClient, HttpTransport, Symbols
                client = cache.get("client")
                if client is None:
                    client = ApiClient(HttpTransport())
                    try:
                        client.begin_request(50)
                    except Exception:  # noqa: BLE001
                        pass
                    cache["client"] = client
                syms = Symbols(client)
                cache["syms"] = syms
            symbol = syms.ticker_for(engine.store.company)
            sector = syms.sector_for(symbol) if symbol else None
        except Exception:  # noqa: BLE001 — offline/missing dep: leave unknown, never crash
            pass
        cache["identity"] = {"symbol": symbol, "sector": sector}
    return cache["identity"]


def call_api(engine, name: str, params: dict | None = None):
    """Generic: fetch+parse one catalog API by name and return its rows as external evidence.
    The data lives in each item's claim + locator (numbers there are admitted by the numeric
    guard as cited external chunks). Some APIs return the whole market — filtered to the symbol
    when one is given. Returns (result, external_evidence, [])."""
    from .apis.registry import BY_NAME
    api = BY_NAME.get(name)
    if api is None:
        return {"api": name, "note": f"unknown api '{name}'"}, [], []
    params = params or {}
    try:
        rf = _registry_fetcher(engine)   # builds + caches the syms adapter too
    except Exception as e:  # noqa: BLE001
        return {"api": name, "note": f"fetch setup failed: {e}"}, [], []
    # resolve the ticker AFTER the fetcher exists (so syms is available) — many APIs need it,
    # and market-wide ones are filtered to it below.
    sym = params.get("symbol")
    syms = engine.__dict__.get("_ext", {}).get("syms")
    if sym is None and params.get("company") and syms is not None:
        sym = syms.ticker_for(params["company"])
    try:
        res = rf.fetch(api, symbol=sym, query=params.get("query"),
                       year=params.get("year"), sector=params.get("sector"))
    except Exception as e:  # noqa: BLE001
        return {"api": name, "note": f"fetch failed: {e}"}, [], []
    items = list(res.items or [])
    if sym:  # market-wide APIs (e.g. stock_screener) return everything — narrow to the symbol
        narrowed = [it for it in items
                    if it.citations and (it.citations[0].locator or {}).get("symbol") == sym]
        if narrowed:
            items = narrowed
    rows = []
    for it in items[:12]:
        loc = (it.citations[0].locator if it.citations else {}) or {}
        extra = {k: v for k, v in loc.items()
                 if k not in ("source", "api", "category", "retrieved_at", "chunk_text")}
        rows.append({"claim": it.claim, **extra})
    out = {"api": name, "status": res.status, "count": len(items), "rows": rows}
    if not items:
        out["note"] = "no data returned"
    return out, items, []


def news_search(engine, query: str, *, symbol=None):
    """Recent news via the existing 8-provider finance-ordered failover chain (apis/news.py).
    Returns article evidence (title + URL + snippet). Degrades honestly to 'no news' when no
    provider key is configured or none yields articles. Returns (result, external_evidence, [])."""
    cache = engine.__dict__.setdefault("_ext", {})
    news = getattr(getattr(engine, "external", None), "news", None)
    if news is None:
        if "news" not in cache:
            try:
                from .apis import ApiClient, HttpTransport
                from .apis.news import News
                client = cache.get("client")
                if client is None:
                    client = ApiClient(HttpTransport())
                    try:
                        client.begin_request(50)
                    except Exception:  # noqa: BLE001
                        pass
                    cache["client"] = client
                cache["news"] = News(client)
            except Exception as e:  # noqa: BLE001
                return {"news": query, "note": f"news unavailable: {e}"}, [], []
        news = cache["news"]

    if symbol is None and cache.get("syms") is not None:
        symbol = cache["syms"].ticker_for(engine.store.company)
    try:
        res = news.search(query, symbol=symbol)
    except Exception as e:  # noqa: BLE001
        return {"news": query, "note": f"news search failed: {e}"}, [], []
    items = list(res.items or [])
    rows = []
    for it in items[:8]:
        loc = (it.citations[0].locator if it.citations else {}) or {}
        rows.append({"title": it.claim, "url": loc.get("url"),
                     "snippet": (loc.get("snippet") or "")[:160], "published": loc.get("published_at")})
    out = {"news": query, "count": len(items), "articles": rows}
    if not items:
        out["note"] = "no recent news found" + (f" ({res.note})" if res.note else "")
    return out, items, []


def build_search_query(query: str, hints: dict | None) -> str:
    """Compose a precise open-web query from the user's question plus the LLM-extracted hints
    (company, sector, years, keywords). Hints make PSX/web lookups specific instead of vague."""
    h = hints or {}
    parts = [str(h.get("company") or "").strip(), str(h.get("sector") or "").strip(),
             str(h.get("keywords") or "").strip()]
    yrs = h.get("years") or []
    if isinstance(yrs, list):
        parts.append(" ".join(str(y) for y in yrs if y))
    extra = " ".join(p for p in parts if p)
    base = (query or "").strip()
    # keywords lead (they're the distilled intent); fall back to the raw question if no hints.
    return (f"{extra} — {base}" if extra else base).strip(" —") or base


def web_search(engine, query: str, *, hints: dict | None = None):
    """TERMINAL last-resort open-web search via the hosted OpenAI `web_search` tool. Captures the
    grounded summary + each cited source as EXTERNAL EvidenceItems so (a) any figure quoted in the
    answer is backed by the numeric guard (it appears verbatim in the summary/snippet text) and
    (b) the grounding gate's scope check is satisfied (real external evidence now exists, so a
    sector/peer/other-company answer is legitimate). Degrades to an honest 'not found' note."""
    q = build_search_query(query, hints)
    llm = getattr(engine, "llm", None)
    res = llm.web_search(q) if (llm and hasattr(llm, "web_search")) else None
    if not res or not (res.get("text") or res.get("sources")):
        return {"web": q, "note": "open-web search returned nothing"}, [], []

    text = (res.get("text") or "").strip()
    sources = res.get("sources") or []
    evidence: list[EvidenceItem] = []
    # 1) the grounded SUMMARY — carries the figures the answer will quote; cite it to the top
    #    source so it dedupes onto a real link. Its claim text is what the numeric guard whitelists.
    top = sources[0] if sources else {}
    if text:
        evidence.append(EvidenceItem(
            claim=text[:1500], value=None, unit=None, kind="external",
            citations=[Citation(ref_id="C?", kind="external",
                                 display=(top.get("title") or "Open-web result"),
                                 locator={"source": "web", "url": top.get("url"),
                                          "link": top.get("url"), "snippet": text[:1500]})],
            reliability=0.6))
    # 2) one EvidenceItem per distinct source — gives the response a citation per link, and each
    #    snippet's numbers are independently whitelisted.
    for s in sources:
        snip = (s.get("snippet") or "")[:1000]
        evidence.append(EvidenceItem(
            claim=(s.get("title") or s.get("url") or "source")[:200], value=None, unit=None,
            kind="external",
            citations=[Citation(ref_id="C?", kind="external", display=(s.get("title") or s.get("url")),
                                locator={"source": "web", "url": s.get("url"), "link": s.get("url"),
                                         "snippet": snip})],
            reliability=0.6))
    return {"web": q, "text": text, "n_sources": len(sources)}, evidence, []


def _ext_evidence(claim: str, value: float, year: int, sector: str, n: int) -> EvidenceItem:
    cite = Citation(ref_id="C?", kind="external",
                    display=f"PSX analysis report {year} — {sector} sector ({n} companies)",
                    locator={"source": "PSX.AnalysisReports", "year": int(year),
                             "sector": sector, "companies": n})
    return EvidenceItem(claim=claim, value=value, unit="percent", kind="external",
                        citations=[cite], reliability=0.85)


def sector_profitability(engine, year, *, symbol=None):
    """Sector NET and PBT margin for the subject company's sector, from the PSX analysis report.
    Returns (result, external_evidence, calcs). Gross margin is NOT available (PSX has no COGS)."""
    try:
        ar, syms = _adapters(engine)
    except Exception as e:  # noqa: BLE001 — missing httpx / offline must degrade, not crash
        return {"note": f"external data unavailable: {e}"}, [], []

    company = engine.store.company
    sym = symbol or (syms.ticker_for(company) if syms else None)

    # try the requested year, else newest DATA year first. NB: store.years includes empty
    # forecast slots (e.g. 2026-2030) that have no PSX report — iterate only years that actually
    # carry workbook values, so the search doesn't burn out on unpublished future years.
    if year is not None:
        yrs = [int(year)]
    else:
        df = engine.store.findata
        data_years = (sorted({int(y) for y in df[df["value"].notna()]["year"].dropna().unique()})
                      if df is not None and not df.empty else list(engine.store.years))
        yrs = list(reversed(data_years))
    recs, used = {}, None
    for y in yrs[:6]:
        recs = _load_report(ar, int(y))
        if recs:
            used = int(y)
            break
    if not recs:
        return {"note": f"PSX analysis report unavailable for {year or 'recent years'}"}, [], []

    me = recs.get(sym) if sym else None
    sector = (me or {}).get("sector")
    if not sector:
        return {"note": f"could not resolve the sector for {company} in PSX data"}, [], []

    peers = [r for r in recs.values() if (r.get("sector") or "") == sector and r.get("sales")]
    ssales = sum(r["sales"] for r in peers)
    spat = sum((r.get("pat") or 0.0) for r in peers)
    spbt = sum((r.get("pbt") or 0.0) for r in peers)
    if not ssales:
        return {"note": f"no sector sales recorded for {sector} in {used}"}, [], []

    net = round(spat / ssales, 6)
    pbt = round(spbt / ssales, 6)
    ev = [_ext_evidence(f"{sector} sector net margin FY{used}", net, used, sector, len(peers)),
          _ext_evidence(f"{sector} sector PBT margin FY{used}", pbt, used, sector, len(peers))]
    calcs = [
        CalcResult(formula_id="sector_net_margin", value=net, unit="percent", confidence="Medium",
                   expression="sum(PAT)/sum(sales) across the sector",
                   note=f"{sector}, {len(peers)} companies, FY{used} (PSX analysis report)"),
        CalcResult(formula_id="sector_pbt_margin", value=pbt, unit="percent", confidence="Medium",
                   expression="sum(PBT)/sum(sales) across the sector",
                   note=f"{sector}, {len(peers)} companies, FY{used} (PSX analysis report)"),
    ]
    res = {
        "kind": "sector", "sector": sector, "year": used, "companies": len(peers),
        "net_margin": net, "pbt_margin": pbt,
        "peers": [r["symbol"] for r in peers],
        "note": ("PSX publishes no cost-of-sales, so a sector GROSS margin cannot be computed; "
                 "these are sector NET and PBT margins."),
    }
    _log.info("sector_profitability: sector=%s year=%s cos=%d net=%.4f pbt=%.4f",
              sector, used, len(peers), net, pbt, extra={"component": "Retrieve"})
    return res, ev, calcs
