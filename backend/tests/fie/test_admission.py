"""Numeric admission role model: source/kind -> role policy, the structural
invariant that external numbers can never be a baseline, and the confidence cap
for answers resting only on non-authoritative evidence."""

import pytest

from app.engines.fie import admission as adm
from app.engines.fie.admission import NumericRole as R
from app.engines.fie.confidence import ConfidenceScorer
from app.engines.fie.models import Citation, EvidenceItem


def _ext(source=None, *, provider=None, link=None, kind="external"):
    loc = {"source": source}
    if provider:
        loc["provider"] = provider
    if link:
        loc["link"] = link
    return EvidenceItem(claim="x", kind=kind, value=1.0,
                        citations=[Citation(ref_id="C1", kind="external", display="d", locator=loc)])


# --- classification --------------------------------------------------------
@pytest.mark.parametrize("kind", ["statement", "detail", "calc"])
def test_workbook_kinds_are_baseline(kind):
    assert adm.classify(None, kind) is R.BASELINE


def test_external_source_roles():
    assert adm.classify("PSX.CompanyPayouts", "external") is R.EVENT_FACT
    assert adm.classify("PSX.CompanyOverview", "external") is R.SUPPORTING
    assert adm.classify("PSX.Quote", "external") is R.SUPPORTING
    assert adm.classify("Macro.Indicators", "external") is R.SUPPORTING
    assert adm.classify("PSX.AnalysisReports", "external") is R.FORECAST_CONTEXT
    assert adm.classify("SECP.Notices", "external") is R.SUPPORTING
    assert adm.classify("something.unknown", "external") is R.NON_AUTHORITATIVE
    assert adm.classify(None, "external", is_news=True) is R.NON_AUTHORITATIVE
    assert adm.classify(None, "insight") is R.SUPPORTING


def test_classify_evidence_detects_news_vs_feed():
    news = _ext(source="WSJ", provider="marketaux", link="https://wsj/1")
    payout = _ext(source="PSX.CompanyPayouts")
    overview = _ext(source="PSX.CompanyOverview")
    assert adm.classify_evidence(news) is R.NON_AUTHORITATIVE
    assert adm.classify_evidence(payout) is R.EVENT_FACT
    assert adm.classify_evidence(overview) is R.SUPPORTING
    assert adm.is_baseline(EvidenceItem(claim="rev", kind="statement", value=1.0)) is True


# --- structural invariant: external -> baseline is unconstructable ---------
def test_admission_decision_invariant():
    d = adm.admit("PSX.CompanyOverview", "external")
    assert d.role is R.SUPPORTING and d.can_be_baseline is False
    b = adm.admit(None, "statement")
    assert b.role is R.BASELINE and b.can_be_baseline is True
    # cannot construct an external (non-baseline) datum flagged baseline-eligible
    with pytest.raises(Exception):
        adm.AdmissionDecision(role=R.SUPPORTING, can_be_baseline=True)
    with pytest.raises(Exception):
        adm.AdmissionDecision(role=R.NON_AUTHORITATIVE, can_be_baseline=True)


# --- audit -----------------------------------------------------------------
def test_audit_role_distribution():
    evs = [EvidenceItem(claim="rev", kind="statement", value=1.0),
           _ext(source="PSX.CompanyOverview"),
           _ext(source="WSJ", provider="marketaux", link="https://wsj/1")]
    for e in evs:
        e.role = adm.classify_evidence(e).value
    a = adm.audit(evs)
    assert a == {"baseline": 1, "supporting": 1, "non_authoritative": 1}


# --- confidence cap for non-authoritative-only answers ---------------------
def test_confidence_capped_for_news_only():
    news = [_ext(source="WSJ", provider="marketaux", link="https://wsj/1"),
            _ext(source="Reuters", provider="gnews", link="https://r/2")]
    for e in news:
        e.role = adm.classify_evidence(e).value          # 'non_authoritative'
    rep = ConfidenceScorer().score(evidence=news, calcs=[], conflicts=[],
                                   selected_insights=[])
    assert rep.band in ("Medium", "Low")                 # never High on news alone
    assert "non-authoritative sources only" in rep.caps_applied


def test_confidence_not_capped_when_baseline_present():
    base = EvidenceItem(claim="rev", kind="statement", value=100.0,
                        citations=[Citation(ref_id="C1", kind="financial", display="d",
                                            locator={"sheet": "PL", "cell": "C5", "page": 1})])
    base.role = adm.classify_evidence(base).value        # 'baseline'
    rep = ConfidenceScorer().score(evidence=[base], calcs=[], conflicts=[],
                                   selected_insights=[])
    assert "non-authoritative sources only" not in rep.caps_applied
