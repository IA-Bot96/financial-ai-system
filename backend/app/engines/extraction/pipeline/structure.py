"""Layer 3a — Structuring + ambiguous-table classification.

Logic:
  - Structuring is RULE-BASED (no GPT): Layer 2 now yields clean grids
    (label + per-year values), so each RawTable is parsed locally into a
    FinancialTable.
  - GPT is used ONLY to classify the tables Layer 2 flagged `needs_review`,
    and those are sent in a SINGLE batched request.
  - If no table needs review, NO OpenAI request is made at all.
"""
from __future__ import annotations

import re

from app.core.logging import get_logger
from app.engines.extraction.models.classification import BatchClassification
from app.engines.extraction.models.common import TARGET_STATEMENT_TYPES
from app.engines.extraction.models.financials import FinancialTable, LineItem, LineItemValue
from app.engines.extraction.models.table import RawTable, TableSet
from app.engines.extraction.pipeline import gridutils as gu
from app.engines.extraction.services import prompts
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


def _signature(raw: RawTable) -> str:
    labels = " ".join(r[0] for r in raw.rows[:12] if r)
    return " ".join([raw.title, " ".join(raw.header), labels]).strip()[:500]


def _batch_classify(raws: list[RawTable], gpt) -> dict[str, object]:
    """One GPT request classifying all ambiguous tables. Returns id -> type."""
    allowed = ", ".join(st.value for st in TARGET_STATEMENT_TYPES)
    listing = "\n".join(f"- {r.table_id} :: {_signature(r)}" for r in raws)
    system, user = prompts.render("classify", allowed_types=allowed, tables=listing)
    try:
        result = gpt.complete_structured(system, user, BatchClassification)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Batch classification failed (%d tables): %s", len(raws), exc)
        return {}
    return {c.table_id: c.statement_type for c in result.classifications}


def structure_tables(table_set: TableSet, gpt=None) -> list[FinancialTable]:
    """Parse all tables (rule-based); classify only the ambiguous ones via GPT.

    Makes at most ONE OpenAI request (the batch), and none if every table was
    already classified by Layer 2.
    """
    tables = [build_financial_table(r) for r in table_set.tables]

    review = [r for r in table_set.tables if r.needs_review]
    if not review:
        logger.info("All %d tables classified by Layer 2 — no GPT request.", len(tables))
        return tables

    if gpt is None:
        logger.info("%d tables need review but no GPT client supplied.", len(review))
        return tables

    logger.info("Batch-classifying %d ambiguous table(s) in one GPT request.", len(review))
    mapping = _batch_classify(review, gpt)
    index = {r.table_id: i for i, r in enumerate(table_set.tables)}
    for table_id, st in mapping.items():
        if table_id in index:
            tables[index[table_id]].statement_type = st
    return tables
