"""Fallback valuation paths must be scale-correct.

When the market page has price + share COUNT but no market cap, P/B and EV/EBITDA are
derived from market cap = price × shares (absolute PKR) and the workbook magnitudes
(equity / EBITDA / debt / cash, "Rupees in thousand") normalized to canonical PKR — so
they are NOT 1000x off. Uses the real Millat workbook (equity + derivable EBITDA)."""

import os

import pytest

from app.engines.fie import ExternalSources, FinancialFactStore, FinancialIntelligenceEngine
from app.engines.fie.apis.base import CallResult
from app.engines.fie.calc import CalcEngine
from app.engines.fie.models import Citation, EvidenceItem

_WB = os.path.join("storage", "outputs", "millat_filled_fixed.xlsx")
pytestmark = pytest.mark.skipif(not os.path.exists(_WB), reason="millat workbook not present")

_PRICE = 600.0
_SHARES = 200_000_000.0          # a raw share COUNT (as the overview reports it)


class _StubSymbols:
    def ticker_for(self, name):
        return "MTL"


class _StubOverview:
    """Returns price + share count only — NO market cap — to force the fallback paths."""
    def fetch(self, symbol=None, company=None):
        def ev(field, val, unit):
            c = Citation(ref_id="C?", kind="external", display="overview",
                         locator={"source": "PSX.CompanyOverview", "symbol": "MTL", "field": field})
            return EvidenceItem(claim=f"{field}={val}", value=float(val), unit=unit,
                                kind="external", citations=[c], reliability=0.9)
        return CallResult(items=[ev("price", _PRICE, "PKR/share"),
                                 ev("shares", _SHARES, "shares")], status="ok")


def _engine():
    store = FinancialFactStore.from_workbook(_WB)
    ext = ExternalSources(symbols=_StubSymbols(), company_overview=_StubOverview())
    return FinancialIntelligenceEngine(store, external=ext), store


def test_fallback_pb_and_ev_are_scale_correct():
    eng, store = _engine()
    y = 2024
    equity = store.lookup("total_equity", y).value                 # Rupees in thousand
    ebitda = CalcEngine(store).evaluate("ebitda", y).value          # Rupees in thousand
    mcap_abs = _PRICE * _SHARES                                     # absolute PKR

    r = eng.answer(f"valuation for MTL {y}")
    fids = {c.formula_id: c.value for c in r.calculations}

    # P/B = (price × shares) / (equity × 1000)  -> single/low-double digits, not ~10,000
    expected_pb = round(mcap_abs / (equity * 1000.0), 4)
    assert fids.get("pb_ratio") == expected_pb
    assert 1 < fids["pb_ratio"] < 100               # sane magnitude (was 1000x off before)

    # EV/EBITDA likewise normalized; sane single/low-double-digit multiple
    assert "ev_ebitda" in fids
    assert 1 < fids["ev_ebitda"] < 100
