"""Tests for accounting-identity consistency checks on face truth."""
from app.engines.extraction.services.identity_checks import check_identities, check_sign_sanity


def _face(d):
    # {(metric, year): value} -> {(metric, year): (value, source)}
    return {k: (v, None) for k, v in d.items()}


def _byname(findings):
    return {(f.name.split(" =")[0], f.year): f.ok for f in findings}


def test_pl_and_bs_identities_pass_when_consistent():
    face = _face({
        ("revenue", 2025): 100.0, ("cost_of_sales", 2025): -60.0, ("gross_profit", 2025): 40.0,
        ("profit_before_tax", 2025): 30.0, ("tax_expense", 2025): -10.0, ("profit_after_tax", 2025): 20.0,
        ("non_current_assets", 2025): 70.0, ("current_assets", 2025): 30.0,
        ("total_assets", 2025): 100.0, ("total_equity_and_liabilities", 2025): 100.0,
    })
    findings = check_identities(face, [2025])
    assert findings and all(f.ok for f in findings)


def test_gross_profit_mismatch_flagged():
    face = _face({("revenue", 2025): 100.0, ("cost_of_sales", 2025): -60.0,
                  ("gross_profit", 2025): 55.0})   # should be 40
    res = _byname(check_identities(face, [2025]))
    assert res[("gross_profit", 2025)] is False


def test_pat_mismatch_flagged():
    # The Lucky-2023 shape: PAT far from PBT - tax.
    face = _face({("profit_before_tax", 2023): 30.0, ("tax_expense", 2023): -10.0,
                  ("profit_after_tax", 2023): 28.0})   # should be 20
    res = _byname(check_identities(face, [2023]))
    assert res[("profit_after_tax", 2023)] is False


def test_balance_identity_flagged():
    face = _face({("total_assets", 2025): 100.0, ("total_equity_and_liabilities", 2025): 95.0})
    res = _byname(check_identities(face, [2025]))
    assert res[("total_assets", 2025)] is False     # 100 != 95


def test_missing_input_is_skipped_not_failed():
    # No cost_of_sales -> the gross_profit identity is skipped entirely (no false fail).
    face = _face({("revenue", 2025): 100.0, ("gross_profit", 2025): 40.0})
    findings = check_identities(face, [2025])
    assert all("gross_profit" not in f.name for f in findings)


def test_tax_alias_resolves():
    # 'taxation' alias works in place of 'tax_expense'.
    face = _face({("profit_before_tax", 2025): 30.0, ("taxation", 2025): -10.0,
                  ("profit_after_tax", 2025): 20.0})
    res = _byname(check_identities(face, [2025]))
    assert res[("profit_after_tax", 2025)] is True


def test_cash_flow_rollforward_pass_and_fail():
    ok = _face({("cash_at_beginning_of_period", 2025): 100.0, ("operating_cash_flow", 2025): 50.0,
                ("investing_cash_flow", 2025): -30.0, ("financing_cash_flow", 2025): -10.0,
                ("cash_at_end_of_period", 2025): 110.0})
    res = _byname(check_identities(ok, [2025]))
    assert res[("cash_at_end", 2025)] is True
    bad = dict(ok); bad[("cash_at_end_of_period", 2025)] = (70.0, None)   # should be 110
    res2 = _byname(check_identities(bad, [2025]))
    assert res2[("cash_at_end", 2025)] is False


def test_sign_sanity_flags_negative_liabilities_not_equity():
    # total_liabilities < 0 is flagged; equity < 0 (accumulated losses) is NOT.
    face = _face({("total_liabilities", 2025): -2959654.0, ("equity", 2025): -500.0,
                  ("total_assets", 2025): 32988591.0})
    findings = check_sign_sanity(face, [2025])
    flagged = {f.name for f in findings}
    assert "total_liabilities >= 0" in flagged
    assert "equity >= 0" not in flagged          # equity can legitimately be negative
    assert all(not f.ok for f in findings)       # only violations are emitted


def test_sign_sanity_clean_when_all_positive():
    face = _face({("total_assets", 2025): 100.0, ("current_assets", 2025): 40.0,
                  ("revenue", 2025): 200.0})
    assert check_sign_sanity(face, [2025]) == []


def test_rounding_within_tolerance_passes():
    face = _face({("revenue", 2025): 100000.0, ("cost_of_sales", 2025): -60000.0,
                  ("gross_profit", 2025): 40050.0})   # 0.125% off -> within 1%
    res = _byname(check_identities(face, [2025]))
    assert res[("gross_profit", 2025)] is True
