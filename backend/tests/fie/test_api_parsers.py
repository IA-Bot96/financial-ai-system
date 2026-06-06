"""Per-API response parsers (L3b): HTML-table extraction, XLSX, JSON passthrough,
and the announcements adapter end-to-end over HTML (real parser path)."""

import io

import openpyxl
import pytest

from app.engines.fie import PSXAnnouncements, SECPNotices
from app.engines.fie.apis import ApiClient
from app.engines.fie.apis import parsers as P


_ANN_HTML = """<html><body><table>
<tr><th>Date</th><th>Company</th><th>Subject</th></tr>
<tr><td>2026-05-01</td><td>MTL</td><td>Board meeting on dividend</td></tr>
<tr><td>2026-04-15</td><td>MTL</td><td>Plant expansion announced</td></tr>
</table></body></html>"""

_MW_HTML = """<table>
<tr><th>Symbol</th><th>Current</th><th>Change</th><th>Volume</th><th>Sector</th></tr>
<tr><td>MTL</td><td>1,234.50</td><td>+1.2%</td><td>10000</td><td>Auto</td></tr>
<tr><td>LUCK</td><td>900.00</td><td>-0.5%</td><td>5000</td><td>Cement</td></tr>
</table>"""


# --- generic / per-API parsing ---

def test_parse_announcements_maps_columns():
    rows = P.parse_company_announcements(_ANN_HTML)
    assert len(rows) == 2
    assert rows[0]["title"] == "Board meeting on dividend"
    assert rows[0]["date"] == "2026-05-01"


def test_parse_market_watch_numeric_price():
    rows = P.parse_market_watch(_MW_HTML)
    assert {r["symbol"] for r in rows} == {"MTL", "LUCK"}
    assert rows[0]["price"] == 1234.5  # comma-stripped float


def test_parse_xlsx_records():
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Company", "PAT", "Assets"]); ws.append(["MTL", 6372928, 32988591])
    buf = io.BytesIO(); wb.save(buf)
    rows = P.parse_analysis_report_xlsx(buf.getvalue())
    assert rows == [{"Company": "MTL", "PAT": 6372928, "Assets": 32988591}]


def test_symbols_normalized_and_malformed_safe():
    recs = P.parse_symbols_master([{"symbol": "MTL", "name": "Millat", "sectorName": "AUTO"}])
    assert recs[0]["symbol"] == "MTL" and recs[0]["sector"] == "AUTO"
    assert P.parse_market_watch("not html at all") == []
    assert P.parse_company_announcements({"unexpected": "dict"}) == []


def test_parsers_registry_covers_catalog():
    expected = {
        "symbols_master", "company_announcements", "secp_notices", "company_overview",
        "company_payouts", "market_watch", "deliverable_futures_market_watch",
        "cash_settled_futures_market_watch", "daily_market_summary",
        "analysis_reports", "sector_summary",
    }
    assert expected <= set(P.PARSERS)


# --- adapter end-to-end over HTML (live parser path) ---

class _HtmlPostT:
    """Fake transport returning HTML (like the real PSX endpoints)."""
    def get(self, url, params, timeout):
        raise AssertionError("announcements is POST")
    def post(self, url, body, timeout, content_type="json"):
        return _ANN_HTML


def test_announcements_adapter_parses_html_end_to_end():
    client = ApiClient(_HtmlPostT(), sleep=lambda s: None, now=lambda: "2026-06-06")
    res = PSXAnnouncements(client).recent("MTL")  # default real parser, HTML in
    assert res.status == "ok"
    assert any("dividend" in (i.claim or "").lower() for i in res.items)
    assert all(i.kind == "external" and i.citations for i in res.items)


def test_secp_uses_its_own_parser():
    # SECP adapter wires parse_secp_notices (type B); still parses the same table shape
    client = ApiClient(_HtmlPostT(), sleep=lambda s: None, now=lambda: "2026-06-06")
    res = SECPNotices(client).recent("MTL")
    assert res.status == "ok" and res.items
    assert SECPNotices(client).spec.response_type == "html"
