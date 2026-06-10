"""Claim-type-scoped authority matrix: effective_rank, contextual trust, special
rules, evidence→class/claim mapping, and the scale-aware internal-vs-external
conflict resolver."""

import pytest

from app.engines.fie import authority as AU
from app.engines.fie.authority import AuthorityClass as A, ClaimType as C
from app.engines.fie.models import Citation, EvidenceItem, FactRef


def _ext(value, *, source, unit, field=None, role="supporting"):
    loc = {"source": source}
    if field:
        loc["field"] = field
    e = EvidenceItem(claim="x", kind="external", value=value, unit=unit,
                     citations=[Citation(ref_id="C1", kind="external", display="d", locator=loc)])
    e.role = role
    return e


def _wb(metric, value, *, unit="Rupees in thousand"):
    fr = FactRef(company="MTL", metric=metric, label=metric, year=2025, value=value,
                 unit=unit, statement="pl", level="headline", sheet="PL", cell="C5")
    e = EvidenceItem(claim=f"{metric}={value}", kind="statement", value=value, unit=unit,
                     fact_refs=[fr],
                     citations=[Citation(ref_id="C2", kind="financial", display="wb",
                                         locator={"sheet": "PL", "cell": "C5"})])
    e.role = "baseline"
    return e


# --- matrix --------------------------------------------------------------
def test_totality_invariant_holds():
    AU.validate_matrix()                       # every claim type ranked, no dupes
    assert set(AU.AUTHORITY_MATRIX) == set(C)


def test_contextual_trust_differs_by_claim_type():
    # audited issuer dominates audited facts...
    assert AU.effective_rank(C.AUDITED_FACT, A.AUDITED_ISSUER) == 0
    # ...but ranks BELOW independent opinion for forward expectations
    assert (AU.effective_rank(C.FORWARD_EXPECTATION, A.INDEPENDENT_OPINION)
            < AU.effective_rank(C.FORWARD_EXPECTATION, A.AUDITED_ISSUER))
    assert AU.authority_weight(C.AUDITED_FACT, A.AUDITED_ISSUER) == 1.0
    assert AU.effective_rank(C.AUDITED_FACT, A.SECTOR_AGGREGATE) is None   # unranked


def test_special_rules_observation_only():
    assert AU.can_create_standalone_fact(A.NEWS_MEDIA) is False
    assert AU.can_create_standalone_fact(A.MARKET_REVEALED) is False
    assert AU.can_create_standalone_fact(A.AUDITED_ISSUER) is True
    assert AU.can_create_standalone_fact(A.EXCHANGE_OFFICIAL) is True


def test_evidence_mapping():
    assert AU.claim_type_for(_wb("revenue", 100)) is C.AUDITED_FACT
    assert AU.authority_class_for(_wb("revenue", 100)) is A.AUDITED_ISSUER
    news = _ext(1.0, source="WSJ", unit=None, role="non_authoritative")
    assert AU.claim_type_for(news) is C.SENTIMENT and AU.authority_class_for(news) is A.NEWS_MEDIA
    payout = _ext(15.0, source="PSX.CompanyPayouts", unit=None, role="event_fact")
    assert AU.authority_class_for(payout) is A.EXCHANGE_OFFICIAL


def test_resolve_workbook_wins_audited_fact():
    wb = _wb("revenue", 100.0)
    ext = _ext(120.0, source="PSX.AnalysisReports", unit="Rs. million", role="forecast_context")
    r = AU.resolve(wb, ext, claim_type=C.AUDITED_FACT)
    assert r["winner"] == "a"                  # workbook (audited_issuer) outranks external
