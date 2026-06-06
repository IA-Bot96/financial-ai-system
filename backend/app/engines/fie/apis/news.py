"""News adapter (L3b) — Phase 4.

Returns recent company/sector headlines as (textual) EvidenceItems. Date-aware so
the conflict layer can compare an insight against a fresher disclosure.
"""

from __future__ import annotations

from .base import ApiClient, ApiSpec
from ..models import Citation, EvidenceItem


def _news_normalizer(raw: dict, params: dict, spec: ApiSpec, retrieved_at: str):
    items = []
    for i, art in enumerate(raw.get("articles", []), start=1):
        cite = Citation(
            ref_id="C?", kind="external",
            display=f"{art.get('source', 'news')} — {art.get('date', '')}",
            locator={"source": spec.id, "url": art.get("url"),
                     "date": art.get("date"), "retrieved_at": retrieved_at},
            retrieved_at=retrieved_at,
        )
        items.append(EvidenceItem(
            claim=art.get("title") or "", kind="external", citations=[cite],
            reliability=spec.reliability_rating, freshness=art.get("date"),
            as_of=art.get("date"),
        ))
    return items


class News:
    def __init__(self, client: ApiClient, base_url: str = "https://news.example/api") -> None:
        self.client = client
        self.spec = ApiSpec(
            id="News.Company", base_url=base_url, path="search",
            reliability_rating=0.6, refresh_frequency="hourly",
            failure_mode="omit", normalizer=_news_normalizer,
        )

    def headlines(self, query: str):
        return self.client.call(self.spec, q=query)
