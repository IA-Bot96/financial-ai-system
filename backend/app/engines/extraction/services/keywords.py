"""Keyword sets and embedding prototypes per statement type.

Used by:
  - section detection (heading keywords),
  - the free local classifier (fuzzy keyword match + embedding similarity).
"""
from __future__ import annotations

from app.engines.extraction.models.common import StatementType

# Lowercased keyword phrases. Exact substring -> full score; fuzzy -> capped.
STATEMENT_KEYWORDS: dict[StatementType, list[str]] = {
    # Face statements use whole-statement signals only (no component line items
    # like "revenue"/"cost of sales"/"non-current assets") so they don't collide
    # with the note-level breakdown categories below.
    StatementType.balance_sheet: [
        "balance sheet",
        "statement of financial position",
        "total assets",
        "total liabilities",
        "total equity",
        "total equity and liabilities",
    ],
    StatementType.income_statement: [
        "income statement",
        "statement of profit or loss",
        "profit and loss account",
        "statement of comprehensive income",
        "statement of operations",
        "operating profit",
        "profit before tax",
        "profit after tax for the year",
        "earnings per share",
    ],
    # Statement-level signals only, to avoid colliding with breakdown notes.
    StatementType.cash_flow: [
        "statement of cash flows",
        "cash flow statement",
        "cash flows from operating activities",
        "cash flows from investing activities",
        "cash flows from financing activities",
        "net increase in cash and cash equivalents",
        "net decrease in cash and cash equivalents",
    ],
    StatementType.equity_changes: [
        "statement of changes in equity",
        "changes in equity",
        "transactions with owners",
        "total comprehensive income for the year",
    ],
    StatementType.financial_highlights: [
        "financial highlights",
        "six years at a glance",
        "six year summary",
        "key operating and financial data",
        "performance at a glance",
        "horizontal and vertical analysis",
    ],
    # --- P&L note-level breakdown tables (template PL1–PL7) ---
    StatementType.revenue: [
        "revenue from contracts with customers",
        "gross revenue",
        "local sales",
        "export sales",
        "net revenue",
        "sales tax and federal excise duty",
        "trade discount",
    ],
    StatementType.cost_of_sales: [
        "cost of sales",
        "manufacturing cost",
        "raw material consumed",
        "components consumed",
        "packing material consumed",
        "cost of goods manufactured",
        "work-in-process",
        "stores and spares consumed",
    ],
    StatementType.operating_expenses: [
        "distribution and marketing expenses",
        "distribution costs",
        "administrative expenses",
        "other operating expenses",
        "selling expenses",
        "advertisement and sales promotion",
    ],
    StatementType.other_income: [
        "other income",
        "dividend income",
        "income from financial assets",
        "income from non-financial assets",
        "gain on disposal of property",
        "scrap sales",
        "return on bank deposits",
    ],
    StatementType.finance_cost: [
        "finance cost",
        "mark-up on short-term borrowings",
        "interest expense on long-term finances",
        "mark-up / interest on long-term finances",
        "bank charges",
        "interest expense against lease liabilities",
    ],
    StatementType.taxation: [
        "levy",
        "taxation",
        "final taxes",
        "minimum tax",
        "current tax",
        "deferred tax",
        "income tax charge",
        "tax on exports",
    ],
    StatementType.oci: [
        "other comprehensive income",
        "items not to be reclassified to profit or loss",
        "remeasurement loss",
        "unrealized loss on revaluation",
        "fair value through other comprehensive income",
        "fvoci",
    ],
    # --- Balance-sheet note-level breakdown tables (template BS1–BS5) ---
    StatementType.non_current_assets: [
        "property, plant and equipment",
        "operating fixed assets",
        "capital work in progress",
        "right-of-use assets",
        "investment property",
        "intangible assets",
        "long-term investments",
        "long-term loans and advances",
    ],
    StatementType.current_assets: [
        "stores, spare parts and loose tools",
        "stock-in-trade",
        "trade debts",
        "loans and advances",
        "trade deposits and short-term prepayments",
        "other receivables",
        "short-term investments",
        "cash and bank balances",
    ],
    StatementType.share_capital_reserves: [
        "issued, subscribed and paid-up capital",
        "share capital",
        "reserves",
        "capital reserves",
        "revenue reserves",
        "unappropriated profit",
        "general reserve",
    ],
    StatementType.non_current_liabilities: [
        "long-term finances",
        "deferred grant",
        "lease liabilities",
        "long-term deposits",
        "deferred tax liabilities",
        "long-term financing",
    ],
    StatementType.current_liabilities: [
        "trade and other payables",
        "trade creditors",
        "contract liabilities",
        "short-term borrowings",
        "short-term running finance",
        "current portion of long-term",
        "unclaimed dividend",
    ],
}

# Headings (subset) used for section boundary detection — these are strong,
# title-like signals rather than line-item keywords.
SECTION_HEADINGS: dict[StatementType, list[str]] = {
    StatementType.balance_sheet: ["balance sheet", "statement of financial position"],
    StatementType.income_statement: [
        "income statement",
        "statement of profit or loss",
        "profit and loss account",
        "statement of comprehensive income",
        "statement of operations",
    ],
    StatementType.cash_flow: ["statement of cash flows", "cash flow statement"],
    StatementType.equity_changes: ["statement of changes in equity"],
    StatementType.financial_highlights: [
        "financial highlights",
        "six years at a glance",
        "six year summary",
        "key operating and financial data",
    ],
}

# Natural-language prototypes for embedding similarity (one centroid per type).
EMBEDDING_PROTOTYPES: dict[StatementType, list[str]] = {
    StatementType.balance_sheet: [
        "consolidated balance sheet showing total assets, liabilities and equity",
        "statement of financial position with current and non-current items",
    ],
    StatementType.income_statement: [
        "income statement / statement of profit or loss with operating profit, "
        "profit after tax and earnings per share",
    ],
    StatementType.cash_flow: [
        "statement of cash flows from operating, investing and financing activities",
    ],
    StatementType.equity_changes: [
        "statement of changes in equity with share capital, reserves and retained earnings movements",
    ],
    StatementType.financial_highlights: [
        "financial highlights and six-year summary of key operating and financial data and ratios",
    ],
    # P&L breakdown note tables
    StatementType.revenue: [
        "revenue from contracts with customers split into local and export sales with deductions",
    ],
    StatementType.cost_of_sales: [
        "cost of sales with manufacturing cost, raw and packing material consumed and work in process",
    ],
    StatementType.operating_expenses: [
        "distribution, marketing and administrative operating expenses with salaries and depreciation",
    ],
    StatementType.other_income: [
        "other income from dividends, bank deposits, scrap sales and gains on disposal",
    ],
    StatementType.finance_cost: [
        "finance cost from mark-up and interest on short and long-term borrowings and lease liabilities",
    ],
    StatementType.taxation: [
        "levy, final taxes, current and deferred income tax charge",
    ],
    StatementType.oci: [
        "other comprehensive income, remeasurement losses and unrealized revaluation of investments",
    ],
    # Balance-sheet breakdown note tables
    StatementType.non_current_assets: [
        "non-current assets: property plant and equipment, right-of-use assets, intangibles and long-term investments",
    ],
    StatementType.current_assets: [
        "current assets: stock-in-trade, trade debts, loans and advances, receivables and cash and bank balances",
    ],
    StatementType.share_capital_reserves: [
        "share capital and reserves with paid-up capital, general reserve and unappropriated profit",
    ],
    StatementType.non_current_liabilities: [
        "non-current liabilities: long-term finances, lease liabilities, deferred grant and deferred tax",
    ],
    StatementType.current_liabilities: [
        "current liabilities: trade and other payables, short-term borrowings and current portion of long-term liabilities",
    ],
}
