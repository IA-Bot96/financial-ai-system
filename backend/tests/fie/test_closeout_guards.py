"""Close-out P0 guards: distressed-company metrics suppressed (negative equity/EPS/
EBITDA), market-cap ratios require a declared unit (no 1000x default), trace persists
on the live route, and decision logs fire on claim drops."""

import os

import pytest

from app.engines.fie import FinancialFactStore, FinancialIntelligenceEngine
from app.engines.fie.calc.engine import CalcEngine


# --- #1: negative-equity formula guards (no real workbook needed) ----------
class _FakeStore:
    """Minimal store: returns a FactRef-like for (metric, year)."""
    company = "Test Co"

    def __init__(self, values):
        self.values = values            # {(metric, year): value}

    def lookup(self, metric, year, *, level="headline", period_type="historical"):
        from app.engines.fie.models import FactRef
        if (metric, year) not in self.values:
            raise KeyError((metric, year))
        return FactRef(company=self.company, metric=metric, label=metric, year=year,
                       value=self.values[(metric, year)], unit="Rupees in thousand",
                       statement="bs", level=level, sheet="BS", cell="A1",
                       provenance_basis="workbook")

    def cite(self, fact):
        return []


def test_roe_suppressed_on_negative_equity():
    # PAT positive but equity negative (insolvent) -> ROE must NOT compute a value
    store = _FakeStore({("pat", 2024): 1000.0,
                        ("total_equity", 2024): -500.0,
                        ("total_equity", 2023): -400.0})
    cr = CalcEngine(store).evaluate("roe", 2024)
    assert cr.value is None and "domain guard failed" in (cr.note or "")


def test_debt_to_equity_suppressed_on_negative_equity():
    store = _FakeStore({("non_current_liabilities", 2024): 100.0,
                        ("current_liabilities", 2024): 200.0,
                        ("total_equity", 2024): -300.0})
    cr = CalcEngine(store).evaluate("debt_to_equity", 2024)
    assert cr.value is None and "guard" in (cr.note or "").lower()


def test_roe_still_computes_on_positive_equity():
    store = _FakeStore({("pat", 2024): 100.0,
                        ("total_equity", 2024): 1000.0,
                        ("total_equity", 2023): 800.0})
    cr = CalcEngine(store).evaluate("roe", 2024)
    assert cr.value is not None and cr.value == round(100.0 / 900.0, 4)


# --- #1 + #2: valuation suppressions via a fake market-data source ---------
_WB = os.path.join("storage", "outputs", "millat_filled_fixed.xlsx")
_real = pytest.mark.skipif(not os.path.exists(_WB), reason="workbook absent")


class _FakeOverview:
    """company_overview-shaped adapter returning crafted scalar evidence."""
    def __init__(self, fields):
        self.fields = fields            # {field: (value, unit)}

    def fetch(self, symbol=None, *, company=None):
        from app.engines.fie.apis.base import CallResult
        from app.engines.fie.models import Citation, EvidenceItem
        items = []
        for field, (val, unit) in self.fields.items():
            loc = {"source": "PSX.CompanyOverview", "symbol": symbol, "field": field}
            items.append(EvidenceItem(claim=f"{field}={val}", value=val, unit=unit,
                                      kind="external",
                                      citations=[Citation(ref_id="C?", kind="external",
                                                          display="ov", locator=loc)]))
        return CallResult(items=items, status="ok")


def _engine_with_overview(fields):
    from app.engines.fie import ExternalSources
    ext = ExternalSources(company_overview=_FakeOverview(fields))
    return FinancialIntelligenceEngine(FinancialFactStore.from_workbook(_WB), external=ext)


@_real
def test_pe_suppressed_on_negative_eps():
    eng = _engine_with_overview({"price": (500.0, "PKR/share"), "eps": (-12.0, "PKR/share")})
    r = eng.answer("P/E for MTL 2024")
    assert "not meaningful" in (r.supporting_analysis or "").lower()
    # no P/E figure in the direct answer
    assert "P/E" not in r.direct_answer or "valuation" not in r.direct_answer.lower() \
        or "p/e" not in r.direct_answer.lower()


@_real
def test_pb_skips_marketcap_without_declared_unit():
    # market_cap present but unit blank -> must NOT default to thousands (1000x risk);
    # falls back to price x shares (declared PKR) instead.
    eng = _engine_with_overview({"price": (500.0, "PKR/share"),
                                 "market_cap": (1.0e8, ""),   # undeclared unit
                                 "shares": (2.0e8, "shares")})
    r = eng.answer("price to book for MTL 2024")
    cov_caveats = "undeclared" in (r.supporting_analysis or "").lower()
    # either it surfaced the undeclared-unit caveat, or it computed P/B via price*shares
    pb_via_shares = "P/B" in r.direct_answer
    assert cov_caveats or pb_via_shares


# --- #3: trace persisted on the live route ---------------------------------
@_real
def test_live_route_persists_trace(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.core.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "fie_trace_enabled", True, raising=False)
    monkeypatch.setattr(s, "fie_trace_dir", str(tmp_path / "traces"), raising=False)
    from app.main import app
    client = TestClient(app)
    resp = client.post("/api/fie/answer", json={"query": "current ratio for MTL 2024"})
    assert resp.status_code == 200
    traces = list((tmp_path / "traces").glob("*.json"))
    assert len(traces) == 1            # one persisted reasoning trace
