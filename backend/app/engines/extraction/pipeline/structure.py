"""Interpretation — reconstruct financial tables from detected grids.

  - Grids confidently classified by Layer 2 -> rule-based reconstruction (free).
  - Unclassified grids -> GPT classifies AND reconstructs in one request
    (see gpt_tables.gpt_structure_grid); kept only if GPT judges them financial.
  - No GPT request is made when every grid was already classified.
"""
from __future__ import annotations

import re

from app.core.logging import get_logger
from app.engines.extraction.models.financials import FinancialTable, LineItem, LineItemValue
from app.engines.extraction.models.table import RawTable, TableSet
from app.engines.extraction.pipeline import gridutils as gu
from app.engines.extraction.services.styles import is_section_header

logger = get_logger(__name__)

_DASH = {"-", "–", "—", "�", ""}
_YEAR_ONLY = re.compile(r"^(?:19|20)\d{2}$")


def _parse_value(text: str) -> tuple[float | None, str | None]:
    """Parse an accounting number: '(1,234)' -> -1234, '–' -> None."""
    s = (text or "").strip()
    if s in _DASH:
        return None, (s or None)
    neg = "(" in s and ")" in s
    cleaned = re.sub(r"[()]", "", s).replace(",", "").replace("%", "").strip()
    try:
        v = float(cleaned)
    except ValueError:
        return None, s
    return (-v if neg else v), s


def _year_columns(header: list[str]) -> dict[int, int]:
    """Map each header column index that contains a year -> that year."""
    cols: dict[int, int] = {}
    for c, cell in enumerate(header):
        years = gu.extract_years([cell])
        if years:
            cols[c] = years[0]
    return cols


def build_financial_table(raw: RawTable, resolver=None) -> FinancialTable:
    """Rule-based parse of a clean Layer-2 grid into a FinancialTable.

    Each line label is resolved to a canonical metric (registry-backed) so the
    mapping/multi-year layers can join on a stable key instead of raw text.
    """
    if resolver is None:
        from app.engines.extraction.services.metric_resolver import get_resolver

        resolver = get_resolver()

    header = raw.header
    year_cols = _year_columns(header)
    if not year_cols and raw.years:
        # Fallback: assume the rightmost N columns are the value columns.
        ncols = len(header) if header else (len(raw.rows[0]) if raw.rows else 0)
        value_cols = list(range(max(0, ncols - len(raw.years)), ncols))
        year_cols = dict(zip(value_cols, raw.years))

    header_years = set(year_cols.values())
    line_items: list[LineItem] = []
    current_section: str | None = None
    for row in raw.rows:
        if not row:
            continue
        label = (row[0] or "").strip()
        if not label:
            continue
        values: list[LineItemValue] = []
        for c, year in sorted(year_cols.items()):
            cell = row[c] if c < len(row) else ""
            raw_txt = (cell or "").strip()
            # Reject a bare year that is just the column header repeated as a value
            # (e.g. a sub-note header row "Components consumed | 2024 | 2025").
            if _YEAR_ONLY.match(raw_txt) and int(raw_txt) in header_years:
                continue
            value, parsed_raw = _parse_value(cell)
            if value is None and not parsed_raw:
                continue
            values.append(LineItemValue(year=year, value=value, raw=parsed_raw))

        if not values:
            # A value-less row is a section header only if it's ALL-CAPS or ends
            # with ':' (e.g. 'LOCAL SALES', 'Local:'). Otherwise it's a value-less
            # leaf / junk row — skip it without clobbering the current section.
            stripped = label.rstrip(":").strip()
            if is_section_header(label) or label.rstrip().endswith(":"):
                current_section = stripped
            continue

        match = resolver.resolve(label)
        line_items.append(
            LineItem(
                label=label,
                section=current_section,
                values=values,
                canonical_metric=match.canonical_key if match else None,
                canonical_category=match.category if match else None,
            )
        )

    return FinancialTable(
        statement_type=raw.statement_type,
        title=raw.title,
        currency=raw.currency,
        unit_scale=raw.unit_scale,
        consolidated=raw.consolidated,
        years=raw.years or sorted(set(year_cols.values())),
        line_items=line_items,
        source=raw.source,
    )


def structure_tables(table_set: TableSet, gpt=None) -> list[FinancialTable]:
    """Reconstruct financial tables from detected grids.

      - Grids confidently classified by Layer 2 -> rule-based reconstruction (free).
      - Unclassified grids -> GPT classifies AND reconstructs in one request; a
        FinancialTable is kept only if GPT judges it a financial table.

    No GPT request is made when every grid was already classified.
    """
    confident = [r for r in table_set.tables if not r.needs_review]
    review = [r for r in table_set.tables if r.needs_review]

    tables = [build_financial_table(r) for r in confident]

    if not review:
        logger.info("All %d grids classified by Layer 2 — no GPT for grids.", len(tables))
        return tables
    if gpt is None:
        logger.info("%d unclassified grid(s) but no GPT client supplied.", len(review))
        return tables

    from app.engines.extraction.pipeline.gpt_tables import gpt_structure_grid

    logger.info("GPT classifying + reconstructing %d unclassified grid(s).", len(review))
    for raw in review:
        ft = gpt_structure_grid(raw, gpt)
        # If GPT reconstructs it as a financial table, use that; otherwise keep
        # the rule-based grid (as `unclassified`) so the no-template output still
        # emits it (the template path drops non-target tables anyway).
        tables.append(ft if ft is not None else build_financial_table(raw))
    return tables
