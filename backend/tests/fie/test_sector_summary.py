"""sector_summary: precise parser over the real PSX /sector-summary/sectorwise page.

Parses the sector-level table (.sectorSummary__sectors): code, name, advance/decline/
unchange, turnover, market cap (B). Sector name comes from the cell's data-order (clean
'&'); the per-sector constituent quote tables on the page are market_watch's shape and
are not re-parsed here.
"""

from app.engines.fie.apis import parsers as P
from app.engines.fie.apis import shortlist
from app.engines.fie.apis.registry import BY_NAME


# trimmed to the sector-level table head + a few rows (incl. an '&' name and a
# comma'd market cap). The page's constituent tables are omitted.
_REAL_HTML = """
<div class="sectorSummary__sectors">
  <div class="tbl__wrapper">
    <table class="tbl" data-page-length="100">
      <thead class="tbl__head"><tr>
        <th style="width:110px;">Sector Code</th>
        <th>Sector Name</th>
        <th class="right">Advance</th>
        <th class="right">Decline</th>
        <th class="right">Unchange</th>
        <th class="right">Turnover</th>
        <th class="right">Market Cap. (B)</th>
      </tr></thead>
      <tbody class="tbl__body">
        <tr>
          <td>0801</td>
          <td data-order="AUTOMOBILE ASSEMBLER"><a href="javascript:" data-code="0801"><strong>AUTOMOBILE ASSEMBLER</strong></a></td>
          <td class="right">6</td><td class="right">4</td><td class="right">0</td>
          <td class="right" data-order="4272380">4,272,380</td>
          <td class="right">763.78</td>
        </tr>
        <tr>
          <td>0802</td>
          <td data-order="AUTOMOBILE PARTS &amp; ACCESSORIES"><a href="javascript:" data-code="0802"><strong>AUTOMOBILE PARTS &amp;ACCESSORIES</strong></a></td>
          <td class="right">7</td><td class="right">4</td><td class="right">0</td>
          <td class="right" data-order="12092888">12,092,888</td>
          <td class="right">101.55</td>
        </tr>
        <tr>
          <td>0804</td>
          <td data-order="CEMENT"><a href="javascript:" data-code="0804"><strong>CEMENT</strong></a></td>
          <td class="right">8</td><td class="right">10</td><td class="right">0</td>
          <td class="right" data-order="55884114">55,884,114</td>
          <td class="right">1,569.78</td>
        </tr>
      </tbody>
    </table>
  </div>
</div>
<div class="sectorSummary__companies">
  <div class="sectorSummary__companies__table" data-code="0801"><h3>AUTOMOBILE ASSEMBLER</h3>
    <table class="tbl"><thead><tr><th data-name="symbol">SYMBOL</th></tr></thead>
    <tbody><tr class="traded"><td data-search="MTL"><a class="tbl__symbol" href="/company/MTL"><strong>MTL</strong></a></td></tr></tbody></table>
  </div>
</div>
"""


def test_sector_summary_rows():
    rows = P.parse_sector_summary(_REAL_HTML)
    assert len(rows) == 3                       # 3 sectors; constituent table NOT counted
    by_code = {r["sector_code"]: r for r in rows}
    auto = by_code["0801"]
    assert auto["sector"] == "AUTOMOBILE ASSEMBLER"
    assert auto["advance"] == 6 and auto["decline"] == 4 and auto["unchange"] == 0
    assert auto["turnover"] == 4272380 and isinstance(auto["turnover"], int)
    assert auto["market_cap_b"] == 763.78


def test_sector_summary_name_and_marketcap_parsing():
    by_code = {r["sector_code"]: r for r in P.parse_sector_summary(_REAL_HTML)}
    # data-order gives the clean name with a real '& ' (strong text drops the space)
    assert by_code["0802"]["sector"] == "AUTOMOBILE PARTS & ACCESSORIES"
    assert by_code["0804"]["market_cap_b"] == 1569.78    # comma thousands parsed


def test_sector_summary_malformed_safe():
    assert P.parse_sector_summary("not html") == []
    out = P.parse_sector_summary("<table><tr><td>x</td></tr></table>")   # generic fallback
    assert isinstance(out, list)


def test_sector_summary_registry_entry():
    a = BY_NAME["sector_summary"]
    assert a.method == "GET" and a.response_type == "html"
    assert a.endpoint.endswith("/sector-summary/sectorwise")
    assert a.parser_fn is P.parse_sector_summary
    # exposes its real per-sector output fields (provides tags removed; shortlist now ranks on
    # description+returns, and the LLM selects — so we no longer assert a heuristic pick)
    assert "sector" in a.returns and "turnover" in a.returns
