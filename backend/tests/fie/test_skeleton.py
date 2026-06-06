"""Phase 1 — walking skeleton: understanding, planning, calc, end-to-end."""

import pytest

from app.engines.fie import FinancialIntelligenceEngine
from app.engines.fie import understanding
from app.engines.fie import planner
from app.engines.fie.calc import CalcEngine


# --- L1 understanding (1.2) ---

def test_build_frame_current_ratio():
    f = understanding.build_frame("What is the current ratio for MTL in 2024?")
    assert f.intent == "ratio_analysis"
    assert f.formula == "current_ratio"
    assert f.company == "Millat Tractors Limited"
    assert f.year == 2024
    assert f.metrics == ["current_assets", "current_liabilities"]


def test_build_frame_unknown_intent():
    f = understanding.build_frame("tell me a joke")
    assert f.intent == "unknown"
    assert f.formula is None


# --- L2 planner (1.3 / Phase 2 routing) ---

def test_plan_ratio_self_fetches():
    """ratio_analysis: calc fetches its own multi-year inputs, so plan is empty."""
    f = understanding.build_frame("current ratio for lucky 2023")
    p = planner.plan(f)
    assert p.formula == "current_ratio"
    assert p.requirements == []


def test_plan_metric_lookup_emits_requirement():
    f = understanding.build_frame("what was revenue for lucky in 2023")
    p = planner.plan(f)
    assert any(r.metric == "revenue" and r.year == 2023 and r.kind == "internal"
               for r in p.requirements)


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


# --- end-to-end (1.1 + 1.8 golden test) ---

@pytest.fixture(scope="module")
def engine(millat_store):
    return FinancialIntelligenceEngine(millat_store)


def test_end_to_end_current_ratio_has_number_and_citation(engine):
    r = engine.answer("What is the current ratio for MTL in 2024?")
    # a number
    assert r.calculations and r.calculations[0].value is not None
    assert round(r.calculations[0].value, 2) == 1.24
    assert "1.24x" in r.direct_answer
    # AND a citation to a Source Ledger-derived row
    assert r.citations, "answer must carry at least one citation"
    assert any("millat" in (c.display or "").lower() for c in r.citations)
    assert r.calculations[0].citations  # calc inputs are cited
    # 7-section structure populated
    assert r.key_findings and r.evidence_used and r.confidence is not None


def test_end_to_end_unsupported_intent_degrades(engine):
    r = engine.answer("tell me a joke")
    assert not r.calculations
    assert "not supported" in r.direct_answer.lower()
    assert r.confidence is None
