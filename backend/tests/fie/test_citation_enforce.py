"""Claim-level citation enforcement + precision-capping: uncitable claims are
dropped (never shipped uncited), an all-dropped findings answer degrades to
INSUFFICIENT_EVIDENCE, and the Response model rejects an uncited key finding."""

import pytest

from app.engines.fie import citation_enforce as ce
from app.engines.fie import citations as citations_mod
from app.engines.fie import response
from app.engines.fie.models import Citation, ConfidenceReport, EvidenceItem, QueryFrame, Response


def _cit(kind, loc, ref="C?"):
    return Citation(ref_id=ref, kind=kind, display="d", locator=loc)


# --- precision per citation kind ------------------------------------------
def test_citation_precision_levels():
    assert ce.citation_precision(_cit("financial", {"sheet": "BS", "cell": "C12"})) == "CELL"
    assert ce.citation_precision(_cit("financial", {"sheet": "BS"})) == "REF"
    assert ce.citation_precision(_cit("financial", {})) == "NONE"
    assert ce.citation_precision(_cit("insight", {"page": 10})) == "PAGE"
    assert ce.citation_precision(_cit("insight", {"source_section": "Risks", "year": 2025})) == "REF"
    assert ce.citation_precision(_cit("insight", {})) == "NONE"
    assert ce.citation_precision(_cit("external", {"link": "https://x/1", "source": "WSJ"})) == "REF"
    assert ce.citation_precision(_cit("external", {})) == "NONE"


def test_citation_ok_and_valid_refs():
    good = _cit("financial", {"sheet": "BS", "cell": "C12"}, ref="C1")
    weak = _cit("insight", {"source_section": "Risks", "year": 2025}, ref="C2")
    empty = _cit("external", {}, ref="C3")
    assert ce.citation_ok(good) and ce.citation_ok(weak) and not ce.citation_ok(empty)
    assert ce.valid_ref_ids([good, weak, empty]) == {"C1", "C2"}


# --- enforce_findings keep/drop -------------------------------------------
def test_enforce_findings():
    findings = ["Revenue rose [C1]", "Vague risk [C3]", "No handle here", "Margin up [—]"]
    kept, dropped = ce.enforce_findings(findings, valid={"C1", "C2"})
    assert kept == ["Revenue rose [C1]"]
    assert set(dropped) == {"Vague risk [C3]", "No handle here", "Margin up [—]"}


# --- bind propagates a real Cn to every (incl. de-duped) citation ----------
def test_bind_propagates_handles_no_stale_placeholder():
    a = EvidenceItem(claim="x", kind="insight",
                     citations=[_cit("insight", {"insight_id": "i1", "source_section": "Risks", "year": 2025})])
    # a second evidence pointing at the SAME locator (de-dup target)
    b = EvidenceItem(claim="x2", kind="insight",
                     citations=[_cit("insight", {"insight_id": "i1", "source_section": "Risks", "year": 2025})])
    cites, _ = citations_mod.bind([a, b], [])
    assert len(cites) == 1                                   # de-duped to one citation
    # both evidence items now resolve to the canonical handle (no stale 'C?')
    assert a.citations[0].ref_id == "C1" and b.citations[0].ref_id == "C1"


# --- render integration ----------------------------------------------------
def _risk_frame():
    return QueryFrame(raw_query="risks", intent="risk_assessment", company="MTL", year=2025)


def _conf():
    return ConfidenceReport(band="Medium", score=0.6)


def test_render_drops_uncitable_finding():
    good = EvidenceItem(claim="Coal price exposure", kind="insight",
                        citations=[_cit("insight", {"insight_id": "i1", "source_section": "Risks",
                                                    "page": 10, "year": 2025})])
    bad = EvidenceItem(claim="Unsourced worry", kind="insight",
                       citations=[_cit("insight", {})])      # empty locator -> NONE -> dropped
    cites, _ = citations_mod.bind([good, bad], [])
    resp = response.render(_risk_frame(), [good, bad], [], cites, _conf())
    joined = " | ".join(resp.key_findings)
    assert "Coal price exposure" in joined
    assert "Unsourced worry" not in joined                  # dropped, not shipped uncited
    assert resp.coverage.get("dropped_claims") == 1
    assert all("[C" in f for f in resp.key_findings)        # survivors all cited


def test_render_insufficient_evidence_off_ramp():
    bad = EvidenceItem(claim="Unsourced worry", kind="insight",
                       citations=[_cit("insight", {})])
    cites, _ = citations_mod.bind([bad], [])
    resp = response.render(_risk_frame(), [bad], [], cites, _conf())
    assert resp.key_findings == []
    assert "Insufficient citable evidence" in resp.direct_answer
    assert resp.confidence.band == "Low" and resp.confidence.score == 0.0
    assert resp.coverage.get("insufficient_evidence") is True


# --- model-boundary invariant ----------------------------------------------
def test_response_rejects_uncited_finding():
    with pytest.raises(Exception):
        Response(direct_answer="x", key_findings=["a claim with no handle"])


def test_response_accepts_cited_finding():
    r = Response(direct_answer="x", key_findings=["a cited claim [C1]"])
    assert r.key_findings == ["a cited claim [C1]"]
