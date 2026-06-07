"""FIE workbook session layer: ingest-once / answer-many / reload-busts, the no-reparse
regression guard, and upload validation. Engines are used, never modified."""

import os

import pytest
from fastapi.testclient import TestClient

from app.api.routes import sessions as S
from app.main import app

client = TestClient(app)

_WB = os.path.join("storage", "outputs", "millat_filled_fixed.xlsx")
_real = pytest.mark.skipif(not os.path.exists(_WB), reason="workbook absent")


def _upload(path=_WB):
    with open(path, "rb") as fh:
        return client.post("/api/fie/sessions",
                           files={"file": ("wb.xlsx", fh.read(),
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


# --- create / metadata ------------------------------------------------------
@_real
def test_create_session_returns_metadata():
    r = _upload()
    assert r.status_code == 200
    m = r.json()
    assert m["session_id"] and m["company"] and m["years"]
    assert any(s["editable"] for s in m["sheets"])          # at least one financial sheet
    assert any(not s["editable"] for s in m["sheets"])      # meta-sheets read-only
    assert isinstance(m["metrics"], list) and m["metrics"]
    client.delete(f"/api/fie/sessions/{m['session_id']}")


@_real
def test_answer_in_session_is_cited():
    sid = _upload().json()["session_id"]
    try:
        r = client.post(f"/api/fie/sessions/{sid}/answer",
                        json={"query": "current ratio for MTL 2024"})
        assert r.status_code == 200
        d = r.json()
        assert d["direct_answer"]
        import re
        assert all(re.search(r"\[C\d+\]", f) for f in d["key_findings"])
    finally:
        client.delete(f"/api/fie/sessions/{sid}")


# --- THE regression guard: answer must NOT re-parse the workbook -----------
@_real
def test_answer_never_calls_from_workbook(monkeypatch):
    calls = {"n": 0}
    orig = S.FinancialFactStore.from_workbook           # bound classmethod

    def _counting(path, **kw):
        calls["n"] += 1
        return orig(path, **kw)

    monkeypatch.setattr(S.FinancialFactStore, "from_workbook", staticmethod(_counting))

    sid = _upload().json()["session_id"]                # ingest #1
    assert calls["n"] == 1
    for _ in range(5):                                  # 5 questions, ZERO re-ingest
        client.post(f"/api/fie/sessions/{sid}/answer", json={"query": "revenue 2024"})
    assert calls["n"] == 1, "answer path must not call from_workbook"

    with open(_WB, "rb") as fh:                          # reload -> ingest #2
        client.post(f"/api/fie/sessions/{sid}/reload",
                    files={"file": ("wb.xlsx", fh.read(), "application/octet-stream")})
    assert calls["n"] == 2, "reload should re-ingest exactly once"
    client.delete(f"/api/fie/sessions/{sid}")


@_real
def test_series_returns_metric_values_per_year():
    sid = _upload().json()["session_id"]
    try:
        r = client.get(f"/api/fie/sessions/{sid}/series")
        assert r.status_code == 200
        body = r.json()
        assert body["years"] and isinstance(body["series"], dict)
        # a known headline metric resolves to a value for at least one year
        rev = body["series"].get("revenue") or {}
        assert any(v is not None for v in rev.values())
    finally:
        client.delete(f"/api/fie/sessions/{sid}")


@_real
def test_reload_replaces_resident_store():
    sid = _upload().json()["session_id"]
    before = S._SESSIONS[sid]["store"]
    with open(_WB, "rb") as fh:
        client.post(f"/api/fie/sessions/{sid}/reload",
                    files={"file": ("wb.xlsx", fh.read(), "application/octet-stream")})
    after = S._SESSIONS[sid]["store"]
    assert before is not after                           # store object replaced (cache-bust)
    client.delete(f"/api/fie/sessions/{sid}")


# --- errors / validation ----------------------------------------------------
def test_unknown_session_404():
    assert client.post("/api/fie/sessions/nope/answer",
                       json={"query": "x"}).status_code == 404
    assert client.get("/api/fie/sessions/nope").status_code == 404
    assert client.delete("/api/fie/sessions/nope").status_code == 404


def test_non_xlsx_upload_rejected():
    r = client.post("/api/fie/sessions",
                    files={"file": ("evil.exe", b"MZ\x00\x00not a workbook", "application/octet-stream")})
    assert r.status_code == 400


@_real
def test_empty_and_overlong_query_422():
    sid = _upload().json()["session_id"]
    try:
        assert client.post(f"/api/fie/sessions/{sid}/answer", json={"query": "   "}).status_code == 422
        assert client.post(f"/api/fie/sessions/{sid}/answer", json={"query": "x" * 5000}).status_code == 422
    finally:
        client.delete(f"/api/fie/sessions/{sid}")
