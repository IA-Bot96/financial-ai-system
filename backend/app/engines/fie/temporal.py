"""Temporal provenance: value_year vs source_report_year (L6 support).

Ported in spirit from the legacy MSIL ``MetricValue``: a financial value carries two
distinct years — the year it *represents* (``value_year`` = FactRef.year) and the
annual report it was *found in* (``source_report_year`` = FactRef.report_year).

Separating them is what makes three things correct:
  - restatement       the same value_year can hold different values across report_years
  - as-reported view  the value as first published vs the latest (restated) view
  - leakage / as-of   a forecast or analysis must only use data KNOWN as of its basis
                      report year — no look-ahead from later reports
"""

from __future__ import annotations

from typing import Iterable, Optional


def value_year(fact) -> Optional[int]:
    """The financial year the value represents."""
    return getattr(fact, "year", None)


def source_report_year(fact) -> Optional[int]:
    """The annual-report year the value was sourced from (None for workbook-only)."""
    return getattr(fact, "report_year", None)


def known_as_of(facts: Iterable, as_of_report_year: Optional[int]) -> list:
    """Look-ahead leakage guard: keep only facts known *as of* ``as_of_report_year``
    — i.e. whose source_report_year <= as_of. A fact with no report_year is treated as
    known (the uploaded workbook). ``as_of_report_year=None`` disables filtering."""
    facts = list(facts)
    if as_of_report_year is None:
        return facts
    out = []
    for f in facts:
        ry = source_report_year(f)
        if ry is None or ry <= as_of_report_year:
            out.append(f)
    return out


def prefer(pairs: Iterable[tuple], preference: str = "latest") -> Optional[tuple]:
    """Given ``(report_year, value)`` pairs for ONE value_year, pick by preference:
    ``latest`` → the newest report (restatement-aware default); ``as_reported`` → the
    value as first published (oldest report). Returns the chosen (report_year, value),
    or None if there are no usable pairs."""
    ps = [(int(r), v) for r, v in pairs if r is not None]
    if not ps:
        return None
    return (max(ps, key=lambda p: p[0]) if preference == "latest"
            else min(ps, key=lambda p: p[0]))
