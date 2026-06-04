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


def resolve_multiyear(results: list[DocumentResult], company: str | None = None) -> CompanyResult:
    """Merge per-report DocumentResults into one multi-year CompanyResult."""
    results = [r for r in results if r is not None]

    # st -> {key -> meta};  (st, key) -> {report_year -> {data_year -> LineItemValue}}
    lines_by_st: dict[StatementType, dict[str, dict]] = {}
    st_info: dict[StatementType, tuple] = {}
    values_index: dict[tuple, dict[int, dict[int, LineItemValue]]] = {}

    # Iterate latest report first so the most recent report defines line order/labels.
    for res in sorted(results, key=lambda r: (r.report_year or 0), reverse=True):
        ry = res.report_year
        if ry is None:
            continue
        for table in res.tables:
            st = table.statement_type
            st_info.setdefault(st, (table.title, table.currency, table.unit_scale))
            km = lines_by_st.setdefault(st, {})
            for li in table.line_items:
                key = _line_key(li)
                if not key:
                    continue
                km.setdefault(key, {
                    "label": li.label,
                    "canonical_metric": li.canonical_metric,
                    "canonical_category": li.canonical_category,
                })
                rmap = values_index.setdefault((st, key), {}).setdefault(ry, {})
                for v in li.values:
                    if v.year is not None:
                        rmap[v.year] = v

    data_years = sorted({
        y for idx in values_index.values() for rmap in idx.values() for y in rmap
    })

    merged: list[FinancialTable] = []
    for st, km in lines_by_st.items():
        items: list[LineItem] = []
        present_years: set[int] = set()
        for key, meta in km.items():
            idx = values_index.get((st, key), {})
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
                    canonical_metric=meta["canonical_metric"],
                    canonical_category=meta["canonical_category"],
                    values=values,
                ))
        title, currency, unit = st_info.get(st, ("", None, None))
        merged.append(FinancialTable(
            statement_type=st, title=title, currency=currency, unit_scale=unit,
            years=sorted(present_years), line_items=items,
        ))

    insights = [i for res in results for i in res.insights]
    review = [i for res in results for i in res.insights_review]

    logger.info(
        "Multi-year resolution: %d reports, years %s, %d merged tables",
        len(results), data_years, len(merged),
    )
    return CompanyResult(
        company=company,
        fiscal_years=data_years,
        source_reports=[r.file_name for r in results],
        tables=merged,
        insights=insights,
        insights_review=review,
    )
