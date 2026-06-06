"""PSX yearly analysis-report adapter (L3b).

GET https://dps.psx.com.pk/download/analysis_report/year-{year}.xlsx -> one row of
fundamentals per listed company (equity, total assets, sales, PBT/PAT, financial
charges, …) in **Rs. million**. Parsed by ``parse_analysis_report_xlsx``.

Role in the trust model: this is an exchange-published, *unaudited* restatement of
figures the workbook also holds as *audited* facts. It therefore CORROBORATES the
workbook (admission=supporting, authority < audited_issuer) and can never override it —
when it disagrees with the workbook on a metric, ``conflicts.detect_internal_vs_external``
scale-reconciles the two and the workbook wins (architecture §8.2). This is the
overlapping-metric external source the divergence / scale-reconcile machinery needs.

The endpoint returns the whole market (~535 companies); the adapter fetches+caches the
year once, then narrows to one company and emits only the requested overlapping metrics
as cited evidence.
"""

from __future__ import annotations

from typing import Optional

from .base import ApiClient, ApiSpec, CallResult
from .parsers import parse_analysis_report_xlsx
from ..models import Citation, EvidenceItem

# analysis_reports record field -> (canonical FIE metric id, unit, human label).
# Only fields that overlap the workbook's audited headline facts (so a comparison is
# meaningful) are surfaced. Financials are Rs. million.
_FIELD_TO_METRIC: dict[str, tuple[str, str, str]] = {
    "equity": ("total_equity", "Rs. million", "shareholders' equity"),
    "total_assets": ("total_assets", "Rs. million", "total assets"),
    "sales": ("revenue", "Rs. million", "sales / total income"),
    "pbt": ("profit_before_tax", "Rs. million", "profit before taxation"),
    "pat": ("pat", "Rs. million", "profit after taxation"),
    "financial_charges": ("finance_cost", "Rs. million", "financial charges"),
}


def _normalizer(raw, params, spec, retrieved_at):
    """Emit one lightweight carrier EvidenceItem per company row (full record in the
    locator) — mirrors the Symbols adapter so ``ApiClient`` caches the parse. The
    adapter reconstructs records and builds the per-metric evidence in ``facts_for``."""
    items: list[EvidenceItem] = []
    for rec in parse_analysis_report_xlsx(raw):
        cite = Citation(ref_id="C?", kind="external",
                        display=f"PSX analysis report {rec.get('fiscal_year')}: {rec['symbol']}",
                        locator={"source": spec.id, "retrieved_at": retrieved_at, **rec},
                        retrieved_at=retrieved_at)
        items.append(EvidenceItem(claim=rec.get("name") or rec["symbol"], kind="external",
                                  citations=[cite], reliability=spec.reliability_rating,
                                  freshness=retrieved_at))
    return items


class AnalysisReports:
    def __init__(self, client: ApiClient, *, symbols=None,
                 base_url: str = "https://dps.psx.com.pk") -> None:
        self.client = client
        self.symbols = symbols   # optional: resolve company name -> ticker
        self.spec = ApiSpec(id="PSX.AnalysisReports", base_url=base_url,
                            path="download/analysis_report/year-{year}.xlsx",
                            method="GET", response_type="xlsx", reliability_rating=0.85,
                            refresh_frequency="yearly", failure_mode="cache",
                            normalizer=_normalizer)
        # year -> {symbol: record}
        self._by_year: dict[int, dict[str, dict]] = {}
        self._degraded_years: set[int] = set()

    def _records(self, year: int) -> dict[str, dict]:
        if year not in self._by_year:
            res = self.client.call(self.spec, year=year)
            if res.status == "cached":
                self._degraded_years.add(year)
            self._by_year[year] = {
                c.locator["symbol"]: c.locator
                for i in res.items for c in i.citations if c.locator.get("symbol")
            }
        return self._by_year[year]

    def _resolve(self, symbol: str | None, company: str | None) -> str | None:
        if symbol:
            return symbol
        if company and self.symbols is not None:
            return self.symbols.ticker_for(company)
        return None

    def record(self, year: int, *, symbol: str | None = None,
               company: str | None = None) -> Optional[dict]:
        """The raw fundamentals record for one company in a given year (or None)."""
        sym = self._resolve(symbol, company)
        if sym is None:
            return None
        return self._records(year).get(sym)

    def facts_for(self, year: int, *, symbol: str | None = None,
                  company: str | None = None,
                  metrics: Optional[list[str]] = None) -> CallResult:
        """Per-metric cited external EvidenceItems for one company, restricted to the
        overlapping canonicals (optionally filtered to ``metrics``). Each item carries
        ``locator['metric']`` = canonical id so ``detect_internal_vs_external`` matches
        it against the workbook fact. Returns an empty CallResult if unresolved/missing."""
        sym = self._resolve(symbol, company)
        if sym is None:
            return CallResult(items=[], status="failed", note="unresolved symbol")
        rec = self._records(year).get(sym)
        if not rec:
            return CallResult(items=[], status="failed", note="company not in dataset")
        want = set(metrics) if metrics else None
        retrieved_at = rec.get("retrieved_at")
        items: list[EvidenceItem] = []
        for field, (metric, unit, label) in _FIELD_TO_METRIC.items():
            if want is not None and metric not in want:
                continue
            v = rec.get(field)
            if v is None:
                continue
            cite = Citation(
                ref_id="C?", kind="external",
                display=f"PSX analysis report {rec.get('fiscal_year')} ({sym}) — {label}",
                locator={"source": self.spec.id, "symbol": sym, "metric": metric,
                         "field": field, "fiscal_year": rec.get("fiscal_year"),
                         "year_end": rec.get("year_end"), "retrieved_at": retrieved_at},
                retrieved_at=retrieved_at)
            items.append(EvidenceItem(
                claim=f"{sym} {label} = {v} (Rs. million, FY{rec.get('fiscal_year')})",
                value=float(v), unit=unit, kind="external", citations=[cite],
                reliability=self.spec.reliability_rating, freshness=retrieved_at,
                as_of=retrieved_at))
        status = "cached" if year in self._degraded_years else "ok"
        return CallResult(items=items, status=status, retrieved_at=retrieved_at)
