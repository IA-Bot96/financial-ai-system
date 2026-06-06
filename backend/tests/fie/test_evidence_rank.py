"""Deterministic evidence ranker: provenance-completeness dominates, then authority
(admission role), recency, reliability — stable and order-only."""

from app.engines.fie import evidence_rank as er
from app.engines.fie.models import Citation, EvidenceItem


def _ev(claim, kind, loc, *, role=None, freshness=None, reliability=1.0, value=None):
    e = EvidenceItem(claim=claim, kind=kind, value=value, reliability=reliability,
                     freshness=freshness,
                     citations=[Citation(ref_id="C1", kind=("financial" if kind in
                                 ("statement", "detail") else kind if kind != "calc" else "calc"),
                                         display="d", locator=loc)])
    e.role = role
    return e


def test_baseline_outranks_supporting_outranks_news():
    workbook = _ev("Revenue 100", "statement", {"sheet": "PL", "cell": "C5"},
                   role="baseline", value=100.0)
    overview = _ev("Market cap", "external", {"source": "PSX.CompanyOverview"},
                   role="supporting", value=1.0)
    news = _ev("Some headline", "external",
               {"source": "WSJ", "provider": "marketaux", "link": "https://w/1"},
               role="non_authoritative")
    ranked = er.rank([news, overview, workbook])
    assert [e.claim for e in ranked] == ["Revenue 100", "Market cap", "Some headline"]
    # provenance completeness dominates: workbook (CELL) scores highest
    assert er.score(workbook) > er.score(overview) > er.score(news)


def test_recency_breaks_ties_within_same_authority():
    older = _ev("Old item", "external", {"source": "WSJ", "provider": "p", "link": "u1"},
                role="non_authoritative", freshness="2022-01-01")
    newer = _ev("New item", "external", {"source": "WSJ", "provider": "p", "link": "u2"},
                role="non_authoritative", freshness="2026-06-01")
    assert er.rank([older, newer])[0].claim == "New item"


def test_top_caps_and_is_stable():
    evs = [_ev(f"i{i}", "external", {"source": "X", "provider": "p", "link": f"u{i}"},
               role="non_authoritative", freshness="2026-01-01") for i in range(5)]
    top3 = er.top(evs, 3)
    assert len(top3) == 3
    assert [e.claim for e in top3] == ["i0", "i1", "i2"]   # equal scores -> input order


def test_missing_citation_scores_zero_provenance():
    bare = EvidenceItem(claim="no cite", kind="external")
    bare.role = "non_authoritative"
    cited = _ev("cited", "external", {"source": "WSJ", "provider": "p", "link": "u"},
                role="non_authoritative")
    assert er.score(cited) > er.score(bare)
