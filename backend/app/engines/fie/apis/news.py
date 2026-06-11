"""News adapter (L3b) — multi-provider, finance-native failover search.

Walks the free news APIs in ``news_providers.PROVIDERS`` order (most finance-native
first) and returns the first provider that yields query-relevant articles. A provider
that hits its free-tier limit (HTTP 429 → transport raises → status 'failed') or
returns an error/empty body just hands off to the next. Providers with no configured
key, and ticker-only feeds on a keyword-only query, are skipped.

Each article is a ``NewsArticle`` (content + source); the adapter converts it to an
external, cited ``EvidenceItem`` for the engine, preserving the full article (title,
snippet, body, url, publisher, provider, publish time, tagged symbols) in the citation
locator so downstream layers keep both the content and its source.
"""

from __future__ import annotations

from typing import Optional

from app.core.security import DailyQuota
from .base import ApiClient, ApiSpec, CallResult
from .news_providers import PROVIDERS, NewsProvider
from ..models import Citation, EvidenceItem, NewsArticle

# process-global per-provider daily call ceiling (News is recreated per request, so
# the quota must outlive a single instance). 0 in settings => unlimited.
_QUOTA: DailyQuota | None = None


def _quota() -> DailyQuota:
    global _QUOTA
    if _QUOTA is None:
        from app.core.config import get_settings
        _QUOTA = DailyQuota(cap=get_settings().news_daily_call_cap_per_provider)
    return _QUOTA


class News:
    def __init__(self, client: ApiClient, *, settings=None,
                 providers: Optional[list[NewsProvider]] = None,
                 max_articles: Optional[int] = None) -> None:
        self.client = client
        if settings is None:                       # lazy: avoid env load when injected
            from app.core.config import get_settings
            settings = get_settings()
        self.settings = settings
        self.providers = providers if providers is not None else PROVIDERS
        self.max_articles = (max_articles
                             or getattr(settings, "news_max_articles", 10) or 10)

    # --- per-provider spec (parser closure stores content + source) ---
    def _spec(self, p: NewsProvider, path: str) -> ApiSpec:
        cap = self.max_articles

        def _norm(raw, params, spec, retrieved_at):
            items = []
            for a in (p.parse(raw) or [])[:cap]:
                loc = {"source": a.source, "author": a.author, "provider": a.provider,
                       "url": a.url, "link": a.url,            # 'link' = external locator key
                       "published_at": a.published_at, "snippet": a.description,
                       "content": a.content, "symbols": a.symbols,
                       "sentiment": a.sentiment, "retrieved_at": retrieved_at}
                # display: "<publisher> — <author> (<date>)"  (article source for the citation)
                disp = (a.source or a.provider or "external")
                if a.author:
                    disp += f" — {a.author}"
                if a.published_at:
                    disp += f" ({a.published_at})"
                items.append(EvidenceItem(
                    claim=a.title or "", kind="external",
                    citations=[Citation(ref_id="C?", kind="external", display=disp,
                                        locator=loc, retrieved_at=retrieved_at)],
                    reliability=p.reliability, freshness=a.published_at, as_of=a.published_at))
            return items

        return ApiSpec(id=f"News.{p.id}", base_url=p.base_url, path=path, method="GET",
                       response_type="json", reliability_rating=p.reliability,
                       refresh_frequency="hourly", failure_mode="omit", normalizer=_norm)

    def search(self, query: str, *, symbol: Optional[str] = None,
               limit: Optional[int] = None, anchor_date: Optional[str] = None) -> CallResult:
        """Finance-ordered failover search. Relevance: scope to ``symbol`` when a
        ticker is known, else free-text on ``query``. Returns the first provider with
        articles; status 'failed' (with a tried/skipped note) if none yield any."""
        limit = limit or self.max_articles
        tried: list[str] = []
        skipped: list[str] = []
        for p in self.providers:
            key = getattr(self.settings, p.key_setting, "") or ""
            if not key:
                skipped.append(f"{p.id}:no_key")
                continue
            if p.requires_symbol and not symbol:
                skipped.append(f"{p.id}:needs_symbol")
                continue
            if not _quota().allow(p.id):                 # daily cost ceiling reached
                skipped.append(f"{p.id}:quota")
                continue
            path, params = p.build(query=query, symbol=symbol, key=key,
                                   limit=limit, anchor_date=anchor_date)
            res = self.client.call(self._spec(p, path), **params)
            tried.append(p.id)
            if res.status != "failed" and res.items:
                res.note = f"served_by={p.id}" + (f"; skipped={skipped}" if skipped else "")
                return res
        return CallResult(items=[], status="failed",
                          note=f"no news; tried={tried}; skipped={skipped}")

    def search_articles(self, query: str, *, symbol: Optional[str] = None,
                        limit: Optional[int] = None,
                        anchor_date: Optional[str] = None) -> list[NewsArticle]:
        """Same search, returning the typed NewsArticle list (content + source)
        reconstructed from the served provider's evidence."""
        res = self.search(query, symbol=symbol, limit=limit, anchor_date=anchor_date)
        out: list[NewsArticle] = []
        for it in res.items:
            loc = it.citations[0].locator if it.citations else {}
            out.append(NewsArticle(
                title=it.claim, description=loc.get("snippet"), content=loc.get("content"),
                url=loc.get("url"), source=loc.get("source"), author=loc.get("author"),
                provider=loc.get("provider", ""), published_at=loc.get("published_at"),
                symbols=loc.get("symbols") or [], sentiment=loc.get("sentiment")))
        return out
