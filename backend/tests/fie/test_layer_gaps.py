"""Remaining layer gaps now closed: L6a semantic conflicts, L2 LLM source-assist,
L3b POST + date-window multi-call."""

import pytest

from app.engines.fie import (
    ExternalSources,
    FinancialIntelligenceEngine,
    PSXAnnouncements,
    SECPNotices,
)
from app.engines.fie.apis import ApiClient, monthly_windows


# ---------- stubs ----------

class _PostT:
    def __init__(self):
        self.posts = 0
    def get(self, u, p, t):
        raise AssertionError("expected POST")
    def post(self, u, body, t, content_type="json"):
        self.posts += 1
        return {"items": [{"title": f"news {body.get('date_from')}", "date": body.get("date_to")}]}


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
