"""PSX announcements & SECP notices adapters (L3b) — Phase 4/upgrade.

Implements the real PSX shape: POST to /announcements with a typed body
(type 'C' = company announcements, 'B' = SECP notices), called once per date
window for the last ~3 months (architecture / PSX catalog).

The endpoints return HTML; real parsing is an INJECTABLE ``parser`` callable
(html_text -> {"items":[{title,date,...}]}) so the orchestration is testable
offline. Default parser passes through an already-structured dict.
"""

from __future__ import annotations

from typing import Callable, Optional

from .base import ApiClient, ApiSpec, CallResult, monthly_windows
from .parsers import parse_company_announcements, parse_secp_notices
from ..models import Citation, EvidenceItem


def _passthrough_parser(raw):
    """Fallback when a structured dict is supplied (e.g. tests)."""
    if isinstance(raw, dict):
        return raw.get("items") or raw.get("announcements") or []
    return []


def _make_normalizer(parser: Callable, source_id: str):
    def _norm(raw, params, spec, retrieved_at):
        items = []
        for art in parser(raw):
            url = art.get("pdf_url") or art.get("url")
            cite = Citation(
                ref_id="C?", kind="external",
                display=f"{source_id}: {(art.get('title') or '')[:60]} ({art.get('date', '')})",
                locator={"source": source_id, "date": art.get("date"),
                         "symbol": art.get("symbol"), "status": art.get("status"),
                         "doc_id": art.get("doc_id"), "url": url,
                         "retrieved_at": retrieved_at},
                retrieved_at=retrieved_at,
            )
            items.append(EvidenceItem(
                claim=art.get("title") or "", kind="external", citations=[cite],
                reliability=spec.reliability_rating, freshness=art.get("date"),
                as_of=art.get("date")))
        return items
    return _norm


class _AnnouncementsBase:
    _ID = "PSX.Announcements"
    _TYPE = "C"
    _REAL_PARSER = staticmethod(parse_company_announcements)

    def __init__(self, client: ApiClient, *, parser: Callable | None = None,
                 symbols=None, base_url: str = "https://dps.psx.com.pk") -> None:
        self.client = client
        self.symbols = symbols  # Symbols adapter: resolves company name -> PSX symbol
        real = parser or self._REAL_PARSER

        def _dispatch(raw):
            # live HTML -> real per-API parser; structured dict (tests) -> passthrough
            return real(raw) if isinstance(raw, str) else _passthrough_parser(raw)

        # real PSX contract: POST /announcements, form-encoded, full param set
        self.spec = ApiSpec(
            id=self._ID, base_url=base_url, path="announcements", method="POST",
            content_type="form", response_type="html",
            request_body={"type": self._TYPE, "symbol": "", "query": "",
                          "count": 50, "offset": 0, "date_from": "", "date_to": "",
                          "page": "annc"},
            reliability_rating=0.85, refresh_frequency="daily", failure_mode="omit",
            normalizer=_make_normalizer(_dispatch, self._ID),
        )

    def recent(self, query: str | None = None, *, symbol: str | None = None,
               company: str | None = None, anchor_date: Optional[str] = None,
               months: int = 3) -> CallResult:
        """One call per monthly window over the last ``months`` (PSX catalog note).
        Filter by ``symbol`` (a PSX symbol) and/or ``query`` (keyword). If only a
        ``company`` name is given and a Symbols resolver is configured, the symbol is
        acquired from the symbols API (the announcements API expects a real symbol).
        Without an anchor date, makes a single undated call."""
        if symbol is None and company and self.symbols is not None:
            symbol = self.symbols.ticker_for(company)
            if symbol is None:  # not found in the registry -> fall back to keyword
                query = query or company
        windows = monthly_windows(anchor_date, n=months) if anchor_date else [None]
        items, statuses = [], []
        for w in windows:
            body: dict = {}
            if symbol:
                body["symbol"] = symbol
            if query:
                body["query"] = query
            if w:
                body |= {"date_from": w["date_from"], "date_to": w["date_to"]}
            res = self.client.call(self.spec, body=body)
            items += res.items
            statuses.append(res.status)
        status = "ok" if any(s == "ok" for s in statuses) else "failed"
        return CallResult(items=items, status=status)


class PSXAnnouncements(_AnnouncementsBase):
    _ID = "PSX.Announcements"
    _TYPE = "C"
    _REAL_PARSER = staticmethod(parse_company_announcements)


class SECPNotices(_AnnouncementsBase):
    _ID = "PSX.SECPNotices"
    _TYPE = "B"
    _REAL_PARSER = staticmethod(parse_secp_notices)
