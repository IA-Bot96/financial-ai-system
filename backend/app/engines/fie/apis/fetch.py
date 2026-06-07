"""Generic registry-driven fetcher (L3b).

Any of the catalogued data APIs (``apis.registry.REGISTRY``) can be called WITHOUT a
bespoke adapter class: this builds an :class:`ApiSpec` from the ``ApiInfo`` declarative
metadata (endpoint / method / content-type / static + dynamic params / parser), fills the
dynamic params from the query context, calls via the resilient :class:`ApiClient`, and
normalizes the parser's raw records into ``EvidenceItem`` objects that carry their own
citation + provenance — so external data is cited and conflict-checked exactly like
internal data. Pairs with the query-driven ``registry.shortlist`` selector.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from .base import ApiClient, ApiSpec, CallResult, monthly_windows
from .registry import ApiInfo
from ..models import Citation, EvidenceItem

# Salient fields, in priority order, used to build a human-readable claim from a record so
# the LLM gets legible context regardless of which of the 17 APIs produced it.
_CLAIM_FIELDS = (
    "title", "headline", "subject", "name", "company", "symbol", "sector",
    "type", "status", "date", "price", "change", "change_pct", "volume",
    "dividend", "payout", "bonus", "eps", "pe", "value", "note",
)
# Identifier/link fields worth preserving in the citation locator (for navigation/dedupe).
_LOC_FIELDS = ("date", "symbol", "sector", "sector_code", "url", "pdf_url", "doc_id",
               "type", "status", "book_closure")


def _split_endpoint(endpoint: str) -> tuple[str, str]:
    """Full URL -> (base_url, path) keeping any {placeholders} in the path."""
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.netloc}", parts.path.lstrip("/")


def _record_claim(rec: dict) -> str:
    if not isinstance(rec, dict):
        return str(rec)[:300]
    bits = [f"{k}={rec[k]}" for k in _CLAIM_FIELDS if rec.get(k) not in (None, "", [])]
    if not bits:  # unknown shape — surface the first few non-empty fields
        bits = [f"{k}={v}" for k, v in list(rec.items())[:5] if v not in (None, "", [])]
    return "; ".join(bits)[:300]


def _make_normalizer(api: ApiInfo):
    """Turn a registry parser (raw -> list[dict] | dict) into an EvidenceItem normalizer."""
    def _norm(raw, params, spec, retrieved_at) -> list[EvidenceItem]:
        parser = api.parser_fn
        recs = parser(raw) if parser else []
        if isinstance(recs, dict):  # overview / daily_market_summary return one dict
            recs = [recs]
        items: list[EvidenceItem] = []
        for rec in recs or []:
            if not isinstance(rec, dict):
                rec = {"value": rec}
            loc = {"source": api.name, "api": api.name, "category": api.category,
                   "retrieved_at": retrieved_at}
            for k in _LOC_FIELDS:
                if rec.get(k) not in (None, ""):
                    loc[k] = rec[k]
            claim = _record_claim(rec)
            items.append(EvidenceItem(
                claim=claim, kind="external",
                citations=[Citation(ref_id="C?", kind="external",
                                    display=f"{api.name}: {claim[:80]}",
                                    locator=loc, retrieved_at=retrieved_at)],
                reliability=0.85, freshness=rec.get("date"), as_of=rec.get("date")))
        return items
    return _norm


class RegistryFetcher:
    """Calls any registry ``ApiInfo`` generically and returns EvidenceItems."""

    def __init__(self, client: ApiClient, *, as_of: Optional[str] = None,
                 max_records_per_api: int = 8, windows_months: int = 3) -> None:
        self.client = client
        self.as_of = as_of
        self.max_records = max_records_per_api
        self.windows_months = windows_months

    def _spec_for(self, api: ApiInfo) -> ApiSpec:
        base, path = _split_endpoint(api.endpoint)
        return ApiSpec(
            id=f"Registry.{api.name}", base_url=base, path=path,
            method=api.method, content_type=api.content_type,
            response_type=api.response_type,
            # POST: static params travel as the form/JSON body; GET: as query params (below)
            request_body=(dict(api.params) if api.method.upper() == "POST" else None),
            reliability_rating=0.85, failure_mode="omit",
            normalizer=_make_normalizer(api),
        )

    def fetch(self, api: ApiInfo, *, symbol: str | None = None, query: str | None = None,
              year: int | None = None, sector: str | None = None) -> CallResult:
        """Fetch one registry API, filling only the dynamic params it declares.

        Date-windowed disclosure APIs (date_from/date_to + an anchor date) are called once
        per monthly window over the last ``windows_months``, matching the PSX contract.
        """
        spec = self._spec_for(api)
        dyn = set(api.dynamic_params)
        is_post = api.method.upper() == "POST"

        def _dynamic_values(window) -> dict:
            vals: dict = {}
            for p in dyn:
                if p in ("date_from", "date_to"):
                    if window:
                        vals[p] = window[p]
                elif p == "symbol" and symbol:
                    vals["symbol"] = symbol
                elif p == "query" and query:
                    vals["query"] = query
                elif p == "year" and year is not None:
                    vals["year"] = year
                elif p == "sector" and sector:
                    vals["sector"] = sector
            return vals

        windowed = self.as_of and ("date_from" in dyn or "date_to" in dyn)
        windows = monthly_windows(self.as_of, n=self.windows_months) if windowed else [None]

        items: list[EvidenceItem] = []
        statuses: list[str] = []
        for w in windows:
            dynamic = _dynamic_values(w)
            if is_post:
                # static params already in request_body; dynamic go in the per-call body
                res = self.client.call(spec, body=dynamic)
            else:
                # GET: static params + dynamic params as query params; {placeholders}
                # (e.g. {symbol}) are filled from the same kwargs by ApiClient.
                call_params = {**(api.params or {}), **dynamic}
                res = self.client.call(spec, **call_params)
            items += res.items
            statuses.append(res.status)

        status = "ok" if any(s == "ok" for s in statuses) else (
            "cached" if "cached" in statuses else "failed")
        if self.max_records:
            items = items[: self.max_records]
        return CallResult(items=items, status=status)
