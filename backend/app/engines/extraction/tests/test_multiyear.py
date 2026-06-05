"""Tests for rule-based multi-year resolution (prefer the N+1 report)."""
from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.financials import FinancialTable, LineItem, LineItemValue
from app.engines.extraction.models.insight import Insight
from app.engines.extraction.models.result import DocumentResult
from app.engines.extraction.pipeline.multiyear import _rank, resolve_multiyear


def _revenue_table(label, year_values: dict[int, float]) -> FinancialTable:
    return FinancialTable(
        statement_type=StatementType.income_statement,
        title="Income Statement",
        line_items=[LineItem(
            label=label,
            canonical_metric="revenue",
            canonical_category="income_statement",
            values=[LineItemValue(year=y, value=v) for y, v in year_values.items()],
        )],
    )


def _doc(report_year, label, year_values, insights=None, company=None) -> DocumentResult:
    return DocumentResult(
        file_name=f"r{report_year}.pdf",
        company=company,
        report_year=report_year,
        tables=[_revenue_table(label, year_values)],
        insights=insights or [],
    )


def test_rank_prefers_next_year_then_self_then_later():
    assert _rank(2025, 2024) == 0   # N+1
    assert _rank(2024, 2024) == 1   # self
    assert _rank(2026, 2024) < _rank(2027, 2024)  # closer later wins


def _values(table, label_key="revenue"):
    li = next(li for li in table.line_items if li.canonical_metric == label_key)
    return {v.year: (v.value, v.source_report_year) for v in li.values}


def test_prefers_next_year_report_comparative():
    # 2024 report: 2024 current + 2023 comparative; 2025 report: 2025 + 2024 (restated).
    results = [
        _doc(2024, "Revenue from contracts with customers", {2024: 100.0, 2023: 90.0}),
        _doc(2025, "Net Revenue", {2025: 120.0, 2024: 110.0}),
    ]
    company = resolve_multiyear(results, company="Acme")
    assert company.fiscal_years == [2023, 2024, 2025]
    table = company.tables[0]
    vals = _values(table)
    assert vals[2023] == (90.0, 2024)    # only in 2024 report (which is 2023's N+1)
    assert vals[2024] == (110.0, 2025)   # restated value from the 2025 report wins
    assert vals[2025] == (120.0, 2025)
    # Latest report defines the merged label.
    assert table.line_items[0].label == "Net Revenue"


def test_missing_next_year_falls_back():
    # Reports 2023 and 2025 only (no 2024 report).
    results = [
        _doc(2023, "Revenue", {2023: 90.0, 2022: 80.0}),
        _doc(2025, "Revenue", {2025: 120.0, 2024: 110.0}),
    ]
    company = resolve_multiyear(results)
    vals = _values(company.tables[0])
    assert vals[2022] == (80.0, 2023)   # 2023 is 2022's N+1
    assert vals[2023] == (90.0, 2023)   # N+1 (2024) absent -> own report
    assert vals[2024] == (110.0, 2025)  # only the 2025 report has 2024
    assert vals[2025] == (120.0, 2025)


def test_insights_concatenated_across_reports():
    ins24 = Insight(area="x", takeaway="t24", source_report_year=2024, year=2024, page=1, confidence=0.9)
    ins25 = Insight(area="y", takeaway="t25", source_report_year=2025, year=2025, page=2, confidence=0.9)
    results = [
        _doc(2024, "Revenue", {2024: 1.0}, insights=[ins24]),
        _doc(2025, "Revenue", {2025: 2.0}, insights=[ins25]),
    ]
    company = resolve_multiyear(results)
    assert {i.takeaway for i in company.insights} == {"t24", "t25"}


def test_extracted_company_is_used_as_truth():
    results = [
        _doc(2024, "Revenue", {2024: 1.0}, company="Millat Tractors Limited"),
        _doc(2025, "Revenue", {2025: 2.0}, company="Millat Tractors Limited"),
    ]
    # Caller passes a different name; the extracted (document) name wins.
    company = resolve_multiyear(results, company="wrong-name-from-cli")
    assert company.company == "Millat Tractors Limited"


def test_lines_merge_by_canonical_key_despite_label_change():
    results = [
        _doc(2024, "Turnover - net", {2024: 100.0}),
        _doc(2025, "Revenue from contracts with customers", {2025: 120.0}),
    ]
    company = resolve_multiyear(results)
    # One merged revenue line, not two.
    revenue_lines = [li for li in company.tables[0].line_items if li.canonical_metric == "revenue"]
    assert len(revenue_lines) == 1
    years = {v.year for v in revenue_lines[0].values}
    assert years == {2024, 2025}
