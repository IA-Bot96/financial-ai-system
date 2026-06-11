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

@pytest.mark.skip(reason="shortlist heuristic retired from the planner path; the planner now "
                         "selects named tools only — no opaque API catalog")
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
    # company-scoped: uses symbol (not query)
    assert a.scope == "company" and "symbol" in a.dynamic_params
    assert "query" not in a.dynamic_params


def test_sector_secp_entry():
    # SECP notices are a single sector/keyword-scoped entry (the SECP feed has no symbol column,
    # so there is no symbol-scoped variant). Type B, query-driven.
    s = BY_NAME["sector_secp_notices"]
    assert s.params["type"] == "B"
    assert s.scope == "sector"
    assert s.parser_fn is P.parse_secp_notices
    assert s.dynamic_params == ("query", "date_from", "date_to")


def test_registry_matches_curl_contracts():
    """Lock each entry's calling metadata to the real curl-bash evidence (method,
    encoding, endpoint, key params). Entries with no captured curl are listed as
    unverified so the split is explicit."""
    # (method, content_type-or-None, response_type, endpoint-substr, required static/dynamic keys)
    confirmed = {
        "symbols_master":      ("GET",  None,   "json", "/symbols", set()),
        "company_announcements": ("POST", "form", "html", "/announcements",
                                  {"type", "count", "offset", "page", "symbol"}),
        "company_overview":      ("GET",  None,   "html", "/company/{symbol}", {"symbol"}),
        "company_payouts":       ("POST", "form", "html", "/company/payouts", {"symbol"}),
        "market_watch":          ("GET",  None,   "html", "/market-watch", set()),
        "performers":            ("GET",  None,   "html", "/performers", set()),
        "debt_performers":       ("GET",  None,   "html", "/debt-performers", set()),
        "debt_market_watch":     ("GET",  None,   "html", "/market-watch-debt", set()),
        "deliverable_futures_market_watch": ("GET", None, "html", "/market-watch-futures", set()),
        "cash_settled_futures_market_watch": ("GET", None, "html", "/market-watch-csf", set()),
        "daily_market_summary": ("GET", None, "html", "/market-summary/", set()),
        "sector_summary": ("GET", None, "html", "/sector-summary/sectorwise", set()),
        # api_info + real year-2025.xlsx sample (no curl, but response/parser verified)
        "analysis_reports": ("GET", None, "xlsx", "/download/analysis_report/", {"year"}),
        "stock_screener": ("GET", None, "html", "/screener", {"symbol"}),
    }
    for name, (method, ct, resp, ep_sub, keys) in confirmed.items():
        a = BY_NAME[name]
        assert a.method == method, f"{name} method"
        if ct is not None:
            assert a.content_type == ct, f"{name} content_type"
        assert a.response_type == resp, f"{name} response_type"
        assert ep_sub in a.endpoint, f"{name} endpoint"
        present = set(a.params) | set(a.dynamic_params)
        assert keys <= present, f"{name} missing params {keys - present}"

    # announcements static defaults observed verbatim in the curl body
    ann = BY_NAME["company_announcements"]
    assert ann.params["type"] == "C" and ann.params["count"] == 50
    assert ann.params["offset"] == 0 and ann.params["page"] == "annc"
    assert BY_NAME["sector_secp_notices"].params["type"] == "B"

    # the remaining entry is a shared-endpoint variant (same /announcements endpoint + SECP parser
    # as company_announcements, differing only by scope/param — covered by the scope tests).
    shared_variants = {"sector_secp_notices"}
    # every entry is therefore accounted for — none left spec-only/unverified
    assert set(confirmed) | shared_variants == set(BY_NAME)


def test_scope_classification_consistent():
    """Company/sector separation: every symbol-driven entry is scope 'company';
    every query/sector-driven entry is scope 'sector'."""
    for api in REGISTRY:
        if "symbol" in api.dynamic_params:
            assert api.scope == "company", f"{api.name} takes a symbol but scope={api.scope}"
        if "query" in api.dynamic_params or "sector" in api.dynamic_params:
            assert api.scope == "sector", f"{api.name} is query/sector-driven but scope={api.scope}"


@pytest.mark.skip(reason="`provides` tags removed (they oversold); APIs now expose `returns`, and "
                         "selection is by the LLM over description+returns — no provides to leak")
def test_company_scoped_entries_carry_no_sector_vocab():
    """Obsolete: company-scoped APIs no longer carry `provides` tags at all."""
    pass


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
