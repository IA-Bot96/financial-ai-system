"""Upgrades from the concern review: multi-year/trend, registry, insight conflicts."""

import pytest

from app.engines.fie import FinancialIntelligenceEngine
from app.engines.fie.calc import CalcEngine
from app.engines.fie.insights import InsightSelector
from app.engines.fie.models import QueryFrame


# --- Q2: temporal range parsing + trend intent ---

def test_trend_explicit_range(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)
    r = eng.answer("revenue trend for MTL 2022 to 2025")
    assert "2022" in r.direct_answer.lower() or "FY2022" in r.direct_answer
    # series matches the requested span (4 yrs) and each point is cited
    assert len(r.key_findings) == 4
    assert all("[" in f for f in r.key_findings)


def test_trend_window(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)
    r = eng.answer("revenue over the last 3 years for MTL")
    assert len(r.key_findings) == 3
    assert "CAGR" in r.direct_answer


# --- aggregation operator: "average increase" + bare "assets" alias + caveat ---

def test_average_increase_reports_growth_abs_and_cagr(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)
    r = eng.answer("average increase in total assets 2022 to 2025")
    d = r.direct_answer
    assert "average annual growth" in d
    assert "average absolute increase" in d
    assert "CAGR" in d


def test_flagged_year_surfaces_caveat(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)
    r = eng.answer("average increase in assets from 2021 - 2025")
    assert "NO_FACE_TRUTH" in r.supporting_analysis  # FY2021 caveat
    assert r.coverage["partial_coverage"] is True


def test_clean_span_has_no_caveat(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)
    r = eng.answer("average increase in total assets 2022 to 2025")
    assert r.supporting_analysis == ""  # 2022-2025 are 'ok' in the ledger
    assert r.coverage["partial_coverage"] is False


# --- Q6: new registry formulas compute from existing metrics ---

@pytest.mark.parametrize("formula", [
    "cash_ratio", "debt_to_assets", "equity_multiplier", "asset_turnover",
])
def test_new_formulas_compute(millat_store, formula):
    r = CalcEngine(millat_store).evaluate(formula, 2024)
    assert r.value is not None and r.inputs


def test_debt_to_assets_value(millat_store):
    # (NCL 2,160,981 + CL 19,759,295) / total_assets 32,873,428
    r = CalcEngine(millat_store).evaluate("debt_to_assets", 2024)
    assert round(r.value, 3) == round((2160981 + 19759295) / 32873428, 3)


# --- Q4/Q5: ambiguous insight conflicts ---

def _ins(iid, year, area, conf, takeaway="x"):
    return {"insight_id": iid, "year": year, "area": area, "confidence": conf,
            "takeaway": takeaway, "source_section": "S", "page": 1}


def test_close_call_is_ambiguous_keep_both_no_llm():
    sel = InsightSelector()  # no LLM
    recs = [_ins("A", 2025, "Margin", 0.91, "margins improving"),
            _ins("B", 2025, "Margin", 0.90, "margins deteriorating")]
    res = sel.resolve_conflicts(recs)
    assert res[0]["ambiguous"] is True
    assert res[0]["decision"] == "keep_both"


def test_keep_both_retains_both_and_marks_unresolved():
    sel = InsightSelector()
    recs = [_ins("A", 2025, "Margin", 0.91), _ins("B", 2025, "Margin", 0.90)]
    chosen, resolutions = sel.select_and_resolve(
        QueryFrame(raw_query="margin risk", intent="risk_assessment"), recs,
        min_relevance=0.0)
    ids = {c["insight_id"] for c in chosen}
    assert ids == {"A", "B"}  # both retained


def test_clear_winner_is_resolved_and_supersedes():
    sel = InsightSelector()
    recs = [_ins("A", 2023, "Margin", 0.95), _ins("B", 2025, "Margin", 0.70)]
    chosen, resolutions = sel.select_and_resolve(
        QueryFrame(raw_query="margin", intent="risk_assessment"), recs, min_relevance=0.0)
    assert resolutions[0]["decision"] == "pick"
    assert {c["insight_id"] for c in chosen} == {"B"}  # newer wins, A superseded


class _PickLLM:
    """Adjudicator stub that resolves an ambiguous tie to a specific winner."""
    def __init__(self, winner_id):
        self.winner_id = winner_id
    def complete_json(self, system, user, schema):
        return {"contradict": True, "keep_both": False,
                "winner_id": self.winner_id, "reason": "more credible source"}
    def complete_text(self, system, user):
        return None


def test_llm_adjudicates_ambiguous_to_pick():
    sel = InsightSelector(llm=_PickLLM("B"))
    recs = [_ins("A", 2025, "Margin", 0.91), _ins("B", 2025, "Margin", 0.90)]
    chosen, resolutions = sel.select_and_resolve(
        QueryFrame(raw_query="margin", intent="risk_assessment"), recs, min_relevance=0.0)
    assert resolutions[0]["decision"] == "pick"
    assert resolutions[0]["winner"]["insight_id"] == "B"
    assert {c["insight_id"] for c in chosen} == {"B"}  # adjudicated -> A dropped
