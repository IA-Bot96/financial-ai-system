"""deliverable_futures_market_watch: precise parser + company-scoped (by base symbol).

The futures feed shares the market-watch table (data-name headers, data-order cells),
with two differences: symbols are BASE-CONTRACT (MTL-JUN) and the data-name='sector'
column holds the futures CONTRACT month (JUN/JUNB/JULB), NOT an industry sector. So the
parser emits `contract` + `base_symbol` (no PSX_SECTORS resolution), and the company
entry narrows the feed to one company by base symbol.
"""

from app.engines.fie.apis import parsers as P
from app.engines.fie.apis import shortlist
from app.engines.fie.apis.registry import BY_NAME


# trimmed real markup: two MTL contracts, an OGDC JULB row, a no-change row, and the
# NATF zero-open row — symbols are BASE-CONTRACT, SECTOR column = contract month.
_REAL_HTML = """
<table class="tbl" data-page-length="25">
  <thead class="tbl__head">
    <tr>
      <th data-name="symbol">SYMBOL</th>
      <th data-name="sector">SECTOR</th>
      <th data-name="listed">LISTED IN</th>
      <th class="right" data-name="ldcp">LDCP</th>
      <th class="right" data-name="open">OPEN</th>
      <th class="right" data-name="high">HIGH</th>
      <th class="right" data-name="low">LOW</th>
      <th class="right" data-name="close">CURRENT</th>
      <th class="right" data-name="change">CHANGE</th>
      <th class="right" data-name="percentChange">CHANGE (%)</th>
      <th class="right" data-name="volume">VOLUME</th>
    </tr>
  </thead>
  <tbody class="tbl__body">
    <tr>
      <td data-search="MTL-JUN" data-order="MTL-JUN">
        <a class="tbl__symbol" href="/company/MTL-JUN"><strong>MTL-JUN</strong></a>
      </td>
      <td>JUN</td>
      <td></td>
      <td class="right" data-order="563.33">563.33</td>
      <td class="right" data-order="569.93">569.93</td>
      <td class="right" data-order="570">570.00</td>
      <td class="right" data-order="567">567.00</td>
      <td class="right" data-order="567">567.00</td>
      <td class="right change__text--pos" data-order="3.67"><i class="icon-up-dir"></i>3.67</td>
      <td class="right change__text--pos" data-order="0.65"><i class="icon-up-dir"></i>0.65%</td>
      <td class="right" data-order="4000">4,000</td>
    </tr>
    <tr>
      <td data-search="OGDC-JULB" data-order="OGDC-JULB">
        <a class="tbl__symbol" href="/company/OGDC-JULB"><strong>OGDC-JULB</strong></a>
      </td>
      <td>JULB</td>
      <td></td>
      <td class="right" data-order="328.89">328.89</td>
      <td class="right" data-order="325.5">325.50</td>
      <td class="right" data-order="325.5">325.50</td>
      <td class="right" data-order="325.5">325.50</td>
      <td class="right" data-order="325.5">325.50</td>
      <td class="right change__text--neg" data-order="-3.39"><i class="icon-down-dir"></i>-3.39</td>
      <td class="right change__text--neg" data-order="-1.03"><i class="icon-down-dir"></i>-1.03%</td>
      <td class="right" data-order="5000">5,000</td>
    </tr>
    <tr>
      <td data-search="CNERGY-JUN" data-order="CNERGY-JUN">
        <a class="tbl__symbol" href="/company/CNERGY-JUN"><strong>CNERGY-JUN</strong></a>
      </td>
      <td>JUN</td>
      <td></td>
      <td class="right" data-order="8.23">8.23</td>
      <td class="right" data-order="8.2">8.20</td>
      <td class="right" data-order="8.32">8.32</td>
      <td class="right" data-order="8.16">8.16</td>
      <td class="right" data-order="8.23">8.23</td>
      <td class="right change__text--noc" data-order="0"><i class=""></i>0.00</td>
      <td class="right change__text--noc" data-order="0"><i class=""></i>0.00%</td>
      <td class="right" data-order="2399000">2,399,000</td>
    </tr>
    <tr>
      <td data-search="MTL-JUL" data-order="MTL-JUL">
        <a class="tbl__symbol" href="/company/MTL-JUL"><strong>MTL-JUL</strong></a>
      </td>
      <td>JUL</td>
      <td></td>
      <td class="right" data-order="565">565.00</td>
      <td class="right" data-order="566">566.00</td>
      <td class="right" data-order="568">568.00</td>
      <td class="right" data-order="564">564.00</td>
      <td class="right" data-order="566.5">566.50</td>
      <td class="right change__text--pos" data-order="1.5"><i class="icon-up-dir"></i>1.50</td>
      <td class="right change__text--pos" data-order="0.27"><i class="icon-up-dir"></i>0.27%</td>
      <td class="right" data-order="1500">1,500</td>
    </tr>
  </tbody>
</table>
"""


def test_parse_futures_fields_and_contract():
    rows = P.parse_deliverable_futures_market_watch(_REAL_HTML)
    assert len(rows) == 4
    by_sym = {r["symbol"]: r for r in rows}
    mtl = by_sym["MTL-JUN"]
    assert mtl["base_symbol"] == "MTL" and mtl["contract"] == "JUN"
    assert "sector" not in mtl and "sector_code" not in mtl   # futures has no industry sector
    assert mtl["price"] == 567.0 and mtl["ldcp"] == 563.33
    assert mtl["change"] == 3.67 and mtl["change_pct"] == 0.65
    assert mtl["volume"] == 4000 and isinstance(mtl["volume"], int)
    assert by_sym["OGDC-JULB"]["base_symbol"] == "OGDC" and by_sym["OGDC-JULB"]["contract"] == "JULB"
    assert by_sym["CNERGY-JUN"]["change"] == 0.0   # change__text--noc row


def test_filter_futures_by_symbol_gets_all_contracts():
    rows = P.parse_deliverable_futures_market_watch(_REAL_HTML)
    mtl = P.filter_futures_by_symbol(rows, "MTL")          # base symbol -> all its contracts
    assert sorted(r["symbol"] for r in mtl) == ["MTL-JUL", "MTL-JUN"]
    assert P.filter_futures_by_symbol(rows, "ogdc")[0]["symbol"] == "OGDC-JULB"  # case-insensitive
    assert P.filter_futures_by_symbol(rows, "MTL-JUN")[0]["symbol"] == "MTL-JUN"  # full symbol ok
    assert P.filter_futures_by_symbol(rows, "NONE") == []
    assert P.filter_futures_by_symbol(rows, "") == rows     # no filter


def test_filter_futures_anchors_on_base_segment():
    """Match is base-anchored: 'ASL' hits 'ASL-JUN' but not 'ASLPS-JUN' (no raw substring)."""
    rows = [{"symbol": "ASL-JUN", "base_symbol": "ASL"},
            {"symbol": "ASLPS-JUN", "base_symbol": "ASLPS"}]
    got = [r["symbol"] for r in P.filter_futures_by_symbol(rows, "ASL")]
    assert got == ["ASL-JUN"]


def test_company_futures_registry_entry():
    base = BY_NAME["deliverable_futures_market_watch"]
    comp = BY_NAME["company_deliverable_futures_market_watch"]
    assert base.endpoint == comp.endpoint and comp.method == "GET"
    assert comp.parser_fn is P.parse_deliverable_futures_market_watch   # same feed/parser
    assert comp.scope == "company" and comp.dynamic_params == ("symbol",)
    # company-scoped: no industry-sector vocab in provides
    assert "sector" not in comp.provides and "industry" not in comp.provides


def test_company_futures_shortlists_for_symbol_query():
    ranked = [a.name for a, _ in shortlist("MTL deliverable futures price", top_k=3)]
    assert "company_deliverable_futures_market_watch" in ranked


def test_futures_malformed_input():
    assert P.parse_deliverable_futures_market_watch("not html") == []
    assert P.parse_cash_settled_futures_market_watch(None) == []


# --- cash-settled futures: same table shape as deliverable; feed can be empty ---

_CSF_EMPTY = """
<table class="tbl" data-page-length="25">
  <thead class="tbl__head"><tr>
    <th data-name="symbol">SYMBOL</th><th data-name="sector">SECTOR</th>
    <th data-name="listed">LISTED IN</th><th class="right" data-name="ldcp">LDCP</th>
    <th class="right" data-name="open">OPEN</th><th class="right" data-name="high">HIGH</th>
    <th class="right" data-name="low">LOW</th><th class="right" data-name="close">CURRENT</th>
    <th class="right" data-name="change">CHANGE</th>
    <th class="right" data-name="percentChange">CHANGE (%)</th>
    <th class="right" data-name="volume">VOLUME</th>
  </tr></thead>
  <tbody class="tbl__body"></tbody>
</table>
"""


def test_cash_settled_empty_feed_returns_empty():
    # real CSF response had an empty tbody — parse cleanly to []
    assert P.parse_cash_settled_futures_market_watch(_CSF_EMPTY) == []


def test_cash_settled_parses_like_deliverable_when_populated():
    # identical table shape, so a populated CSF row yields the futures contract fields
    rows = P.parse_cash_settled_futures_market_watch(_REAL_HTML)
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["MTL-JUN"]["base_symbol"] == "MTL" and by_sym["MTL-JUN"]["contract"] == "JUN"
    assert by_sym["MTL-JUN"]["price"] == 567.0


def test_cash_settled_registry_entry():
    a = BY_NAME["cash_settled_futures_market_watch"]
    assert a.method == "GET" and a.response_type == "html"
    assert a.endpoint.endswith("/market-watch-csf")
    assert a.parser_fn is P.parse_cash_settled_futures_market_watch
