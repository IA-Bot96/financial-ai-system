"""Per-query token usage + cost assembly, and the controller's context/hint helpers.

All fixture-free: exercises the pure logic added this session (LLM usage capture, pricing.usage_cost
delta, controller._merge_hints / _recent_context) without needing a workbook."""

import pytest

from app.engines.fie import controller, pricing
from app.engines.fie.llm import NullLLM, OpenAILLM
from app.engines.fie.models import Citation, EvidenceItem


def _ev(claim: str) -> EvidenceItem:
    return EvidenceItem(claim=claim, value=None, unit=None, kind="external",
                        citations=[Citation(ref_id="C?", kind="external", display=claim,
                                            locator={"source": "web"})])


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


# ── controller recent-context: REAL Q&A, no lossy distillation ────────────────
def test_recent_context_is_real_qa_uniform_shape():
    # uniform {role, content} messages (redesign §10); timestamp added only when present
    hist = [
        {"role": "user", "text": "revenue in 2024?"},
        {"role": "assistant", "text": "Revenue in 2024 was 91,534,501.",
         "frame": {"metrics": ["revenue"], "year": 2024}},  # frame is ignored now
    ]
    rc = controller._recent_context(hist)
    assert rc == [
        {"role": "user", "content": "revenue in 2024?"},
        {"role": "assistant", "content": "Revenue in 2024 was 91,534,501."},
    ]  # verbatim conversation — no 'resolved' projection


def test_recent_context_includes_timestamp_when_present():
    rc = controller._recent_context([{"role": "user", "text": "hi", "timestamp": "2026-06-12T09:15:23Z"}])
    assert rc == [{"timestamp": "2026-06-12T09:15:23Z", "role": "user", "content": "hi"}]


def test_recent_context_keeps_full_list_answer():
    # the real answer survives (not truncated mid-list as the old 220-char snippet did)
    hist = [{"role": "assistant",
             "text": "The AUTOMOBILE ASSEMBLER sector has 13 companies: AGTL, ATLH, DFML, GAIL.",
             "frame": {}}]
    rc = controller._recent_context(hist)
    assert "13 companies" in rc[0]["content"] and "GAIL" in rc[0]["content"]


def test_recent_context_answer_capped_generously():
    rc = controller._recent_context([{"role": "assistant", "text": "A" * 1500, "frame": {}}])
    assert len(rc[0]["content"]) == 1000   # bounded, but far more than the old 220


# ── confidence band from the two verify gates (grade, don't reject) ───────────
@pytest.mark.parametrize("grounding,numbers,band", [
    (True, True, "High"),       # both gates pass
    (True, False, "Medium"),    # numbers gate fails
    (False, True, "Medium"),    # grounding gate fails
    (False, False, "Low"),      # both fail
])
def test_confidence_band_mapping(grounding, numbers, band):
    b, reasons = controller._confidence_band(grounding, numbers)
    assert b == band
    # one reason per failing gate; none when both pass
    assert len(reasons) == (0 if grounding else 1) + (0 if numbers else 1)


# ── analysis-report year precedence: explicit > context > latest ──────────────
class _FakeStore:
    findata = None              # forces _recent_data_years to use `years`
    years = [2023, 2024, 2025]


class _FakeEngine:
    def __init__(self, hint_years):
        self.store = _FakeStore()
        self._hints = {"years": hint_years}


def test_report_year_context_wins_over_latest():
    from app.engines.fie import tools
    out = tools._default_report_years(_FakeEngine([2023]))
    assert out[0] == 2023                 # contextual year tried first
    assert 2025 in out and 2024 in out    # latest data years remain as fallbacks


def test_report_year_latest_when_no_context():
    from app.engines.fie import tools
    out = tools._default_report_years(_FakeEngine([]))
    assert out[0] == 2025                 # newest data year when the conversation gave none


# ── frame remembers the SUBJECT SET (companies[]) + tools[] ───────────────────
def test_frame_captures_offworkbook_subject_and_tool():
    plan = {"needs": [{"kind": "tool", "tool": "getCompanyOverview",
                       "args": {"company": "Systems Limited"}}],
            "hints": {"company": "Systems Limited"}}
    f = controller._frame("systems limited gp margin?", plan, "Millat Tractors Limited")
    assert f.companies == ["Systems Limited"]      # the subject SET (not the workbook company)
    assert f.company == "Systems Limited"          # primary mirrors companies[0]
    assert f.tools == ["getCompanyOverview"]


def test_frame_comparison_keeps_both_companies():
    plan = {"needs": [{"kind": "tool", "tool": "getCompanyOverview", "args": {"company": "Millat Tractors Limited"}},
                      {"kind": "tool", "tool": "getCompanyOverview", "args": {"company": "Lucky Cement"}}],
            "hints": {"company": "Millat Tractors Limited"}}
    f = controller._frame("millat vs lucky gp margin", plan, "Millat Tractors Limited")
    assert f.companies == ["Millat Tractors Limited", "Lucky Cement"]   # BOTH kept, in order
    assert f.tools == ["getCompanyOverview", "getCompanyOverview"]


def test_frame_keeps_workbook_company_when_subject_is_workbook():
    plan = {"needs": [{"kind": "metric", "metric": "revenue", "year": 2024}],
            "hints": {"company": "Millat Tractors Limited"}}
    f = controller._frame("revenue 2024?", plan, "Millat Tractors Limited")
    assert f.companies == ["Millat Tractors Limited"] and f.tools == [] and f.year == 2024


def test_frame_placeholder_company_falls_back_to_workbook():
    f = controller._frame("hi", {"needs": [], "hints": {"company": "unknown"}},
                          "Millat Tractors Limited")
    assert f.companies == ["Millat Tractors Limited"]


def test_frame_multiyear_populates_years_range():
    plan = {"needs": [{"kind": "metric", "metric": "revenue", "year": 2022},
                      {"kind": "metric", "metric": "revenue", "year": 2024}], "hints": {}}
    f = controller._frame("revenue 2022 and 2024", plan, "Millat Tractors Limited")
    assert f.years == [2022, 2024] and f.year == 2024   # range populated; operative = latest


# ── agentic compose loop: insufficient -> re-fetch more_needs -> re-compose ───
class _Rl:
    def dump(self, *a, **k): pass
    def phase(self, *a, **k): pass


class _LoopLLM:
    """Round 0: incomplete + asks for a tool need. Round 1: complete."""
    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        if self.calls == 1:
            return {"answer": "partial", "confidence": "high", "completeness": 0.3,
                    "more_needs": {"tools": [{"tool": "getCompanyOverview", "args": {"company": "AGTL"}}]}}
        return {"answer": "complete answer", "confidence": "high", "completeness": 1.0}


def test_compose_agentic_loop_refetches_then_finishes(monkeypatch):
    calls = {"fetch": 0}

    def fake_execute_need(engine, need, frame):
        calls["fetch"] += 1
        return ({"tool": need.get("tool"), "value": 1}, [], [])
    monkeypatch.setattr(controller, "execute_need", fake_execute_need)
    monkeypatch.setattr(controller.verify, "grounding_ok", lambda *a, **k: (True, None))
    monkeypatch.setattr(controller.verify, "numbers_ok", lambda *a, **k: True)
    monkeypatch.setattr(controller, "_web_enabled", lambda engine: True)

    valid = {"sheets": set(), "metrics": set(), "formulas": set(), "tools": {"getCompanyOverview"}}
    direct, source, cites, band, reasons, completeness = controller._compose(
        _LoopLLM(), object(), "gp margin of AGTL", [], [], None, [], [], _Rl(), valid)

    assert calls["fetch"] == 1            # the requested tool need WAS re-fetched
    assert direct == "complete answer"    # the round-1 (post-refetch) answer is returned
    assert band == "High" and completeness == 1.0


def test_compose_loop_dedups_repeated_need(monkeypatch):
    # if the composer keeps asking for the SAME need, it is fetched once, not every round
    fetches = {"n": 0}

    def fake_execute_need(engine, need, frame):
        fetches["n"] += 1
        return ({"tool": need.get("tool"), "value": 1}, [], [])

    class _StuckLLM:  # always incomplete, always asks for the same need
        def complete_json(self, system, user, schema):
            return {"answer": "x", "confidence": "high", "completeness": 0.1,
                    "more_needs": {"tools": [{"tool": "getCompanyOverview", "args": {"company": "AGTL"}}]}}
    monkeypatch.setattr(controller, "execute_need", fake_execute_need)
    monkeypatch.setattr(controller.verify, "grounding_ok", lambda *a, **k: (True, None))
    monkeypatch.setattr(controller.verify, "numbers_ok", lambda *a, **k: True)
    monkeypatch.setattr(controller, "_web_enabled", lambda engine: False)   # no terminal web in this test
    valid = {"sheets": set(), "metrics": set(), "formulas": set(), "tools": {"getCompanyOverview"}}
    controller._compose(_StuckLLM(), object(), "q", [], [], None, [], [], _Rl(), valid)
    assert fetches["n"] == 1   # deduped: the same need is not re-fetched across rounds


def test_compose_terminal_web_only_after_rule_based(monkeypatch):
    # composer stays incomplete with NO rule-based more_needs -> exactly one terminal web round
    webs = {"n": 0}

    class _NoRuleLLM:
        def complete_json(self, system, user, schema):
            return {"answer": "still partial", "confidence": "high", "completeness": 0.2,
                    "search_query": "psx auto sector net margin history", "more_needs": {}}

    def fake_web(engine, q, hints=None):
        webs["n"] += 1
        return ({"kind": "web", "n_sources": 1}, [], [])   # no evidence -> no recompose loop
    monkeypatch.setattr(controller.verify, "grounding_ok", lambda *a, **k: (True, None))
    monkeypatch.setattr(controller.verify, "numbers_ok", lambda *a, **k: True)
    monkeypatch.setattr(controller, "_web_enabled", lambda engine: True)
    import app.engines.fie.external as ext
    monkeypatch.setattr(ext, "web_search", fake_web)
    valid = {"sheets": set(), "metrics": set(), "formulas": set(), "tools": set()}
    controller._compose(_NoRuleLLM(), object(), "q", [], [], None, [], [], _Rl(), valid)
    assert webs["n"] == 1   # web fired exactly once, as the terminal step


def test_web_phase_escalates_to_llm_search_when_rulebased_not_enough(monkeypatch):
    # rule-based web returns sources but the answer stays incomplete -> the composer searches the
    # web ITSELF (complete_json_web); its citations are bound and its answer is used.
    rb = {"n": 0}
    llm_web = {"n": 0}

    def fake_web(engine, q, hints=None):
        rb["n"] += 1
        return ({"kind": "web", "n_sources": 1},
                [_ev("rule-based web source")], [])

    class _WebLLM:
        def __init__(self): self.calls = 0
        def complete_json(self, system, user, schema):
            self.calls += 1
            # round 0 (initial compose): incomplete, no rule-based more_needs -> go to web phase
            return {"answer": "partial", "confidence": "high", "completeness": 0.2, "more_needs": {}}
        def complete_json_web(self, system, user, schema):
            llm_web["n"] += 1
            return ({"answer": "answer with LLM web search", "confidence": "high", "completeness": 1.0},
                    [{"url": "https://x.com", "title": "X", "snippet": "found it"}])

    monkeypatch.setattr(controller.verify, "grounding_ok", lambda *a, **k: (True, None))
    monkeypatch.setattr(controller.verify, "numbers_ok", lambda *a, **k: True)
    monkeypatch.setattr(controller, "_web_enabled", lambda engine: True)
    import app.engines.fie.external as ext
    monkeypatch.setattr(ext, "web_search", fake_web)
    valid = {"sheets": set(), "metrics": set(), "formulas": set(), "tools": set()}
    direct, source, cites, band, reasons, completeness = controller._compose(
        _WebLLM(), object(), "q", [], [], None, [], [], _Rl(), valid)
    assert rb["n"] == 1 and llm_web["n"] == 1            # rule-based web first, THEN LLM web
    assert direct == "answer with LLM web search" and completeness == 1.0


def test_compose_payload_carries_available_menu(monkeypatch):
    # COMPOSE must receive `available` (sheet/metric/formula/tool names) so its more_needs use real
    # ids instead of guesses like 'balance_sheet'/'receivables'
    seen = {}

    class _CaptureLLM:
        def complete_json(self, system, user, schema):
            seen["user"] = user
            return {"answer": "ok", "confidence": "high", "completeness": 1.0}
    monkeypatch.setattr(controller.verify, "grounding_ok", lambda *a, **k: (True, None))
    monkeypatch.setattr(controller.verify, "numbers_ok", lambda *a, **k: True)
    available = {"sheets": {"Balance Sheet": ["total_assets", "trade_debts"]},
                 "formulas": ["gross_margin"], "tools": ["getCompanyOverview"]}
    controller._compose(_CaptureLLM(), object(), "q", [], [], None, [], [], _Rl(),
                        {"sheets": set(), "metrics": set(), "formulas": set(), "tools": set()}, available)
    assert "Balance Sheet" in seen["user"] and "trade_debts" in seen["user"]   # real ids in payload


def test_web_sources_to_evidence_are_cited():
    ev = controller._web_sources_to_evidence([{"url": "https://a.com", "title": "A", "snippet": "s"}])
    assert len(ev) == 1 and ev[0].citations[0].locator["url"] == "https://a.com"


def test_aggregate_is_rule_based_over_fetched_values():
    # engine computes the mean; values that appear in fetched data are used (safeguard)
    fetched = [{"tool": "getSectorAnalysisReport", "year": 2025, "net_margin_pct": 9.72},
               {"tool": "getSectorAnalysisReport", "year": 2024, "net_margin_pct": 8.38},
               {"tool": "getSectorAnalysisReport", "year": 2023, "net_margin_pct": 1.84}]
    need = {"kind": "aggregate", "op": "mean", "values": [9.72, 8.38, 1.84], "label": "avg", "unit": "%"}
    res, calc = controller._aggregate(need, fetched, [])
    assert round(res["value"], 2) == 6.65 and calc.formula_id == "aggregate"


@pytest.mark.parametrize("fid,metric", [
    ("operating_profit_growth", "operating_profit"),
    ("gross_profit_growth", "gross_profit"),
    ("pretax_profit_growth", "profit_before_tax"),
])
def test_growth_formulas_registered_and_canonical(fid, metric):
    from app.engines.fie.calc.registry import _SPECS
    spec = next((s for s in _SPECS if s.id == fid), None)
    assert spec is not None and spec.category == "growth" and str(spec.output_unit) == "percent"
    assert {i.metric for i in spec.inputs} == {metric}          # canonical metric id
    assert sorted(i.year_offset for i in spec.inputs) == [-1, 0]  # current + prior year (YoY)


def test_operating_profit_growth_arithmetic():
    import app.engines.fie.calc.registry as reg
    spec = next(s for s in reg._SPECS if s.id == "operating_profit_growth")
    v = reg.safe_eval(spec.expression, {"op_t": 18017751.0, "op_t1": 6707223.0})
    assert round(v, 3) == 1.686   # FY2024 op-profit growth vs FY2023


def test_plan_to_needs_maps_aggregate():
    plan = {"aggregate": [{"op": "mean", "values": [1, 2, 3], "label": "avg"}]}
    needs = controller._plan_to_needs(plan)
    assert needs == [{"kind": "aggregate", "op": "mean", "values": [1, 2, 3],
                      "label": "avg", "unit": None}]


@pytest.mark.parametrize("val,expected", [
    ("just a string", "just a string"),
    ({"question": "Did you mean X?"}, "Did you mean X?"),
    ({"text": "t"}, "t"),
    ({"unrelated": 1}, ""),       # dict with no known text field -> empty, not a crash
    (None, ""),
    (["a", "b"], "['a', 'b']"),   # list -> str(), never crashes downstream .strip()/[:n]
])
def test_text_coerces_mistyped_llm_fields(val, expected):
    # the LLM sometimes returns clarification/interpretation/confidence as a dict/list despite the
    # schema; _text must coerce safely so .strip()/[:n]/.lower() never raise (caused a 500)
    assert controller._text(val) == expected
    controller._text(val).strip()[:5].lower()   # the ops that previously crashed


def test_clarify_rule_and_schema_present():
    from app.engines.fie import schemas
    assert "CLARIFY" in schemas.PLAN_SYS and "recent_messages" in schemas.PLAN_SYS
    assert "clarification" in schemas.PLAN_SCHEMA["properties"]
    # source-scoped plan keys exist (redesign §12)
    for k in ("financial", "formulas", "tools", "insights", "forecast"):
        assert k in schemas.PLAN_SCHEMA["properties"]


# ── plan adapter: source-scoped plan -> internal needs[] (redesign §4/§12) ────
def test_plan_to_needs_financial_cross_products_years():
    plan = {"financial": [{"sheet": "P&L", "metrics": ["revenue", "gross_profit"]}],
            "years": [2023, 2024]}
    needs = controller._plan_to_needs(plan)
    metrics = {(n["metric"], n["year"]) for n in needs if n["kind"] == "metric"}
    assert metrics == {("revenue", 2023), ("revenue", 2024),
                       ("gross_profit", 2023), ("gross_profit", 2024)}


def test_plan_to_needs_formula_no_years_is_series():
    needs = controller._plan_to_needs({"formulas": ["gross_margin"]})
    assert needs == [{"kind": "formula", "formula": "gross_margin", "year": None}]


def test_plan_to_needs_fans_out_tools():
    plan = {"tools": [{"tool": "getCompanyOverview", "args": {"company": "AGTL"}},
                      {"tool": "getCompanyOverview", "args": {"company": "INDU"}}]}
    needs = controller._plan_to_needs(plan)
    assert [n["args"]["company"] for n in needs] == ["AGTL", "INDU"]


def test_plan_to_needs_drops_placeholder_tool_arg():
    plan = {"tools": [{"tool": "getCompanyOverview",
                       "args": {"company": "each company from the prior comparison set"}}]}
    assert controller._plan_to_needs(plan) == []   # D4(a): unresolved placeholder is dropped


def test_plan_to_needs_off_workbook_has_no_workbook_shape():
    # a tool-only plan produces only tool needs (no metric/formula collapse to the workbook)
    plan = {"tools": [{"tool": "getCompanyOverview", "args": {"company": "Lucky Cement"}}]}
    needs = controller._plan_to_needs(plan)
    assert all(n["kind"] == "tool" for n in needs) and len(needs) == 1


def test_subject_companies_handles_list_hint_company():
    # the planner sometimes returns the whole subject set as a list in hints.company (scalar by
    # schema) — _subject_companies must flatten it, not crash on .strip()
    out = controller._subject_companies([], {"company": ["AGTL", "ATLH", "INDU"]}, "Millat Tractors Limited")
    assert out == ["AGTL", "ATLH", "INDU"]


def test_frame_does_not_crash_on_list_hint_company():
    plan = {"needs": [{"kind": "tool", "tool": "getCompanyOverview", "args": {"company": "AGTL"}}],
            "hints": {"company": ["AGTL", "ATLH"], "sector": "AUTOMOBILE ASSEMBLER"}}
    f = controller._frame("gp margin of each", plan, "Millat Tractors Limited")
    assert "AGTL" in f.companies and f.company == "AGTL"   # subject set, not the workbook company


def test_plan_to_needs_compute_uses_its_own_years():
    plan = {"compute": [{"expression": "operating_profit/(total_assets-current_liabilities)",
                         "label": "ROIC", "years": [2024]}]}
    needs = controller._plan_to_needs(plan)
    assert needs == [{"kind": "compute",
                      "expression": "operating_profit/(total_assets-current_liabilities)",
                      "label": "ROIC", "year": 2024}]


def test_plan_to_needs_forecast_carries_growth():
    plan = {"forecast": [{"metric": "revenue", "year": 2026, "growth": 0.10}]}
    assert controller._plan_to_needs(plan) == [
        {"kind": "forecast", "metric": "revenue", "year": 2026, "growth": 0.10}]


def test_plan_to_needs_qualitative_and_audit_kinds():
    plan = {"insights": {"areas": ["risks"]}, "validation": {"metrics": ["total_assets"]},
            "edit_history": {}}
    kinds = {n["kind"] for n in controller._plan_to_needs(plan)}
    assert kinds == {"insights", "validation", "edit_history"}   # edit_history:{} still counts


def test_plan_to_needs_news_and_web():
    plan = {"news": [{"query": "MTL dividend"}], "web": [{"query": "tractor outlook"}]}
    needs = controller._plan_to_needs(plan)
    assert {n["kind"] for n in needs} == {"news", "web"}


def test_plan_to_needs_caps_total():
    plan = {"financial": [{"sheet": "P&L", "metrics": [f"m{i}" for i in range(40)]}]}
    assert len(controller._plan_to_needs(plan)) == controller._MAX_NEEDS


@pytest.mark.parametrize("phrase,bad", [
    ("each company from the prior comparison set", True),
    ("the above firms", True),
    ("those companies", True),
    ("same company as before", True),
    ("unknown", True),
    ("Millat Tractors Limited", False),
    ("MTL", False),
    ("CEMENT", False),
])
def test_is_placeholder_arg(phrase, bad):
    assert controller._is_placeholder_arg(phrase) is bad


def test_plan_to_needs_drops_hallucinated_ids_when_validated():
    valid = {"sheets": {"P&L"}, "metrics": {"revenue"},
             "formulas": {"gross_margin"}, "tools": {"getCompanyOverview"}}
    plan = {
        "financial": [{"sheet": "Made Up Sheet", "metrics": ["revenue"]},      # bad sheet -> drop
                      {"sheet": "P&L", "metrics": ["revenue", "not_a_metric"]}],  # bad metric -> drop
        "formulas": ["gross_margin", "fake_ratio"],                            # fake -> drop
        "tools": [{"tool": "getCompanyOverview", "args": {"company": "MTL"}},
                  {"tool": "getNonexistentTool", "args": {}}],                 # unknown tool -> drop
    }
    needs = controller._plan_to_needs(plan, valid)
    assert [n for n in needs if n["kind"] == "metric"] == [{"kind": "metric", "metric": "revenue", "year": None}]
    assert [n["formula"] for n in needs if n["kind"] == "formula"] == ["gross_margin"]
    assert [n["tool"] for n in needs if n["kind"] == "tool"] == ["getCompanyOverview"]
