"""Tests for post-merge leaf consolidation — folding the same leaf split across reports
by section drift, while keeping genuinely-different same-label lines separate."""
from app.engines.extraction.models.financials import LineItem, LineItemValue
from app.engines.extraction.pipeline.multiyear import _consolidate_split_lines


def _li(label, section, vals):
    return LineItem(label=label, section=section, canonical_metric=None,
                    canonical_category=None,
                    values=[LineItemValue(year=y, value=v, source_report_year=ry)
                            for (y, v, ry) in vals])


def test_folds_same_leaf_split_by_section_across_reports():
    # 'Mark-up on short-term borrowings' filed under different sections in each report.
    items = [
        _li("Mark-up on short-term borrowings", "38 Finance cost", [(2022, 217534, 2023), (2023, 1196780, 2023)]),
        _li("Mark-up on short-term borrowings", "Finance cost",    [(2023, 1196780, 2024), (2024, 802081, 2024)]),
        _li("Mark-up on short-term borrowings", None,              [(2024, 911458, 2025), (2025, 2007839, 2025)]),
    ]
    out = _consolidate_split_lines(items)
    assert len(out) == 1
    by_year = {v.year: v.value for v in out[0].values}
    assert set(by_year) == {2022, 2023, 2024, 2025}        # all years recovered
    assert by_year[2024] == 911458                          # restatement: newest report wins
    assert by_year[2025] == 2007839


def test_keeps_different_lines_sharing_a_label():
    # Distribution vs Administrative 'Salaries' coexist in the SAME report -> must NOT fold.
    items = [
        _li("Salaries and wages", "Distribution",   [(2024, 5000, 2025), (2025, 5500, 2025)]),
        _li("Salaries and wages", "Administrative",  [(2024, 3000, 2025), (2025, 3200, 2025)]),
    ]
    out = _consolidate_split_lines(items)
    assert len(out) == 2                                    # conflict on (2024, rpt2025) -> kept apart


def test_singletons_and_distinct_labels_untouched():
    items = [
        _li("Revenue", None, [(2025, 100, 2025)]),
        _li("Cost of sales", None, [(2025, 60, 2025)]),
    ]
    out = _consolidate_split_lines(items)
    assert len(out) == 2
    assert {li.label for li in out} == {"Revenue", "Cost of sales"}
