"""Tests for Layer 7 orchestration (assembly + output routing)."""
import openpyxl
import pytest

from app.core.config import get_settings
from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.financials import FinancialTable, LineItem, LineItemValue
from app.engines.extraction.models.insight import Insight
from app.engines.extraction.models.result import DocumentResult
from app.engines.extraction.pipeline.orchestrator import process_documents


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("USE_EMBEDDINGS", "false")
    yield
    get_settings.cache_clear()


def _doc(report_year, st, lines) -> DocumentResult:
    table = FinancialTable(statement_type=st, title=st.value.replace("_", " ").title(), currency="PKR",
                           unit_scale="thousands", years=[report_year - 1, report_year], line_items=lines)
    ins = [Insight(year=report_year, source_report_year=report_year, area="x",
                   takeaway=f"point {report_year}", source_section="CEO Review", page=1, confidence=0.9)]
    return DocumentResult(file_name=f"r{report_year}.pdf", report_year=report_year, tables=[table], insights=ins)


def _rev_lines(y0, y1, local, export):
    return [
        LineItem(label="Local sales", values=[LineItemValue(year=y0, value=local[0]), LineItemValue(year=y1, value=local[1])]),
        LineItem(label="Export sales", values=[LineItemValue(year=y0, value=export[0]), LineItemValue(year=y1, value=export[1])]),
    ]


def test_no_template_writes_styled_workbook(tmp_path):
    results = [_doc(2025, StatementType.income_statement,
                    [LineItem(label="Revenue", values=[LineItemValue(year=2024, value=100.0), LineItemValue(year=2025, value=120.0)])])]
    out = tmp_path / "out.xlsx"
    res = process_documents(results, out)
    assert res.mode == "no_template" and res.plan is None
    wb = openpyxl.load_workbook(out)
    assert "Income Statement" in wb.sheetnames and "Insights" in wb.sheetnames
    wb.close()


def _make_template(path):
    wb = openpyxl.Workbook()
    pl = wb.active
    pl.title = "PL1 - Revenue"
    pl["A1"] = "Note 27 - Gross Revenue"
    pl["D2"] = "Forecasted"
    pl["A3"], pl["B3"], pl["C3"], pl["D3"] = "Particulars", 2024, 2025, 2026
    pl["A4"] = "GROSS REVENUE"
    pl["A5"] = "Local sales"
    pl["A6"] = "Export sales"
    pl["A7"], pl["B7"], pl["C7"] = "Gross Revenue", "=SUM(B5:B6)", "=SUM(C5:C6)"
    wb.save(path)


def test_template_fills_and_appends_insights(tmp_path):
    tpl = tmp_path / "tpl.xlsx"
    _make_template(tpl)
    results = [_doc(2025, StatementType.revenue, _rev_lines(2024, 2025, (50.0, 60.0), (30.0, 40.0)))]
    out = tmp_path / "filled.xlsx"

    res = process_documents(results, out, template_path=tpl)
    assert res.mode == "template" and res.plan and res.plan.writes

    wb = openpyxl.load_workbook(out)
    pl = wb["PL1 - Revenue"]
    assert pl["B5"].value == 50.0 and pl["C5"].value == 60.0   # Local sales filled
    assert pl["B7"].value == "=SUM(B5:B6)"                     # subtotal formula preserved
    assert "Insights" in wb.sheetnames                          # insights appended
    wb.close()
