"""Shared enums and references used across extraction layers."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class StatementType(str, Enum):
    """Tables we recognize. Covers the Millat/Lucky template union plus the
    other standard statements that future templates are likely to need.
    """

    # --- Primary (face) statements ---
    income_statement = "income_statement"     # template Output 'P&L'
    balance_sheet = "balance_sheet"           # template Output 'Balance Sheet'
    cash_flow = "cash_flow"                   # statement of cash flows
    equity_changes = "equity_changes"         # statement of changes in equity
    financial_highlights = "financial_highlights"  # six-year / key-data summary

    # --- P&L note-level breakdown tables (template PL1–PL7) ---
    revenue = "revenue"                       # PL1
    cost_of_sales = "cost_of_sales"           # PL2
    operating_expenses = "operating_expenses"  # PL3 (distribution/admin/other op.)
    other_income = "other_income"             # PL4
    finance_cost = "finance_cost"             # PL5
    taxation = "taxation"                     # PL6 (levy + income tax)
    oci = "oci"                               # PL7 (other comprehensive income)

    # --- Balance-sheet note-level breakdown tables (template BS1–BS5) ---
    non_current_assets = "non_current_assets"          # BS1
    current_assets = "current_assets"                  # BS2
    share_capital_reserves = "share_capital_reserves"  # BS3
    non_current_liabilities = "non_current_liabilities"  # BS4
    current_liabilities = "current_liabilities"        # BS5

    # --- Sentinels ---
    unclassified = "unclassified"  # Layer 2 unsure -> sent to GPT to classify
    other = "other"                # confirmed NOT a target table -> rejected


# Sentinels are never extraction targets.
_SENTINELS = {StatementType.unclassified, StatementType.other}

# The set of tables we actually extract & map. Even with NO template provided,
# only these are emitted — not every table found in the PDF. Both `unclassified`
# and `other` are excluded (the former is routed to GPT first; the latter is
# rejected outright).
TARGET_STATEMENT_TYPES: frozenset[StatementType] = frozenset(
    st for st in StatementType if st not in _SENTINELS
)


class SourceRef(BaseModel):
    """Where a piece of data came from (for traceability)."""

    report_file: str
    report_year: Optional[int] = None
    pages: list[int] = Field(default_factory=list, description="1-based pages the data spans")
    section: Optional[str] = Field(None, description="Section/heading title")
    table_id: Optional[str] = Field(None, description="Stable id of the source table, e.g. 'file:p12:0'")
    table_title: Optional[str] = Field(None, description="Title/heading of the source table, if any")


# --- statement polarity (P3): the eval-free signal for cross-statement bleed ---
# Maps a statement type to its side of the books. Used to reject e.g. a cash
# (asset) line landing in a share-capital (equity) row.
_POLARITY: dict[StatementType, str] = {
    StatementType.non_current_assets: "asset",
    StatementType.current_assets: "asset",
    StatementType.non_current_liabilities: "liability",
    StatementType.current_liabilities: "liability",
    StatementType.share_capital_reserves: "equity",
    StatementType.equity_changes: "equity",
    StatementType.revenue: "income",
    StatementType.other_income: "income",
    StatementType.cost_of_sales: "expense",
    StatementType.operating_expenses: "expense",
    StatementType.finance_cost: "expense",
    StatementType.taxation: "expense",
}


def polarity(statement_type: StatementType, section: Optional[str] = None) -> Optional[str]:
    """'asset' | 'liability' | 'equity' | 'income' | 'expense' | None.

    Returns None for mixed/face statements (balance_sheet, income_statement, …)
    and for unknown types — a None polarity never blocks a match (we only reject
    on a *confident* conflict)."""
    return _POLARITY.get(statement_type)
