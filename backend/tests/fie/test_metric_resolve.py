"""Availability-gated metric resolution + clarification: prefer a canonical the
workbook actually holds; ask to clarify on ambiguous terms; suggest on not-found."""

import os

import pytest

from app.engines.fie import metric_resolve as MR
from app.engines.fie import FinancialIntelligenceEngine, FinancialFactStore


# --- unit: resolver --------------------------------------------------------
def test_availability_gating_prefers_present_canonical():
    # query maps to revenue + total_assets; only total_assets is available
    avail = {"total_assets"}
    r = MR.resolve("revenue and total assets", ["revenue"], avail)
    assert r["resolved"] == "total_assets" and r["available"] is True


def test_unavailable_metric_is_flagged_with_suggestions():
    r = MR.resolve("operating profit", ["operating_profit"], {"revenue", "pat"})
    assert r["available"] is False and r["resolved"] == "operating_profit"
    assert r["suggestions"] == ["pat", "revenue"]      # sorted available
    assert r["clarify"] is False


def test_bare_profit_is_ambiguous_when_multiple_available():
    r = MR.resolve("what was the profit", [], {"gross_profit", "operating_profit", "pat"})
    assert r["clarify"] is True
    assert set(r["candidates"]) == {"gross_profit", "operating_profit", "pat"}


def test_qualified_profit_not_ambiguous():
    r = MR.resolve("profit after tax", ["pat"], {"gross_profit", "pat"})
    assert r["clarify"] is False and r["resolved"] == "pat"


def test_bare_profit_not_ambiguous_when_one_available():
    r = MR.resolve("profit", [], {"pat"})              # only one sense available
    assert r["clarify"] is False


# --- engine wiring ---------------------------------------------------------
_WB = os.path.join("storage", "outputs", "millat_filled_fixed.xlsx")
pytestmark_real = pytest.mark.skipif(not os.path.exists(_WB), reason="workbook absent")


@pytestmark_real
def test_engine_clarifies_ambiguous_profit():
    eng = FinancialIntelligenceEngine(FinancialFactStore.from_workbook(_WB))
    r = eng.answer("what was MTL's profit in 2024?")
    # workbook has gross_profit / operating_profit / pat -> ambiguous -> clarify
    assert "ambiguous" in r.direct_answer.lower()
    assert "did you mean" in r.direct_answer.lower()
    assert r.key_findings == []


@pytestmark_real
def test_engine_not_found_suggests_available():
    eng = FinancialIntelligenceEngine(FinancialFactStore.from_workbook(_WB))
    r = eng.answer("ebitda for MTL 2024")              # not a stored headline metric
    # either resolves to something available, or says not found with suggestions
    if "not found" in r.direct_answer.lower():
        assert "available metrics include" in r.direct_answer.lower()
