"""market_watch: precise parser against the real PSX /market-watch markup.

The header <th> carry data-name and each <td> a data-order — the parser keys on
both so it keeps machine precision (data-order) rather than display text, pulls the
symbol/company from the <a class="tbl__symbol"> anchor, splits the LISTED-IN index
list, and captures the status tag (NC/XD/...). Equities and ETFs are both covered.
"""

import pytest

from app.engines.fie.apis import parsers as P
from app.engines.fie.apis import shortlist
from app.engines.fie.apis.registry import BY_NAME


# trimmed real markup: an up mover, a down mover, a no-change row, an NC-tagged row,
# and an ETF (href /etf/, empty LISTED-IN).
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
      <td data-search="MTL" data-order="MTL">
        <a class="tbl__symbol" href="/company/MTL" data-title="Millat Tractors Limited"><strong>MTL</strong></a>
      </td>
      <td>0801</td>
      <td>ACI,ALLSHR,KMIALLSHR,KSE100,KSE100PR,MII30,PSXDIV20</td>
      <td class="right" data-order="558.37">558.37</td>
      <td class="right" data-order="561">561.00</td>
      <td class="right" data-order="565">565.00</td>
      <td class="right" data-order="561">561.00</td>
      <td class="right" data-order="563.63">563.63</td>
      <td class="right change__text--pos" data-order="5.26"><i class="icon-up-dir"></i>5.26</td>
      <td class="right change__text--pos" data-order="0.942"><i class="icon-up-dir"></i>0.94%</td>
      <td class="right" data-order="146045">146,045</td>
    </tr>
    <tr>
      <td data-search="LUCK" data-order="LUCK">
        <a class="tbl__symbol" href="/company/LUCK" data-title="Lucky Cement Limited"><strong>LUCK</strong></a>
      </td>
      <td>0804</td>
      <td>ACI,ALLSHR,KMI30,KSE100</td>
      <td class="right" data-order="434.4">434.40</td>
      <td class="right" data-order="435.6">435.60</td>
      <td class="right" data-order="437.67">437.67</td>
      <td class="right" data-order="430.2">430.20</td>
      <td class="right" data-order="431.5">431.50</td>
      <td class="right change__text--neg" data-order="-2.9"><i class="icon-down-dir"></i>-2.90</td>
      <td class="right change__text--neg" data-order="-0.668"><i class="icon-down-dir"></i>-0.67%</td>
      <td class="right" data-order="850824">850,824</td>
    </tr>
    <tr>
      <td data-search="TSBL" data-order="TSBL">
        <a class="tbl__symbol" href="/company/TSBL" data-title="Trust Securities &amp; Brokerage Limited"><strong>TSBL</strong></a>
      </td>
      <td>0813</td>
      <td>ALLSHR</td>
      <td class="right" data-order="1.79">1.79</td>
      <td class="right" data-order="1.79">1.79</td>
      <td class="right" data-order="1.92">1.92</td>
      <td class="right" data-order="1.77">1.77</td>
      <td class="right" data-order="1.79">1.79</td>
      <td class="right change__text--noc" data-order="0"><i class=""></i>0.00</td>
      <td class="right change__text--noc" data-order="0"><i class=""></i>0.00%</td>
      <td class="right" data-order="9912968">9,912,968</td>
    </tr>
    <tr>
      <td data-search="HASCOL" data-order="HASCOL">
        <a class="tbl__symbol" href="/company/HASCOL" data-title="Hascol Petroleum Limited"><strong>HASCOL</strong></a>
        <div class="tag tag--skim tag--def">NC</div>
      </td>
      <td>0821</td>
      <td>ALLSHR</td>
      <td class="right" data-order="22.8">22.80</td>
      <td class="right" data-order="22.97">22.97</td>
      <td class="right" data-order="23.65">23.65</td>
      <td class="right" data-order="22.71">22.71</td>
      <td class="right" data-order="22.79">22.79</td>
      <td class="right change__text--neg" data-order="-0.01"><i class="icon-down-dir"></i>-0.01</td>
      <td class="right change__text--neg" data-order="-0.044"><i class="icon-down-dir"></i>-0.04%</td>
      <td class="right" data-order="23842888">23,842,888</td>
    </tr>
    <tr>
      <td data-search="MZNPETF" data-order="MZNPETF">
        <a class="tbl__symbol" href="/etf/MZNPETF" data-title="Meezan Pakistan ETF"><strong>MZNPETF</strong></a>
      </td>
      <td>0837</td>
      <td></td>
      <td class="right" data-order="20.56">20.56</td>
      <td class="right" data-order="20.63">20.63</td>
      <td class="right" data-order="20.78">20.78</td>
      <td class="right" data-order="20.43">20.43</td>
      <td class="right" data-order="20.5">20.50</td>
      <td class="right change__text--neg" data-order="-0.06"><i class="icon-down-dir"></i>-0.06</td>
      <td class="right change__text--neg" data-order="-0.292"><i class="icon-down-dir"></i>-0.29%</td>
      <td class="right" data-order="607500">607,500</td>
    </tr>
  </tbody>
</table>
"""


def test_parse_market_watch_row_fields():
    rows = P.parse_market_watch(_REAL_HTML)
    assert len(rows) == 5
    by_sym = {r["symbol"]: r for r in rows}

    mtl = by_sym["MTL"]
    assert mtl["name"] == "Millat Tractors Limited"
    assert mtl["sector_code"] == "0801"
    assert mtl["is_etf"] is False and mtl["status"] is None
    # data-name 'close' -> CURRENT price; values from data-order (not display text)
    assert mtl["price"] == 563.63 and mtl["ldcp"] == 558.37
    assert mtl["open"] == 561.0 and mtl["high"] == 565.0 and mtl["low"] == 561.0
    assert mtl["change"] == 5.26 and mtl["change_pct"] == 0.942
    assert mtl["volume"] == 146045 and isinstance(mtl["volume"], int)
    assert "KSE100" in mtl["listed_in"] and "ACI" in mtl["listed_in"]


def test_market_watch_signs_and_zero_change():
    by_sym = {r["symbol"]: r for r in P.parse_market_watch(_REAL_HTML)}
    assert by_sym["LUCK"]["change"] == -2.9 and by_sym["LUCK"]["change_pct"] == -0.668
    z = by_sym["TSBL"]
    assert z["change"] == 0.0 and z["change_pct"] == 0.0  # change__text--noc row


def test_market_watch_status_tag_and_etf():
    by_sym = {r["symbol"]: r for r in P.parse_market_watch(_REAL_HTML)}
    assert by_sym["HASCOL"]["status"] == "NC"        # tag div captured
    etf = by_sym["MZNPETF"]
    assert etf["is_etf"] is True                     # href /etf/
    assert etf["listed_in"] == []                    # empty LISTED-IN cell
    assert etf["price"] == 20.5


def test_market_watch_registry_uses_precise_parser():
    a = BY_NAME["market_watch"]
    assert a.method == "GET" and a.response_type == "html"
    assert a.parser_fn is P.parse_market_watch


def test_market_watch_resolves_sector_name():
    by_sym = {r["symbol"]: r for r in P.parse_market_watch(_REAL_HTML)}
    # numeric sector code resolved to its name via PSX_SECTORS
    assert by_sym["MTL"]["sector_code"] == "0801"
    assert by_sym["MTL"]["sector"] == "AUTOMOBILE ASSEMBLER"
    assert by_sym["LUCK"]["sector"] == "CEMENT"
    assert by_sym["MZNPETF"]["sector"] == "EXCHANGE TRADED FUNDS"


def test_market_watch_malformed_input():
    assert P.parse_market_watch("not html") == []
    assert P.parse_market_watch(None) == []


# --- PSX sector reference (code -> name) ---

_SECTOR_SELECT = """
<select class="dropdown__select" name="sector">
  <option value="">Select...</option>
  <option value="0801">AUTOMOBILE ASSEMBLER</option>
  <option value="0804">CEMENT</option>
  <option value="0810">FOOD &amp; PERSONAL CARE PRODUCTS</option>
  <option value="0813">INV. BANKS / INV. COS. / SECURITIES COS.</option>
  <option value="0837">EXCHANGE TRADED FUNDS</option>
</select>
"""


def test_parse_sector_dropdown():
    m = P.parse_sector_dropdown(_SECTOR_SELECT)
    assert "" not in m                                   # empty 'Select...' skipped
    assert m["0801"] == "AUTOMOBILE ASSEMBLER"
    assert m["0810"] == "FOOD & PERSONAL CARE PRODUCTS"   # &amp; decoded
    assert m["0813"] == "INV. BANKS / INV. COS. / SECURITIES COS."


def test_psx_sectors_constant_matches_dropdown():
    # the baked-in table agrees with the dropdown for every parsed code
    for code, name in P.parse_sector_dropdown(_SECTOR_SELECT).items():
        assert P.PSX_SECTORS[code] == name
    assert "0817" not in P.PSX_SECTORS                   # code not issued by PSX
    assert len(P.PSX_SECTORS) == 38


# --- sector_market_watch: same feed, narrowed by sector id ---

def test_resolve_sector_code():
    assert P.resolve_sector_code("0804") == "0804"            # already a code
    assert P.resolve_sector_code("CEMENT") == "0804"          # exact name
    assert P.resolve_sector_code("cement") == "0804"          # keyword/substring
    assert P.resolve_sector_code("automobile") == "0801"
    assert P.resolve_sector_code("not a sector") is None
    assert P.resolve_sector_code("") is None


def test_sector_market_watch_parser_returns_full_feed():
    # same parse as market_watch (the narrowing happens after parsing)
    assert P.parse_sector_market_watch(_REAL_HTML) == P.parse_market_watch(_REAL_HTML)


def test_filter_market_watch_by_sector():
    rows = P.parse_sector_market_watch(_REAL_HTML)
    cement = P.filter_market_watch_by_sector(rows, "cement")  # name keyword
    assert [r["symbol"] for r in cement] == ["LUCK"]
    by_code = P.filter_market_watch_by_sector(rows, "0801")   # sector id
    assert [r["symbol"] for r in by_code] == ["MTL"]
    assert P.filter_market_watch_by_sector(rows, "tobacco") == []   # none in sample
    assert P.filter_market_watch_by_sector(rows, "") == rows        # no filter


def test_sector_market_watch_registry_entry():
    a = BY_NAME["sector_market_watch"]
    mw = BY_NAME["market_watch"]
    # same endpoint + parser as market_watch, but sector-scoped and sector-param
    assert a.endpoint == mw.endpoint and a.method == "GET"
    assert a.parser_fn is P.parse_sector_market_watch
    assert a.scope == "sector" and a.dynamic_params == ("sector",)


@pytest.mark.skip(reason="shortlist heuristic retired with `provides`; APIs are selected by the "
                         "LLM over index + description + returns (external.list_apis)")
def test_shortlist_distinguishes_company_vs_sector_market_watch():
    assert shortlist("current share price", top_k=1)[0][0].name == "market_watch"
    ranked = [a.name for a, _ in shortlist("cement sector share prices", top_k=4)]
    assert ranked[0] == "sector_market_watch"
