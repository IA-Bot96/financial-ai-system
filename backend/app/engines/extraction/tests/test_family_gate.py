"""Statement-family compatibility gate in leaf matching: an income-family line must not
fill a balance-sheet note row (the 'Cost of sales' -35M bleed) and vice versa; cross-family
metrics (depreciation, taxes-paid, …) stay exempt."""
from types import SimpleNamespace

from app.engines.extraction.models.common import StatementType
from app.engines.extraction.pipeline.template_map import _best_candidate, _match_norm


def _cands(label, home_type, cm=None):
    line = SimpleNamespace(label=label, role="leaf", canonical_metric=cm, section=None, values=[])
    return [(_match_norm(label), "", line, home_type)], line


def test_income_line_rejected_for_balance_sheet_row():
    cands, line = _cands("Cost of sales", StatementType.income_statement, "cost_of_sales")
    got, _ = _best_candidate("Cost of sales", "", cands, 80.0, sheet_family="balance")
    assert got is None                                  # bleed blocked
    got2, _ = _best_candidate("Cost of sales", "", cands, 80.0, sheet_family="income")
    assert got2 is line                                 # same line fine on an income sheet


def test_balance_line_rejected_for_income_sheet_row():
    cands, line = _cands("Trade debts", StatementType.current_assets, "trade_debts")
    got, _ = _best_candidate("Trade debts", "", cands, 80.0, sheet_family="income")
    assert got is None


def test_cross_family_metric_is_exempt():
    # depreciation_expense legitimately appears in both income and cash/balance contexts.
    cands, line = _cands("Depreciation", StatementType.income_statement, "depreciation_expense")
    got, _ = _best_candidate("Depreciation", "", cands, 80.0, sheet_family="balance")
    assert got is line


def test_no_gate_when_sheet_family_unknown():
    cands, line = _cands("Cost of sales", StatementType.income_statement, "cost_of_sales")
    got, _ = _best_candidate("Cost of sales", "", cands, 80.0, sheet_family=None)
    assert got is line                                  # never block on a missing signal
