"""Task 0.6 — provenance binding (3-tier) against the real workbook."""


def test_headline_revenue_cited_via_detail(millat_store):
    f = millat_store.lookup("revenue", 2024)
    cites = millat_store.cite(f)
    assert cites, "headline revenue must resolve a citation via the detail bridge"
    assert all(c.locator.get("basis") == "via_detail" for c in cites)
    assert any("millat" in (c.display or "").lower() for c in cites)
    assert f.provenance_basis == "via_detail"
    assert f.report_year == 2025  # newest report tagged primary


def test_detail_row_cited_directly(millat_store):
    # any detail row that exists in the Source Ledger should resolve tier-1 direct
    sl = millat_store.source_ledger
    sample = sl.iloc[0]
    det = millat_store.detail(sheet=sample["Sheet"])
    hit = det[det["cell"] == sample["Cell"]]
    if not hit.empty:
        f = millat_store._row_to_factref(hit.iloc[0].to_dict())
        cites = millat_store.cite(f)
        assert cites and cites[0].locator.get("basis") == "direct"


def test_via_detail_citations_are_collapsed_and_newest_report(millat_store):
    """A1: a derived headline total cites distinct source locations from the
    NEWEST report only — not one cite per underlying line, not older-report rows."""
    f = millat_store.lookup("revenue", 2024)
    cites = millat_store.cite(f)
    # collapsed: revenue note is a single table -> a single citation
    assert len(cites) == 1
    c = cites[0]
    assert c.locator["report_year"] == 2025          # newest report only
    assert c.locator["derived_from_rows"] > 1         # represents many lines
    assert c.locator["cell"] is None                  # collapsed, not a single cell


def test_via_detail_drops_older_report_rows(millat_store):
    """All emitted citations for a restated figure share one (newest) report year."""
    f = millat_store.lookup("current_assets", 2024)
    cites = millat_store.cite(f)
    years = {c.locator["report_year"] for c in cites}
    assert years == {max(years)}  # single, newest report year across all citations


def test_value_bearing_fact_falls_back_to_workbook_cell(millat_store):
    """A value with no Source Ledger match still cites its workbook cell (tier 3)."""
    from app.engines.fie.models import FactRef
    fake = FactRef(
        company=millat_store.company, metric="made_up_metric", label="Nonexistent",
        year=2024, value=1.0, statement="pl", level="headline", sheet="P&L", cell="Z99",
    )
    cites = millat_store.cite(fake)
    assert len(cites) == 1 and cites[0].locator["basis"] == "workbook"


def test_no_citation_without_a_value(millat_store):
    from app.engines.fie.models import FactRef
    empty = FactRef(
        company=millat_store.company, metric="x", label="x", year=2024, value=None,
        statement="pl", level="headline", sheet="P&L", cell="Z99",
    )
    assert millat_store.cite(empty) == []
