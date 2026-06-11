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


_PLACEHOLDER_HINTS = {"unknown", "n/a", "none", "null", ""}


def _clean_hint(v) -> str:
    """A hint value as a clean string: lists are joined, placeholder values ('unknown', 'n/a',
    'none') are dropped — so a vague LLM hint never leaks a literal 'unknown' into the web query."""
    if isinstance(v, (list, tuple)):
        return " ".join(_clean_hint(x) for x in v if _clean_hint(x))
    s = str(v if v is not None else "").strip()
    return "" if s.lower() in _PLACEHOLDER_HINTS else s


def build_search_query(query: str, hints: dict | None) -> str:
    """Compose a precise open-web query from the user's question plus the LLM-extracted hints
    (company, sector, years, keywords). Hints make PSX/web lookups specific instead of vague.
    Placeholder/empty hint values are dropped and list-valued keywords/years are flattened, so the
    query is company-scoped rather than carrying junk like 'unknown' or a Python list repr."""
    h = hints or {}
    parts = [_clean_hint(h.get("company")), _clean_hint(h.get("sector")),
             _clean_hint(h.get("keywords"))]
    yrs = h.get("years") or []
    parts.append(" ".join(str(y) for y in yrs if y) if isinstance(yrs, list) else _clean_hint(yrs))
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
