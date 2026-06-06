"""analysis_reports adapter + engine wiring: PSX yearly fundamentals (Rs. million)
become per-metric cited external evidence that CORROBORATES the workbook — a clean
×1000 unit difference reconciles silently, a genuine divergence surfaces with the
workbook (audited) winning."""

import os

import pytest

from app.engines.fie import (AnalysisReports, ExternalSources,
                             FinancialIntelligenceEngine, FinancialFactStore)
from app.engines.fie.apis import ApiClient


class _NullT:
    """Transport that never serves — the adapter is primed directly in tests."""
    def get(self, url, params, timeout):
        raise AssertionError("no live fetch in unit test")
    def post(self, url, body, timeout, content_type="json"):
        raise AssertionError


def _adapter(record: dict, year: int = 2024) -> AnalysisReports:
    ar = AnalysisReports(ApiClient(_NullT(), sleep=lambda s: None, now=lambda: "2026-06-06"))
    ar._by_year[year] = {record["symbol"]: {"retrieved_at": "2026-06-06", **record}}
    return ar


# --- adapter unit ----------------------------------------------------------
def test_facts_for_emits_metric_keyed_external_evidence():
    ar = _adapter({"symbol": "MTL", "name": "Millat Tractors Limited",
                   "fiscal_year": 2024, "total_assets": 32873.428, "equity": 11628.983,
                   "sales": 91534.501, "pat": 10207.71})
    res = ar.facts_for(2024, symbol="MTL")
    by_metric = {i.citations[0].locator["metric"]: i for i in res.items}
    assert set(by_metric) == {"total_assets", "total_equity", "revenue", "pat"}
    ta = by_metric["total_assets"]
    assert ta.value == 32873.428 and ta.unit == "Rs. million"
    assert ta.kind == "external" and ta.citations[0].locator["source"] == "PSX.AnalysisReports"


def test_facts_for_filters_to_requested_metrics():
    ar = _adapter({"symbol": "MTL", "fiscal_year": 2024,
                   "total_assets": 32873.428, "sales": 91534.501})
    res = ar.facts_for(2024, symbol="MTL", metrics=["total_assets"])
    assert [i.citations[0].locator["metric"] for i in res.items] == ["total_assets"]


def test_facts_for_unresolved_or_missing_is_empty():
    ar = _adapter({"symbol": "MTL", "fiscal_year": 2024, "total_assets": 1.0})
    assert ar.facts_for(2024, symbol="NOPE").items == []          # not in dataset
    assert ar.facts_for(2024, company="x").items == []            # no symbols resolver


# --- engine wiring (workbook-gated) ----------------------------------------
_WB = os.path.join("storage", "outputs", "millat_filled_fixed.xlsx")
_real = pytest.mark.skipif(not os.path.exists(_WB), reason="workbook absent")


@_real
def test_agreeing_external_corroborates_without_conflict():
    # workbook total_assets FY2024 = 32,873,428 thousand == 32,873.428 million -> agree
    ar = _adapter({"symbol": "MTL", "fiscal_year": 2024, "total_assets": 32873.428})
    eng = FinancialIntelligenceEngine(FinancialFactStore.from_workbook(_WB),
                                      external=ExternalSources(analysis_reports=ar))
    r = eng.answer("What were Millat's total assets in 2024?")
    # corroborating external evidence is present...
    assert any(c.locator.get("source") == "PSX.AnalysisReports" for c in r.citations)
    # ...but a clean ×1000 unit difference is NOT a conflict
    assert not [c for c in r.conflicts if c.type == "internal_vs_external"]


@_real
def test_divergent_external_surfaces_conflict_workbook_wins():
    # 50,000 million == 50,000,000 thousand vs workbook 32,873,428 thousand -> divergent
    ar = _adapter({"symbol": "MTL", "fiscal_year": 2024, "total_assets": 50000.0})
    eng = FinancialIntelligenceEngine(FinancialFactStore.from_workbook(_WB),
                                      external=ExternalSources(analysis_reports=ar))
    r = eng.answer("What were Millat's total assets in 2024?")
    ive = [c for c in r.conflicts if c.type == "internal_vs_external"]
    assert ive, "divergent external value should surface an internal_vs_external conflict"
    c = ive[0]
    assert c.resolved is True and c.topic == "total_assets"
    assert "workbook authoritative" in c.resolution


# --- peer comparison: corroboration is company-scoped ----------------------
_LUCK = os.path.join("storage", "outputs", "lucky_filled_fixed.xlsx")
_real_peer = pytest.mark.skipif(not (os.path.exists(_WB) and os.path.exists(_LUCK)),
                                reason="millat+lucky workbooks absent")


def _two_company_adapter(year: int = 2024) -> AnalysisReports:
    """analysis_reports primed with each company's OWN equity (Rs. million), both
    agreeing with their workbook value at ×1000 scale."""
    ar = AnalysisReports(ApiClient(_NullT(), sleep=lambda s: None, now=lambda: "2026-06-06"))
    ar._by_year[year] = {
        # 11,628,983 thousand == 11,628.983 million
        "MTL": {"retrieved_at": "2026-06-06", "symbol": "MTL", "fiscal_year": year,
                "equity": 11628.983},
        # 147,761,277 thousand == 147,761.277 million  (very different magnitude)
        "LUCK": {"retrieved_at": "2026-06-06", "symbol": "LUCK", "fiscal_year": year,
                 "equity": 147761.277},
    }
    return ar


@_real_peer
def test_peer_corroboration_is_company_scoped_no_cross_match():
    millat = FinancialFactStore.from_workbook(_WB)
    lucky = FinancialFactStore.from_workbook(_LUCK)
    ext = ExternalSources(peers={lucky.company: lucky},
                          analysis_reports=_two_company_adapter())
    eng = FinancialIntelligenceEngine(millat, external=ext)
    r = eng.answer("total equity MTL vs Lucky 2024")
    # each peer's external value AGREES with its OWN workbook; with company-scoped
    # matching there is NO conflict. (Metric-only matching would compare Lucky's
    # 147.8bn external against Millat's 11.6bn workbook -> a false divergence.)
    assert not [c for c in r.conflicts if c.type == "internal_vs_external"]
    # both companies' external actuals were attached, tagged to the right entity
    cos = {c.locator.get("company") for c in r.citations
           if c.locator.get("source") == "PSX.AnalysisReports"}
    assert cos == {millat.company, lucky.company}
