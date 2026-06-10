"""P2 polish: proactive citation logging (#15), LLM response cache (#17),
and the FIE per-query log wording (#18)."""

import logging
import os

import pytest

from app.engines.fie import FinancialFactStore, FinancialIntelligenceEngine
from app.engines.fie.llm import OpenAILLM


# --- #17: OpenAILLM response cache -----------------------------------------
class _FakeOpenAIClient:
    def __init__(self):
        self.calls = 0
        self.chat = self  # so .chat.completions.create resolves
        self.completions = self
    def create(self, **kw):
        self.calls += 1
        class _Msg:  # minimal shape
            content = '{"ok": 1}'
        class _Choice:
            message = _Msg()
        class _Resp:
            choices = [_Choice()]
        return _Resp()


def test_llm_caches_identical_json_prompts():
    llm = OpenAILLM(api_key="x")
    fake = _FakeOpenAIClient()
    llm._client = fake                            # inject fake; skip real OpenAI
    a = llm.complete_json("sys", "user", {"type": "object"})
    b = llm.complete_json("sys", "user", {"type": "object"})
    assert a == b == {"ok": 1}
    assert fake.calls == 1                         # second call served from cache


def test_llm_does_not_cache_failures():
    llm = OpenAILLM(api_key="x")
    class _Boom:
        chat = completions = None
    # force an exception path: no client wiring -> _ensure imports real openai; instead
    # monkeypatch _ensure to raise so complete_json returns None and nothing is cached
    llm._ensure = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    assert llm.complete_json("s", "u", {}) is None
    assert llm._cache == {}                         # failures are not cached (ret​ryable)


# --- #15 + #18: proactive citation log + per-query log wording -------------
_WB = os.path.join("storage", "outputs", "millat_filled_fixed.xlsx")


@pytest.mark.skipif(not os.path.exists(_WB), reason="workbook absent")
def test_uncited_valued_evidence_is_logged(caplog):
    from app.engines.fie import response as R
    from app.engines.fie.models import EvidenceItem, QueryFrame
    frame = QueryFrame(raw_query="x", intent="metric_lookup", company="MTL")
    ev = [EvidenceItem(claim="revenue = 100", value=100.0, kind="statement")]  # no citations
    with caplog.at_level(logging.WARNING, logger="app.engines.fie"):
        R.render(frame, ev, [], [], None)
    assert any("lack a citation" in r.message for r in caplog.records)


def test_per_query_log_wording():
    import inspect
    from app.core import logging as L
    src = inspect.getsource(L.per_query_log)
    assert "FIE query" in src and "trace" in src
