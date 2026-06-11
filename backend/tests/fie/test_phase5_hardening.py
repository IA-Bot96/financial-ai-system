"""Phase 5 — hardening: trace/replay, eval harness, coverage, API route."""

import pytest

from app.engines.fie import FinancialIntelligenceEngine
from app.engines.fie.trace import TraceStore


# --- 5.1 trace & replay ---

def test_trace_persist_load_roundtrip(millat_store, tmp_path):
    eng = FinancialIntelligenceEngine(millat_store, trace_id_factory=lambda: "t_fixed")
    resp, trace = eng.answer_with_trace("current ratio for MTL 2024")
    store = TraceStore(str(tmp_path))
    path = store.persist(trace)
    loaded = store.load("t_fixed")
    assert loaded.query == "current ratio for MTL 2024"
    assert loaded.frame.intent == "ratio_analysis"
    assert loaded.response.calculations[0].value == resp.calculations[0].value
    assert loaded.evidence  # full evidence captured


def test_replay_is_deterministic(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)
    a = eng.answer("debt to equity for millat 2024")
    b = eng.answer("debt to equity for millat 2024")
    assert a.direct_answer == b.direct_answer
    assert a.calculations[0].value == b.calculations[0].value


# --- 5.4 observability / coverage ---

def test_coverage_surfaced_on_response(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)
    r = eng.answer("what are the key risks for MTL?")
    assert "dropped_insights" in r.coverage
    assert "superseded_insights" in r.coverage
    assert r.coverage["dropped_insights"] >= 0


def test_coverage_flags_degradation(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)  # no external
    r = eng.answer("is my 2026 revenue forecast for MTL still valid?")
    assert r.coverage["degraded"] is True


@pytest.fixture(scope="module")
def lucky_store(lucky_path):
    from app.engines.fie import FinancialFactStore
    return FinancialFactStore.from_workbook(lucky_path)


# --- 5.2 API route ---

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_api_answer_ratio(client):
    resp = client.post("/api/fie/answer",
                       json={"query": "current ratio for MTL 2024"})
    assert resp.status_code == 200
    body = resp.json()
    assert "1.24x" in body["direct_answer"]
    assert body["citations"]
    assert body["confidence"]["band"] in ("High", "Medium", "Low")


def test_api_peer_comparison(client):
    resp = client.post("/api/fie/answer",
                       json={"query": "current ratio MTL vs Lucky 2024"})
    assert resp.status_code == 200
    assert "Lucky" in resp.json()["direct_answer"]


def test_api_companies(client):
    resp = client.get("/api/fie/companies")
    assert resp.status_code == 200
    assert "Millat Tractors Limited" in resp.json()["companies"]


def test_api_unknown_company_404(client):
    resp = client.post("/api/fie/answer",
                       json={"query": "revenue 2024", "company": "Nonexistent Co"})
    assert resp.status_code == 404
