"""Surface-don't-resolve divergence: authority + chronology verdict, workbook settles
truth only when it is a side, and external-vs-external (cross_api) is surfaced."""

from app.engines.fie import authority as AU
from app.engines.fie import divergence as DV
from app.engines.fie.conflicts import ConflictResolver
from app.engines.fie.models import Citation, EvidenceItem, FactRef


def _ext(value, *, source, unit, field=None, role="forecast_context", freshness=None):
    loc = {"source": source}
    if field:
        loc["field"] = field
    e = EvidenceItem(claim="x", kind="external", value=value, unit=unit, freshness=freshness,
                     citations=[Citation(ref_id="C1", kind="external", display="d", locator=loc)])
    e.role = role
    return e


def _wb(metric, value, *, unit="Rupees in thousand"):
    fr = FactRef(company="MTL", metric=metric, label=metric, year=2025, value=value, unit=unit,
                 statement="pl", level="headline", sheet="PL", cell="C5")
    e = EvidenceItem(claim=f"{metric}={value}", kind="statement", value=value, unit=unit,
                     fact_refs=[fr],
                     citations=[Citation(ref_id="C2", kind="financial", display="wb",
                                         locator={"sheet": "PL", "cell": "C5"})])
    e.role = "baseline"
    return e


# --- verdict ---------------------------------------------------------------
def test_verdict_workbook_settles_truth():
    wb = _wb("revenue", 100.0)
    ext = _ext(120.0, source="PSX.AnalysisReports", unit="Rs. million")
    v = DV.verdict(wb, ext, claim_type=AU.ClaimType.AUDITED_FACT)
    assert v["truth_resolution"] == "workbook_authoritative" and v["surfaced"] is False
    assert v["authority_weighting"] == "side_a_higher_authority"     # workbook outranks


def test_verdict_external_vs_external_surfaced():
    a = _ext(100.0, source="PSX.AnalysisReports", unit="Rs. million",
             role="forecast_context", freshness="2026-01-01")
    b = _ext(120.0, source="PSX.SectorSummary", unit="Rs. million",
             role="supporting", freshness="2026-06-01")
    v = DV.verdict(a, b)
    assert v["truth_resolution"] == "not_determined" and v["surfaced"] is True
    assert v["chronology"] == "side_b_newer"


def test_present_outputs_both_sides():
    from app.engines.fie.models import Conflict
    c = Conflict(type="cross_api", topic="revenue", resolved=False,
                 values=[{"source": "A", "value": 100.0}, {"source": "B", "value": 120.0}],
                 resolution="equal authority; side b newer — surfaced for review")
    text = DV.present([c])
    assert "Divergence on revenue" in text and "A)" in text and "B)" in text
    assert "surfaced for review" in text


# --- cross_api detector (surfaced, not resolved) ---------------------------
def _resolver():
    return ConflictResolver(store=None)


def test_cross_api_surfaces_external_divergence():
    a = _ext(100_000.0, source="PSX.AnalysisReports", unit="Rs. million", field="revenue")
    b = _ext(60_000.0, source="PSX.SectorSummary", unit="Rs. million", field="revenue")  # diverges
    conflicts = _resolver().detect_cross_api([a, b])
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.type == "cross_api" and c.resolved is False        # surfaced, not picked
    assert {v["source"] for v in c.values} == {"PSX.AnalysisReports", "PSX.SectorSummary"}


def test_cross_api_same_source_or_agreement_not_flagged():
    # same source -> not cross-api
    a = _ext(100.0, source="PSX.AnalysisReports", unit="Rs. million", field="revenue")
    a2 = _ext(160.0, source="PSX.AnalysisReports", unit="Rs. million", field="revenue")
    assert _resolver().detect_cross_api([a, a2]) == []
    # agreement after normalization -> not flagged
    b = _ext(100.0, source="X", unit="Rs. million", field="revenue")
    b2 = _ext(100_000.0, source="Y", unit="Rs '000", field="revenue")   # equal magnitude
    assert _resolver().detect_cross_api([b, b2]) == []
