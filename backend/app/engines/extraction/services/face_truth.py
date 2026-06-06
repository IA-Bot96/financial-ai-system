"""Trusted face-statement truth + table-role inference (validation primitives P1/P3).

`TrustedFaceIndex` is the single source of truth used to validate emitted values
(template and no-template) against the audited primary statements. It is built
ONLY from `table_role == 'primary'` face tables, with currency-scale and
magnitude-sanity filters and explicit conflict resolution — so a ratio row, a
six-year-summary cell, or a mislabelled note can never become "truth".
"""
from __future__ import annotations

import re
import statistics
from typing import Optional

from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.financials import FinancialTable, LineItem

# Face (primary) statement types.
FACE_TYPES = {
    StatementType.income_statement, StatementType.balance_sheet,
    StatementType.cash_flow, StatementType.equity_changes,
}

# Headline statement totals: a populated value for one of these must reconcile to
# the audited face statement, or it is withheld (never silently shipped wrong).
KEY_METRICS = frozenset({
    "revenue", "cost_of_sales", "gross_profit", "operating_profit",
    "profit_before_tax", "profit_after_tax", "total_assets", "total_liabilities",
    "equity", "total_equity_and_liabilities", "finance_cost", "taxation",
    "income_tax", "tax_expense", "non_current_assets", "current_assets",
    "non_current_liabilities", "current_liabilities", "other_income",
})

# Canonical metrics that name the SAME face concept — folded together for face truth so
# a value under either name supplies the shared metric's truth. "Share capital and
# reserves" is the total-equity line in many statements (= equity); verified equal where
# both appear. Only the alias TARGET is a KEY metric, so lookups stay consistent.
_METRIC_ALIASES = {
    "share_capital_and_reserves": "equity",
}

# Expense/deduction metrics are ALWAYS negative on an additive P&L. Sources report them
# inconsistently (a cost is positive in a cost note, parenthesised in the statement), so
# face truth is normalised to -abs for these — keeping it consistent with the output
# statement's formula sign (=-'PL2'!.. ) so the signed tie-out passes. Profit subtotals
# and other_income are NOT here (they can legitimately be negative / vary). #6.
_EXPENSE_KEY_METRICS = frozenset({"cost_of_sales", "finance_cost", "tax_expense", "taxation", "income_tax"})

# Metrics that LEGITIMATELY appear across statement families — never quarantined
# (deferred tax on the BS, depreciation in PP&E notes, finance-cost-paid in cash flow…).
CROSS_FAMILY_OK = frozenset({
    "deferred_tax", "deferred_tax_liability", "depreciation_expense", "depreciation_pp&e",
    "depreciation_right_of_use_assets", "depreciation_related_parties", "amortization_expense",
    "finance_cost_paid", "income_taxes_paid", "workers_profit_participation_fund",
    "workers_welfare_fund", "dividends_paid",
})

# canonical_category -> statement family.
_CATEGORY_FAMILY = {"income_statement": "income", "balance_sheet": "balance", "cash_flow": "cash"}
# statement_type -> statement family.
_TYPE_FAMILY = {
    StatementType.income_statement: "income", StatementType.revenue: "income",
    StatementType.cost_of_sales: "income", StatementType.operating_expenses: "income",
    StatementType.other_income: "income", StatementType.finance_cost: "income",
    StatementType.taxation: "income", StatementType.oci: "income",
    StatementType.balance_sheet: "balance", StatementType.non_current_assets: "balance",
    StatementType.current_assets: "balance", StatementType.share_capital_reserves: "balance",
    StatementType.non_current_liabilities: "balance", StatementType.current_liabilities: "balance",
    StatementType.equity_changes: "balance", StatementType.cash_flow: "cash",
}


def tieout(value: float, truth: float) -> bool:
    """A populated total agrees with the audited face value (5% + abs epsilon).
    Gross mis-maps (cash vs share capital, a 15x PAT error) are far outside this;
    legitimate rounding/restatement is well inside it."""
    return abs(value - truth) <= 1.0 + 0.05 * abs(truth)


def metric_incompatible(metric: str | None, category: str | None,
                        statement_type: StatementType | None) -> bool:
    """True when `metric` (in `category`) is CONFIDENTLY in a different statement
    family than `statement_type`. Cross-family metrics and unknown families are
    exempt (never blocks on a missing signal)."""
    if not metric or metric in CROSS_FAMILY_OK:
        return False
    cat_family = _CATEGORY_FAMILY.get(category or "")
    home_family = _TYPE_FAMILY.get(statement_type) if statement_type else None
    return bool(cat_family and home_family and cat_family != home_family)


def confidently_incompatible(li: LineItem, statement_type: StatementType) -> bool:
    """P2: True only when the line's canonical metric is CONFIDENTLY in a different
    statement family than its home table (e.g. a `cost_of_sales`/income line inside
    a balance-sheet table)."""
    return metric_incompatible(li.canonical_metric, li.canonical_category, statement_type)

# STRONG analytical markers: a transformed/summarized view that must NEVER be truth,
# regardless of content (a six-year summary lists real totals but is still a summary).
_ANALYTICAL_RE = re.compile(
    r"six[\s-]?year|ten[\s-]?year|highlight|ratio|horizontal|vertical|"
    r"key\s+(?:data|figures|indicators)|at\s+a\s+glance|common[\s-]?size",
    re.I,
)
# WEAK marker: bare "analysis" is ambiguous. "Analysis of Statement of Financial
# Position" is the statement itself (real absolute figures that balance), whereas
# "Horizontal/Vertical analysis" is already caught by the strong markers above. So
# "analysis" only demotes a table when there is NO corroborating primary face
# evidence (a face title or a headline-total line).
_WEAK_ANALYTICAL_RE = re.compile(r"\banalysis\b", re.I)
# A handful of headline-total metrics — their presence confirms a real face statement.
_FACE_TOTAL_METRICS = {
    "revenue", "gross_profit", "operating_profit", "profit_before_tax", "profit_after_tax",
    "total_assets", "total_liabilities", "total_equity_and_liabilities", "equity",
    "cash_at_end_of_period",
}
# Balance-sheet GRAND totals — only the face statement carries these (a PP&E or payables
# note never does). A balance-family sub-type table holding one is really a split-off
# section of the primary statement (e.g. an "ASSETS - NON-CURRENT ASSETS" half-page the
# classifier typed `non_current_assets`), so it should contribute face truth. Subtotals
# like `current_assets` are deliberately excluded — notes carry those (often wrong).
_BS_GRAND_TOTALS = {"total_assets", "total_equity_and_liabilities", "total_liabilities"}
# Face-statement title keywords.
_FACE_TITLE_RE = re.compile(
    r"statement of financial position|balance sheet|statement of profit|"
    r"profit (?:or|and) loss|income statement|cash flow|changes in equity|"
    r"comprehensive income",
    re.I,
)


def infer_table_role(table: FinancialTable) -> str:
    """'primary' | 'note' | 'analytical'. Used when the extractor/classifier
    didn't set `table_role` (e.g. GPT page tables)."""
    title = (table.title or "").lower()
    # Definite analytical: a highlights type, a %/ratio scale, or a STRONG analytical
    # title (six-year summary, ratios, horizontal/vertical analysis…).
    if (table.statement_type == StatementType.financial_highlights
            or _ANALYTICAL_RE.search(title)
            or (table.unit_scale or "").strip() in {"%", "percent", "ratio"}):
        return "analytical"
    metrics = {li.canonical_metric for li in table.line_items if li.canonical_metric}
    if table.statement_type in FACE_TYPES:
        # A face TYPE alone isn't enough (note pages get mis-typed) — require a face
        # title or a headline-total line as corroboration. This corroboration also
        # OUTRANKS a bare "analysis" in the title, so a real statement titled
        # "Analysis of Statement of Financial Position" is correctly primary.
        if _FACE_TITLE_RE.search(title) or (metrics & _FACE_TOTAL_METRICS):
            return "primary"
    # A balance-family section the classifier split off as a sub-type note but which
    # carries a balance-sheet GRAND total is really part of the primary statement.
    if _TYPE_FAMILY.get(table.statement_type) == "balance" and (metrics & _BS_GRAND_TOTALS):
        return "primary"
    # A bare "analysis" block with no primary face evidence is analytical, not a note.
    if _WEAK_ANALYTICAL_RE.search(title):
        return "analytical"
    return "note"


def table_role_of(table: FinancialTable) -> str:
    return table.table_role or infer_table_role(table)


def _is_currency_scale(table: FinancialTable) -> bool:
    unit = (table.unit_scale or "").strip().lower()
    return unit not in {"%", "percent", "ratio"}


def build_face_truth(tables: list[FinancialTable]) -> dict[tuple[str, int], tuple[float, object]]:
    """{(canonical_metric, year): (value, source)} from face statements, primary-first.

    Candidates are collected from primary (tier 0) AND note (tier 1) tables; analytical
    /ratio tables are excluded entirely. For each (metric, year) a PRIMARY value always
    wins; a NOTE value is used only as a FALLBACK when no primary candidate exists for
    that pair (e.g. the oldest comparative year, whose primary statement extracted
    poorly but whose disaggregation notes still carry the real total).

    Within the chosen tier: newest report wins; ties broken by closeness to the metric's
    median (never larger magnitude — that would bless an extra-digit error). The
    magnitude-outlier filter's median is anchored to PRIMARY values so a mis-tagged note
    partial can't shift the reference."""
    # Collect primary + note candidates, tagged by tier (0=primary, 1=note).
    cand: dict[tuple[str, int], list[tuple[float, int, object, int]]] = {}
    for t in tables:
        role = table_role_of(t)
        if role == "analytical" or not _is_currency_scale(t):
            continue
        tier = 0 if role == "primary" else 1
        for li in t.line_items:
            cm = _METRIC_ALIASES.get(li.canonical_metric, li.canonical_metric)
            if not cm:
                continue
            for v in li.values:
                if v.year and v.value is not None:
                    ry = v.source_report_year or (t.source.report_year if t.source else 0) or 0
                    cand.setdefault((cm, v.year), []).append((v.value, ry, v.source or t.source, tier))

    # Per-metric magnitude reference: median of |value|. Anchored to PRIMARY candidates
    # when the metric has any (so note partials don't move it); else fall back to all.
    # Includes zeros — a legitimately-zero metric must not be dropped from the median.
    #
    # CURRENCY-SCALE anchor: a mixed "Analysis of …" table can tag BOTH the real figure
    # (266,748,030) AND its common-size percentage (100) as the same total. The % rows
    # often OUTNUMBER the real ones, which would drag a plain median down to ~100 and make
    # the outlier band reject the real values. So the reference ignores any value more
    # than 1000x below the metric's largest — percentages are ~1e6x smaller than a money
    # figure in thousands, while real year-on-year / consolidated variation stays within ~3x.
    prim_abs: dict[str, list[float]] = {}
    all_abs: dict[str, list[float]] = {}
    for (cm, _y), lst in cand.items():
        for c in lst:
            all_abs.setdefault(cm, []).append(abs(c[0]))
            if c[3] == 0:
                prim_abs.setdefault(cm, []).append(abs(c[0]))

    def _scale_ref(vals: list[float]) -> float:
        nz = [v for v in vals if v]
        if not nz:
            return 0.0
        floor = max(nz) / 1000.0
        return statistics.median([v for v in nz if v >= floor] or nz)

    median_abs = {cm: _scale_ref(prim_abs.get(cm) or vals) for cm, vals in all_abs.items() if vals}

    truth: dict[tuple[str, int], tuple[float, object]] = {}
    for (cm, year), lst in cand.items():
        med = median_abs.get(cm, 0.0)
        # Drop magnitude outliers: an extra-digit OCR error (~10x) must NOT survive,
        # so the band is tight (8x) — well outside normal year-on-year variation but
        # inside a digit slip. Only applied when there's a meaningful median.
        if med > 0:
            plausible = [c for c in lst if abs(c[0]) and med / 8 <= abs(c[0]) <= med * 8]
        else:
            plausible = list(lst)
        chosen = plausible or lst
        # Primary (tier 0) always beats a note fallback (tier 1) for the same pair.
        best_tier = min(c[3] for c in chosen)
        tiered = [c for c in chosen if c[3] == best_tier]
        # Newest report wins; then prefer the value CLOSEST to the metric's median.
        value, _ry, src, _tier = max(tiered, key=lambda c: (c[1], -abs(abs(c[0]) - med)))
        # Normalise expense metrics to the additive-P&L convention (always negative) so
        # face truth, the output formula, and the override all share one sign.
        if cm in _EXPENSE_KEY_METRICS:
            value = -abs(value)
        truth[(cm, year)] = (value, src)
    return truth
