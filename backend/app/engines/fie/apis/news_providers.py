"""News provider definitions (L3b) — the free-tier APIs the news layer fails over
across, ordered by how *finance-native* they are.

Each provider declares: where its key lives in Settings, how to build a
QUERY-RELEVANT request (symbol-scoped when we have a ticker, else keyword search),
and how to parse its JSON into ``NewsArticle`` (content + source). The adapter
(news.py) walks ``PROVIDERS`` in order and uses the first one that returns articles
— so a provider hitting its free-tier limit (429 / error body / empty) just hands
off to the next.

Finance-native ranking:
  1. marketaux      — finance-native: symbol/entity tagging + sentiment + search
  2. finnhub        — finance-native: company-news by ticker (symbol-only)
  3. alphavantage   — finance-native: NEWS_SENTIMENT by ticker (symbol-only)
  4. newsdata.io    — general, but a 'business' category + real keyword search
  5. newsapi.ai     — general (Event Registry): strong keyword search
  6. worldnewsapi   — general keyword search
  7. gnews.io       — general keyword search
  8. newsapi.org    — general; free tier is 24h-delayed + non-commercial → last

finnhub & alphavantage are ticker-only feeds (no free-text search), so they are
marked ``requires_symbol`` and skipped on keyword-only queries to keep results
relevant to the user's query.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Optional

from ..models import NewsArticle


@dataclass(frozen=True)
class NewsProvider:
    id: str
    base_url: str
    path: str
    key_setting: str                 # attribute on Settings holding this provider's key
    build: Callable                  # (query, symbol, key, limit, anchor_date) -> (path, params)
    parse: Callable                  # (raw) -> list[NewsArticle]
    reliability: float = 0.6
    requires_symbol: bool = False    # ticker-only feed; skip on keyword-only queries


def _s(v) -> Optional[str]:
    return v if isinstance(v, str) and v else None


def _author(v) -> Optional[str]:
    """Normalize a byline: str, list[str], or list[{'name': ...}] -> comma string."""
    if isinstance(v, str):
        return v or None
    if isinstance(v, list):
        names = []
        for a in v:
            if isinstance(a, str) and a:
                names.append(a)
            elif isinstance(a, dict) and _s(a.get("name")):
                names.append(a["name"])
        return ", ".join(names) or None
    return None


def _window(anchor_date: Optional[str], days: int = 30) -> tuple[str, str]:
    end = anchor_date or date.today().isoformat()
    try:
        start = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    except ValueError:
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=days)).isoformat()
    return start, end


# --- 1. marketaux ----------------------------------------------------------
def _build_marketaux(query, symbol, key, limit, anchor_date):
    p = {"api_token": key, "language": "en", "limit": min(limit, 100)}
    if symbol:
        p["symbols"] = symbol           # query-relevant: scope to the ticker
        p["filter_entities"] = "true"
    else:
        p["search"] = query             # else free-text search
    return "v1/news/all", p


def _parse_marketaux(raw):
    if not isinstance(raw, dict):
        return []
    out = []
    for a in raw.get("data", []) or []:
        ents = [e.get("symbol") for e in (a.get("entities") or []) if e.get("symbol")]
        sent = next((e.get("sentiment_score") for e in (a.get("entities") or [])
                     if e.get("sentiment_score") is not None), None)
        out.append(NewsArticle(
            title=_s(a.get("title")) or "", description=_s(a.get("description") or a.get("snippet")),
            content=_s(a.get("snippet")), url=_s(a.get("url")), source=_s(a.get("source")),
            provider="marketaux", published_at=_s(a.get("published_at")), language="en",
            symbols=ents, sentiment=sent))
    return out


# --- 2. finnhub (company-news by ticker; symbol-only) ----------------------
def _build_finnhub(query, symbol, key, limit, anchor_date):
    start, end = _window(anchor_date, 30)
    return "company-news", {"symbol": symbol, "from": start, "to": end, "token": key}


def _parse_finnhub(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        ts = a.get("datetime")
        when = None
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                from datetime import datetime, timezone
                when = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            except (ValueError, OSError, OverflowError):
                when = None
        out.append(NewsArticle(
            title=_s(a.get("headline")) or "", description=_s(a.get("summary")),
            content=_s(a.get("summary")), url=_s(a.get("url")), source=_s(a.get("source")),
            provider="finnhub", published_at=when, language="en",
            symbols=[s for s in [_s(a.get("related"))] if s]))
    return out


# --- 3. alphavantage NEWS_SENTIMENT (by ticker; symbol-only) ---------------
def _build_alphavantage(query, symbol, key, limit, anchor_date):
    return "query", {"function": "NEWS_SENTIMENT", "tickers": symbol,
                     "apikey": key, "limit": min(limit, 1000), "sort": "LATEST"}


def _parse_alphavantage(raw):
    if not isinstance(raw, dict):
        return []
    out = []
    for a in raw.get("feed", []) or []:
        tp = _s(a.get("time_published"))          # YYYYMMDDTHHMMSS
        when = (f"{tp[0:4]}-{tp[4:6]}-{tp[6:8]}" if tp and len(tp) >= 8 else None)
        syms = [t.get("ticker") for t in (a.get("ticker_sentiment") or []) if t.get("ticker")]
        try:
            sent = float(a["overall_sentiment_score"]) if a.get("overall_sentiment_score") is not None else None
        except (TypeError, ValueError):
            sent = None
        out.append(NewsArticle(
            title=_s(a.get("title")) or "", description=_s(a.get("summary")),
            content=_s(a.get("summary")), url=_s(a.get("url")), source=_s(a.get("source")),
            author=_author(a.get("authors")), provider="alphavantage", published_at=when,
            language="en", symbols=syms, sentiment=sent))
    return out


# --- 4. newsdata.io --------------------------------------------------------
def _build_newsdata(query, symbol, key, limit, anchor_date):
    return "api/1/news", {"apikey": key, "q": query or symbol or "", "language": "en"}


def _parse_newsdata(raw):
    if not isinstance(raw, dict) or raw.get("status") not in (None, "success"):
        return []
    out = []
    for a in raw.get("results", []) or []:
        src = _s(a.get("source_id")) or _s(a.get("source_name"))
        out.append(NewsArticle(
            title=_s(a.get("title")) or "", description=_s(a.get("description")),
            content=_s(a.get("content") or a.get("description")), url=_s(a.get("link")),
            source=src, author=_author(a.get("creator")), provider="newsdata",
            published_at=_s(a.get("pubDate")), language="en"))
    return out


# --- 5. newsapi.ai (Event Registry) ----------------------------------------
def _build_newsapi_ai(query, symbol, key, limit, anchor_date):
    return "api/v1/article/getArticles", {
        "apiKey": key, "keyword": query or symbol or "", "lang": "eng",
        "resultType": "articles", "articlesSortBy": "date",
        "articlesCount": min(limit, 100), "articleBodyLen": -1}


def _parse_newsapi_ai(raw):
    if not isinstance(raw, dict):
        return []
    results = ((raw.get("articles") or {}).get("results")
               if isinstance(raw.get("articles"), dict) else None) or []
    out = []
    for a in results:
        if not isinstance(a, dict):
            continue
        src = a.get("source")
        src = _s(src.get("title")) if isinstance(src, dict) else _s(src)
        out.append(NewsArticle(
            title=_s(a.get("title")) or "", description=_s(a.get("body"))[:280] if _s(a.get("body")) else None,
            content=_s(a.get("body")), url=_s(a.get("url")), source=src,
            author=_author(a.get("authors")), provider="newsapi_ai",
            published_at=_s(a.get("dateTime") or a.get("date")), language=_s(a.get("lang"))))
    return out


# --- 6. worldnewsapi.com ---------------------------------------------------
def _build_worldnews(query, symbol, key, limit, anchor_date):
    return "search-news", {"api-key": key, "text": query or symbol or "",
                           "language": "en", "number": min(limit, 100),
                           "sort": "publish-time", "sort-direction": "DESC"}


def _parse_worldnews(raw):
    if not isinstance(raw, dict):
        return []
    out = []
    for a in raw.get("news", []) or []:
        if not isinstance(a, dict):
            continue
        out.append(NewsArticle(
            title=_s(a.get("title")) or "", description=_s(a.get("summary")),
            content=_s(a.get("text")), url=_s(a.get("url")),
            source=_s(a.get("source_country")), author=_author(a.get("authors") or a.get("author")),
            provider="worldnews", published_at=_s(a.get("publish_date")), language="en"))
    return out


# --- 7. gnews.io -----------------------------------------------------------
def _build_gnews(query, symbol, key, limit, anchor_date):
    return "api/v4/search", {"apikey": key, "q": query or symbol or "",
                             "lang": "en", "max": min(limit, 100)}


def _parse_gnews(raw):
    if not isinstance(raw, dict):
        return []
    out = []
    for a in raw.get("articles", []) or []:
        src = a.get("source")
        src = _s(src.get("name")) if isinstance(src, dict) else _s(src)
        out.append(NewsArticle(
            title=_s(a.get("title")) or "", description=_s(a.get("description")),
            content=_s(a.get("content") or a.get("description")), url=_s(a.get("url")),
            source=src, provider="gnews", published_at=_s(a.get("publishedAt")), language="en"))
    return out


# --- 8. newsapi.org (last: 24h delay + non-commercial free tier) -----------
def _build_newsapi_org(query, symbol, key, limit, anchor_date):
    return "v2/everything", {"apiKey": key, "q": query or symbol or "",
                             "language": "en", "sortBy": "publishedAt",
                             "pageSize": min(limit, 100)}


def _parse_newsapi_org(raw):
    if not isinstance(raw, dict) or raw.get("status") == "error":
        return []
    out = []
    for a in raw.get("articles", []) or []:
        src = a.get("source")
        src = _s(src.get("name")) if isinstance(src, dict) else _s(src)
        out.append(NewsArticle(
            title=_s(a.get("title")) or "", description=_s(a.get("description")),
            content=_s(a.get("content") or a.get("description")), url=_s(a.get("url")),
            source=src, author=_author(a.get("author")), provider="newsapi_org",
            published_at=_s(a.get("publishedAt")), language="en"))
    return out


# finance-native first; ticker-only feeds flagged requires_symbol
PROVIDERS: list[NewsProvider] = [
    NewsProvider("marketaux", "https://api.marketaux.com", "v1/news/all",
                 "news_marketaux_key", _build_marketaux, _parse_marketaux, reliability=0.7),
    NewsProvider("finnhub", "https://finnhub.io/api/v1", "company-news",
                 "news_finnhub_key", _build_finnhub, _parse_finnhub,
                 reliability=0.7, requires_symbol=True),
    NewsProvider("alphavantage", "https://www.alphavantage.co", "query",
                 "news_alphavantage_key", _build_alphavantage, _parse_alphavantage,
                 reliability=0.68, requires_symbol=True),
    NewsProvider("newsdata", "https://newsdata.io", "api/1/news",
                 "news_newsdata_io_key", _build_newsdata, _parse_newsdata, reliability=0.6),
    NewsProvider("newsapi_ai", "https://eventregistry.org", "api/v1/article/getArticles",
                 "news_newsapi_ai_key", _build_newsapi_ai, _parse_newsapi_ai, reliability=0.58),
    NewsProvider("worldnews", "https://api.worldnewsapi.com", "search-news",
                 "news_worldnewsapi_key", _build_worldnews, _parse_worldnews, reliability=0.55),
    NewsProvider("gnews", "https://gnews.io", "api/v4/search",
                 "news_gnews_io_key", _build_gnews, _parse_gnews, reliability=0.55),
    NewsProvider("newsapi_org", "https://newsapi.org", "v2/everything",
                 "news_newsapi_org_key", _build_newsapi_org, _parse_newsapi_org, reliability=0.5),
]

PROVIDERS_BY_ID = {p.id: p for p in PROVIDERS}
