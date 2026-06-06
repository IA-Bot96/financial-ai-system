"""PSX company-payouts adapter (L3b).

POST https://dps.psx.com.pk/company/payouts, form-encoded body {symbol} -> the
payout history table (dividends/bonus, %, book-closure dates). Symbol is required
and is resolved from the symbols API when only a company name is given.
"""

from __future__ import annotations

from .base import ApiClient, ApiSpec, CallResult
from .parsers import parse_company_payouts
from ..models import Citation, EvidenceItem


def _normalizer(raw, params, spec, retrieved_at):
    rows = parse_company_payouts(raw) if isinstance(raw, str) else (raw or [])
    items = []
    for p in rows:
        kind = "dividend" if p.get("dividend") else ("bonus" if p.get("bonus") else "payout")
        when = "interim" if p.get("interim") else ("final" if p.get("final") else "")
        claim = (f"{p.get('payout_pct')}% {when} {kind}".strip()
                 + (f", book closure {p['book_closure']}" if p.get("book_closure") else ""))
        cite = Citation(ref_id="C?", kind="external",
                        display=f"PSX payouts: {p.get('date')}",
                        locator={"source": spec.id, "date": p.get("date"),
                                 "financial_results": p.get("financial_results"),
                                 "details": p.get("details"),
                                 "book_closure": p.get("book_closure"),
                                 "retrieved_at": retrieved_at}, retrieved_at=retrieved_at)
        items.append(EvidenceItem(claim=claim, value=p.get("payout_pct"), unit="percent",
                                  kind="external", citations=[cite],
                                  reliability=spec.reliability_rating, freshness=p.get("date")))
    return items


class CompanyPayouts:
    def __init__(self, client: ApiClient, *, symbols=None,
                 base_url: str = "https://dps.psx.com.pk") -> None:
        self.client = client
        self.symbols = symbols
        self.spec = ApiSpec(id="PSX.CompanyPayouts", base_url=base_url,
                            path="company/payouts", method="POST", content_type="form",
                            response_type="html", reliability_rating=0.9,
                            refresh_frequency="daily", failure_mode="cache",
                            normalizer=_normalizer)

    def payouts(self, symbol: str | None = None, *, company: str | None = None) -> CallResult:
        sym = symbol or (self.symbols.ticker_for(company) if (company and self.symbols) else None)
        if sym is None:
            return CallResult(items=[], status="failed", note="unresolved symbol")
        return self.client.call(self.spec, body={"symbol": sym})
