"""Layer 6 — Excel output writer (no-template path) + styled insight sheets.

Generates a workbook with one styled sheet per detected table plus `Insights`
and `Insights Review` sheets, all using the Millat/Lucky template look. The
insight-sheet helper is reusable by the template path too.
"""
from __future__ import annotations

import re
from pathlib import Path

from openpyxl.utils import get_column_letter

from app.core.logging import get_logger
from app.engines.extraction.models.company import CompanyResult
from app.engines.extraction.models.financials import FinancialTable
from app.engines.extraction.models.insight import INSIGHT_COLUMNS, Insight
from app.engines.extraction.services import styles as S

logger = get_logger(__name__)

_INVALID_SHEET = re.compile(r"[:\\/?*\[\]]")
_INSIGHT_WIDTHS = [8, 16, 28, 70, 20, 6, 11]  # per INSIGHT_COLUMNS


def _sheet_name(title: str, used: set[str]) -> str:
    name = _INVALID_SHEET.sub(" ", title or "Table").strip()[:31] or "Table"
    base, i = name, 2
    while name.lower() in used:
        suffix = f" {i}"
        name = base[: 31 - len(suffix)] + suffix
        i += 1
    used.add(name.lower())
    return name


def _table_title(table: FinancialTable) -> str:
    base = table.title.strip() if (table.title and table.title.strip()) else \
        table.statement_type.value.replace("_", " ").title()
    # Label the set so consolidated vs unconsolidated tables are distinguishable.
    if table.consolidated is True and "consolidat" not in base.lower():
        base = f"{base} (Consolidated)"
    elif table.consolidated is False and "unconsolidat" not in base.lower() and "separate" not in base.lower():
        base = f"{base} (Unconsolidated)"
    return base


def _write_table(ws, table: FinancialTable) -> None:
    years = sorted(table.years or sorted({v.year for li in table.line_items for v in li.values if v.year}))
    ncols = 1 + len(years)
    last = get_column_letter(ncols)

    # Row 1 — title banner (navy).
    ws.merge_cells(f"A1:{last}1")
    c = ws.cell(1, 1, _table_title(table))
    c.font, c.fill, c.alignment = S.TITLE_FONT, S.TITLE_FILL, S.LEFT

    # Row 2 — unit / currency line (light blue).
    unit_bits = [b for b in (table.currency, table.unit_scale) if b]
    ws.merge_cells(f"A2:{last}2")
    u = ws.cell(2, 1, f"({' in '.join(unit_bits)})" if unit_bits else "")
    u.font, u.fill, u.alignment = S.UNIT_FONT, S.UNIT_FILL, S.LEFT

    # Row 3 — header (navy, white).
    h = ws.cell(3, 1, "Particulars")
    h.font, h.fill, h.alignment = S.HEADER_FONT, S.HEADER_FILL, S.CENTER
    for i, year in enumerate(years, start=2):
        hc = ws.cell(3, i, year)
        hc.font, hc.fill, hc.alignment = S.HEADER_FONT, S.HEADER_FILL, S.CENTER

    # Data rows.
    row = 4
    for li in table.line_items:
        label = (li.label or "").strip()
        if not label:
            continue
        total = S.is_total_row(label)
        section = (not total) and S.is_section_header(label)
        by_year = {v.year: v.value for v in li.values}

        a = ws.cell(row, 1, label)
        a.alignment = S.LEFT
        a.font = S.TOTAL_FONT if total else (S.SECTION_FONT if section else S.LABEL_FONT)
        if total:
            a.fill = S.TOTAL_FILL
        elif section:
            a.fill = S.SECTION_FILL

        for i, year in enumerate(years, start=2):
            vc = ws.cell(row, i)
            vc.number_format, vc.alignment = S.NUMBER_FORMAT, S.RIGHT
            value = by_year.get(year)
            if value is not None:
                vc.value = value
            vc.font = S.TOTAL_FONT if total else S.VALUE_FONT
            if total:
                vc.fill = S.TOTAL_FILL
            elif section:
                vc.fill = S.SECTION_FILL
        row += 1

    ws.column_dimensions["A"].width = S.LABEL_COL_WIDTH
    for i in range(2, ncols + 1):
        ws.column_dimensions[get_column_letter(i)].width = S.VALUE_COL_WIDTH
    ws.freeze_panes = "B4"


def write_insights_sheet(ws, insights: list[Insight]) -> None:
    for col, (name, width) in enumerate(zip(INSIGHT_COLUMNS, _INSIGHT_WIDTHS), start=1):
        hc = ws.cell(1, col, name)
        hc.font, hc.fill, hc.alignment = S.HEADER_FONT, S.HEADER_FILL, S.CENTER
        ws.column_dimensions[get_column_letter(col)].width = width
    for r, ins in enumerate(insights, start=2):
        for col, value in enumerate(ins.to_row(), start=1):
            cell = ws.cell(r, col, value)
            cell.font = S.VALUE_FONT
            cell.alignment = S.LEFT if col in (3, 4, 5) else S.CENTER
            if col == len(INSIGHT_COLUMNS):  # Confidence
                cell.number_format = S.CONFIDENCE_FORMAT
    ws.freeze_panes = "A2"


def write_workbook(
    tables: list[FinancialTable],
    insights: list[Insight],
    insights_review: list[Insight],
    output_path: str | Path,
) -> Path:
    """No-template output: one styled sheet per table + Insights / Insights Review."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    for table in tables:
        _write_table(wb.create_sheet(_sheet_name(_table_title(table), used)), table)

    write_insights_sheet(wb.create_sheet("Insights"), insights)
    if insights_review:
        write_insights_sheet(wb.create_sheet("Insights Review"), insights_review)

    if not wb.sheetnames:
        wb.create_sheet("Empty")
    wb.save(output_path)
    logger.info("Wrote workbook %s (%d tables, %d insights)", output_path, len(tables), len(insights))
    return Path(output_path)


def write_company_workbook(company: CompanyResult, output_path: str | Path) -> Path:
    return write_workbook(company.tables, company.insights, company.insights_review, output_path)


def append_insights_sheets(
    workbook_path: str | Path,
    insights: list[Insight],
    insights_review: list[Insight],
) -> Path:
    """Add styled Insights / Insights Review sheets to an existing workbook
    (used by the template path after the breakdown sheets are populated)."""
    from openpyxl import load_workbook

    wb = load_workbook(workbook_path)
    for name in ("Insights", "Insights Review"):
        if name in wb.sheetnames:
            del wb[name]
    write_insights_sheet(wb.create_sheet("Insights"), insights)
    if insights_review:
        write_insights_sheet(wb.create_sheet("Insights Review"), insights_review)
    wb.save(workbook_path)
    return Path(workbook_path)
