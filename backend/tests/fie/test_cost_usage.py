"""Per-query token usage + cost assembly, and the controller's context/hint helpers.

All fixture-free: exercises the pure logic added this session (LLM usage capture, pricing.usage_cost
delta, controller._merge_hints / _recent_context) without needing a workbook."""

from app.engines.fie import controller, pricing
from app.engines.fie.llm import NullLLM, OpenAILLM


# ── LLM usage capture (llm.py) ────────────────────────────────────────────────
class _ChatUsage:        # Chat Completions shape
    prompt_tokens = 100
    completion_tokens = 20


class _RespUsage:        # Responses API shape (web_search)
    input_tokens = 50
    output_tokens = 5


def test_record_usage_chat_completions_shape():
    llm = OpenAILLM(model="gpt-5.4-mini")   # no network until a call is made
    llm._record_usage(_ChatUsage())
    s = llm.usage_snapshot()
    assert s["prompt_tokens"] == 100 and s["completion_tokens"] == 20 and s["calls"] == 1


def test_record_usage_responses_api_shape():
    llm = OpenAILLM(model="gpt-5.4-mini")
    llm._record_usage(_RespUsage())          # input_tokens/output_tokens normalize
    s = llm.usage_snapshot()
    assert s["prompt_tokens"] == 50 and s["completion_tokens"] == 5 and s["calls"] == 1


def test_record_usage_none_is_safe():
    llm = OpenAILLM(model="gpt-5.4-mini")
    llm._record_usage(None)
    assert llm.usage_snapshot()["calls"] == 0


# ── per-query cost assembly (pricing.usage_cost) ──────────────────────────────
class _FakeLLM:
    def __init__(self, model="gpt-5.4-mini"):
        self.model = model
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "cached_calls": 0}


def test_usage_cost_bills_the_delta():
    llm = _FakeLLM()
    before = pricing.usage_snapshot(llm)
    llm.usage.update(prompt_tokens=8_000, completion_tokens=200, calls=2)
    uc = pricing.usage_cost(llm, before)
    assert uc is not None
    assert uc.prompt_tokens == 8_000 and uc.completion_tokens == 200
    assert uc.api_calls == 2 and uc.total_tokens == 8_200
    assert uc.total_usd > 0 and uc.source == "estimated"


def test_usage_cost_none_for_null_llm():
    assert pricing.usage_cost(NullLLM(), {}) is None


def test_usage_cost_none_when_llm_never_called():
    llm = _FakeLLM()
    before = pricing.usage_snapshot(llm)
    assert pricing.usage_cost(llm, before) is None   # zero delta -> no chip


def test_usage_cost_cached_only_yields_zero_dollar_chip():
    llm = _FakeLLM()
    before = pricing.usage_snapshot(llm)
    llm.usage.update(cached_calls=3)   # all served from cache, no tokens billed
    uc = pricing.usage_cost(llm, before)
    assert uc is not None and uc.total_usd == 0.0
    assert uc.cached_calls == 3 and uc.api_calls == 0


# ── controller hint merge ─────────────────────────────────────────────────────
def test_merge_hints_earlier_source_wins():
    out = controller._merge_hints({"company": "Millat", "sector": None},
                                  {"company": "unknown", "sector": "AUTOMOBILE ASSEMBLER"})
    assert out["company"] == "Millat"                  # earlier real value wins over 'unknown'
    assert out["sector"] == "AUTOMOBILE ASSEMBLER"     # filled from the second source


def test_merge_hints_drops_placeholders_entirely():
    out = controller._merge_hints({"company": "unknown"}, {"company": "n/a"})
    assert "company" not in out


# ── controller recent-context (follow-up resolution input) ────────────────────
def test_recent_context_surfaces_resolved_and_answer():
    hist = [
        {"role": "user", "text": "revenue in 2024?"},
        {"role": "assistant", "text": "Revenue in 2024 was 91,534,501.",
         "frame": {"metrics": ["revenue"], "year": 2024}},
    ]
    rc = controller._recent_context(hist)
    asst = [e for e in rc if e["role"] == "assistant"][0]
    assert asst["resolved"] == {"metrics": ["revenue"], "year": 2024}
    assert "91,534,501" in asst["answer"]


def test_recent_context_keeps_non_metric_answer():
    # a sector/list answer resolves to no metric/formula/year, but the answer snippet must survive
    # so a referential follow-up ('names?') has a referent (the bug fixed earlier this session).
    hist = [{"role": "assistant", "text": "The AUTOMOBILE ASSEMBLER sector has 13 companies.",
             "frame": {}}]
    rc = controller._recent_context(hist)
    assert rc and rc[0].get("answer") and "13 companies" in rc[0]["answer"]
