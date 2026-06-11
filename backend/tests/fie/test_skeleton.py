"""Phase 1 — walking skeleton: understanding, planning, calc, end-to-end."""

import pytest

from app.engines.fie import FinancialIntelligenceEngine
from app.engines.fie.calc import CalcEngine


# --- L4 calc (1.5 / Phase 2 registry-backed) ---

def test_calc_current_ratio(millat_store):
    eng = CalcEngine(millat_store)
    r = eng.evaluate("current_ratio", 2024)
    assert round(r.value, 2) == 1.24
    assert r.confidence in ("High", "Medium")
    assert r.citations


def test_calc_safe_eval_guard():
    from app.engines.fie.calc.registry import FormulaError, safe_eval
    assert safe_eval("(a - b) / b", {"a": 3.0, "b": 2.0}) == 0.5
    with pytest.raises(FormulaError):
        safe_eval("a / b", {"a": 1.0, "b": 0.0})


def test_calc_missing_input(millat_store):
    eng = CalcEngine(millat_store)
    res = eng.evaluate("revenue_growth", 2021)  # needs revenue@2020 (absent)
    assert res.value is None and "missing inputs" in res.note


