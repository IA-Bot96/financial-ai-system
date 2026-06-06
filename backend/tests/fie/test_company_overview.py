"""company_overview: structured parser + adapter + valuation chain (price/PE/
market cap/shares from the page enable P/E, P/B and EV/EBITDA)."""

import pytest

from app.engines.fie import (
    CompanyOverview,
    ExternalSources,
    FinancialIntelligenceEngine,
    Symbols,
)
from app.engines.fie.apis import ApiClient
from app.engines.fie.apis.parsers import parse_company_overview


_OVERVIEW_HTML = """<html><body>
<div class="pageHeader__title">MTL</div>
<div class="company" id="quote"><div class="company__quote"><div class="quote__details">
  <div class="quote__name">Millat Tractors Limited</div>
  <div class="quote__sector"><span>AUTOMOBILE ASSEMBLER</span></div>
  <div class="quote__price"><div class="quote__close">Rs.563.63</div></div></div>
  <div class="tabs__panels">
    <div class="tabs__panel" data-name="REG">
      <div class="stats"><div class="stats_item"><div class="stats_label">Open</div><div class="stats_value">561.00</div></div>
      <div class="stats_item"><div class="stats_label">Volume</div><div class="stats_value">146,045</div></div>
      <div class="stats_item"><div class="stats_label">P/E Ratio (TTM) **</div><div class="stats_value">15.55</div></div></div>
    </div>
    <div class="tabs__panel" data-name="FUT">
      <div class="stats_item"><div class="stats_label">Open</div><div class="stats_value">569.93</div></div>
    </div>
  </div></div></div>
<div class="companyEquity" id="equity">
  <div class="stats_item"><div class="stats_label">Market Cap (000's)</div><div class="stats_value">112,453,173.21</div></div>
  <div class="stats_item"><div class="stats_label">Shares</div><div class="stats_value">199,515,947</div></div>
  <div class="stats_item"><div class="stats_label">Free Float</div><div class="stats_value">89,782,176</div></div>
  <div class="stats_item"><div class="stats_label">Free Float</div><div class="stats_value">45.00%</div></div></div>
<div class="company" id="financials"><div class="tabs__panels"><div class="tabs__panel" data-name="Annual">
  <table><thead><tr><th></th><th>2025</th><th>2024</th></tr></thead><tbody>
   <tr><td>Sales</td><td>52,108,997</td><td>91,534,501</td></tr>
   <tr><td>Profit after Taxation</td><td>6,372,928</td><td>10,224,875</td></tr>
   <tr><td>EPS</td><td>31.94</td><td>52.26</td></tr></tbody></table></div></div></div>
<div class="company" id="ratios"><table><thead><tr><th></th><th>2025</th><th>2024</th></tr></thead><tbody>
  <tr><td>Gross Profit Margin (%)</td><td>26.61</td><td>23.42</td></tr>
  <tr><td>EPS Growth (%)</td><td><span class="change__text--neg">(38.88)</span></td><td>196.76</td></tr></tbody></table></div>
</body></html>"""


# --- structured parser ---

def test_overview_quote_and_pe():
    o = parse_company_overview(_OVERVIEW_HTML)
    assert o["symbol"] == "MTL"
    assert o["name"] == "Millat Tractors Limited"
    assert o["sector"] == "AUTOMOBILE ASSEMBLER"
    assert o["price"] == 563.63
    assert o["pe_ratio"] == 15.55                 # the REG panel's TTM P/E, not FUT
    assert o["quote_stats"]["Volume"] == "146,045"


def test_overview_equity():
    o = parse_company_overview(_OVERVIEW_HTML)
    assert o["market_cap"] == 112453173.21
    assert o["shares"] == 199515947.0
    assert o["free_float"] == 89782176.0
    assert o["free_float_pct"] == 45.0            # both "Free Float" rows captured


def test_overview_financials_and_ratios():
    o = parse_company_overview(_OVERVIEW_HTML)
    assert o["financials_annual"]["2025"] == {"sales": 52108997.0, "pat": 6372928.0, "eps": 31.94}
    assert o["ratios"]["2025"]["Gross Profit Margin (%)"] == 26.61
    assert o["ratios"]["2025"]["EPS Growth (%)"] == -38.88   # parenthesised negative


def test_overview_malformed_safe():
    assert parse_company_overview("not html") == {}


# --- adapter ---

class _OverviewT:
    def __init__(self):
        self.path = None
    def get(self, url, params, timeout):
        self.path = url
        return _OVERVIEW_HTML
    def post(self, url, body, timeout, content_type="json"):
        raise AssertionError("overview is GET")


def _client():
    return ApiClient(_OverviewT(), sleep=lambda s: None, now=lambda: "2026-06-06")


def test_overview_adapter_emits_scalar_evidence():
    t = _OverviewT()
    client = ApiClient(t, sleep=lambda s: None, now=lambda: "2026-06-06")
    res = CompanyOverview(client).fetch(symbol="MTL")
    assert "/company/MTL" in t.path            # symbol goes in the route
    fields = {i.citations[0].locator["field"]: i.value for i in res.items}
    assert fields["price"] == 563.63 and fields["pe_ratio"] == 15.55
    assert fields["market_cap"] == 112453173.21 and fields["shares"] == 199515947.0


# --- valuation chain: overview unlocks P/E + P/B + EV/EBITDA ---

class _ChainT:
    _SYMS = [{"name": "Millat Tractors Limited", "sectorName": "AUTO", "symbol": "MTL"}]
    def get(self, url, params, timeout):
        return self._SYMS if url.endswith("/symbols") else _OVERVIEW_HTML
    def post(self, url, body, timeout, content_type="json"):
        raise AssertionError


def test_valuation_uses_overview_for_pe_pb_ev(millat_store):
    client = ApiClient(_ChainT(), sleep=lambda s: None, now=lambda: "2026-06-06")
    sym = Symbols(client)
    ext = ExternalSources(symbols=sym, company_overview=CompanyOverview(client, symbols=sym))
    eng = FinancialIntelligenceEngine(millat_store, external=ext)
    r = eng.answer("valuation for MTL 2024")
    fids = {c.formula_id: c.value for c in r.calculations}
    assert fids.get("pe_ratio") == 15.55          # page-reported TTM P/E
    assert "ev_ebitda" in fids                     # market cap (page) + internal EBITDA
    assert "pb_ratio" in fids                       # market cap / total equity
    assert "P/E" in r.direct_answer and "EV/EBITDA" in r.direct_answer