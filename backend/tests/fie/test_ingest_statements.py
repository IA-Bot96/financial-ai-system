"""Task 0.2 — pure parser unit tests with synthetic sheets."""

import openpyxl

from app.engines.fie.ingest.statements import (
    _coerce_number,
    detect_header_row,
    parse_grid_sheet,
)
from app.engines.fie.ontology import MetricOntology


def _ws(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "P&L"
    for r, row in enumerate(rows, start=1):
        for c, val in enumerate(row, start=1):
            ws.cell(r, c, val)
    return ws


def test_coerce_number_variants():
    assert _coerce_number("(1,234)") == -1234.0
    assert _coerce_number("45,665,237") == 45665237.0
    assert _coerce_number("–") is None
    assert _coerce_number(None) is None
    assert _coerce_number(-79287) == -79287.0


def test_header_detection_headline_layout():
    ws = _ws([
        ["Millat Tractors Limited"],
        ["Unconsolidated P&L"],
        ["(Rupees in thousand)", None, "Historical", None, "Forecasted"],
        ["Particulars", "Notes", 2023, 2024, 2025],
        ["INCOME STATEMENT"],
        ["Revenue from contracts with customers", 32, 44190843, 91534501, 52108997],
    ])
    assert detect_header_row(ws) == 4


def test_parse_grid_sections_periods_and_metric():
    ws = _ws([
        ["Co"], ["P&L"],
        ["(Rupees in thousand)", None, "Historical", "Historical", "Forecasted"],
        ["Particulars", "Notes", 2024, 2025, 2026],
        ["INCOME STATEMENT"],  # section header, no values -> skipped as section
        ["Revenue from contracts with customers", 32, 91534501, 52108997, None],
    ])
    recs = parse_grid_sheet(ws, level="headline", ontology=MetricOntology())
    rev = [r for r in recs if r["metric"] == "revenue"]
    assert {r["year"]: r["value"] for r in rev} == {2024: 91534501.0, 2025: 52108997.0, 2026: None}
    assert {r["year"]: r["period_type"] for r in rev} == {
        2024: "historical", 2025: "historical", 2026: "forecasted"}
    assert all(r["section"] == "INCOME STATEMENT" for r in rev)
