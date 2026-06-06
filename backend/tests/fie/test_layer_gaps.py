"""Remaining layer gaps now closed: L6a semantic conflicts, L2 LLM source-assist,
L3b POST + date-window multi-call."""

import pytest

from app.engines.fie import (
    ExternalSources,
    FinancialIntelligenceEngine,
    PSXAnnouncements,
    SECPNotices,
)
from app.engines.fie import planner, understanding
from app.engines.fie.apis import ApiClient, monthly_windows
from app.engines.fie.conflicts import ConflictResolver


# ---------- stubs ----------

class _PostT:
    def __init__(self):
        self.posts = 0
    def get(self, u, p, t):
        raise AssertionError("expected POST")
    def post(self, u, body, t, content_type="json"):
        self.posts += 1
        return {"items": [{"title": f"news {body.get('date_from')}", "date": body.get("date_to")}]}


class _SrcLLM:
    def complete_json(self, s, u, schema):
        return {"sources": ["psx", "not_a_real_source"]}
    def complete_text(self, s, u):
        return None


class _SemLLM:
    def __init__(self, pairs):
        self._pairs = pairs
    def complete_json(self, s, u, schema):
        return {"pairs": self._pairs}
    def complete_text(self, s, u):
        return None


def _client(t):
    return ApiClient(t, sleep=lambda s: None, now=lambda: "2026-06-06")


# ---------- L3b: POST + multi-call date window ----------

def test_monthly_windows_count_and_order():
    w = monthly_windows("2026-06-06", n=3)
    assert len(w) == 3
    assert w[0]["date_to"] == "2026-06-06"        # most recent first
    assert w[1]["date_to"] == w[0]["date_from"]   # contiguous, stepping back


def test_announcements_post_three_windows():
    t = _PostT()
    res = PSXAnnouncements(_client(t)).recent("MTL", anchor_date="2026-06-06", months=3)
    assert t.posts == 3 and len(res.items) == 3
    assert res.items[0].kind == "external" and res.items[0].citations


def test_announcements_single_call_without_anchor():
    t = _PostT()
    PSXAnnouncements(_client(t)).recent("MTL")  # no anchor -> 1 undated call
    assert t.posts == 1


def test_secp_uses_type_b():
    assert SECPNotices(_client(_PostT())).spec.request_body["type"] == "B"
    assert PSXAnnouncements(_client(_PostT())).spec.request_body["type"] == "C"


# ---------- L2: rule + LLM-assisted source selection ----------

def test_planner_rule_sources():
    f = understanding.build_frame("latest news on MTL")
    assert set(planner.plan(f).external_sources) == {"news", "psx_announcements"}


def test_planner_llm_augments_and_validates():
    f = understanding.build_frame("latest news on MTL")
    p = planner.plan(f, llm=_SrcLLM())
    assert "psx" in p.external_sources              # LLM-added
    assert "not_a_real_source" not in p.external_sources  # validated against catalog


def test_planner_no_llm_for_internal_intent():
    f = understanding.build_frame("current ratio MTL 2024")
    assert planner.plan(f, llm=_SrcLLM()).external_sources == []  # internal -> no sources


# ---------- L6a: cross-Area semantic contradiction ----------

def _ins(iid, area, year, takeaway):
    return {"insight_id": iid, "area": area, "year": year, "takeaway": takeaway}


def test_semantic_contradiction_detected_cross_area():
    cr = ConflictResolver(None, llm=_SemLLM([{"a_id": "A", "b_id": "B",
                                              "subject": "margins", "reason": "opposite"}]))
    ins = [_ins("A", "Margins", 2025, "margins improving"),
           _ins("B", "Outlook", 2025, "margins deteriorating")]
    out = cr.detect_semantic_contradictions(ins)
    assert len(out) == 1
    assert out[0].type == "insight_vs_insight" and out[0].resolved is False
    assert out[0].topic == "margins"


def test_semantic_no_llm_returns_none():
    cr = ConflictResolver(None, llm=None)
    ins = [_ins("A", "X", 2025, "a"), _ins("B", "Y", 2025, "b")]
    assert cr.detect_semantic_contradictions(ins) == []


def test_semantic_ignores_unknown_ids():
    cr = ConflictResolver(None, llm=_SemLLM([{"a_id": "A", "b_id": "ZZZ"}]))
    ins = [_ins("A", "X", 2025, "a"), _ins("B", "Y", 2025, "b")]
    assert cr.detect_semantic_contradictions(ins) == []  # ZZZ not in set


def test_risk_assessment_wires_semantic_conflicts(millat_store):
    # with an LLM that flags the first two selected insights as contradictory,
    # the engine surfaces an unresolved conflict (caps confidence)
    class _PairFirstTwo:
        def complete_json(self, s, u, schema):
            import re
            ids = re.findall(r"\[(INS[^\]]+)\]", u)
            if len(ids) >= 2:
                return {"pairs": [{"a_id": ids[0], "b_id": ids[1], "subject": "x"}]}
            return {"pairs": []}
        def complete_text(self, s, u):
            return None
    eng = FinancialIntelligenceEngine(millat_store, llm=_PairFirstTwo())
    r = eng.answer("key risks for MTL")
    assert any(c.type == "insight_vs_insight" and not c.resolved for c in r.conflicts)
