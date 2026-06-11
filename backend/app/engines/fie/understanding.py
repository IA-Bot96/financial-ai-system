"""Formula-keyword catalog (boot-time contract check).

Maps a query regex to a canonical formula id + the metrics it needs. The rule-based query
frame-builder that consumed this was retired in favour of the LLM-first controller planner
(see schemas.PLAN_SYS); only the keyword -> formula-id catalog remains, exercised by
bootcheck to assert every keyworded formula id is registered.
"""

from __future__ import annotations

import re

# formula keyword -> (canonical formula id, required metrics)
_FORMULA_KEYWORDS: list[tuple[re.Pattern, str, list[str]]] = [
    (re.compile(r"current\s+ratio", re.I), "current_ratio",
     ["current_assets", "current_liabilities"]),
    (re.compile(r"quick\s+ratio|acid[- ]test", re.I), "quick_ratio",
     ["current_assets", "stock_in_trade", "current_liabilities"]),
    (re.compile(r"gross\s+margin", re.I), "gross_margin", ["gross_profit", "revenue"]),
    (re.compile(r"operating\s+margin", re.I), "operating_margin",
     ["operating_profit", "revenue"]),
    (re.compile(r"net\s+margin|net\s+profit\s+margin", re.I), "net_margin",
     ["pat", "revenue"]),
    (re.compile(r"\broe\b|return on equity", re.I), "roe", ["pat", "total_equity"]),
    (re.compile(r"\broa\b|return on assets", re.I), "roa", ["pat", "total_assets"]),
    (re.compile(r"debt[- ]to[- ]equity|d/e\b|gearing", re.I), "debt_to_equity",
     ["non_current_liabilities", "current_liabilities", "total_equity"]),
    (re.compile(r"interest\s+coverage|times interest earned", re.I), "interest_coverage",
     ["operating_profit", "finance_cost"]),
    (re.compile(r"revenue\s+growth|sales\s+growth", re.I), "revenue_growth", ["revenue"]),
    (re.compile(r"earnings\s+growth|profit\s+growth", re.I), "earnings_growth", ["pat"]),
    (re.compile(r"free\s+cash\s+flow|\bfcf\b", re.I), "free_cash_flow",
     ["operating_cash_flow", "capex"]),
    (re.compile(r"\bebitda\b", re.I), "ebitda", ["operating_profit", "depreciation_expense"]),
    (re.compile(r"book\s+value\s+per\s+share|\bbvps\b", re.I), "book_value_per_share",
     ["total_equity", "shares_outstanding"]),
]
