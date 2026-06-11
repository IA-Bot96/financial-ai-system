"""company_payouts: precise parser + POST-form adapter + dividend_analysis intent."""

import pytest

from app.engines.fie import (
    CompanyPayouts,
    ExternalSources,
    FinancialIntelligenceEngine,
    Symbols,
)
from app.engines.fie.apis import ApiClient
from app.engines.fie.apis.parsers import parse_company_payouts


_PAYOUTS_HTML = """<table class="tbl">
 <thead class="tbl__head"><tr><th>Date</th><th>Financial Results</th><th>Details</th><th>Book Closure</th></tr></thead>
 <tbody class="tbl__body">
  <tr><td>February 17, 2026 3:48 PM</td><td>31/12/2025(HYR)</td><td>200%(i) (D) </td><td>04/03/2026  - 06/04/2026 </td></tr>
  <tr><td>September 15, 2025 3:56 PM</td><td>30/06/2025(YR)</td><td>150%(F) (D) </td><td>17/10/2025  - 24/10/2025 </td></tr>
 </tbody>
</table>"""


# --- parser ---

def test_parse_payouts_fields():
    rows = parse_company_payouts(_PAYOUTS_HTML)
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["financial_results"] == "31/12/2025(HYR)"
    assert r0["payout_pct"] == 200.0 and r0["interim"] is True and r0["dividend"] is True
    assert r0["book_closure"].startswith("04/03/2026")
    assert rows[1]["final"] is True and rows[1]["payout_pct"] == 150.0


# --- adapter (POST form, symbol body, symbols-resolvable) ---

class _PayoutT:
    def __init__(self):
        self.body = None
        self.ct = None
    def get(self, url, params, timeout):
        raise AssertionError("payouts is POST")
    def post(self, url, body, timeout, content_type="json"):
        self.body, self.ct = body, content_type
        return _PAYOUTS_HTML


def test_payouts_adapter_form_post():
    t = _PayoutT()
    client = ApiClient(t, sleep=lambda s: None, now=lambda: "2026-06-06")
    res = CompanyPayouts(client).payouts(symbol="MTL")
    assert t.body == {"symbol": "MTL"} and t.ct == "form"
    assert res.status == "ok" and len(res.items) == 2
    assert res.items[0].value == 200.0 and "interim dividend" in res.items[0].claim


def test_payouts_resolves_symbol_from_company():
    syms = [{"name": "Millat Tractors Limited", "sectorName": "AUTO", "symbol": "MTL"}]
    class _T:
        def __init__(self):
            self.body = None
        def get(self, url, params, timeout):
            return syms
        def post(self, url, body, timeout, content_type="json"):
            self.body = body
            return _PAYOUTS_HTML
    t = _T()
    client = ApiClient(t, sleep=lambda s: None, now=lambda: "T")
    sym = Symbols(client)
    CompanyPayouts(client, symbols=sym).payouts(company="Millat Tractors")
    assert t.body == {"symbol": "MTL"}  # name -> symbols API -> MTL


# --- end-to-end ---

def test_dividend_analysis_end_to_end(millat_store):
    class _T:
        def get(self, url, params, timeout):
            return [{"name": "Millat Tractors Limited", "sectorName": "AUTO", "symbol": "MTL"}]
        def post(self, url, body, timeout, content_type="json"):
            return _PAYOUTS_HTML
    client = ApiClient(_T(), sleep=lambda s: None, now=lambda: "2026-06-06")
    sym = Symbols(client)
    ext = ExternalSources(symbols=sym, payouts=CompanyPayouts(client, symbols=sym))
    eng = FinancialIntelligenceEngine(millat_store, external=ext)
    r = eng.answer("dividend history for MTL")
    assert "payout" in r.direct_answer.lower()
    assert r.key_findings and "200" in r.key_findings[0]


def test_dividend_degrades_without_adapter(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)  # no payouts adapter
    r = eng.answer("dividend history for MTL")
    assert "No payout history" in r.direct_answer
