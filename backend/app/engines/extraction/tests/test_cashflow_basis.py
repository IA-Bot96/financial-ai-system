"""Tests for identity-guided source-statement reconciliation of face truth
(cash-flow roll-forward and PAT = PBT + tax)."""
from app.engines.extraction.services.face_truth import _reconcile_identities


def _c(val, basis, ti, ry=2025):
    # candidate tuple shape: (value, report_year, source, tier, basis, table_index)
    return (val, ry, f"src{ti}", 0, basis, ti)


# --- cash-flow roll-forward -------------------------------------------------

def test_cashflow_switches_to_statement_that_reconciles():
    y = 2023
    # Mixed pick: flows from the unconsolidated statement (ti=0) but closing cash from
    # the consolidated one (ti=1) -> 100+50-20-10 = 120 != 999.
    truth = {
        ("cash_at_beginning_of_period", y): (100.0, "x"),
        ("operating_cash_flow", y): (50.0, "x"),
        ("investing_cash_flow", y): (-20.0, "x"),
        ("financing_cash_flow", y): (-10.0, "x"),
        ("cash_at_end_of_period", y): (999.0, "x"),
    }
    cand = {
        ("cash_at_beginning_of_period", y): [_c(100.0, "unconsolidated", 0), _c(500.0, "consolidated", 1)],
        ("operating_cash_flow", y): [_c(50.0, "unconsolidated", 0), _c(80.0, "consolidated", 1)],
        ("investing_cash_flow", y): [_c(-20.0, "unconsolidated", 0), _c(-30.0, "consolidated", 1)],
        ("financing_cash_flow", y): [_c(-10.0, "unconsolidated", 0), _c(-15.0, "consolidated", 1)],
        # unconsolidated end (120) reconciles; consolidated end (999) does not.
        ("cash_at_end_of_period", y): [_c(120.0, "unconsolidated", 0), _c(999.0, "consolidated", 1)],
    }
    _reconcile_identities(truth, cand, "unconsolidated")
    assert truth[("cash_at_end_of_period", y)][0] == 120.0     # adopted the reconciling statement


def test_untouched_when_already_reconciles():
    y = 2024
    truth = {
        ("cash_at_beginning_of_period", y): (100.0, "x"),
        ("operating_cash_flow", y): (50.0, "x"),
        ("investing_cash_flow", y): (-20.0, "x"),
        ("financing_cash_flow", y): (-10.0, "x"),
        ("cash_at_end_of_period", y): (120.0, "x"),            # 120 == 120
    }
    snapshot = dict(truth)
    _reconcile_identities(truth, {}, "unconsolidated")
    assert truth == snapshot


# --- PAT = PBT + tax --------------------------------------------------------

def test_pat_fixes_tax_picked_from_wrong_statement():
    y = 2023
    # PBT/PAT from the unconsolidated P&L (ti=0) but tax mixed in from consolidated (ti=1):
    # 21,343,274 + (-12,047,113) = 9,296,161 != PAT 13,725,814.
    truth = {
        ("profit_before_tax", y): (21_343_274.0, "x"),
        ("tax_expense", y): (-12_047_113.0, "consol"),         # wrong-statement tax
        ("profit_after_tax", y): (13_725_814.0, "x"),
    }
    cand = {
        ("profit_before_tax", y): [_c(21_343_274.0, "unknown", 0), _c(62_327_610.0, "consolidated", 1)],
        # unconsolidated statement carries the correct tax (-7,617,460) plus sign-variant noise.
        ("tax_expense", y): [_c(7_617_460.0, "unknown", 0), _c(-7_617_460.0, "unknown", 0),
                             _c(-12_047_113.0, "consolidated", 1)],
        ("profit_after_tax", y): [_c(13_725_814.0, "unknown", 0), _c(59_537_368.0, "consolidated", 1)],
    }
    _reconcile_identities(truth, cand, "unconsolidated")
    assert truth[("tax_expense", y)][0] == -7_617_460.0        # tax repaired from the same statement
    assert truth[("profit_before_tax", y)][0] == 21_343_274.0  # PBT/PAT unchanged
    assert truth[("profit_after_tax", y)][0] == 13_725_814.0


def test_pat_untouched_when_no_statement_reconciles():
    y = 2022
    truth = {
        ("profit_before_tax", y): (100.0, "x"),
        ("tax_expense", y): (-90.0, "x"),                      # 100-90=10 != PAT 50, and...
        ("profit_after_tax", y): (50.0, "x"),
    }
    cand = {                                                   # ...no single statement reconciles
        ("profit_before_tax", y): [_c(100.0, "unknown", 0)],
        ("tax_expense", y): [_c(-90.0, "unknown", 0)],
        ("profit_after_tax", y): [_c(50.0, "unknown", 0)],
    }
    snapshot = dict(truth)
    _reconcile_identities(truth, cand, "unconsolidated")
    assert truth == snapshot
