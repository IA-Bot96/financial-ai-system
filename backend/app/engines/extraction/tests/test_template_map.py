"""Tests for Layer 5 template mapping."""
from pathlib import Path

import openpyxl
import pytest

from app.core.config import get_settings
from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.company import CompanyResult
from app.engines.extraction.models.financials import FinancialTable, LineItem, LineItemValue
from app.engines.extraction.pipeline.template_map import apply_plan, build_plan


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("USE_EMBEDDINGS", "false")
    yield
    get_settings.cache_clear()


def _revenue_company() -> CompanyResult:
    table = FinancialTable(
        statement_type=StatementType.revenue, title="Revenue",
        line_items=[
            LineItem(label="Local sales", values=[
                LineItemValue(year=2024, value=50.0, source_report_year=2025),
                LineItemValue(year=2025, value=60.0, source_report_year=2025)]),
            LineItem(label="Export sales", values=[
                LineItemValue(year=2024, value=30.0),
                LineItemValue(year=2025, value=40.0)]),
        ],
    )
    return CompanyResult(company="Acme", fiscal_years=[2024, 2025], tables=[table])


def _make_template(path: Path) -> None:
    wb = openpyxl.Workbook()
    pl = wb.active
    pl.title = "PL1 - Revenue"
    pl["A1"] = "Note 27 - Gross Revenue"
    pl["B2"], pl["D2"] = "Historic", "Forecasted"
    pl["A3"], pl["B3"], pl["C3"], pl["D3"] = "Particulars", 2024, 2025, 2026  # D = forecast
    pl["A4"] = "GROSS REVENUE"                       # section header
    pl["A5"] = "Local sales"                         # leaf (empty)
    pl["A6"] = "Export sales"                        # leaf (empty)
    pl["A7"], pl["B7"], pl["C7"] = "Gross Revenue", "=SUM(B5:B6)", "=SUM(C5:C6)"  # subtotal

    out = wb.create_sheet("P&L")                     # output sheet: all formulas
    out["A1"] = "Income Statement"
    out["B3"], out["C3"], out["D3"] = 2024, 2025, 2026
    out["D2"] = "Forecasted"
    out["A5"], out["B5"], out["C5"] = "Revenue", "='PL1 - Revenue'!B5", "='PL1 - Revenue'!C5"

    mgmt = wb.create_sheet("Mgmt Info.")             # no year header
    mgmt["A1"] = "Company profile"
    wb.save(path)


def test_build_plan_targets_leaf_historical_cells(tmp_path):
    tpl = tmp_path / "template.xlsx"
    _make_template(tpl)
    plan = build_plan(_revenue_company(), tpl)

    assert "PL1 - Revenue" in plan.sheets_processed
    assert "P&L" in plan.sheets_skipped          # formulas -> low empty fraction
    assert "Mgmt Info." in plan.sheets_skipped   # no year header

    cells = {(w.coordinate, w.value) for w in plan.writes}
    assert ("B5", 50.0) in cells and ("C5", 60.0) in cells   # Local sales 2024/2025
    assert ("B6", 30.0) in cells and ("C6", 40.0) in cells   # Export sales
    # Never write forecast (col D) or formula cells (B7/C7).
    coords = {w.coordinate for w in plan.writes}
    assert not any(c.startswith("D") for c in coords)
    assert "B7" not in coords and "C7" not in coords


def test_apply_plan_preserves_formulas_and_writes_values(tmp_path):
    tpl = tmp_path / "template.xlsx"
    _make_template(tpl)
    out = tmp_path / "out.xlsx"
    apply_plan(build_plan(_revenue_company(), tpl), tpl, out)

    wb = openpyxl.load_workbook(out, data_only=False)
    pl = wb["PL1 - Revenue"]
    assert pl["B5"].value == 50.0 and pl["C5"].value == 60.0
    assert pl["B7"].value == "=SUM(B5:B6)"     # subtotal formula preserved
    assert pl["D5"].value is None              # forecast column untouched
    assert wb["P&L"]["B5"].value == "='PL1 - Revenue'!B5"  # output sheet untouched
    wb.close()


_LUCKY = Path(r"C:\AI Financial Intelligence\data\lucky-template.xlsx")


@pytest.mark.skipif(not _LUCKY.exists(), reason="real Lucky template not available")
def test_real_lucky_template_maps_revenue_leaves(tmp_path):
    plan = build_plan(_revenue_company(), _LUCKY)
    # Output sheets are never written.
    assert "P&L" in plan.sheets_skipped and "Balance Sheet" in plan.sheets_skipped
    # Revenue leaves land on the PL1 sheet.
    pl1 = [w for w in plan.writes if w.sheet == "PL1 - Revenue"]
    assert any(w.matched_label == "Local sales" for w in pl1)
    assert any(w.matched_label == "Export sales" for w in pl1)
