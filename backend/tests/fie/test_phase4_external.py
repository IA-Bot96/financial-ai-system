"""Phase 4 — external orchestration + hard intents (offline, fake transports)."""

import itertools

import pytest

from app.engines.fie import (
    ExternalSources,
    FinancialFactStore,
    FinancialIntelligenceEngine,
    ForecastRepo,
    News,
    PSX,
)
from app.engines.fie.apis import ApiClient
from app.engines.fie import understanding


# --- fake transports ---

class FakeT:
    def __init__(self, payload):
        self.payload = payload
    def get(self, url, params, timeout):
        return self.payload


class FlakyT:
    def __init__(self, fail_times, payload):
        self.calls = 0
        self.fail_times = fail_times
        self.payload = payload
    def get(self, url, params, timeout):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("boom")
        return self.payload


class DeadT:
    def get(self, url, params, timeout):
        raise TimeoutError("down")


def _client(transport):
    return ApiClient(transport, sleep=lambda s: None, now=lambda: "2026-06-06")


@pytest.fixture(scope="module")
def lucky_store(lucky_path):
    return FinancialFactStore.from_workbook(lucky_path)


# --- 4.1 resilient client ---

def test_client_retries_then_succeeds():
    t = FlakyT(2, {"price": 100.0, "eps": 10.0})
    res = PSX(_client(t)).quote("MTL")
    assert res.status == "ok" and t.calls == 3


def test_client_circuit_breaker_opens():
    c = _client(DeadT())
    clk = itertools.count(0, 1)
    c._clock = lambda: next(clk)  # deterministic monotonic clock
    psx = PSX(c)
    for _ in range(4):
        psx.quote("X")
    assert c._breaker_open("PSX.Quote")


def test_psx_unit_labels_per_share(millat_store):
    res = PSX(_client(FakeT({"price": 500.0, "eps": 40.0}))).quote("MTL")
    assert {i.citations[0].locator["field"] for i in res.items} == {"price", "eps"}
    assert all(i.unit == "PKR/share" for i in res.items)


# --- 4.2/4.5 valuation intent ---

def test_valuation_pe(millat_store):
    psx = PSX(_client(FakeT({"price": 1234.5, "eps": 95.0})))
    eng = FinancialIntelligenceEngine(millat_store, external=ExternalSources(psx=psx))
    r = eng.answer("P/E for MTL")
    pe = next(c for c in r.calculations if c.formula_id == "pe_ratio")
    assert round(pe.value, 2) == round(1234.5 / 95.0, 2)
    assert "P/E" in r.direct_answer
    assert any("PSX" in s for s in r.evidence_used)


def test_ev_ebitda_helper_math():
    from app.engines.fie.fie import ev_over_ebitda
    # mktcap 100k, net_debt 30k -> EV 130k / EBITDA 20k = 6.5
    assert ev_over_ebitda(100, 1000, 20000, 40000, 10000)["ev_ebitda"] == 6.5
    assert ev_over_ebitda(100, None, 20000, 40000, 10000) is None  # no shares -> absent
    assert ev_over_ebitda(100, 1000, 0, 40000, 10000) is None       # no EBITDA -> absent
    assert ev_over_ebitda(100, 1000, 20000, 40000, None)["net_debt"] == 40000  # cash None ok


def test_valuation_degrades_without_shares(millat_store):
    # workbook has no share count -> EV/EBITDA silently absent, P/E still computed (no harm)
    psx = PSX(_client(FakeT({"price": 1234.5, "eps": 95.0})))
    eng = FinancialIntelligenceEngine(millat_store, external=ExternalSources(psx=psx))
    r = eng.answer("valuation for MTL")
    fids = {c.formula_id for c in r.calculations}
    assert "pe_ratio" in fids and "ev_ebitda" not in fids


def test_ev_ebitda_query_routes_to_valuation():
    assert understanding.build_frame("EV/EBITDA for MTL").intent == "valuation"
    assert understanding.build_frame("price to book for lucky").intent == "valuation"


# --- 4.5 peer comparison (multi-workbook, internal) ---

def test_peer_comparison(millat_store, lucky_store):
    ext = ExternalSources(peers={lucky_store.company: lucky_store})
    eng = FinancialIntelligenceEngine(millat_store, external=ext)
    r = eng.answer("current ratio MTL vs Lucky 2024")
    assert "Millat" in r.direct_answer and "Lucky" in r.direct_answer
    assert r.confidence.band in ("High", "Medium")


def test_peer_comparison_missing_peer_partial(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)  # no peers registered
    r = eng.answer("current ratio MTL vs Lucky 2024")
    assert any("coverage" in c for c in r.confidence.caps_applied)


# --- 4.4/4.5 forecast validation ---

def test_forecast_validation_with_forecast(millat_store):
    fc = ForecastRepo(overrides={("Millat Tractors Limited", "revenue", 2026): 60_000_000})
    eng = FinancialIntelligenceEngine(millat_store, external=ExternalSources(forecast=fc))
    r = eng.answer("is my 2026 revenue forecast for MTL still valid?")
    assert "forecast" in r.direct_answer.lower()
    assert "52,108,997" in r.direct_answer  # latest actual FY2025 shown


# --- 4.6 graceful degradation ---

def test_degradation_forecast_missing_uses_internal(millat_store):
    # no forecast repo -> degrade, but still report internal latest actual at <= Medium
    eng = FinancialIntelligenceEngine(millat_store, external=ExternalSources())
    r = eng.answer("is my 2026 revenue forecast for MTL still valid?")
    assert r.confidence.band == "Medium"
    assert any("degraded" in c for c in r.confidence.caps_applied)
    assert "52,108,997" in r.direct_answer  # internal data still surfaced


def test_degradation_psx_down(millat_store):
    psx = PSX(_client(DeadT()))
    eng = FinancialIntelligenceEngine(millat_store, external=ExternalSources(psx=psx))
    r = eng.answer("P/E for MTL")
    assert r.calculations == []
    assert "Internal financials remain available" in r.direct_answer
    assert r.confidence.band == "Low"  # pure-external metric, no internal fallback


# --- understanding routes the new intents ---

@pytest.mark.parametrize("q,intent", [
    ("current ratio MTL vs Lucky 2024", "peer_comparison"),
    ("P/E for MTL", "valuation"),
    ("is the 2026 forecast on track", "forecast_validation"),
    ("latest news on MTL", "news_impact"),
])
def test_intent_routing(q, intent):
    assert understanding.build_frame(q).intent == intent
