"""stock_screener: precise parser over the real PSX /screener table.

The screener is one GET returning the whole market; each row carries valuation/liquidity
metrics the quote feeds don't (PE TTM, dividend yield %, 1-year return %, free float, 30d
avg volume) plus market cap and price. Header <th> carry data-name; each <td> a data-order
machine value. Company scope filters by symbol; sector scope reuses the market-watch
sector filter (rows carry sector_code/sector).
"""

from app.engines.fie.apis import parsers as P
from app.engines.fie.apis import shortlist
from app.engines.fie.apis.registry import BY_NAME


# trimmed to the screener table head + a few real rows (numeric ticker, an XD tag, a
# cement name for sector resolution). data-order values are copied verbatim.
_REAL_HTML = """
<table class="tbl" id="screenerTable" data-page-length="25">
  <thead class="tbl__head"><tr>
    <th data-name="symbol">SYMBOL</th>
    <th data-name="sector">SECTOR</th>
    <th data-name="listed">LISTED IN</th>
    <th class="right" data-name="marketCap">MARKET CAP.</th>
    <th class="right" data-name="close">PRICE</th>
    <th class="right" data-name="percentChange">CHANGE (%)</th>
    <th class="right" data-name="changeYear">1-YEAR CH. (%) *</th>
    <th class="right" data-name="peRatio">PE RATIO (TTM)</th>
    <th class="right" data-name="dividendYield">DIVIDEND YIELD (%)</th>
    <th class="right" data-name="freeFloat">FREE FLOAT</th>
    <th class="right" data-name="volume30Avg">30D VOLUME AVG.</th>
  </tr></thead>
  <tbody class="tbl__body">
    <tr>
      <td data-order="786"><a class="tbl__symbol" href="/company/786" data-title="786 Investments Limited"><strong>786</strong></a></td>
      <td>0813</td><td>ALLSHR</td>
      <td class="right" data-order="552617387.68">552.6M</td>
      <td class="right" data-order="27.68">27.68</td>
      <td class="right change__text--pos" data-order="3.6700000000000004">3.67%</td>
      <td class="right change__text--pos" data-order="202.1834061135371">202.18%</td>
      <td class="right" data-order="11.02788844621514">11.03</td>
      <td class="right" data-order="0">0.00</td>
      <td class="right" data-order="8984025">9.0M</td>
      <td class="right" data-order="362186.1">362,186</td>
    </tr>
    <tr>
      <td data-order="AABS"><a class="tbl__symbol" href="/company/AABS" data-title="Al-Abbas Sugar Mills Limited"><strong>AABS</strong></a><div class="tag tag--skim tag--xd">XD</div></td>
      <td>0826</td><td>ALLSHR</td>
      <td class="right" data-order="15710624401">15.7B</td>
      <td class="right" data-order="904.87">904.87</td>
      <td class="right change__text--neg" data-order="-0.008">-0.01%</td>
      <td class="right change__text--pos" data-order="3.17787913340935">3.18%</td>
      <td class="right" data-order="15.098781912230935">15.10</td>
      <td class="right" data-order="5.42998305367926">5.43</td>
      <td class="right" data-order="1736230">1.7M</td>
      <td class="right" data-order="331.6">332</td>
    </tr>
    <tr>
      <td data-order="LUCK"><a class="tbl__symbol" href="/company/LUCK" data-title="Lucky Cement Limited"><strong>LUCK</strong></a></td>
      <td>0804</td><td>ACI,ALLSHR,KMI30</td>
      <td class="right" data-order="632147500000">632.1B</td>
      <td class="right" data-order="431.5">431.50</td>
      <td class="right change__text--neg" data-order="-0.668">-0.67%</td>
      <td class="right change__text--pos" data-order="26.14745951002748">26.15%</td>
      <td class="right" data-order="38.944043321299645">38.94</td>
      <td class="right" data-order="1.1208877430925293">1.12</td>
      <td class="right" data-order="439500000">439.5M</td>
      <td class="right" data-order="1931266.13">1,931,266</td>
    </tr>
    <tr>
      <td data-order="MTL"><a class="tbl__symbol" href="/company/MTL" data-title="Millat Tractors Limited"><strong>MTL</strong></a></td>
      <td>0801</td><td>ACI,ALLSHR,KMIALLSHR,KSE100,KSE100PR,MII30,PSXDIV20</td>
      <td class="right" data-order="112453173207.61">112.5B</td>
      <td class="right" data-order="563.63">563.63</td>
      <td class="right change__text--pos" data-order="0.942">0.94%</td>
      <td class="right change__text--pos" data-order="0.7255571242203737">0.73%</td>
      <td class="right" data-order="15.548413793103448">15.55</td>
      <td class="right" data-order="7.02543206407194">7.03</td>
      <td class="right" data-order="89782176">89.8M</td>
      <td class="right" data-order="255791.83">255,792</td>
    </tr>
  </tbody>
</table>
"""


def test_screener_no_column_drift():
    rows = P.parse_stock_screener(_REAL_HTML)
    assert len(rows) == 4
    by = {r["symbol"]: r for r in rows}
    mtl = by["MTL"]
    assert mtl["name"] == "Millat Tractors Limited"
    assert mtl["sector_code"] == "0801" and mtl["sector"] == "AUTOMOBILE ASSEMBLER"
    assert mtl["market_cap"] == 112453173207.61
    assert mtl["price"] == 563.63
    # valuation/liquidity fields kept distinct (the easy place to drift)
    assert mtl["pe_ratio_ttm"] == 15.548413793103448
    assert mtl["dividend_yield_pct"] == 7.02543206407194
    assert mtl["change_1y_pct"] == 0.7255571242203737
    assert mtl["free_float"] == 89782176
    assert mtl["volume_30d_avg"] == 255791.83
    assert mtl["listed_in"][:2] == ["ACI", "ALLSHR"]


def test_screener_numeric_ticker_and_status():
    by = {r["symbol"]: r for r in P.parse_stock_screener(_REAL_HTML)}
    assert by["786"]["symbol"] == "786"          # numeric ticker survives as a company
    assert by["786"]["dividend_yield_pct"] == 0.0
    assert by["AABS"]["status"] == "XD"          # status tag captured, off the symbol
    assert by["AABS"]["sector"] == "SUGAR & ALLIED INDUSTRIES"


def test_screener_filter_by_symbol():
    rows = P.parse_stock_screener(_REAL_HTML)
    only = P.filter_screener_by_symbol(rows, "mtl")   # case-insensitive
    assert [r["symbol"] for r in only] == ["MTL"]
    assert P.filter_screener_by_symbol(rows, "") == rows  # empty -> all


def test_screener_filter_by_sector_reuses_market_watch_filter():
    rows = P.parse_sector_stock_screener(_REAL_HTML)
    cement = P.filter_market_watch_by_sector(rows, "cement")
    assert [r["symbol"] for r in cement] == ["LUCK"]
    assert cement[0]["pe_ratio_ttm"] == 38.944043321299645


def test_screener_malformed_safe():
    assert P.parse_stock_screener("not html at all") == []
    assert P.parse_stock_screener({"unexpected": "dict"}) == []
    # a non-screener table (no screener data-names) yields []
    assert P.parse_stock_screener("<table><thead><tr><th>X</th></tr></thead></table>") == []


def test_screener_registry_entries():
    comp = BY_NAME["stock_screener"]
    sect = BY_NAME["sector_stock_screener"]
    assert comp.method == "GET" and comp.response_type == "html"
    assert comp.endpoint.endswith("/screener")
    assert comp.scope == "company" and comp.dynamic_params == ("symbol",)
    assert comp.parser_fn is P.parse_stock_screener
    # same feed, sector-scoped variant
    assert sect.endpoint == comp.endpoint
    assert sect.scope == "sector" and sect.dynamic_params == ("sector",)
    assert sect.parser_fn is P.parse_sector_stock_screener
    # exposes the valuation fields it actually returns (provides tags removed)
    assert "pe_ratio_ttm" in comp.returns and "dividend_yield_pct" in comp.returns


def test_screener_shortlists_on_valuation_query():
    ranked = [a.name for a, _ in shortlist("PE ratio and dividend yield", top_k=3)]
    assert "stock_screener" in ranked
