"""Min-weakest-link confidence composition: the score is min over named components,
and the binding (lowest) component is reported as limited_by."""

from app.engines.fie.confidence import ConfidenceScorer
from app.engines.fie.models import CalcResult, Citation, Conflict, EvidenceItem


def _calc(conf="High"):
    return CalcResult(formula_id="current_ratio", value=1.5, confidence=conf,
                      citations=[Citation(ref_id="C1", kind="financial", display="a",
                                          locator={"report_file": "r", "page": 1}),
                                 Citation(ref_id="C2", kind="financial", display="b",
                                          locator={"report_file": "r", "page": 2})])


def test_score_is_min_of_components_and_names_binding():
    # High base calc + an unresolved conflict ceiling (0.6) -> min binds at 0.6
    rep = ConfidenceScorer().score(
        evidence=[], calcs=[_calc("High")],
        conflicts=[Conflict(type="cross_api", topic="x", resolved=False)],
        selected_insights=[])
    assert rep.components                                  # decomposed
    assert rep.score == round(min(c["value"] for c in rep.components), 3)
    assert rep.score == 0.6 and rep.band == "Medium"
    assert "unresolved" in rep.limited_by                  # the weakest link is named
    # the legacy caps string is preserved
    assert any("unresolved" in c for c in rep.caps_applied)


def test_clean_answer_binds_on_base_high():
    rep = ConfidenceScorer().score(evidence=[], calcs=[_calc("High")],
                                   conflicts=[], selected_insights=[])
    assert rep.band == "High" and rep.score == 0.9
    assert rep.limited_by == "financial inputs sourced from the workbook (authoritative)"
    assert rep.components[0]["name"] == "evidence_quality"


def test_no_evidence_low_with_component():
    rep = ConfidenceScorer().score(evidence=[], calcs=[], conflicts=[], selected_insights=[])
    assert rep.band == "Low" and rep.score == 0.3
    assert rep.limited_by and rep.components[0]["value"] == 0.3


def test_lowest_of_multiple_ceilings_binds():
    # news-only (non_authoritative) + degraded: both Medium ceilings; base 0.9 -> 0.6
    news = EvidenceItem(claim="n", kind="external", value=1.0,
                        citations=[Citation(ref_id="C1", kind="external", display="d",
                                            locator={"source": "WSJ"})])
    news.role = "non_authoritative"
    rep = ConfidenceScorer().score(evidence=[news], calcs=[], conflicts=[],
                                   selected_insights=[], degraded=True)
    assert rep.score == 0.6
    assert {"degraded", "non_authoritative"} <= {c["name"] for c in rep.components}
