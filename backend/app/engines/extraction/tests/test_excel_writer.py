"""Tests for Layer 6 styled Excel writer (no-template path)."""
import openpyxl

from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.company import CompanyResult
from app.engines.extraction.models.financials import FinancialTable, LineItem, LineItemValue
from app.engines.extraction.models.insight import Insight
from app.engines.extraction.pipeline.excel_writer import write_company_workbook
from app.engines.extraction.services import styles as S


def _company() -> CompanyResult:
    table = FinancialTable(
        statement_type=StatementType.income_statement, title="Income Statement",
        currency="PKR", unit_scale="thousands", years=[2024, 2025],
        line_items=[
            LineItem(label="Revenue from contracts with customers",
                     values=[LineItemValue(year=2024, value=95020.0), LineItemValue(year=2025, value=53347.0)]),
            LineItem(label="Cost of sales",
                     values=[LineItemValue(year=2024, value=-71048.0), LineItemValue(year=2025, value=-38940.0)]),
            LineItem(label="Gross profit",
                     values=[LineItemValue(year=2024, value=23972.0), LineItemValue(year=2025, value=14407.0)]),
        ],
    )
    ins = [Insight(year=2025, source_report_year=2025, area="Margins",
                   takeaway="Gross margin held.", source_section="CEO Review", page=10, confidence=0.91)]
    review = [Insight(year=2025, source_report_year=2025, area="Risk",
                      takeaway="Mixed signal.", source_section="Risks", page=20, confidence=0.6)]
    return CompanyResult(company="Acme", fiscal_years=[2024, 2025], tables=[table],
                         insights=ins, insights_review=review)


def test_workbook_structure_and_values(tmp_path):
    out = tmp_path / "out.xlsx"
    write_company_workbook(_company(), out)
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Income Statement", "Insights", "Insights Review"]

    ws = wb["Income Statement"]
    assert ws["A1"].value == "Income Statement"
    assert ws["A2"].value == "(PKR in thousands)"
    assert ws["A3"].value == "Particulars" and ws["B3"].value == 2024 and ws["C3"].value == 2025
    assert ws["A4"].value == "Revenue from contracts with customers"
    assert ws["B4"].value == 95020.0 and ws["C4"].value == 53347.0
    assert ws["C5"].value == -38940.0
    assert ws.freeze_panes == "B4"
    wb.close()


def test_template_styling_applied(tmp_path):
    out = tmp_path / "out.xlsx"
    write_company_workbook(_company(), out)
    wb = openpyxl.load_workbook(out)
    ws = wb["Income Statement"]

    # Navy header, white bold, parens number format.
    assert ws["A1"].fill.fgColor.rgb == S.NAVY and ws["A1"].font.color.rgb == S.WHITE
    assert ws["A3"].fill.fgColor.rgb == S.NAVY and ws["A3"].font.bold
    assert ws["A2"].fill.fgColor.rgb == S.UNIT_BLUE
    assert ws["B4"].number_format == S.NUMBER_FORMAT
    # "Gross profit" -> total row -> green + bold.
    assert ws["A6"].value == "Gross profit"
    assert ws["A6"].fill.fgColor.rgb == S.TOTAL_GREEN and ws["A6"].font.bold

    ins = wb["Insights"]
    assert [ins.cell(1, c).value for c in range(1, 8)] == [
        "Year", "Source Report Year", "Area", "Takeaway", "Source Section", "Page", "Confidence"]
    assert ins["A1"].fill.fgColor.rgb == S.NAVY
    assert ins["D2"].value == "Gross margin held." and ins["G2"].value == 0.91
    assert ins.freeze_panes == "A2"
    wb.close()
