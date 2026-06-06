"""Forecast repository (L3b) — Phase 4.

Versioned forecasts. Two sources, in priority order:
  1. injected overrides (a platform forecast store), keyed by (company, metric, year)
  2. the in-workbook 'forecasted' period_type columns (architecture §2.2)

Returns a forecast value + provenance, or None when no forecast exists.
"""

from __future__ import annotations

from typing import Optional

from ..models import Citation, EvidenceItem


class ForecastRepo:
    def __init__(self, store=None, overrides: dict | None = None,
                 version: str = "1.0") -> None:
        self.store = store
        self.overrides = overrides or {}  # {(company, metric, year): value}
        self.version = version

    def get(self, company: str, metric: str, year: int) -> Optional[EvidenceItem]:
        key = (company, metric, year)
        if key in self.overrides:
            val = self.overrides[key]
            cite = Citation(ref_id="C?", kind="forecast",
                            display=f"Forecast repo v{self.version}",
                            locator={"forecast_id": f"FC-{metric}-{year}",
                                     "version": self.version, "company": company})
            return EvidenceItem(claim=f"forecast {metric} {year} = {val}", value=float(val),
                                kind="external", citations=[cite], reliability=0.7)
        # fall back to in-workbook forecast columns
        if self.store is not None:
            try:
                fact = self.store.lookup(metric, year, period_type="forecasted")
            except KeyError:
                fact = None
            if fact is not None and fact.value is not None:
                cite = Citation(ref_id="C?", kind="forecast",
                                display=f"workbook forecast ({fact.sheet}!{fact.cell})",
                                locator={"sheet": fact.sheet, "cell": fact.cell,
                                         "year": year, "period_type": "forecasted"})
                return EvidenceItem(claim=f"forecast {metric} {year} = {fact.value}",
                                    value=fact.value, kind="external",
                                    citations=[cite], reliability=0.7)
        return None
