"""Phase 2 — deterministic core: registry, insights, conflicts, confidence."""

import pytest

from app.engines.fie import FinancialIntelligenceEngine
from app.engines.fie.calc import CalcEngine
from app.engines.fie.conflicts import ConflictResolver
from app.engines.fie.confidence import ConfidenceScorer
from app.engines.fie.insights import InsightSelector
from app.engines.fie.models import CalcResult, Conflict, QueryFrame


# --- 2.1/2.2 formula registry + engine ---

@pytest.mark.parametrize("formula,year,unit", [
    ("current_ratio", 2024, "x"),
    ("gross_margin", 2025, "percent"),
    ("debt_to_equity", 2024, "x"),
    ("revenue_growth", 2024, "percent"),
])
def test_registry_formulas_compute(millat_store, formula, year, unit):
    r = CalcEngine(millat_store).evaluate(formula, year)
    assert r.value is not None
    assert r.unit == unit
    assert r.inputs and r.expression


def test_gross_margin_ties_to_face_truth(millat_store):
    # gross_profit 13,867,091 / revenue 52,108,997 = 0.2661
    r = CalcEngine(millat_store).evaluate("gross_margin", 2025)
    assert round(r.value, 4) == 0.2661


# --- 2.3 insight selection + year->confidence resolution ---

def _ins(iid, year, area, conf, takeaway="x"):
    return {"insight_id": iid, "year": year, "area": area, "confidence": conf,
            "takeaway": takeaway, "source_section": "S", "page": 1}


def test_insight_resolution_year_then_confidence():
    sel = InsightSelector(mode="year_then_confidence")
    recs = [_ins("A", 2023, "Margin", 0.99), _ins("B", 2025, "Margin", 0.70)]
    res = sel.resolve_conflicts(recs)
    assert res[0]["winner"]["insight_id"] == "B"  # newer wins despite lower confidence


def test_insight_resolution_blended_can_flip():
    sel = InsightSelector(mode="blended", alpha=0.5)
    recs = [_ins("A", 2021, "Margin", 1.0), _ins("B", 2022, "Margin", 0.10)]
    res = sel.resolve_conflicts(recs)
    # alpha=0.5: A score=0.5*0+0.5*1.0=0.5 ; B=0.5*1+0.5*0.1=0.55 -> B wins (barely)
    assert res[0]["winner"]["insight_id"] == "B"
    sel2 = InsightSelector(mode="blended", alpha=0.2)  # weight confidence more
    res2 = sel2.resolve_conflicts(recs)
    assert res2[0]["winner"]["insight_id"] == "A"  # high-confidence older wins


def test_insight_relevance_filter():
    sel = InsightSelector()
    frame = QueryFrame(raw_query="key risks", intent="risk_assessment")
    recs = [_ins("A", 2025, "Margin risk", 0.9, "inflation pressures margins"),
            _ins("B", 2025, "Workforce", 0.9, "headcount unchanged")]
    chosen = sel.select(frame, recs, min_relevance=0.5)
    assert chosen and chosen[0]["insight_id"] == "A"


# --- 2.4/2.5 conflicts ---

def test_restatement_detection_real(millat_store):
    # PL3 (expenses) FY2022 line items were reported differently across the 2022/2023
    # reports -> a restatement the detector must surface.
    from app.engines.fie.models import FactRef
    cr = ConflictResolver(millat_store)
    probe = FactRef(company=millat_store.company, metric="administrative_expenses",
                    label="Administrative expenses", year=2022, value=1.0,
                    statement="pl", level="headline", sheet="P&L", cell="X1")
    c = cr.detect_restatement(probe)
    assert c is not None and c.type == "restatement" and c.year == 2022


def test_insight_conflict_emitted():
    cr_resolver = None  # detect_insight_conflicts needs no store interaction
    from app.engines.fie.conflicts import ConflictResolver as CR
    res = [{"area": "Margin", "winner": _ins("B", 2025, "Margin", 0.7),
            "superseded": [_ins("A", 2023, "Margin", 0.99)],
            "rationale": "newest"}]
    conflicts = CR.__new__(CR).detect_insight_conflicts(res)
    assert len(conflicts) == 1 and conflicts[0].type == "insight_vs_insight"


# --- 2.6 confidence (no financial cap) ---

def test_confidence_no_financial_cap_high_when_cited(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)
    r = eng.answer("current ratio for MTL 2024")
    assert r.confidence.band == "High"
    # no cap referencing financial mismatch / reconciliation
    assert not any("reconcil" in c.lower() or "mismatch" in c.lower()
                   for c in r.confidence.caps_applied)


def test_confidence_unresolved_conflict_caps_medium():
    scorer = ConfidenceScorer()
    calc = CalcResult(formula_id="x", value=1.0, confidence="High")
    conflict = Conflict(type="cross_api", topic="eps", resolved=False)
    rep = scorer.score(evidence=[], calcs=[calc], conflicts=[conflict], selected_insights=[])
    assert rep.band in ("Medium", "Low")
    assert any("unresolved" in c for c in rep.caps_applied)


# --- end-to-end risk_assessment ---

def test_risk_assessment_selects_and_resolves(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)
    r = eng.answer("What are the key risks for MTL?")
    assert "risk" in r.direct_answer.lower()
    assert r.key_findings  # insight takeaways with citations
    assert all("[" in f for f in r.key_findings)
