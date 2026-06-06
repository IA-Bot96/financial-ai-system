"""daily_market_summary: precise parser over the real PSX /market-summary page.

The page leads with a timestamp, two .ms-tbl-new strips (Exchange line + market
breadth), and an index board (.indices-single). The parser extracts those — the
market-state figures — and leaves the per-sector constituent quotes to market_watch.
"""

from app.engines.fie.apis import parsers as P
from app.engines.fie.apis.registry import BY_NAME


# trimmed to the summary head: timestamp, exchange strip, breadth strip, a few indices.
_REAL_HTML = """
<div class="col-sm-12 inner-content-table">
  <h4>2026-06-06 16:15:01</h4>
  <div class="table-responsive ms-tbl-new">
    <table><tr>
      <td class="td-fst"><span class="td-hd">Exchange</span></td>
      <td><p><span>Status:</span> Closed</p></td>
      <td><p><span>Volume:</span> 727,166,544</p></td>
      <td><p><span>Value:</span> 26,752,809,157</p></td>
      <td><p><span>Trades:</span> 387,968</p></td>
    </tr></table>
  </div>
  <div class="table-responsive ms-tbl-new">
    <table><tr>
      <td class="td-fst"><span class="td-hd">Symbol </span></td>
      <td><p class="green-clr"><span>Advanced:</span> 248</p></td>
      <td><p class="red-clr"><span>Declined:</span> 205</p></td>
      <td><p class="blue-clr"><span>Unchanged:</span> 110</p></td>
      <td><p><span>Total:</span> 563</p></td>
    </tr></table>
  </div>
  <div class="indices-wrap">
    <h2><span class="td-hd">Indices</span></h2>
    <div class="indices-slide">
      <div class="item indices-single">
        <div class="col-xs-6"><h3>KSE100</h3><h4>170478.94</h4></div>
        <div class="col-xs-6"><h5 class="dwn">-696.56</h5><h6 class="dwn">(-0.41%)</h6></div>
      </div>
      <div class="item indices-single">
        <div class="col-xs-6"><h3>ALLSHR</h3><h4>102885.53</h4></div>
        <div class="col-xs-6"><h5 class="dwn">-297.61</h5><h6 class="dwn">(-0.29%)</h6></div>
      </div>
      <div class="item indices-single">
        <div class="col-xs-6"><h3>KMI30</h3><h4>243917.85</h4></div>
        <div class="col-xs-6"><h5 class="dwn">-1524.88</h5><h6 class="dwn">(-0.63%)</h6></div>
      </div>
    </div>
  </div>
</div>
"""


def test_summary_timestamp_and_exchange():
    d = P.parse_daily_market_summary(_REAL_HTML)
    assert d["timestamp"] == "2026-06-06 16:15:01"
    ex = d["exchange"]
    assert ex["status"] == "Closed"                 # text kept, not coerced to number
    assert ex["volume"] == 727166544 and ex["value"] == 26752809157
    assert ex["trades"] == 387968


def test_summary_breadth():
    b = P.parse_daily_market_summary(_REAL_HTML)["breadth"]
    assert b["advanced"] == 248 and b["declined"] == 205
    assert b["unchanged"] == 110 and b["total"] == 563


def test_summary_indices():
    idx = {i["name"]: i for i in P.parse_daily_market_summary(_REAL_HTML)["indices"]}
    assert idx["KSE100"]["value"] == 170478.94
    assert idx["KSE100"]["change"] == -696.56 and idx["KSE100"]["change_pct"] == -0.41
    assert idx["KMI30"]["change_pct"] == -0.63        # parens/percent stripped


def test_summary_malformed_falls_back():
    # non-summary HTML with a plain table -> generic {tables: [...]} fallback, no crash
    out = P.parse_daily_market_summary("<table><tr><td>x</td></tr></table>")
    assert "tables" in out
    assert P.parse_daily_market_summary("not html") == {"tables": []}


def test_summary_registry_entry():
    a = BY_NAME["daily_market_summary"]
    assert a.method == "GET" and a.response_type == "html"
    assert a.endpoint == "https://www.psx.com.pk/market-summary/"
    assert a.parser_fn is P.parse_daily_market_summary
