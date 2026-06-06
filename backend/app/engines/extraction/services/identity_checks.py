"""Accounting-identity checks — internal-consistency validation of the audited face
truth (and, by extension, the emitted headline statements).

Our other validators compare each metric to face truth (an external reference). These
checks instead verify that the face-truth metrics satisfy the accounting identities they
MUST satisfy if extracted correctly — the only signal that can catch a face-truth
*extraction* error (e.g. a Profit-after-tax that doesn't equal PBT − tax). No alias
dictionary is needed: our face truth is already keyed by canonical metric.

Face truth is stored signed with the additive P&L convention (expenses negative), so the
identities are plain sums. An identity is only evaluated when EVERY metric it needs is
present for that year — a missing metric is skipped, never failed (no false flags)."""
from __future__ import annotations

from dataclasses import dataclass

# Each identity: target == sum(terms). Terms reference canonical metrics; a tuple of
# alternatives means "first one present" (e.g. tax may be tax_expense/taxation/income_tax).
_TAX = ("tax_expense", "taxation", "income_tax")

# Only DEFINITIONALLY-ROBUST identities are included — ones that hold regardless of how
# completely a statement is decomposed. The equity/liability composition
# (total_E&L = equity + NCL + CL) is deliberately EXCLUDED: Pakistani balance sheets
# often carry a separate "Surplus on revaluation of PP&E" section between equity and
# liabilities, so that identity false-fails on correct extraction.
_IDENTITIES = [
    # name, statement, target metric, term metrics (summed, signed).
    ("gross_profit = revenue + cost_of_sales", "P&L", "gross_profit",
     ["revenue", "cost_of_sales"]),
    # PAT = PBT + tax (tax stored negative). Levy / NCI are intentionally NOT added — the
    # available NCI face metric is the balance-sheet equity NCI (wrong concept) and folding
    # it in grossly distorts the check; a genuine large diff (Lucky 2023 PAT +32%) is a
    # real face-truth error, not a missing levy line.
    ("profit_after_tax = profit_before_tax + tax", "P&L", "profit_after_tax",
     ["profit_before_tax", _TAX]),
    ("total_assets = non_current_assets + current_assets", "BS", "total_assets",
     ["non_current_assets", "current_assets"]),
    ("total_assets = total_equity_and_liabilities", "BS", "total_assets",
     ["total_equity_and_liabilities"]),
    # Cash-flow roll-forward: closing cash rolls from opening + the three flow lines.
    # The only cross-statement roll-forward viable on our data — PP&E/debt/RE need
    # movement metrics we don't extract, and BS-cash-vs-CF-cash is a gross-vs-net
    # (running finance) definitional mismatch, so both are intentionally excluded.
    ("cash_at_end = opening + operating + investing + financing", "CF", "cash_at_end_of_period",
     ["cash_at_beginning_of_period", "operating_cash_flow", "investing_cash_flow",
      "financing_cash_flow"]),
]


# Metrics that can never be negative on a face statement. EQUITY is excluded
# (accumulated losses can make it negative) as are P&L profit subtotals (loss years).
_NONNEGATIVE_METRICS = frozenset({
    "revenue", "total_assets", "non_current_assets", "current_assets",
    "total_equity_and_liabilities", "total_liabilities", "non_current_liabilities",
    "current_liabilities", "property_plant_equipment", "stock_in_trade",
    "cash_and_bank_balances", "trade_debts",
})


@dataclass
class IdentityFinding:
    name: str
    statement: str
    year: int
    expected: float     # sum of the term values
    actual: float       # the target value
    ok: bool


def _default_tieout(expected: float, actual: float) -> bool:
    # Tight: rounding only, ~1% — catches real errors (a 22% PAT slip) while tolerating
    # legitimate rounding/restatement across multi-source merges.
    return abs(expected - actual) <= max(1.0, 0.01 * abs(actual))


def _lookup(face: dict, metric, year):
    """Resolve a metric (or tuple of alternatives) for a year from face truth -> value|None."""
    candidates = metric if isinstance(metric, tuple) else (metric,)
    for m in candidates:
        pair = face.get((m, year))
        if pair is not None:
            return pair[0]
    return None


def check_sign_sanity(face: dict, fiscal_years) -> list[IdentityFinding]:
    """Flag emitted face-truth values that are negative on a must-be-positive metric
    (a sign/extraction error or garbage). Only the FACE TRUTH (what we actually ship) is
    checked — raw/analytical tables produce noise (percent rows, already-excluded
    candidates). Emits violations only. Caught e.g. Millat total_liabilities 2025 < 0."""
    years = set(fiscal_years or [])
    out: list[IdentityFinding] = []
    for (metric, year), pair in face.items():
        if metric not in _NONNEGATIVE_METRICS:
            continue
        if years and year not in years:
            continue
        value = pair[0]
        if value is not None and value < 0:
            out.append(IdentityFinding(f"{metric} >= 0", "SANITY", year, 0.0, value, False))
    return out


def check_identities(face: dict, fiscal_years, tieout=None) -> list[IdentityFinding]:
    """Evaluate every accounting identity for every fiscal year, where the inputs exist."""
    tieout = tieout or _default_tieout
    out: list[IdentityFinding] = []
    for year in sorted(set(fiscal_years or [])):
        for name, statement, target_m, term_ms in _IDENTITIES:
            actual = _lookup(face, target_m, year)
            if actual is None:
                continue
            terms = [_lookup(face, m, year) for m in term_ms]
            if any(t is None for t in terms):
                continue        # missing input -> skip, never fail
            expected = sum(terms)
            out.append(IdentityFinding(name, statement, year, expected, actual,
                                       tieout(expected, actual)))
    return out
