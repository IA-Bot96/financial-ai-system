"""API register (shortlisting + calling metadata) + precise announcements parser
against the real PSX #announcementsTable markup."""

import pytest

from app.engines.fie import PSXAnnouncements
from app.engines.fie.apis import ApiClient, shortlist
from app.engines.fie.apis import parsers as P
from app.engines.fie.apis.registry import BY_NAME, REGISTRY


# real PSX announcements markup (trimmed to a few rows incl. REVOKED + PDF)
_REAL_HTML = """
<div class="announcementsResults__header"><div>Showing 1 to 50 of 863 entries</div></div>
<table class="tbl" id="announcementsTable">
 <thead><tr><th>DATE</th><th>TIME</th><th>SYMBOL</th><th>NAME</th><th>TITLE</th><th></th></tr></thead>
 <tbody class="tbl__body">
  <tr><td>May 15, 2026</td><td>10:46 AM</td>
      <td><a class="tbl__symbol" href="/company/MTL"><strong>MTL</strong></a></td>
      <td><a class="tbl__symbol" href="/company/MTL"><strong>Millat Tractors Limited</strong></a></td>
      <td>Notice of Extra Ordinary General Meeting </td>
      <td><i class="icon-file-pdf"></i><a href="/download/document/277264.pdf" target="_blank">PDF</a></td></tr>
  <tr><td>Apr 29, 2026</td><td>9:00 AM</td>
      <td><a href="/company/MTL"><strong>MTL</strong></a></td>
      <td><a href="/company/MTL"><strong>Millat Tractors Limited</strong></a></td>
      <td>Financial results for period ended March 31, 2026 </td>
      <td><a href="/download/document/275611.pdf" target="_blank">PDF</a></td></tr>
  <tr><td>Oct 27, 2025</td><td>1:07 PM</td>
      <td><a href="/company/MTL"><strong>MTL</strong></a></td>
      <td><a href="/company/MTL"><strong>Millat Tractors Limited</strong></a></td>
      <td>Financial Results <div class="tag tag--skim tag--def">REVOKED</div></td>
      <td><a href="javascript:" data-images="263473-1.gif">View</a></td></tr>
 </tbody>
</table>
"""


# --- precise parser against real markup ---

def test_parse_real_announcements_fields():
    rows = P.parse_company_announcements(_REAL_HTML)
    assert len(rows) == 3
    r0 = rows[0]
    assert r0["symbol"] == "MTL" and r0["name"] == "Millat Tractors Limited"
    assert r0["title"] == "Notice of Extra Ordinary General Meeting"
    assert r0["date"] == "May 15, 2026" and r0["time"] == "10:46 AM"
    assert r0["pdf_url"] == "/download/document/277264.pdf" and r0["doc_id"] == "277264"


def test_parse_real_announcements_revoked_status():
    rows = P.parse_company_announcements(_REAL_HTML)
    revoked = rows[2]
    assert revoked["status"] == "REVOKED"
    assert revoked["title"] == "Financial Results"  # tag stripped from title
    assert revoked["pdf_url"] is None  # only a View image, no PDF


def test_announcements_total():
    assert P.announcements_total(_REAL_HTML) == 863


# real SECP notices markup: DATE, TIME, TITLE, PDF (no SYMBOL/NAME column)
_SECP_HTML = """
<div>Showing 1 to 50 of 859 entries</div>
<table class="tbl" id="announcementsTable">
 <thead><tr><th>DATE</th><th>TIME</th><th>TITLE</th><th></th></tr></thead>
 <tbody class="tbl__body">
  <tr><td>Oct 2, 2025</td><td>10:50 AM</td>
      <td>Order-132-Sitara-Peroxide-Ltd.-18.4.25 </td>
      <td><i class="icon-file-pdf"></i><a href="/download/attachment/260800-1.pdf" target="_blank">PDF</a></td></tr>
  <tr><td>Sep 11, 2025</td><td>3:26 PM</td>
      <td>ORDER UNDER SECTION 130 OF THE COMPANIES ACT, 2017 DADABHOY </td>
      <td><a href="/download/attachment/259185-1.pdf" target="_blank">PDF</a></td></tr>
 </tbody>
</table>
"""


def test_parse_secp_4col_layout():
    """SECP table has no SYMBOL/NAME columns — header-driven parsing still works."""
    rows = P.parse_secp_notices(_SECP_HTML)
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["date"] == "Oct 2, 2025" and r0["time"] == "10:50 AM"
    assert r0["title"].startswith("Order-132-Sitara-Peroxide")
    assert r0["symbol"] is None and r0["name"] is None  # absent columns
    assert r0["pdf_url"] == "/download/attachment/260800-1.pdf" and r0["doc_id"] == "260800"
    assert P.announcements_total(_SECP_HTML) == 859


def test_secp_adapter_takes_symbol_and_parses_4col():
    class _T:
        def get(self, u, p, t):
            raise AssertionError
        def post(self, u, body, t, content_type="json"):
            assert content_type == "form" and body.get("type") == "B"
            self_symbol.append(body.get("symbol"))
            return _SECP_HTML
    self_symbol: list = []
    from app.engines.fie import SECPNotices
    client = ApiClient(_T(), sleep=lambda s: None, now=lambda: "2026-06-06")
    res = SECPNotices(client).recent(symbol="MTL")
    assert self_symbol == ["MTL"]            # SECP also accepts a symbol (type B)
    assert res.status == "ok" and len(res.items) == 2


# --- adapter end-to-end over the real HTML (form POST) ---

class _FormPostT:
    def __init__(self):
        self.last_body = None
    def get(self, url, params, timeout):
        raise AssertionError("announcements is POST")
    def post(self, url, body, timeout, content_type="json"):
        assert content_type == "form"  # real endpoint is form-encoded
        self.last_body = body
        return _REAL_HTML


def test_announcements_adapter_form_post_and_parse():
    t = _FormPostT()
    client = ApiClient(t, sleep=lambda s: None, now=lambda: "2026-06-06")
    res = PSXAnnouncements(client).recent(symbol="MTL")
    assert t.last_body.get("symbol") == "MTL"        # ticker passed in form body
    assert res.status == "ok" and len(res.items) == 3
    assert any("Extra Ordinary General Meeting" in (i.claim or "") for i in res.items)
    # provenance carries symbol + doc id
    loc = res.items[0].citations[0].locator
    assert loc["symbol"] == "MTL" and loc["doc_id"] == "277264"


# --- API register ---

@pytest.mark.parametrize("query,expected_top", [
    ("dividend announcements for Millat", "company_announcements"),
    ("current share price", "market_watch"),
    ("sector performance and sentiment", "sector_summary"),
    ("SECP regulatory notice", "secp_notices"),
    ("list of symbols and sectors", "symbols_master"),
])
def test_shortlist_top_pick(query, expected_top):
    ranked = shortlist(query, top_k=3)
    assert ranked and ranked[0][0].name == expected_top


def test_registry_calling_metadata_complete():
    for api in REGISTRY:
        assert api.endpoint.startswith("http")
        assert api.method in ("GET", "POST")
        assert api.parser_fn is not None  # every parser name resolves in PARSERS


def test_announcements_entry_matches_real_contract():
    a = BY_NAME["company_announcements"]
    assert a.method == "POST" and a.content_type == "form" and a.response_type == "html"
    assert a.params["type"] == "C"
    assert {"symbol", "query", "date_from", "date_to"} <= set(a.dynamic_params)


# --- symbols -> announcements chain: symbol acquired from the symbols API ---

class _ChainT:
    """Serves the symbols JSON on GET and the announcements HTML on POST."""
    _SYMBOLS = [
        {"name": "The Thal Industries Corporation Limited",
         "sectorName": "SUGAR & ALLIED INDUSTRIES", "symbol": "TICL"},
        {"name": "Millat Tractors Limited", "sectorName": "AUTO", "symbol": "MTL"},
    ]
    def __init__(self):
        self.posted_symbol = "UNSET"
    def get(self, url, params, timeout):
        return self._SYMBOLS
    def post(self, url, body, timeout, content_type="json"):
        self.posted_symbol = body.get("symbol")
        return _REAL_HTML


def test_announcements_resolves_symbol_via_symbols_api():
    from app.engines.fie import Symbols
    t = _ChainT()
    client = ApiClient(t, sleep=lambda s: None, now=lambda: "2026-06-06")
    ann = PSXAnnouncements(client, symbols=Symbols(client))
    res = ann.recent(company="Thal Industries")          # name -> symbols API -> TICL
    assert t.posted_symbol == "TICL"                      # real symbol passed to announcements
    assert res.status == "ok" and res.items


def test_announcements_unknown_company_falls_back_to_keyword():
    from app.engines.fie import Symbols
    t = _ChainT()
    client = ApiClient(t, sleep=lambda s: None, now=lambda: "2026-06-06")
    ann = PSXAnnouncements(client, symbols=Symbols(client))
    ann.recent(company="Totally Unlisted Co")            # not in registry
    assert not t.posted_symbol                            # empty symbol -> keyword fallback
