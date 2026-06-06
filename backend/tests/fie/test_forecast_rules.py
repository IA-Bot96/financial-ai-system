"""Volatility-adaptive forecast validation: growth-band, trend-break (incl. direction
reversal), and scale-plausibility rules, aggregated worst-wins; plus engine wiring."""

import pytest

from app.engines.fie import forecast_rules as FR
from app.engines.fie import ExternalSources, FinancialIntelligenceEngine, ForecastRepo

_STEADY = [100.0, 110.0, 121.0]          # ~10% YoY, zero volatility


# --- growth band -----------------------------------------------------------
def test_growth_band_pass_warning_fail():
    assert FR.growth_band_rule(_STEADY, 133.1)["outcome"] == "PASS"       # implied ~10%
    assert FR.growth_band_rule(_STEADY, 142.78)["outcome"] == "WARNING"   # ~18%, within 10pp
    assert FR.growth_band_rule(_STEADY, 200.0)["outcome"] == "FAIL"       # ~65%, far outside
    assert FR.growth_band_rule([100.0], 110.0)["outcome"] == "SKIPPED"    # <2 years


# --- trend break -----------------------------------------------------------
def test_trend_break_consistent_vs_deviation():
    assert FR.trend_break_rule(_STEADY, 133.1)["outcome"] == "PASS"
    assert FR.trend_break_rule(_STEADY, 145.2)["outcome"] == "WARNING"    # ~20% dev from 10% median


def test_trend_break_direction_reversal_fails():
    r = FR.trend_break_rule(_STEADY, 100.0)                                # up-trend, forecast falls ~17%
    assert r["outcome"] == "FAIL" and "reversal" in r["reason"]
    assert FR.trend_break_rule([100.0, 110.0], 120.0)["outcome"] == "SKIPPED"  # <3 years


# --- plausibility ----------------------------------------------------------
def test_plausibility_scale_checks():
    assert FR.plausibility_rule(_STEADY, 133.0)["outcome"] == "PASS"
    assert FR.plausibility_rule(_STEADY, 200.0)["outcome"] == "WARNING"   # ~1.65x max
    assert FR.plausibility_rule(_STEADY, 400.0)["outcome"] == "FAIL"      # >2x max
    assert FR.plausibility_rule(_STEADY, 400.0)["detail"]["scale_position"] == "above historical max"


# --- aggregate -------------------------------------------------------------
def test_validate_forecast_worst_wins():
    hist = [(2021, 100.0), (2022, 110.0), (2023, 121.0)]
    assert FR.validate_forecast(hist, 133.1)["outcome"] == "PASS"
    bad = FR.validate_forecast(hist, 400.0)
    assert bad["outcome"] == "FAIL"
    assert {r["id"] for r in bad["rules"]} == {"growth_band", "trend_break", "plausibility"}
    assert FR.validate_forecast([(2021, 100.0)], 110.0)["outcome"] == "SKIPPED"
    assert FR.validate_forecast(hist, None)["outcome"] == "SKIPPED"


# --- engine wiring ---------------------------------------------------------
def test_engine_forecast_validation_flags_implausible(millat_store):
    # an absurd 2026 revenue target should validate as FAIL against the actual history
    fc = ForecastRepo(overrides={("Millat Tractors Limited", "revenue", 2026): 500_000_000})
    eng = FinancialIntelligenceEngine(millat_store, external=ExternalSources(forecast=fc))
    r = eng.answer("is my 2026 revenue forecast for MTL still valid?")
    assert "Validation: FAIL" in r.direct_answer
    assert any("plausibility: FAIL" in f for f in r.key_findings)
    assert all("[C" in f for f in r.key_findings)        # rule findings are cited


def test_engine_forecast_validation_reasonable_target(millat_store):
    fc = ForecastRepo(overrides={("Millat Tractors Limited", "revenue", 2026): 56_000_000})
    eng = FinancialIntelligenceEngine(millat_store, external=ExternalSources(forecast=fc))
    r = eng.answer("is my 2026 revenue forecast for MTL still valid?")
    assert "forecast" in r.direct_answer.lower()
    # a near-trend target should not FAIL
    assert "Validation: FAIL" not in r.direct_answer
