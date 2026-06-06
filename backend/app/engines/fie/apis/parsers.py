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


def parse_market_watch(raw: Any) -> list[dict]:
    return _parse_market_like(raw)


def parse_deliverable_futures_market_watch(raw: Any) -> list[dict]:
    return _parse_market_like(raw)


def parse_cash_settled_futures_market_watch(raw: Any) -> list[dict]:
    return _parse_market_like(raw)


def parse_company_payouts(raw: Any) -> list[dict]:
    """Payouts: dividends/bonus/book-closure rows."""
    out: list[dict] = []
    for df in _tables(raw):
        c_type = _find_col(df, "type", "payout", "kind")
        c_pct = _find_col(df, "%", "rate", "ratio", "amount", "payout")
        c_date = _find_col(df, "date", "book", "closure")
        for r in _records(df):
            out.append({"type": r.get(c_type) if c_type else None,
                        "value": r.get(c_pct) if c_pct else None,
                        "date": r.get(c_date) if c_date else None})
    return out


def parse_company_overview(raw: Any) -> dict:
    """Multi-section page: return every table as records, indexed by order."""
    return {"tables": [_records(df) for df in _tables(raw)]}


def parse_daily_market_summary(raw: Any) -> dict:
    return {"tables": [_records(df) for df in _tables(raw)]}


def parse_sector_summary(raw: Any) -> list[dict]:
    out: list[dict] = []
    for df in _tables(raw):
        c_sec = _find_col(df, "sector", "index")
        if c_sec is None:
            continue
        out += _records(df)
    return out


def parse_analysis_report_xlsx(raw: Any) -> list[dict]:
    """Yearly analysis dataset (XLSX). Accepts bytes or a path."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            df = pd.read_excel(io.BytesIO(raw))
        elif isinstance(raw, str):
            df = pd.read_excel(raw)
        else:
            return []
    except Exception:
        return []
    return _records(df)


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
    "company_overview": parse_company_overview,
    "company_payouts": parse_company_payouts,
    "market_watch": parse_market_watch,
    "deliverable_futures_market_watch": parse_deliverable_futures_market_watch,
    "cash_settled_futures_market_watch": parse_cash_settled_futures_market_watch,
    "daily_market_summary": parse_daily_market_summary,
    "analysis_reports": parse_analysis_report_xlsx,
    "sector_summary": parse_sector_summary,
}
