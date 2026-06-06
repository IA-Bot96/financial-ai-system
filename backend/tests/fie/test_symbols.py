"""PSX symbols-master adapter: parsing + name->ticker / sector resolution, and
engine ticker resolution via the registry."""

import pytest

from app.engines.fie import (
    ExternalSources,
    FinancialIntelligenceEngine,
    PSX,
    Symbols,
)
from app.engines.fie.apis import ApiClient
from app.engines.fie.apis.parsers import parse_symbols_master

_RAW = [
    {"isDebt": False, "isETF": False, "isGEM": False,
     "name": "The Thal Industries Corporation Limited",
     "sectorName": "SUGAR & ALLIED INDUSTRIES", "symbol": "TICL"},
    {"isDebt": False, "isETF": False, "isGEM": False,
     "name": "Millat Tractors Limited", "sectorName": "AUTOMOBILE ASSEMBLER", "symbol": "MTL"},
    {"isDebt": False, "isETF": True, "isGEM": False,
     "name": "Lucky Cement Limited", "sectorName": "CEMENT", "symbol": "LUCK"},
]


class _T:
    def get(self, url, params, timeout):
        return _RAW
    def post(self, url, body, timeout):
        raise AssertionError("symbols is GET")


def _symbols():
    return Symbols(ApiClient(_T(), sleep=lambda s: None, now=lambda: "2026-06-06"))


def test_parse_symbols_master_shape():
    recs = parse_symbols_master(_RAW)
    assert recs[0] == {"symbol": "TICL", "name": "The Thal Industries Corporation Limited",
                       "sector": "SUGAR & ALLIED INDUSTRIES",
                       "is_etf": False, "is_debt": False, "is_gem": False}
    assert recs[2]["is_etf"] is True


def test_parse_symbols_handles_wrapper_and_junk():
    assert parse_symbols_master({"data": _RAW})[1]["symbol"] == "MTL"
    assert parse_symbols_master([{"no_symbol": 1}, "junk"]) == []


@pytest.mark.parametrize("query,ticker", [
    ("Millat Tractors Limited", "MTL"),
    ("millat tractors", "MTL"),           # partial + lowercase
    ("Thal Industries", "TICL"),
    ("Lucky Cement", "LUCK"),
    ("Nonexistent Holdings Co", None),    # no confident match
])
def test_ticker_for(query, ticker):
    assert _symbols().ticker_for(query) == ticker


def test_sector_and_peers():
    s = _symbols()
    assert s.sector_for("MTL") == "AUTOMOBILE ASSEMBLER"
    assert s.by_sector("cement") == ["LUCK"]  # case-insensitive


def test_symbols_cached_single_fetch():
    t = _T()
    calls = {"n": 0}
    orig = t.get
    def counting_get(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)
    t.get = counting_get
    s = Symbols(ApiClient(t, sleep=lambda x: None, now=lambda: "T"))
    s.ticker_for("MTL"); s.sector_for("MTL"); s.by_sector("CEMENT")
    assert calls["n"] == 1  # registry fetched once, then cached


def test_engine_resolves_ticker_via_symbols(millat_store):
    # valuation resolves the ticker through the symbols registry (not the static map)
    class _PriceT:
        def get(self, url, params, timeout):
            if url.endswith("/symbols"):
                return _RAW
            return {"price": 1000.0, "eps": 80.0}
        def post(self, url, body, timeout):
            raise AssertionError
    client = ApiClient(_PriceT(), sleep=lambda s: None, now=lambda: "T")
    ext = ExternalSources(psx=PSX(client), symbols=Symbols(client))
    eng = FinancialIntelligenceEngine(millat_store, external=ext)
    r = eng.answer("valuation for MTL")
    assert any(c.formula_id == "pe_ratio" for c in r.calculations)
