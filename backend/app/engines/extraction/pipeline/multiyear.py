"""Layer 4 — Multi-year resolution (rule-based).

When several annual reports for the same company are provided, each data year
appears in more than one report (the current-year figure, then as a comparative
the following year). The rule: for data year N, prefer the value reported in the
year N+1 report (its comparative column — typically the final/restated figure),
then the year N report itself, then the closest later report.

  reports {2024, 2025}:  2024 <- 2025 report,  2025 <- 2025 report
  reports {2022..2025}:  2021 <- 2022 report, 2022 <- 2023, 2023 <- 2024, 2024 <- 2025, 2025 <- 2025

Lines are aligned across reports by canonical metric key (falling back to the
squashed label), so renamed/garbled labels still merge into one series. Each
resolved value records which report it came from.
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.company import CompanyResult
from app.engines.extraction.models.financials import FinancialTable, LineItem, LineItemValue
from app.engines.extraction.models.result import DocumentResult
from app.engines.extraction.services.metric_resolver import squash

logger = get_logger(__name__)


def _rank(report_year: int, data_year: int) -> int:
    """Preference rank for sourcing `data_year` from `report_year` (lower = better)."""
    if report_year == data_year + 1:
        return 0                       # the prefer-N+1 rule
    if report_year == data_year:
        return 1                       # the year's own report
    if report_year > data_year:
        return 2 + (report_year - data_year)  # later reports, closest first
    return 1000                        # earlier report (can't really contain the year)


def _line_key(item: LineItem) -> str:
    return item.canonical_metric or squash(item.label)


def _resolve_company(results: list[DocumentResult], fallback: str | None) -> str | None:
    """Company extracted from the documents is the source of truth; the caller's
    value is only a fallback when nothing was extracted."""
    from collections import Counter

    names = [r.company.strip() for r in results if r.company and r.company.strip()]
    if names:
        return Counter(names).most_common(1)[0][0]
    return fallback


def resolve_multiyear(results: list[DocumentResult], company: str | None = None) -> CompanyResult:
    """Merge per-report DocumentResults into one multi-year CompanyResult."""
    results = [r for r in results if r is not None]
    company = _resolve_company(results, company)

    # Plausible data-year window from the reports themselves (a report covers its
    # year + the prior comparative). Drops junk years (1990, 2001…) and stale
    # six-year-summary years that pollute the output.
    report_years = sorted({r.report_year for r in results if r.report_year})
    year_lo = (min(report_years) - 1) if report_years else None
    year_hi = max(report_years) if report_years else None

    def _year_ok(y: int) -> bool:
        return year_lo is None or (year_lo <= y <= year_hi)

    # Group by (statement_type, consolidated) so the unconsolidated and
    # consolidated sets stay SEPARATE and LABELED. The "prefer unconsolidated"
    # choice is a template concern (handled in template_map), not here — so the
    # no-template output keeps BOTH sets.
    #   group -> {key -> meta};  (group, key) -> {report_year -> {data_year -> value}}
    groups: dict[tuple, dict[str, dict]] = {}
    group_info: dict[tuple, tuple] = {}
    values_index: dict[tuple, dict[int, dict[int, LineItemValue]]] = {}

    # Iterate latest report first so the most recent report defines line order/labels.
    for res in sorted(results, key=lambda r: (r.report_year or 0), reverse=True):
        ry = res.report_year
        if ry is None:
            continue
        for table in res.tables:
            gkey = (table.statement_type, table.consolidated)
            group_info.setdefault(gkey, (table.title, table.currency, table.unit_scale))
            km = groups.setdefault(gkey, {})
            for li in table.line_items:
                base = _line_key(li)
                if not base:
                    continue
                # Include the sub-section in the key so same-labelled lines in
                # different sub-notes (e.g. Distribution vs Administrative
                # "Salaries and amenities") do NOT collapse into one line. Fall
                # back to the table title when the line has no explicit section.
                section = (li.section or table.title or "").strip()
                key = (base, squash(section))
                km.setdefault(key, {
                    "label": li.label,
                    "section": section,
                    "canonical_metric": li.canonical_metric,
                    "canonical_category": li.canonical_category,
                })
                rmap = values_index.setdefault((gkey, key), {}).setdefault(ry, {})
                for v in li.values:
                    if v.year is not None and _year_ok(v.year):
                        rmap.setdefault(v.year, v)

    data_years = sorted({
        y for idx in values_index.values() for rmap in idx.values() for y in rmap
    })

    merged: list[FinancialTable] = []
    for gkey, km in groups.items():
        st, consolidated = gkey
        items: list[LineItem] = []
        present_years: set[int] = set()
        for key, meta in km.items():
            idx = values_index.get((gkey, key), {})
            values: list[LineItemValue] = []
            for year in data_years:
                candidates = [ry for ry, rmap in idx.items() if year in rmap]
                if not candidates:
                    continue
                best = min(candidates, key=lambda ry: _rank(ry, year))
                src = idx[best][year]
                values.append(LineItemValue(
                    year=year, value=src.value, raw=src.raw, source_report_year=best,
                ))
                present_years.add(year)
            if values:
                items.append(LineItem(
                    label=meta["label"],
                    section=meta.get("section"),
                    canonical_metric=meta["canonical_metric"],
                    canonical_category=meta["canonical_category"],
                    values=values,
                ))
        title, currency, unit = group_info.get(gkey, ("", None, None))
        merged.append(FinancialTable(
            statement_type=st, title=title, currency=currency, unit_scale=unit,
            consolidated=consolidated, years=sorted(present_years), line_items=items,
        ))

    insights = [i for res in results for i in res.insights]
    review = [i for res in results for i in res.insights_review]

    logger.info(
        "Multi-year resolution: company=%r, %d reports, years %s, %d merged tables",
        company, len(results), data_years, len(merged),
    )
    return CompanyResult(
        company=company,
        fiscal_years=data_years,
        source_reports=[r.file_name for r in results],
        tables=merged,
        insights=insights,
        insights_review=review,
    )
