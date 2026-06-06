"""Per-API response parsers (L3b).

Each PSX endpoint has its own parser (per the API catalog). Most endpoints return
HTML tables, so the parsers are built on a generic, markup-agnostic table extractor
(`pandas.read_html`) plus header-keyword column mapping — resilient to exact class
names/ids, which is important since we parse against live HTML we can't pin to a
fixed structure. XLSX (analysis_reports) is parsed with pandas; JSON (symbols) is a
passthrough. All parsers are defensive: malformed input yields [] / {}.

Real adapters inject the matching parser via their ``parser`` hook; PARSERS maps
``api_name -> parser`` for lookup.
"""

from __future__ import annotations

import io
import re
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup


# ---------- generic helpers ----------

def _tables(raw: Any) -> list[pd.DataFrame]:
    """Extract all HTML tables as DataFrames. Accepts an HTML string."""
    if not isinstance(raw, str) or "<" not in raw:
        return []
    try:
        return pd.read_html(io.StringIO(raw))
    except (ValueError, ImportError):
        return []


def _norm_header(name: Any) -> str:
    return str(name).strip().lower()


def _find_col(df: pd.DataFrame, *keywords: str) -> Any | None:
    """First column whose header contains any keyword."""
    for col in df.columns:
        h = _norm_header(col)
        if any(k in h for k in keywords):
            return col
    return None


def _records(df: pd.DataFrame) -> list[dict]:
    return df.where(pd.notna(df), None).to_dict("records")


# ---------- PSX sector reference ----------

# Sector code -> name, from the PSX market-watch sector dropdown (the numeric codes
# that appear in the market-watch SECTOR column / company sector field). 0817 is not
# issued by PSX. Used to resolve `sector_code` to a human-readable `sector` name.
PSX_SECTORS: dict[str, str] = {
    "0801": "AUTOMOBILE ASSEMBLER",
    "0802": "AUTOMOBILE PARTS & ACCESSORIES",
    "0803": "CABLE & ELECTRICAL GOODS",
    "0804": "CEMENT",
    "0805": "CHEMICAL",
    "0806": "CLOSE - END MUTUAL FUND",
    "0807": "COMMERCIAL BANKS",
    "0808": "ENGINEERING",
    "0809": "FERTILIZER",
    "0810": "FOOD & PERSONAL CARE PRODUCTS",
    "0811": "GLASS & CERAMICS",
    "0812": "INSURANCE",
    "0813": "INV. BANKS / INV. COS. / SECURITIES COS.",
    "0814": "JUTE",
    "0815": "LEASING COMPANIES",
    "0816": "LEATHER & TANNERIES",
    "0818": "MISCELLANEOUS",
    "0819": "MODARABAS",
    "0820": "OIL & GAS EXPLORATION COMPANIES",
    "0821": "OIL & GAS MARKETING COMPANIES",
    "0822": "PAPER, BOARD & PACKAGING",
    "0823": "PHARMACEUTICALS",
    "0824": "POWER GENERATION & DISTRIBUTION",
    "0825": "REFINERY",
    "0826": "SUGAR & ALLIED INDUSTRIES",
    "0827": "SYNTHETIC & RAYON",
    "0828": "TECHNOLOGY & COMMUNICATION",
    "0829": "TEXTILE COMPOSITE",
    "0830": "TEXTILE SPINNING",
    "0831": "TEXTILE WEAVING",
    "0832": "TOBACCO",
    "0833": "TRANSPORT",
    "0834": "VANASPATI & ALLIED INDUSTRIES",
    "0835": "WOOLLEN",
    "0836": "REAL ESTATE INVESTMENT TRUST",
    "0837": "EXCHANGE TRADED FUNDS",
    "0838": "PROPERTY",
    "0839": "APPAREL",
}


def parse_sector_dropdown(raw: Any) -> dict[str, str]:
    """Parse a PSX sector <select> into {code: name}, decoding HTML entities and
    skipping the empty 'Select...' option. Useful to refresh PSX_SECTORS from a
    live page."""
    if not isinstance(raw, str) or "<" not in raw:
        return {}
    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:
        soup = BeautifulSoup(raw, "html.parser")
    sel = soup.find("select", attrs={"name": "sector"}) or soup.find("select")
    if sel is None:
        return {}
    out: dict[str, str] = {}
    for opt in sel.find_all("option"):
        code = (opt.get("value") or "").strip()
        name = opt.get_text(strip=True)
        if code and name:
            out[code] = name
    return out


# ---------- per-API parsers ----------

def parse_symbols_master(raw: Any) -> list[dict]:
    """PSX symbols registry (JSON). Each item:
    {isDebt, isETF, isGEM, name, sectorName, symbol} -> normalized records."""
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    out: list[dict] = []
    for r in (data or []):
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        out.append({
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "sector": r.get("sectorName"),
            "is_etf": bool(r.get("isETF")),
            "is_debt": bool(r.get("isDebt")),
            "is_gem": bool(r.get("isGEM")),
        })
    return out


_DOC_ID_RE = re.compile(r"/(?:document|attachment)/(\d+)")


def _parse_announcements_precise(raw: Any) -> list[dict]:
    """Parse the PSX #announcementsTable markup. Header-driven so it handles both
    layouts: company announcements (DATE, TIME, SYMBOL, NAME, TITLE, …) and SECP
    notices (DATE, TIME, TITLE, …). Captures the REVOKED tag and PDF/document link."""
    if not isinstance(raw, str) or "<" not in raw:
        return []
    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:
        soup = BeautifulSoup(raw, "html.parser")
    table = soup.find("table", id="announcementsTable") or soup.find("table")
    if table is None:
        return []

    # map field -> column index from the header row
    field_idx: dict[str, int] = {}
    thead = table.find("thead")
    if thead:
        for i, th in enumerate(thead.find_all("th")):
            h = th.get_text(strip=True).lower()
            for field in ("date", "time", "symbol", "name", "title"):
                if field in h:
                    field_idx[field] = i
                    break

    out: list[dict] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        def _cell(field, _tds=tds):
            i = field_idx.get(field)
            return _tds[i] if (i is not None and i < len(_tds)) else None

        # positional fallback when no usable header was found
        if not field_idx:
            if len(tds) >= 5:
                idx = {"date": 0, "time": 1, "symbol": 2, "name": 3, "title": 4}
            else:
                idx = {"date": 0, "time": 1, "title": 2}
            _cell = lambda field, _tds=tds, _i=idx: (_tds[_i[field]] if field in _i and _i[field] < len(_tds) else None)

        title_td = _cell("title")
        if title_td is None:
            continue
        status = None
        tag = title_td.find(class_="tag")
        if tag:
            status = tag.get_text(strip=True)
            tag.extract()  # strip tag so title text is clean

        a_pdf = tr.find("a", href=_DOC_ID_RE)  # PDF/doc link anywhere in the row
        pdf_url = a_pdf.get("href") if a_pdf else None
        m = _DOC_ID_RE.search(pdf_url or "")
        doc_id = m.group(1) if m else None

        sym_td, name_td, date_td, time_td = (_cell("symbol"), _cell("name"),
                                             _cell("date"), _cell("time"))
        out.append({
            "date": date_td.get_text(strip=True) if date_td else None,
            "time": time_td.get_text(strip=True) if time_td else None,
            "symbol": sym_td.get_text(strip=True) if sym_td else None,
            "name": name_td.get_text(strip=True) if name_td else None,
            "title": title_td.get_text(strip=True),
            "status": status,
            "pdf_url": pdf_url,
            "doc_id": doc_id,
        })
    return out


def _parse_announcement_like(raw: Any) -> list[dict]:
    """Precise parser first; generic table fallback for other markup."""
    rows = _parse_announcements_precise(raw)
    if rows:
        return rows
    out: list[dict] = []
    for df in _tables(raw):
        c_date = _find_col(df, "date", "time")
        c_title = _find_col(df, "title", "subject", "headline", "announcement", "notice")
        c_company = _find_col(df, "company", "symbol", "name")
        for r in _records(df):
            out.append({"title": r.get(c_title) if c_title else None,
                        "date": r.get(c_date) if c_date else None,
                        "company": r.get(c_company) if c_company else None})
    return [a for a in out if a.get("title") or a.get("date")]


def parse_company_announcements(raw: Any) -> list[dict]:
    return _parse_announcement_like(raw)


def parse_secp_notices(raw: Any) -> list[dict]:
    return _parse_announcement_like(raw)


def announcements_total(raw: Any) -> int | None:
    """Total entry count from 'Showing 1 to 50 of 863 entries' (for pagination)."""
    if not isinstance(raw, str):
        return None
    m = re.search(r"of\s+([\d,]+)\s+entries", raw, re.I)
    return int(m.group(1).replace(",", "")) if m else None


def _parse_market_like(raw: Any) -> list[dict]:
    """market-watch / futures: list of {symbol, price, change, volume, sector}."""
    out: list[dict] = []
    for df in _tables(raw):
        c_sym = _find_col(df, "symbol", "scrip", "company")
        c_price = _find_col(df, "current", "price", "last", "close", "ldcp")
        c_chg = _find_col(df, "change", "chg", "%")
        c_vol = _find_col(df, "volume", "turnover", "vol")
        c_sec = _find_col(df, "sector", "index")
        if c_sym is None and c_price is None:
            continue
        for r in _records(df):
            out.append({
                "symbol": r.get(c_sym) if c_sym else None,
                "price": _num(r.get(c_price)) if c_price else None,
                "change": r.get(c_chg) if c_chg else None,
                "volume": r.get(c_vol) if c_vol else None,
                "sector": r.get(c_sec) if c_sec else None,
            })
    return [r for r in out if r.get("symbol")]


def _parse_market_watch_precise(raw: Any, *, futures: bool = False) -> list[dict]:
    """Parse the PSX market-watch table precisely. The header <th> carry data-name
    (symbol/sector/listed/ldcp/open/high/low/close/change/percentChange/volume) and
    each body <td> carries a data-order with the machine value — use both so we keep
    full numeric precision instead of the display-formatted text. Symbol and company
    name come from the <a class="tbl__symbol"> anchor (href /etf/ marks an ETF); a
    status tag div (NC/XD/XB/WU) is captured. Note: data-name 'close' is the CURRENT
    price column, surfaced as `price`.

    The futures market-watch feed shares this exact table, with two differences set
    when ``futures=True``: the symbol is BASE-CONTRACT (e.g. MTL-JUN) so we also emit
    ``base_symbol``, and the data-name='sector' column holds the futures CONTRACT
    month (JUN/JUNB/JULB), not an industry sector — emitted as ``contract`` (no
    PSX_SECTORS resolution)."""
    if not isinstance(raw, str) or "<" not in raw:
        return []
    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:
        soup = BeautifulSoup(raw, "html.parser")

    # the market-watch table is the one whose header th's carry data-name
    table = None
    for t in soup.find_all("table"):
        head = t.find("thead")
        if head and head.find("th", attrs={"data-name": True}):
            table = t
            break
    if table is None:
        return []

    name_idx: dict[str, int] = {}
    for i, th in enumerate(table.find("thead").find_all("th")):
        dn = th.get("data-name")
        if dn:
            name_idx[dn] = i

    # data-name (header) -> our field, numeric value read from data-order
    NUM = {"ldcp": "ldcp", "open": "open", "high": "high", "low": "low",
           "close": "price", "change": "change", "percentChange": "change_pct",
           "volume": "volume"}

    out: list[dict] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        def _td(dn, _tds=tds):
            i = name_idx.get(dn)
            return _tds[i] if (i is not None and i < len(_tds)) else None

        sym_td = _td("symbol")
        if sym_td is None:
            continue
        a = sym_td.find("a", class_="tbl__symbol") or sym_td.find("a")
        symbol = (a.get_text(strip=True) if a else sym_td.get_text(strip=True)) or None
        if not symbol:
            continue
        tag = sym_td.find(class_="tag")
        row: dict = {
            "symbol": symbol,
            "name": a.get("data-title") if a else None,
            "is_etf": bool(a and "/etf/" in (a.get("href") or "")),
            "status": tag.get_text(strip=True) if tag else None,
        }

        sec_td = _td("sector")
        sec_val = sec_td.get_text(strip=True) if sec_td else None
        if futures:
            # futures feed: this column is the contract month, not an industry sector
            row["contract"] = sec_val or None
            row["base_symbol"] = symbol.split("-")[0] if symbol else None
        else:
            row["sector_code"] = sec_val
            row["sector"] = PSX_SECTORS.get(sec_val)  # resolved name, None if unknown
        listed_td = _td("listed")
        listed = listed_td.get_text(strip=True) if listed_td else ""
        row["listed_in"] = [s for s in listed.split(",") if s]

        for dn, field in NUM.items():
            td = _td(dn)
            if td is None:
                row[field] = None
                continue
            val = td.get("data-order")  # precise machine value, else fall back to text
            if val is None:
                val = td.get_text(strip=True).replace("%", "")
            row[field] = _num(val)
        if row.get("volume") is not None:
            row["volume"] = int(row["volume"])
        out.append(row)
    return out


def parse_market_watch(raw: Any) -> list[dict]:
    rows = _parse_market_watch_precise(raw)
    return rows if rows else _parse_market_like(raw)


def parse_sector_market_watch(raw: Any) -> list[dict]:
    """The sector market-watch reuses the same /market-watch feed (one GET returns
    the whole market), so parsing is identical to market_watch. The caller then
    narrows to the queried sector with filter_market_watch_by_sector()."""
    return parse_market_watch(raw)


def resolve_sector_code(sector: Any) -> str | None:
    """Map a user-supplied sector to a PSX sector code. Accepts an exact code
    ('0804'), an exact sector name, or a keyword/substring ('cement' -> 0804).
    Returns None if nothing matches."""
    if not sector:
        return None
    s = str(sector).strip()
    if s in PSX_SECTORS:                      # already a code
        return s
    low = s.lower()
    for code, name in PSX_SECTORS.items():    # exact name
        if name.lower() == low:
            return code
    for code, name in PSX_SECTORS.items():    # keyword / substring
        if low in name.lower():
            return code
    return None


def filter_market_watch_by_sector(rows: list[dict], sector: Any) -> list[dict]:
    """Narrow parsed market-watch rows to the queried sector. `sector` may be a code
    ('0804') or a name/keyword ('cement'); matches on the resolved sector code, with
    a sector-name substring fallback. Empty/unknown sector returns all rows."""
    code = resolve_sector_code(sector)
    if code:
        return [r for r in rows if r.get("sector_code") == code]
    low = str(sector or "").strip().lower()
    if not low:
        return list(rows)
    return [r for r in rows if low in (r.get("sector") or "").lower()]


def parse_deliverable_futures_market_watch(raw: Any) -> list[dict]:
    rows = _parse_market_watch_precise(raw, futures=True)
    return rows if rows else _parse_market_like(raw)


def parse_cash_settled_futures_market_watch(raw: Any) -> list[dict]:
    rows = _parse_market_watch_precise(raw, futures=True)
    return rows if rows else _parse_market_like(raw)


def filter_futures_by_symbol(rows: list[dict], symbol: Any) -> list[dict]:
    """Narrow parsed futures rows to a single company. Futures symbols are
    BASE-CONTRACT (MTL-JUN, MTL-JUL), so a company symbol matches a row when the row
    symbol is ``<symbol>-<contract>`` — i.e. it starts with ``SYMBOL-`` (or equals it
    if the full contract symbol was passed). This is anchored on the base segment, so
    'ASL' matches 'ASL-JUN' but NOT 'ASLPS-JUN'. Case-insensitive; empty -> all rows."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return list(rows)
    out: list[dict] = []
    for r in rows:
        rs = (r.get("symbol") or "").upper()
        if rs == sym or rs.startswith(sym + "-"):
            out.append(r)
    return out


# data-name (screener header) -> our field; values read from each td's data-order
_SCREENER_NUM = {
    "marketCap": "market_cap", "close": "price", "percentChange": "change_pct",
    "changeYear": "change_1y_pct", "peRatio": "pe_ratio_ttm",
    "dividendYield": "dividend_yield_pct", "freeFloat": "free_float",
    "volume30Avg": "volume_30d_avg",
}


def parse_stock_screener(raw: Any) -> list[dict]:
    """Parse the PSX /screener table precisely. One GET returns the whole market; each
    row is a security carrying the valuation/liquidity metrics the quote feeds don't:
    PE (TTM), dividend yield %, 1-year return %, free float, 30-day average volume — plus
    market cap and price. Header <th> carry data-name and each body <td> a data-order
    machine value (used for full precision over the display text). Symbol/company name
    come from the <a class="tbl__symbol"> anchor (data-title = company name); the sector
    <td> holds the PSX sector CODE (resolved to a name via PSX_SECTORS); the status tag
    (NC/XD/XB/WU) is captured. Returns [] on non-HTML/unrecognised markup.

    NOTE: PE/yield/1-year return are market-derived snapshots (source delays data ~5 min
    and 1-year return is NOT payout-adjusted) — point-in-time peer/relative context, not a
    substitute for the uploaded workbook's own accounting numbers."""
    if not isinstance(raw, str) or "<" not in raw:
        return []
    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:
        soup = BeautifulSoup(raw, "html.parser")

    table = soup.find("table", id="screenerTable")
    if table is None:                       # fall back to the screener-shaped table
        for t in soup.find_all("table"):
            head = t.find("thead")
            if head and head.find("th", attrs={"data-name": "peRatio"}):
                table = t
                break
    if table is None or table.find("thead") is None:
        return []

    name_idx: dict[str, int] = {}
    for i, th in enumerate(table.find("thead").find_all("th")):
        dn = th.get("data-name")
        if dn:
            name_idx[dn] = i

    out: list[dict] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        def _td(dn, _tds=tds):
            i = name_idx.get(dn)
            return _tds[i] if (i is not None and i < len(_tds)) else None

        sym_td = _td("symbol")
        if sym_td is None:
            continue
        a = sym_td.find("a", class_="tbl__symbol") or sym_td.find("a")
        symbol = (a.get_text(strip=True) if a else sym_td.get_text(strip=True)) or None
        if not symbol:
            continue
        tag = sym_td.find(class_="tag")
        sec_td = _td("sector")
        sec_code = (sec_td.get_text(strip=True) if sec_td else None) or None
        listed_td = _td("listed")
        listed = listed_td.get_text(strip=True) if listed_td else ""
        row: dict = {
            "symbol": symbol,
            "name": (a.get("data-title") if a else None) or None,
            "sector_code": sec_code,
            "sector": PSX_SECTORS.get(sec_code or ""),   # resolved name, None if unknown
            "listed_in": [s for s in listed.split(",") if s],
            "status": tag.get_text(strip=True) if tag else None,
        }
        for dn, fld in _SCREENER_NUM.items():
            td = _td(dn)
            if td is None:
                row[fld] = None
                continue
            val = td.get("data-order")   # precise machine value, else fall back to text
            if val is None:
                val = td.get_text(strip=True).replace("%", "")
            row[fld] = _num(val)
        out.append(row)
    return out


def parse_sector_stock_screener(raw: Any) -> list[dict]:
    """Sector-scoped screener reuses the same /screener feed (one GET = whole market);
    the caller narrows to the queried sector with filter_market_watch_by_sector()
    (screener rows carry the same sector_code/sector fields)."""
    return parse_stock_screener(raw)


def filter_screener_by_symbol(rows: list[dict], symbol: Any) -> list[dict]:
    """Narrow parsed screener rows to a single company by exact symbol
    (case-insensitive). Empty symbol returns all rows."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return list(rows)
    return [r for r in rows if (r.get("symbol") or "").upper() == sym]


def _parse_payout_details(text: str) -> dict:
    """'200%(i) (D)' -> {payout_pct: 200.0, interim: True, dividend: True, ...}."""
    t = (text or "")
    pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
    low = t.lower()
    return {
        "payout_pct": float(pct.group(1)) if pct else None,
        "interim": "(i)" in low,
        "final": "(f)" in low,
        "dividend": "(d)" in low,
        "bonus": "(b)" in low,
        "right": "(r)" in low,
    }


def parse_company_payouts(raw: Any) -> list[dict]:
    """Precise parse of the payouts table (Date | Financial Results | Details |
    Book Closure). Header-driven; generic fallback for other markup."""
    if isinstance(raw, str) and "<" in raw:
        try:
            soup = BeautifulSoup(raw, "lxml")
        except Exception:
            soup = BeautifulSoup(raw, "html.parser")
        table = soup.find("table")
        if table is not None:
            heads = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            def _idx(*kw):
                for i, h in enumerate(heads):
                    if any(k in h for k in kw):
                        return i
                return None
            i_date = _idx("date")
            i_fin = _idx("financial", "result", "period")
            i_det = _idx("detail", "payout", "dividend")
            i_book = _idx("book", "closure")
            out: list[dict] = []
            body = table.find("tbody") or table
            for tr in body.find_all("tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                def _c(i, _tds=tds):
                    return _tds[i].get_text(" ", strip=True) if (i is not None and i < len(_tds)) else None
                details = _c(i_det if i_det is not None else 2)
                rec = {
                    "date": _c(i_date if i_date is not None else 0),
                    "financial_results": _c(i_fin if i_fin is not None else 1),
                    "details": details,
                    "book_closure": _c(i_book if i_book is not None else 3),
                }
                rec.update(_parse_payout_details(details or ""))
                if rec["date"] or rec["details"]:
                    out.append(rec)
            if out:
                return out
    # generic fallback
    out = []
    for df in _tables(raw):
        for r in _records(df):
            out.append(dict(r))
    return out


def _money(text: Any) -> float | None:
    """Parse '(38.88)', 'Rs.563.63', '112,453,173.21', '45.00%' -> float."""
    if text is None:
        return None
    s = str(text).strip().replace("Rs.", "").replace("Rs", "").replace(",", "").replace("%", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    v = float(m.group(0))
    return -v if neg else v


def _stats_pairs(node) -> dict:
    """Collect {label: value} from .stats_item label/value pairs under a node."""
    out: dict[str, str] = {}
    if node is None:
        return out
    for item in node.select(".stats_item"):
        lab = item.find(class_="stats_label")
        val = item.find(class_="stats_value")
        if lab and val:
            key = lab.get_text(" ", strip=True)
            if key and key not in out:
                out[key] = val.get_text(" ", strip=True)
    return out


def _year_table(table) -> dict:
    """A year-columned metric table -> {year: {metric_label: value}}."""
    if table is None:
        return {}
    rows = table.find_all("tr")
    if not rows:
        return {}
    header_cells = rows[0].find_all(["th", "td"])
    years = []
    for c in header_cells[1:]:
        y = re.search(r"(19|20)\d{2}", c.get_text())
        years.append(y.group(0) if y else c.get_text(strip=True))
    out: dict = {y: {} for y in years}
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True)
        if not label:
            continue
        for i, c in enumerate(cells[1:]):
            if i < len(years):
                out[years[i]][label] = _money(c.get_text(strip=True))
    return out


def parse_company_overview(raw: Any) -> dict:
    """Structured parse of the PSX /company/{symbol} page: quote (price, P/E, day
    stats), equity (market cap, shares, free float), profile, annual financials
    (sales/PAT/EPS by year), and ratios. Generic-table fallback if structure absent."""
    if not isinstance(raw, str) or "<" not in raw:
        return {}
    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:
        soup = BeautifulSoup(raw, "html.parser")

    def _txt(sel):
        el = soup.select_one(sel)
        return el.get_text(" ", strip=True) if el else None

    out: dict = {}
    out["symbol"] = _txt(".pageHeader__title")
    out["name"] = _txt(".quote__name")
    out["sector"] = _txt(".quote__sector")
    out["price"] = _money(_txt(".quote__close"))

    # quote stats — scope to the regular (REG) panel to avoid futures/CSF stats
    reg = soup.select_one('#quote .tabs__panel[data-name="REG"]') or soup.find(id="quote")
    qs = _stats_pairs(reg)
    out["quote_stats"] = qs
    pe_key = next((k for k in qs if "p/e" in k.lower()), None)
    out["pe_ratio"] = _money(qs.get(pe_key)) if pe_key else None

    # equity profile — iterate items directly (labels like "Free Float" repeat)
    eq_node = soup.find(id="equity")
    mc = sh = ff_count = ff_pct = None
    if eq_node is not None:
        for item in eq_node.select(".stats_item"):
            lab = item.find(class_="stats_label")
            val = item.find(class_="stats_value")
            if not (lab and val):
                continue
            key = lab.get_text(" ", strip=True).lower()
            raw_val = val.get_text(" ", strip=True)
            if "market cap" in key:
                mc = raw_val
            elif key.strip() == "shares":
                sh = raw_val
            elif "free float" in key:
                if "%" in raw_val:
                    ff_pct = raw_val
                else:
                    ff_count = raw_val
    out["market_cap"] = _money(mc)
    out["shares"] = _money(sh)
    out["free_float"] = _money(ff_count)
    out["free_float_pct"] = _money(ff_pct)

    # profile
    prof: dict = {}
    desc = soup.select_one(".profile__item--decription p")
    if desc:
        prof["business_description"] = desc.get_text(" ", strip=True)
    for head in soup.select(".company__profile .item__head"):
        label = head.get_text(strip=True).lower()
        nxt = head.find_next_sibling("p")
        if nxt and "website" in label:
            prof["website"] = nxt.get_text(" ", strip=True)
        elif nxt and "auditor" in label:
            prof["auditor"] = nxt.get_text(" ", strip=True)
        elif nxt and "fiscal year" in label:
            prof["fiscal_year_end"] = nxt.get_text(" ", strip=True)
    out["profile"] = prof

    # annual financials (Sales / Profit after Taxation / EPS by year)
    fin_panel = soup.select_one('#financials .tabs__panel[data-name="Annual"]')
    fin_table = fin_panel.find("table") if fin_panel else None
    fin_by_year = _year_table(fin_table)
    norm = {}
    for year, metrics in fin_by_year.items():
        row = {}
        for lab, v in metrics.items():
            ll = lab.lower()
            if "sales" in ll or "revenue" in ll:
                row["sales"] = v
            elif "profit after" in ll or ll == "pat":
                row["pat"] = v
            elif "eps" in ll:
                row["eps"] = v
        if row:
            norm[year] = row
    out["financials_annual"] = norm

    # ratios
    ratios_table = soup.select_one("#ratios table")
    out["ratios"] = _year_table(ratios_table)
    return out


def _parse_market_summary_precise(raw: Any) -> dict:
    """Parse the PSX market-summary page into the figures that define overall market
    state: the timestamp, the exchange line (status/volume/value/trades), market
    breadth (advanced/declined/unchanged/total), and the index board (name/value/
    change/change_pct). Per-sector constituent quotes on the same page are the
    market_watch feed's job and are not duplicated here."""
    if not isinstance(raw, str) or "<" not in raw:
        return {}
    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:
        soup = BeautifulSoup(raw, "html.parser")

    out: dict = {}
    m = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", raw)
    out["timestamp"] = m.group(0) if m else None

    # the two summary strips (.ms-tbl-new): first td-fst names the group ("Exchange",
    # "Symbol"); each <p> is "Label: value" (value numeric except Exchange status text)
    groups: dict[str, dict] = {}
    for tbl in soup.select(".ms-tbl-new"):      # class is on the wrapping div
        fst = tbl.find("td", class_="td-fst")
        if not fst:
            continue
        gname = fst.get_text(" ", strip=True).lower()
        fields: dict = {}
        for p in tbl.find_all("p"):
            sp = p.find("span")
            if not sp:
                continue
            key = sp.get_text(strip=True).rstrip(":").lower()
            val = p.get_text(" ", strip=True)[len(sp.get_text(strip=True)):].strip()
            num = _num(val)
            fields[key] = num if num is not None else (val or None)
        if fields:
            groups[gname] = fields
    if "exchange" in groups:
        out["exchange"] = groups["exchange"]
    if "symbol" in groups:                      # td-fst is "Symbol" -> market breadth
        out["breadth"] = groups["symbol"]

    indices = []
    for item in soup.select(".indices-single"):
        name = item.find("h3")
        if not name or not name.get_text(strip=True):
            continue
        val, chg, pct = item.find("h4"), item.find("h5"), item.find("h6")
        pct_txt = pct.get_text(strip=True).strip("()% ") if pct else ""
        indices.append({
            "name": name.get_text(strip=True),
            "value": _num(val.get_text(strip=True)) if val else None,
            "change": _num(chg.get_text(strip=True)) if chg else None,
            "change_pct": _num(pct_txt) if pct_txt else None,
        })
    if indices:
        out["indices"] = indices
    return out


def parse_daily_market_summary(raw: Any) -> dict:
    data = _parse_market_summary_precise(raw)
    if data.get("indices") or data.get("exchange"):
        return data
    return {"tables": [_records(df) for df in _tables(raw)]}  # generic fallback


def _parse_sector_summary_precise(raw: Any) -> list[dict]:
    """Parse the sector-level table on the PSX sector-summary page (the
    .sectorSummary__sectors table): per sector code, the name, advance/decline/
    unchange breadth, turnover, and market cap (in billions). The sector NAME is read
    from the cell's data-order (clean, real '&') rather than the <strong> text, which
    on this page renders '& ' as '&'. The per-sector constituent quote tables on the
    same page are the market_watch feed's shape and are not duplicated here."""
    if not isinstance(raw, str) or "<" not in raw:
        return []
    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:
        soup = BeautifulSoup(raw, "html.parser")

    sectors_div = soup.find(class_="sectorSummary__sectors")
    table = sectors_div.find("table") if sectors_div else None
    if table is None:                          # fall back: first table whose header names a sector code
        for t in soup.find_all("table"):
            head = t.find("thead")
            if head and "sector code" in head.get_text(" ", strip=True).lower():
                table = t
                break
    if table is None:
        return []

    # map header keyword -> column index
    idx: dict[str, int] = {}
    thead = table.find("thead")
    for i, th in enumerate(thead.find_all("th")) if thead else []:
        h = th.get_text(" ", strip=True).lower()
        for key, kw in (("code", "sector code"), ("name", "sector name"), ("advance", "advance"),
                        ("decline", "decline"), ("unchange", "unchange"),
                        ("turnover", "turnover"), ("market_cap", "market cap")):
            if kw in h and key not in idx:
                idx[key] = i

    out: list[dict] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        def _td(key, _tds=tds):
            i = idx.get(key)
            return _tds[i] if (i is not None and i < len(_tds)) else None

        code_td, name_td = _td("code"), _td("name")
        code = code_td.get_text(strip=True) if code_td else None
        name = None
        if name_td is not None:
            name = name_td.get("data-order") or name_td.get_text(" ", strip=True) or None
        out.append({
            "sector_code": code,
            "sector": name or PSX_SECTORS.get(code),
            "advance": int(_td("advance").get_text(strip=True)) if _td("advance") else None,
            "decline": int(_td("decline").get_text(strip=True)) if _td("decline") else None,
            "unchange": int(_td("unchange").get_text(strip=True)) if _td("unchange") else None,
            "turnover": int(_num(_td("turnover").get("data-order")
                                 or _td("turnover").get_text(strip=True))) if _td("turnover") else None,
            "market_cap_b": _num(_td("market_cap").get_text(strip=True)) if _td("market_cap") else None,
        })
    return [s for s in out if s.get("sector_code")]


def parse_sector_summary(raw: Any) -> list[dict]:
    rows = _parse_sector_summary_precise(raw)
    if rows:
        return rows
    out: list[dict] = []                       # generic fallback
    for df in _tables(raw):
        c_sec = _find_col(df, "sector", "index")
        if c_sec is None:
            continue
        out += _records(df)
    return out


# PSX analysis-report (year-{year}.xlsx) — single "Master Data Entry" sheet:
# 4-row preamble ("DATA FOR THE YEAR YYYY" + label row + units row), then sector
# section-header rows (no Sr. No.) carried down over the company rows beneath them.
# header label (normalized) -> our field
_ANALYSIS_HEADER = {
    "symbol": "symbol", "name of company": "name", "year end": "year_end",
    "paid up capital": "paid_up_capital", "face value": "face_value",
    "number of shares": "shares_m", "shareholders equity": "equity",
    "total assets": "total_assets", "sales / total income": "sales",
    "sales total income": "sales", "financial charges": "financial_charges",
    "profit before taxation": "pbt", "taxation": "taxation",
    "profit after taxation": "pat", "cash dividend": "cash_dividend_pct",
    "stock dividend": "stock_dividend_pct", "total dividend": "total_dividend_pct",
    "right issue": "right_issue_pct", "number of shareholders": "shareholders",
}
# value fields (everything but symbol/name/year_end) are numeric. Units (per the
# sheet): *_m fields & financials are Rs. MILLION except shares_m (million shares)
# and *_pct (percent of face value).
_ANALYSIS_NUM = {f for f in _ANALYSIS_HEADER.values()
                 if f not in ("symbol", "name", "year_end")}


def _norm_label(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s).replace("'", "").strip().lower())


def parse_analysis_report_xlsx(raw: Any) -> list[dict]:
    """Parse a PSX yearly analysis report (year-{year}.xlsx) into one typed record
    per company: symbol, name, sector (carried from the section-header rows), year_end,
    and the fundamentals (paid-up capital, shares, equity, total assets, sales,
    financial charges, PBT, taxation, PAT) plus dividend %s and shareholder count.

    Financials are in Rs. MILLION, shares in million, dividends in % of face value —
    callers must reconcile scale before mixing with the workbook (Rs. thousand).
    Accepts raw bytes or a path. Malformed input -> []."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            df = pd.read_excel(io.BytesIO(raw), header=None)
        elif isinstance(raw, str) and raw.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(raw, header=None)
        else:
            return []
    except Exception:
        return []
    if df.empty:
        return []

    # dataset year from the "DATA FOR THE YEAR YYYY" banner (first rows)
    fiscal_year = None
    head_text = " ".join(str(v) for v in df.head(3).values.ravel() if pd.notna(v))
    m = re.search(r"DATA FOR THE YEAR\s+(\d{4})", head_text, re.I)
    if m:
        fiscal_year = int(m.group(1))

    # locate the header row (has a "Symbol" + "Name of Company" cell)
    header_row = srno_col = None
    for i in range(min(15, len(df))):
        labels = {j: _norm_label(v) for j, v in enumerate(df.iloc[i]) if pd.notna(v)}
        if "symbol" in labels.values() and "name of company" in labels.values():
            header_row = i
            colmap = {_ANALYSIS_HEADER[lbl]: j for j, lbl in labels.items()
                      if lbl in _ANALYSIS_HEADER}
            srno_col = next((j for j, lbl in labels.items() if lbl.startswith("sr")), None)
            break
    if header_row is None or "symbol" not in colmap:
        return []

    def _cell(row, field):
        j = colmap.get(field)
        return row.iloc[j] if (j is not None and j < len(row)) else None

    out: list[dict] = []
    current_sector = None
    for i in range(header_row + 1, len(df)):
        row = df.iloc[i]
        srno = row.iloc[srno_col] if (srno_col is not None and srno_col < len(row)) else None
        sym, name = _cell(row, "symbol"), _cell(row, "name")
        if pd.notna(srno) and pd.notna(sym):           # company row
            rec: dict = {"symbol": str(sym).strip(),
                         "name": str(name).strip() if pd.notna(name) else None,
                         "sector": current_sector, "fiscal_year": fiscal_year}
            ye = _cell(row, "year_end")
            if hasattr(ye, "date"):                    # pandas Timestamp / datetime
                rec["year_end"] = ye.date().isoformat()
            elif pd.notna(ye):
                rec["year_end"] = str(ye).strip()
            else:
                rec["year_end"] = None
            for field in _ANALYSIS_NUM:
                v = _cell(row, field)
                rec[field] = _num(v) if pd.notna(v) else None
            out.append(rec)
        elif pd.isna(srno) and pd.notna(name):          # sector section header (no Sr. No.)
            current_sector = str(name).strip()
    return out


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


# api_name -> parser (per the PSX catalog)
PARSERS = {
    "symbols_master": parse_symbols_master,
    "company_announcements": parse_company_announcements,
    "secp_notices": parse_secp_notices,
    # sector-scoped variants reuse the same parsers (same endpoint, query instead of symbol)
    "sector_announcements": parse_company_announcements,
    "sector_secp_notices": parse_secp_notices,
    "company_overview": parse_company_overview,
    "company_payouts": parse_company_payouts,
    "market_watch": parse_market_watch,
    # sector market-watch hits the same feed, then narrows by sector id (see registry)
    "sector_market_watch": parse_sector_market_watch,
    "deliverable_futures_market_watch": parse_deliverable_futures_market_watch,
    # company-scoped futures hits the same feed, then narrows by base symbol (MTL-JUN -> MTL)
    "company_deliverable_futures_market_watch": parse_deliverable_futures_market_watch,
    "cash_settled_futures_market_watch": parse_cash_settled_futures_market_watch,
    "daily_market_summary": parse_daily_market_summary,
    "analysis_reports": parse_analysis_report_xlsx,
    "sector_summary": parse_sector_summary,
    "stock_screener": parse_stock_screener,
    # sector screener hits the same /screener feed, then narrows by sector id
    "sector_stock_screener": parse_sector_stock_screener,
}
