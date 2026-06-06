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
from app.engines.extraction.models.company import CompanyResult, RejectedLine
from app.engines.extraction.models.financials import FinancialTable, LineItem, LineItemValue
from app.engines.extraction.models.result import DocumentResult
from app.engines.extraction.services.face_truth import (
    confidently_incompatible, infer_table_role, table_role_of,
)
from app.engines.extraction.services.metric_resolver import squash

# Source quality for the merge: an audited primary statement beats a note beats an
# analytical/ratio table when several reports offer the same (metric, year).
_ROLE_PREF = {"primary": 0, "note": 1, "analytical": 2, None: 1}

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


def _consolidate_split_lines(items: list[LineItem]) -> list[LineItem]:
    """Fold line items that share a (squashed) label but were split across reports by
    SECTION drift — e.g. 'Mark-up on short-term borrowings' filed under '38 Finance cost'
    in one report, 'Finance cost' in another, and no section in a third — recovering all
    year columns into one line so older-year leaf detail isn't lost.

    GUARD: never fold when two members carry conflicting values for the SAME
    (year, source_report) — that signals genuinely different lines that coexist in one
    report (e.g. Distribution vs Administrative 'Salaries'), which must stay separate.
    Restatement across DIFFERENT reports (same year, different source_report_year) is NOT
    a conflict and folds, keeping the newest report's value per year."""
    from collections import defaultdict

    groups: dict[str, list[LineItem]] = defaultdict(list)
    order: list[str] = []
    for li in items:
        k = squash(li.label or "")
        if k not in groups:
            order.append(k)
        groups[k].append(li)

    out: list[LineItem] = []
    for k in order:
        grp = groups[k]
        # Only fold PURE note leaves (no canonical metric on any member). Canonical
        # metrics are face truth — folding them on a squash(label) collision corrupts
        # values (e.g. a BS 'Stock in trade' total vs a cash-flow movement of the same
        # name), so they are left strictly untouched.
        if not k or len(grp) == 1 or any(li.canonical_metric for li in grp):
            out.extend(grp)
            continue
        by_yr_rpt: dict[tuple, list[float]] = defaultdict(list)
        for li in grp:
            for v in li.values:
                if v.year is not None and v.value is not None:
                    by_yr_rpt[(v.year, v.source_report_year)].append(v.value)
        conflict = any(
            (max(abs(x) for x in vs) - min(abs(x) for x in vs)) > max(1.0, 0.005 * max(abs(x) for x in vs))
            for vs in by_yr_rpt.values() if len(vs) > 1
        )
        if conflict:
            out.extend(grp)          # genuinely different lines that share a label
            continue
        best: dict[int, LineItemValue] = {}
        for li in grp:
            for v in li.values:
                if v.year is None or v.value is None:
                    continue
                cur = best.get(v.year)
                if cur is None or (v.source_report_year or 0) > (cur.source_report_year or 0):
                    best[v.year] = v
        rep = max(grp, key=lambda li: (li.canonical_metric is not None, len(li.label or "")))
        folded = rep.model_copy(update={"values": [best[y] for y in sorted(best)]})
        logger.debug("consolidated split leaf %r (%d lines) -> years %s",
                     (rep.label or "")[:40], len(grp), [v.year for v in folded.values])
        out.append(folded)
    return out


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
    rejected: list[RejectedLine] = []   # P2 quarantine audit
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
                # P2 quarantine: drop lines confidently incompatible with their home
                # statement (e.g. a cost_of_sales/income line inside a balance table)
                # — recorded for audit, excluded from merge/output. This also means
                # the `_line_key` squash fallback can't re-admit them.
                if confidently_incompatible(li, table.statement_type):
                    rejected.append(RejectedLine(
                        label=li.label, statement_type=table.statement_type,
                        canonical_metric=li.canonical_metric, canonical_category=li.canonical_category,
                        reason=f"metric {li.canonical_metric!r} ({li.canonical_category}) incompatible "
                               f"with home {table.statement_type.value}",
                        source=table.source,
                    ))
                    continue
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
                role = table_role_of(table)
                rmap = values_index.setdefault((gkey, key), {}).setdefault(ry, {})
                for v in li.values:
                    if v.year is not None and _year_ok(v.year):
                        if v.source is None:        # carry value-level provenance (C1)
                            v.source = table.source
                        prev = rmap.get(v.year)     # within a report, prefer the primary source
                        if prev is None or _ROLE_PREF[role] < _ROLE_PREF[prev[1]]:
                            rmap[v.year] = (v, role)

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
                cands = [(ry, vr[0], vr[1]) for ry, rmap in idx.items()
                         if (vr := rmap.get(year)) is not None]
                if not cands:
                    continue
                # Drop analytical/ratio sources when a real (primary/note) value exists.
                non_analytical = [c for c in cands if c[2] != "analytical"]
                cands = non_analytical or cands
                # Rank: primary > note > analytical, then newest-report preference.
                ry, vobj, _role = min(cands, key=lambda c: (_ROLE_PREF[c[2]], _rank(c[0], year)))
                values.append(LineItemValue(
                    year=year, value=vobj.value, raw=vobj.raw, source_report_year=ry,
                    source=vobj.source,
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
        items = _consolidate_split_lines(items)   # recover leaves split by section drift across reports
        title, currency, unit = group_info.get(gkey, ("", None, None))
        mt = FinancialTable(
            statement_type=st, title=title, currency=currency, unit_scale=unit,
            consolidated=consolidated, years=sorted(present_years), line_items=items,
        )
        mt.table_role = infer_table_role(mt)   # primary | note | analytical (P3/#12)
        merged.append(mt)

    insights = [i for res in results for i in res.insights]
    review = [i for res in results for i in res.insights_review]

    logger.info(
        "Multi-year resolution: company=%r, %d reports, years %s, %d merged tables, %d quarantined",
        company, len(results), data_years, len(merged), len(rejected),
    )
    return CompanyResult(
        company=company,
        fiscal_years=data_years,
        source_reports=[r.file_name for r in results],
        tables=merged,
        insights=insights,
        insights_review=review,
        rejected_lines=rejected,
    )
